"""Vanilla bed observations and server-confirmed player sleeping."""

import asyncio
from dataclasses import dataclass
import json

from .blocks import FACE_OFFSETS


_HEAD_OFFSETS = {"south": (0, 1), "west": (-1, 0), "north": (0, -1), "east": (1, 0)}
_SLEEPING_FLAG = 0x02
_REFUSALS = ("tile.bed.noSleep", "tile.bed.occupied")
_INVALID_RESPAWN_BED = ("tile.bed.notValid",)
_BED_MESSAGES = {
    **{key: {"rawtext": [{"text": "§7"}, {"translate": key}]} for key in _REFUSALS},
    "tile.bed.notValid": {"rawtext": [{"translate": "tile.bed.notValid"}]},
}


@dataclass(frozen=True, slots=True)
class BedObservation:
    position: tuple[int, int, int]
    dimension_id: int
    name: str
    facing: str
    is_head: bool
    head_position: tuple[int, int, int]
    foot_position: tuple[int, int, int]
    occupied: bool | None


@dataclass(frozen=True, slots=True)
class PlayerRespawnPoint:
    position: tuple[int, int, int]
    dimension_id: int
    spawn_block_position: tuple[int, int, int]


class SleepRejected(RuntimeError):
    """The server supplied an explicit sleep refusal."""

    def __init__(self, translation_key: str):
        self.translation_key = translation_key
        super().__init__(translation_key)


def _position(value) -> tuple[int, int, int]:
    return int(value.x), int(value.y), int(value.z)


def _bed_state(blocks, position):
    block = blocks.observed_block(position)
    state = None if block is None else blocks._vanilla_state(block.runtime_id)
    return None if state is None else state.bed


def _bed_message(protocol, raw, keys: tuple[str, ...]) -> str | None:
    packet = protocol.decode(raw)
    if packet.localize or packet.body.type != protocol.binding.TextPacketType.TEXT_OBJECT:
        return None
    try:
        message = json.loads(packet.body.message)
    except ValueError:
        return None
    return next((key for key in keys if message == _BED_MESSAGES[key]), None)


def invalidates_bed_respawn(protocol, raw) -> bool:
    return _bed_message(protocol, raw, _INVALID_RESPAWN_BED) is not None


