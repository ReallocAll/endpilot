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
            # Empty or not-yet-observed slots remain valid player selections.
            await inventory.select_hotbar(2)
            await inventory.select_hotbar(8)
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
        finally:
            inventory.close()
        self.assertEqual(callbacks, [])
