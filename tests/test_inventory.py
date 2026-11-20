import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from endpilot.inventory import Inventory
from endpilot.protocol import Protocol, RawPacket


class InventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_selects_reported_item_and_allows_empty_hotbar_slot(self):
        protocol = Protocol(2193)
        b = protocol.binding
        observers = []
        sent = []
        packets = SimpleNamespace(
            protocol=protocol,
            subscribe_raw=lambda callback: observers.append(callback) or (lambda: observers.remove(callback)),
            send=AsyncMock(side_effect=lambda packet: sent.append(packet)),
        )
        inventory = Inventory(packets, 7)
        try:
            selected = asyncio.create_task(inventory.select_hotbar(2))
            await asyncio.sleep(0)
            self.assertFalse(selected.done())  # Unknown is not equivalent to empty.

            item = b.SerializedNetworkItemStackDescriptor(
                id=1, stack_size=1, aux_value=0, net_id_variant=4,
                block_runtime_id=123, user_data_buffer=b"\x00" * 10,
            )
            slot_packet = b.InventorySlotPacket(
                inventory_id=b.ContainerID.INVENTORY, slot=2,
                full_container_name=None, storage_item=None, item=item,
            )
            observers[0](protocol.encode(slot_packet))
            await selected
            self.assertEqual(len(sent), 1)
            equipment = protocol.decode(protocol.encode(sent[0]))
            self.assertIsInstance(equipment, b.MobEquipmentPacket)
            self.assertEqual(int(equipment.runtime_id), 7)
            self.assertEqual(int(equipment.slot), 2)
            self.assertEqual(int(equipment.selected_slot), 2)
            self.assertEqual(int(equipment.item.id), 1)
            self.assertEqual(int(equipment.item.net_id_variant), 4)

            empty = b.SerializedNetworkItemStackDescriptor(
                id=0, stack_size=0, aux_value=0, net_id_variant=None,
                block_runtime_id=0, user_data_buffer=b"",
            )
            empty_slot = b.InventorySlotPacket(
                inventory_id=b.ContainerID.INVENTORY, slot=8,
                full_container_name=None, storage_item=None, item=empty,
            )
            observers[0](protocol.encode(empty_slot))
            await inventory.select_hotbar(8)
            empty_equipment = protocol.decode(protocol.encode(sent[1]))
            self.assertEqual(int(empty_equipment.selected_slot), 8)
            self.assertEqual(int(empty_equipment.item.id), 0)

            with self.assertRaises(ValueError):
                await inventory.select_hotbar(9)
            self.assertEqual(len(sent), 2)
        finally:
            inventory.close()
        self.assertEqual(observers, [])
