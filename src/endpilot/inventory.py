"""Server-reported inventory slots needed by player item actions."""

import asyncio


class Inventory:
    def __init__(self, packets, player_id: int):
        self.packets = packets
        self.player_id = int(player_id)
        self._slots = {}
        self._events = {}
        self._changed = asyncio.Event()
        self._unsubscribe = packets.subscribe_raw(self._on_packet)

    def _set_slot(self, slot, item):
        self._slots[int(slot)] = item
        event = self._events.get(int(slot))
        if event is not None:
            event.set()
        self._changed.set()

    def _on_packet(self, raw):
        b = self.packets.protocol.binding
        if raw.packet_id == b.InventoryContentPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.inventory_id) != int(b.ContainerID.INVENTORY):
                return
            self._slots = dict(enumerate(packet.slots))
            for event in self._events.values():
                event.set()
            self._changed.set()
        elif raw.packet_id == b.InventorySlotPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.inventory_id) == int(b.ContainerID.INVENTORY):
                self._set_slot(packet.slot, packet.item)
        elif raw.packet_id == b.InventoryTransactionPacket.ID:
            packet = self.packets.protocol.decode(raw)
            # Server-initiated inventory changes, including automatic pickup.
            # Match EndstoneMC/client's container/action reconciliation.
            transaction = getattr(packet.variant_transaction, "transaction", None)
            for action in getattr(transaction, "actions", ()):
                source = action.source
                if (source.type.name == "CONTAINER_INVENTORY"
                        and source.container_id is not None
                        and int(source.container_id) == int(b.ContainerID.INVENTORY)):
                    self._set_slot(action.slot, action.to_item_descriptor)

    def count_item(self, item_id: int) -> int:
        """Sum the latest server-described inventory slots for a numeric item ID."""
        return sum(
            int(item.stack_size) for item in self._slots.values()
            if int(item.id) == item_id
        )

    async def wait_for_item_count(self, item_id: int, minimum: int, *, timeout: float = 15) -> int:
        """Wait for an observed inventory count; unrelated slot changes are allowed."""
        async with asyncio.timeout(timeout):
            while True:
                count = self.count_item(item_id)
                if count >= minimum:
                    return count
                self._changed.clear()
                await self._changed.wait()

    async def wait_for_item(self, slot: int, timeout: float = 15):
        """Return the current server-reported nonempty inventory slot."""
        event = self._events.setdefault(slot, asyncio.Event())
        async with asyncio.timeout(timeout):
            while True:
                item = self._slots.get(slot)
                if item is not None and int(item.id) != 0:
                    return item
                event.clear()
                await event.wait()

    async def select_hotbar(self, slot: int) -> None:
        """Request a selected hotbar slot; only BDS can confirm the held item."""
        if not 0 <= slot < 9:
            raise ValueError("hotbar slot must be between 0 and 8")
        b = self.packets.protocol.binding
        # BDS 1.26.52 accepts this even though EndstoneMC/client uses
        # MobEquipment; that packet alone did not switch the server's slot.
        await self.packets.send(b.PlayerHotbarPacket(
            selected_slot=slot,
            container_id=b.ContainerID.INVENTORY,
            should_select_slot=True,
        ))

    async def announce_equipment(self, slot: int, item) -> None:
        """Carry the server-observed item descriptor for a use transaction."""
        b = self.packets.protocol.binding
        await self.packets.send(b.MobEquipmentPacket(
            runtime_id=self.player_id,
            item=item,
            slot=slot,
            selected_slot=slot,
            container_id=b.ContainerID.INVENTORY,
        ))

    def close(self):
        self._unsubscribe()
