"""Block container interaction and authoritative window-open observation."""

import asyncio
from dataclasses import dataclass


_FACE_OFFSETS = {
    0: (0, -1, 0), 1: (0, 1, 0), 2: (0, 0, -1),
    3: (0, 0, 1), 4: (-1, 0, 0), 5: (1, 0, 0),
}


@dataclass(frozen=True, slots=True)
class ContainerWindow:
    container_id: int
    container_type: str
    position: tuple[int, int, int]
    entity_unique_id: int


class Containers:
    def __init__(self, packets, movement, inventory):
        self.packets = packets
        self.movement = movement
        self.inventory = inventory
        self.window: ContainerWindow | None = None
        self._unsubscribe = packets.subscribe_raw(self._on_packet)

    def _decode_open(self, raw) -> ContainerWindow:
        packet = self.packets.protocol.decode(raw)
        return ContainerWindow(
            int(packet.container_id),
            packet.type.name if hasattr(packet.type, "name") else str(int(packet.type)),
            (int(packet.pos.x), int(packet.pos.y), int(packet.pos.z)),
            int(packet.entity_unique_id),
        )

    def _on_packet(self, raw) -> None:
        b = self.packets.protocol.binding
        if raw.packet_id == b.ContainerOpenPacket.ID:
            self.window = self._decode_open(raw)
        elif raw.packet_id == b.ContainerClosePacket.ID:
            packet = self.packets.protocol.decode(raw)
            if self.window is not None and int(packet.container_id) == self.window.container_id:
                self.window = None

    async def open(self, position: tuple[int, int, int], blocks, *, face: int = 1,
                   timeout: float = 15) -> ContainerWindow:
        """Return only after BDS sends ContainerOpenPacket for the clicked block."""
        b = self.packets.protocol.binding
        x, y, z = await self.movement.wait_for_position_sync(timeout)
        observed = blocks.observed_block(position)
        runtime_id = 0 if observed is None or observed.runtime_id is None else observed.runtime_id
        px, py, pz = position
        dx, dy, dz = _FACE_OFFSETS.get(face, (0, 0, 0))
        transaction = b.ItemUseInventoryTransaction(
            transaction=b.InventoryTransaction(actions=[]),
            action_type=b.ItemUseInventoryTransaction.ActionType.PLACE,
            trigger_type=b.ItemUseInventoryTransaction.TriggerType.PLAYER_INPUT,
            pos=b.BlockPos(x=px, y=py, z=pz),
            face=face,
            slot=self.inventory.selected_slot,
            hand=b.HandSlot.MAINHAND,
            item=self.inventory.item_or_empty(self.inventory.selected_slot),
            from_pos=b.Vec3(x=x, y=y, z=z),
            click_pos=b.Vec3(x=0.5 + dx * 0.5, y=0.5 + dy * 0.5, z=0.5 + dz * 0.5),
            target_block_id=runtime_id,
            client_predicted_result=b.ItemUseInventoryTransaction.PredictedResult.SUCCESS,
            client_cooldown_state=b.ItemUseInventoryTransaction.ClientCooldownState.OFF,
        )

        def matches(raw):
            packet = self.packets.protocol.decode(raw)
            return (int(packet.pos.x), int(packet.pos.y), int(packet.pos.z)) == position

        # Subscribe before submission: a local BDS can open the window in the same batch.
        pending = asyncio.create_task(self.packets.wait_for_packet(
            b.ContainerOpenPacket.ID, timeout=timeout, match=matches,
        ))
        try:
            await asyncio.sleep(0)
            await self.packets.send(b.InventoryTransactionPacket(
                legacy_request_id=b.ItemStackLegacyRequestId(raw_id=0),
                legacy_set_item_slots=None,
                variant_transaction=transaction,
            ))
            return self._decode_open(await pending)
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    def close(self) -> None:
        self._unsubscribe()
