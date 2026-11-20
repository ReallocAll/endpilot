"""Block updates, observed sub-chunks and player-initiated placement."""

import asyncio
from dataclasses import dataclass

from .subchunks import SubChunk, parse_sub_chunk

# Network hash of minecraft:air, as used by EndstoneMC/client.
HASHED_AIR_RUNTIME_ID = 0xDBF44120


@dataclass(frozen=True, slots=True)
class BlockUpdate:
    position: tuple[int, int, int]
    runtime_id: int
    layer: int


@dataclass(frozen=True, slots=True)
class BlockObservation:
    """Known server block. None from observed_block() instead means unknown terrain."""

    position: tuple[int, int, int]
    runtime_id: int | None
    layer: int
    is_air: bool | None


class Blocks:
    def __init__(self, packets, movement, inventory, *, hashed_runtime_ids: bool = False):
        self.packets = packets
        self.movement = movement
        self.inventory = inventory
        self.hashed_runtime_ids = hashed_runtime_ids
        self.dimension_id = 0
        self._updates: dict[tuple[int, int, int, int], int] = {}
        self._sub_chunks: dict[tuple[int, int, int], SubChunk | None] = {}
        self._unsubscribe = None

    def start(self) -> None:
        self._unsubscribe = self.packets.subscribe_raw(self._on_packet)

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _on_packet(self, raw) -> None:
        b = self.packets.protocol.binding
        if raw.packet_id == b.ChangeDimensionPacket.ID:
            self.dimension_id = int(self.packets.protocol.decode(raw).dimension_id)
            self._updates.clear()
            self._sub_chunks.clear()
        elif raw.packet_id == b.UpdateBlockPacket.ID:
            packet = self.packets.protocol.decode(raw)
            pos = (int(packet.pos.x), int(packet.pos.y), int(packet.pos.z), int(packet.layer))
            self._updates[pos] = int(packet.runtime_id)
        elif raw.packet_id == b.UpdateSubChunkBlocksPacket.ID:
            packet = self.packets.protocol.decode(raw)
            for layer, changes in enumerate((packet.blocks_changed.standards, packet.blocks_changed.extras)):
                for change in changes:
                    pos = (int(change.pos.x), int(change.pos.y), int(change.pos.z), layer)
                    self._updates[pos] = int(change.runtime_id)
        elif raw.packet_id == b.SubChunkPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.dimension_type) != self.dimension_id:
                return
            center = packet.center_pos
            for entry in packet.sub_chunk_data:
                delta = entry.sub_chunk_pos_offset
                key = (int(center.x) + int(delta.x),
                       int(center.y) + int(delta.y), int(center.z) + int(delta.z))
                if entry.result.name == "SUCCESS_ALL_AIR":
                    self._set_sub_chunk(key, None)
                elif entry.result.name == "SUCCESS" and entry.serialized_sub_chunk is not None:
                    self._set_sub_chunk(key, parse_sub_chunk(entry.serialized_sub_chunk))

    def _set_sub_chunk(self, key: tuple[int, int, int], chunk: SubChunk | None) -> None:
        self._sub_chunks[key] = chunk
        for pos in tuple(self._updates):
            if (pos[0] >> 4, pos[1] >> 4, pos[2] >> 4) == key:
                del self._updates[pos]

    def observed_block(self, position: tuple[int, int, int], *, layer: int = 0) -> BlockObservation | None:
        """Return the latest server observation, or None for unknown/unreceived terrain."""
        x, y, z = position
        runtime_id = self._updates.get((x, y, z, layer))
        if runtime_id is None:
            key = (x >> 4, y >> 4, z >> 4)
            if key not in self._sub_chunks:
                return None
            chunk = self._sub_chunks[key]
            if chunk is None:
                if layer != 0:
                    return None
                runtime_id = HASHED_AIR_RUNTIME_ID if self.hashed_runtime_ids else None
                return BlockObservation(position, runtime_id, layer, True)
            runtime_id = chunk.get_block(x & 15, y & 15, z & 15, layer)
            if runtime_id is None:
                return None
        return BlockObservation(
            position, runtime_id, layer,
            runtime_id == HASHED_AIR_RUNTIME_ID if self.hashed_runtime_ids else None,
        )

    async def request_sub_chunk(self, position: tuple[int, int, int]) -> None:
        """Request the 16³ section containing position; submission is not a response."""
        b = self.packets.protocol.binding
        x, y, z = position
        request = b.SubChunkRequestPacket(
            dimension_type=self.dimension_id,
            center_pos=b.SubChunkPos(x=x >> 4, y=0, z=z >> 4),
            sub_chunk_pos_offsets=[b.SubChunkPacket.SubChunkPosOffset(x=0, y=y >> 4, z=0)],
        )
        await self.packets.send(request)

    async def wait_for_update(
        self, position: tuple[int, int, int], *, layer: int = 0, timeout: float = 15
    ) -> BlockUpdate:
        """Wait for a *new* UpdateBlock packet, not a snapshot of the world."""
        binding = self.packets.protocol.binding

        def matches(raw):
            packet = self.packets.protocol.decode(raw)
            pos = (int(packet.pos.x), int(packet.pos.y), int(packet.pos.z))
            return pos == position and int(packet.layer) == layer

        raw = await self.packets.wait_for_packet(
            binding.UpdateBlockPacket.ID, timeout=timeout, match=matches
        )
        packet = self.packets.protocol.decode(raw)
        return BlockUpdate(position, int(packet.runtime_id), int(packet.layer))

    async def place_against(self, against: BlockUpdate, *, face: int = 1, slot: int = 0) -> int:
        """Submit a placement on a known block face; return the input tick, not acceptance.

        `against` comes from a server UpdateBlock. Observe the adjacent block and/or
        check BDS state to establish whether the server actually placed the item.
        """
        b = self.packets.protocol.binding
        item = await self.inventory.wait_for_item(slot)
        x, y, z = await self.movement.wait_for_position_sync()
        await self.inventory.announce_equipment(slot, item)
        px, py, pz = against.position
        transaction = b.ItemUseInventoryTransaction(
            transaction=b.InventoryTransaction(actions=[]),
            action_type=b.ItemUseInventoryTransaction.ActionType.PLACE,
            trigger_type=b.ItemUseInventoryTransaction.TriggerType.PLAYER_INPUT,
            pos=b.BlockPos(x=px, y=py, z=pz),
            face=face,
            slot=slot,
            hand=b.HandSlot.MAINHAND,
            item=item,
            from_pos=b.Vec3(x=x, y=y, z=z),
            click_pos=b.Vec3(x=0.5, y=0.5, z=0.5),
            target_block_id=against.runtime_id,
            client_predicted_result=b.ItemUseInventoryTransaction.PredictedResult.SUCCESS,
            client_cooldown_state=b.ItemUseInventoryTransaction.ClientCooldownState.OFF,
        )
        packed = b.PackedItemUseLegacyInventoryTransaction(
            id=b.ItemStackLegacyRequestId(raw_id=0),
            slots=None,
            transaction=transaction,
        )
        return await self.movement.send_item_use(packed)

    async def mine_block(
        self, position: tuple[int, int, int], *, face: int = 1, timeout: float = 15
    ) -> BlockUpdate:
        """Hold a server-authoritative break until a block update is observed.

        On hashed-runtime-ID worlds, require an air update. Palette-ID worlds
        return the first matching update; callers must interpret its runtime ID.
        A timed-out or cancelled break is aborted, not treated as completed.
        """
        b = self.packets.protocol.binding

        def matches(raw):
            packet = self.packets.protocol.decode(raw)
            return (
                (int(packet.pos.x), int(packet.pos.y), int(packet.pos.z)) == position
                and int(packet.layer) == 0
                and (not self.hashed_runtime_ids
                     or int(packet.runtime_id) == HASHED_AIR_RUNTIME_ID)
            )

        waiter = asyncio.create_task(self.packets.wait_for_packet(
            b.UpdateBlockPacket.ID, timeout=timeout, match=matches
        ))
        started = False
        try:
            # Register the observation before sending START; cancellation must
            # also clean up the waiting task at this suspension point.
            await asyncio.sleep(0)
            await self.movement.send_block_actions([
                ("START_DESTROY_BLOCK", position, face),
            ])
            started = True
            raw = await waiter
        except BaseException as error:
            waiter.cancel()
            if started:
                try:
                    await self.movement.send_block_actions([
                        ("ABORT_DESTROY_BLOCK", position, 0),
                        ("STOP_DESTROY_BLOCK", (0, 0, 0), 0),
                    ])
                except Exception as cleanup_error:
                    # A lost transport may also reject ABORT. Keep the cause.
                    error.add_note(f"mining abort failed: {cleanup_error!r}")
            raise
        await self.movement.send_block_actions([
            ("STOP_DESTROY_BLOCK", (0, 0, 0), 0),
        ])
        packet = self.packets.protocol.decode(raw)
        return BlockUpdate(position, int(packet.runtime_id), int(packet.layer))

