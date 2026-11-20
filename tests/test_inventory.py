import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from endpilot.inventory import Inventory
from endpilot.protocol import Protocol


class InventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_select_hotbar_uses_the_server_accepted_packet(self):
        protocol = Protocol(2193)
        b = protocol.binding
        callbacks = []
        packets = SimpleNamespace(
            protocol=protocol,
            subscribe_raw=lambda callback: callbacks.append(callback) or (lambda: callbacks.remove(callback)),
            send=AsyncMock(),
        )
        inventory = Inventory(packets, 7)
        try:
            self.assertEqual(inventory.selected_slot, 0)
            # Empty or not-yet-observed slots remain valid player selections.
            await inventory.select_hotbar(2)
            self.assertEqual(inventory.selected_slot, 2)
            await inventory.select_hotbar(8)
            self.assertEqual(inventory.selected_slot, 8)
            self.assertEqual(packets.send.await_count, 2)
            for index, call in zip((2, 8), packets.send.await_args_list):
                encoded = protocol.encode(call.args[0])
                decoded = protocol.decode(encoded)
                self.assertIsInstance(decoded, b.PlayerHotbarPacket)
                self.assertEqual(int(decoded.selected_slot), index)
                self.assertEqual(int(decoded.container_id), int(b.ContainerID.INVENTORY))
                self.assertTrue(decoded.should_select_slot)

            for invalid in (-1, 9):
                with self.assertRaises(ValueError):
                    await inventory.select_hotbar(invalid)
            self.assertEqual(packets.send.await_count, 2)
            self.assertEqual(inventory.selected_slot, 8)
        finally:
            inventory.close()
        self.assertEqual(callbacks, [])

    async def test_equipment_announcement_updates_locally_selected_slot(self):
        protocol = Protocol(2193)
        b = protocol.binding
        callbacks = []
        packets = SimpleNamespace(
            protocol=protocol, send=AsyncMock(),
            subscribe_raw=lambda cb: callbacks.append(cb) or (lambda: callbacks.remove(cb)),
        )
        inventory = Inventory(packets, 7)
        try:
            item = b.SerializedNetworkItemStackDescriptor(
                id=1, stack_size=1, aux_value=0, net_id_variant=None,
                block_runtime_id=0, user_data_buffer=b"",
            )
            await inventory.announce_equipment(3, item)
            self.assertEqual(inventory.selected_slot, 3)
            sent = protocol.decode(protocol.encode(packets.send.await_args.args[0]))
            self.assertIsInstance(sent, b.MobEquipmentPacket)
            self.assertEqual(int(sent.selected_slot), 3)
            self.assertEqual(int(sent.slot), 3)
        finally:
            inventory.close()
        self.assertEqual(callbacks, [])

    async def test_empty_inventory_slot_needs_no_server_content_for_attack(self):
        protocol = Protocol(2193)
        b = protocol.binding
        callbacks = []
        packets = SimpleNamespace(
            protocol=protocol,
            subscribe_raw=lambda cb: callbacks.append(cb) or (lambda: callbacks.remove(cb)),
        )
        inventory = Inventory(packets, 7)
        try:
            empty = inventory.item_or_empty(0)
            self.assertEqual(int(empty.id), 0)
            self.assertEqual(int(empty.stack_size), 0)
            self.assertIsNone(empty.net_id_variant)
            self.assertEqual(empty.user_data_buffer, b"")
            item = b.SerializedNetworkItemStackDescriptor(
                id=42, stack_size=1, aux_value=0, net_id_variant=None,
                block_runtime_id=0, user_data_buffer=b"",
            )
            inventory._set_slot(0, item)
            self.assertIs(inventory.item_or_empty(0), item)
        finally:
            inventory.close()

    async def test_server_inventory_transaction_updates_only_player_slots(self):
        protocol = Protocol(2193)
        b = protocol.binding
        callbacks = []
        decoded = {
            b"content": SimpleNamespace(
                inventory_id=b.ContainerID.INVENTORY,
                slots=[SimpleNamespace(id=0, stack_size=0)],
            ),
            b"pickup": SimpleNamespace(variant_transaction=SimpleNamespace(
                transaction=SimpleNamespace(actions=[
                    SimpleNamespace(
                        source=SimpleNamespace(
                            type=SimpleNamespace(name="CONTAINER_INVENTORY"),
                            container_id=b.ContainerID.INVENTORY,
                        ), slot=0, to_item_descriptor=SimpleNamespace(id=42, stack_size=1),
                    ),
                    SimpleNamespace(
                        source=SimpleNamespace(
                            type=SimpleNamespace(name="WORLD_INTERACTION"),
                            container_id=None,
                        ), slot=0, to_item_descriptor=SimpleNamespace(id=43, stack_size=7),
                    ),
                ])
            )),
            b"slot": SimpleNamespace(
                inventory_id=b.ContainerID.INVENTORY,
                slot=0, item=SimpleNamespace(id=42, stack_size=2),
            ),
        }
        stream = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: decoded[raw.body]),
            subscribe_raw=lambda callback: callbacks.append(callback)
            or (lambda: callbacks.remove(callback)),
        )
        from endpilot.protocol import RawPacket
        inventory = Inventory(stream, 7)
        try:
            callbacks[0](RawPacket(b.InventoryContentPacket.ID, b"content"))
            self.assertEqual(inventory.count_item(42), 0)
            pending = asyncio.create_task(inventory.wait_for_item_count(42, 1, timeout=1))
            await asyncio.sleep(0)
            callbacks[0](RawPacket(b.InventoryTransactionPacket.ID, b"pickup"))
            self.assertEqual(await pending, 1)
            callbacks[0](RawPacket(b.InventorySlotPacket.ID, b"slot"))
            self.assertEqual(inventory.count_item(42), 2)
            self.assertEqual(inventory.count_item(43), 0)
        finally:
            inventory.close()
        self.assertEqual(callbacks, [])
