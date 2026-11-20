"""Block updates and player-initiated placement against observed server blocks."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BlockUpdate:
    position: tuple[int, int, int]
    runtime_id: int
    layer: int


class Blocks:
    def __init__(self, packets, movement, inventory):
        self.packets = packets
        self.movement = movement
        self.inventory = inventory

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
        await self.inventory.select_hotbar(slot, item)
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