class Beds:
    def __init__(self, packets, blocks, movement, inventory, runtime_id, initial_packets=()):
        self.packets = packets
        self.blocks = blocks
        self.movement = movement
        self.inventory = inventory
        self.runtime_id = runtime_id
        self.sleeping: bool | None = None
        self._bed_position: tuple[int, int, int] | None = None
        self.respawn_point: PlayerRespawnPoint | None = None
        self._dimension_epoch = 0
        self._active = False
        for raw in initial_packets:
            self._on_packet(raw)
        self._unsubscribe = packets.subscribe_raw(self._on_packet)

    def close(self) -> None:
        self._unsubscribe()
        self._dimension_epoch += 1

    @property
    def sleeping_bed(self) -> tuple[int, int, int] | None:
        return self._bed_position if self.sleeping else None

    def observed(self, position: tuple[int, int, int]) -> BedObservation | None:
        state = _bed_state(self.blocks, position)
        if state is None:
            return None
        x, y, z = position
        dx, dz = _HEAD_OFFSETS[state.facing]
        if state.is_head:
            head, foot = position, (x - dx, y, z - dz)
            occupied = state.occupied
        else:
            head, foot = (x + dx, y, z + dz), position
            companion = _bed_state(self.blocks, head)
            occupied = None
            if (companion is not None and companion.is_head
                    and (companion.name, companion.facing) == (state.name, state.facing)):
                occupied = companion.occupied
        return BedObservation(position, self.blocks.dimension_id, state.name, state.facing,
                              state.is_head, head, foot, occupied)

    def _sleep_metadata(self, raw) -> tuple[bool | None, tuple[int, int, int] | None]:
        protocol = self.packets.protocol
        b = protocol.binding
        packet = protocol.decode(raw)
        if packet.id != self.runtime_id:
            return None, None
        sleeping = bed_position = None
        for item in packet.packed_items.data:
            if item.id == b.ActorDataIDs.PLAYER_FLAGS and isinstance(item.payload, b.DataItemBytePayload):
                sleeping = bool(item.payload.value & _SLEEPING_FLAG)
            elif item.id == b.ActorDataIDs.BED_POSITION and isinstance(item.payload, b.DataItemPosPayload):
                bed_position = _position(item.payload.value)
        return sleeping, bed_position

    def _on_packet(self, raw) -> None:
        protocol = self.packets.protocol
        b = protocol.binding
        if raw.packet_id == b.SetActorDataPacket.ID:
            sleeping, bed_position = self._sleep_metadata(raw)
            if sleeping is not None:
                self.sleeping = sleeping
            if bed_position is not None:
                # The server sends only changed fields when the same bed is reused.
                self._bed_position = bed_position
        elif raw.packet_id == b.SetSpawnPositionPacket.ID:
            packet = protocol.decode(raw)
            if packet.spawn_pos_type == b.SpawnPositionType.PLAYER_RESPAWN:
                self.respawn_point = PlayerRespawnPoint(_position(packet.pos), int(packet.dimension_type),
                                                        _position(packet.spawn_block_pos))
        elif raw.packet_id == b.TextPacket.ID and self.respawn_point is not None:
            if invalidates_bed_respawn(protocol, raw):
                self.respawn_point = None
        elif raw.packet_id == b.ChangeDimensionPacket.ID:
            self.sleeping = None
            self._bed_position = None
            self._dimension_epoch += 1

    async def _click(self, position, face):
        b = self.packets.protocol.binding
        block = self.blocks.observed_block(position)
        if block is None or block.runtime_id is None:
            raise LookupError(f"no current server bed at {position}")
        px, py, pz = position
        x, y, z = self.movement.input_position
        dx, dy, dz = FACE_OFFSETS[face]
        slot = self.inventory.selected_slot
        transaction = b.ItemUseInventoryTransaction(
            transaction=b.InventoryTransaction(actions=[]),
            action_type=b.ItemUseInventoryTransaction.ActionType.PLACE,
            trigger_type=b.ItemUseInventoryTransaction.TriggerType.PLAYER_INPUT,
            pos=b.BlockPos(x=px, y=py, z=pz), face=face,
            slot=slot, hand=b.HandSlot.MAINHAND, item=self.inventory.item_or_empty(slot),
            from_pos=b.Vec3(x=x, y=y, z=z),
            click_pos=b.Vec3(x=0.5 + 0.5 * dx, y=0.28125 * (1 + dy), z=0.5 + 0.5 * dz),
            target_block_id=block.runtime_id,
            client_predicted_result=b.ItemUseInventoryTransaction.PredictedResult.SUCCESS,
            client_cooldown_state=b.ItemUseInventoryTransaction.ClientCooldownState.OFF,
        )
        await self.packets.send(b.InventoryTransactionPacket(
            legacy_request_id=b.ItemStackLegacyRequestId(raw_id=0),
            legacy_set_item_slots=None, variant_transaction=transaction,
        ))

    async def stop_sleeping(self) -> None:
        b = self.packets.protocol.binding
        zero = b.BlockPos(x=0, y=0, z=0)
        await self.packets.send(b.PlayerActionPacket(runtime_id=self.runtime_id,
                                                     action=b.PlayerActionType.STOP_SLEEPING,
                                                     pos=zero, result_pos=zero, face=0))

    async def _confirm(self, submit, timeout, epoch, *, sleeping: bool, head=None) -> None:
        b = self.packets.protocol.binding
        submitted = False
        refusal = None

        def ready():
            if sleeping:
                return self.sleeping is True and self._bed_position == head
            return self.sleeping is False

        def matches(raw):
            nonlocal refusal
            if raw.packet_id in (b.ChangeDimensionPacket.ID, b.DisconnectPacket.ID):
                return True
            if not submitted:
                return False
            if raw.packet_id == b.TextPacket.ID:
                if sleeping:
                    refusal = _bed_message(self.packets.protocol, raw, _REFUSALS)
                return refusal is not None
            flag, position = self._sleep_metadata(raw)
            if not sleeping:
                return flag is False
            return (flag is True or position is not None) and ready()

        ids = (b.SetActorDataPacket.ID, b.TextPacket.ID, b.ChangeDimensionPacket.ID, b.DisconnectPacket.ID)
        while True:
            pending = asyncio.create_task(self.packets.wait_for_packet(ids, timeout=timeout, match=matches))
            response = None
            try:
                await asyncio.sleep(0)
                if epoch != self._dimension_epoch:
                    raise RuntimeError("dimension changed during bed action")
                already_confirmed = ready() and (submitted or not sleeping)
                if pending.done():
                    response = await pending
                elif not already_confirmed:
                    if not submitted:
                        submitted = True
                        await submit()
                    response = await pending
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            if response is not None and response.packet_id == b.DisconnectPacket.ID:
                raise ConnectionError("server disconnected during bed action")
            if epoch != self._dimension_epoch:
                raise RuntimeError("dimension changed during bed action")
            if refusal is not None:
                raise SleepRejected(refusal)
            # More metadata in the same batch may replace the provisional bed position.
            if ready():
                return

    async def sleep(self, position: tuple[int, int, int], *, face: int, timeout: float) -> tuple[int, int, int]:
        if face not in FACE_OFFSETS:
            raise ValueError("bed face must be in 0..5")
        if self._active:
            raise RuntimeError("bed action already in progress")
        if self.sleeping is True:
            raise RuntimeError("player is already sleeping")
        bed = self.observed(position)
        if bed is None:
            raise LookupError(f"no current server bed at {position}")
        epoch = self._dimension_epoch

        async def click():
            current = self.observed(position)
            if current is None or (current.name, current.head_position) != (bed.name, bed.head_position):
                raise LookupError(f"target bed changed at {position}")
            await self._click(position, face)

        self._active = True
        try:
            async with asyncio.timeout(timeout):
                await self.movement.wait_for_position_sync(timeout)
                x, y, z = position
                await self.movement.look_at((x + 0.5, y + 0.5625, z + 0.5))
                await self._confirm(click, timeout, epoch, sleeping=True, head=bed.head_position)
        finally:
            self._active = False
        return bed.head_position

    async def wake(self, *, timeout: float) -> None:
        if self._active:
            raise RuntimeError("bed action already in progress")
        if self.sleeping is not True:
            raise RuntimeError("player has no observed sleep session")
        self._active = True
        try:
            async with asyncio.timeout(timeout):
                await self._confirm(self.stop_sleeping, timeout, self._dimension_epoch, sleeping=False)
        finally:
            self._active = False
