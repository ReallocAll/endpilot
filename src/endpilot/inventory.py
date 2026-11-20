"""Server-reported inventory slots needed by player item actions."""

import asyncio


class Inventory:
    def __init__(self, packets, player_id: int):
        self.packets = packets
        self.player_id = int(player_id)
        self._slots = {}
        self._events = {}
        self._unsubscribe = packets.subscribe_raw(self._on_packet)

    def _on_packet(self, raw):
        b = self.packets.protocol.binding
        if raw.packet_id == b.InventoryContentPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.inventory_id) != int(b.ContainerID.INVENTORY):
                return
            self._slots = dict(enumerate(packet.slots))
            for event in self._events.values():
                event.set()
        elif raw.packet_id == b.InventorySlotPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.inventory_id) != int(b.ContainerID.INVENTORY):
                return
            slot = int(packet.slot)
            self._slots[slot] = packet.item
            if slot in self._events:
                self._events[slot].set()

    async def wait_for_slot(self, slot: int, timeout: float = 15):
        """Wait until BDS reports the slot, including an empty slot."""
        event = self._events.setdefault(slot, asyncio.Event())
        async with asyncio.timeout(timeout):
            while slot not in self._slots:
                event.clear()
                await event.wait()
        return self._slots[slot]

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

    async def select_hotbar(self, slot: int, item=None) -> None:
        """Submit a hotbar selection; server acceptance is not implied."""
        if not 0 <= slot < 9:
            raise ValueError("hotbar slot must be between 0 and 8")
        if item is None:
            item = await self.wait_for_slot(slot)
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
