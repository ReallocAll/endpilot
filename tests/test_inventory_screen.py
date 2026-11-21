import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from endpilot.containers import Containers, ContainerWindow
from endpilot.inventory import Inventory
from endpilot.protocol import Protocol, RawPacket


class InventoryScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_player_inventory_open_waits_for_server_window(self):
        codec = Protocol(2193)
        b = codec.binding
        callbacks = []
        response = SimpleNamespace(
            container_id=4, type=SimpleNamespace(name="INVENTORY"),
            pos=b.BlockPos(x=0, y=0, z=0), entity_unique_id=-1,
        )
        opened = RawPacket(b.ContainerOpenPacket.ID, b"opened")
        protocol = SimpleNamespace(
            binding=b, decode=lambda raw:
            response if raw.body == b"opened" else codec.decode(raw),
        )

        async def wait(packet_id, *, timeout, match):
            self.assertEqual(packet_id, b.ContainerOpenPacket.ID)
            self.assertEqual(timeout, 3)
            self.assertTrue(match(opened))
            callbacks[1](opened)
            return opened

        stream = SimpleNamespace(
            protocol=protocol, send=AsyncMock(), wait_for_packet=wait,
            subscribe_raw=lambda cb: callbacks.append(cb)
            or (lambda: callbacks.remove(cb)),
        )
        inventory = Inventory(stream, 77)
        containers = Containers(stream, None, inventory)
        try:
            window = await containers.open_inventory_screen(timeout=3)
            self.assertEqual(window, ContainerWindow(4, "INVENTORY", (0, 0, 0), -1))
            self.assertEqual(containers.window, window)
            sent = codec.decode(codec.encode(stream.send.await_args.args[0]))
            self.assertIsInstance(sent, b.InteractPacket)
            self.assertEqual(sent.action, b.InteractPacket.Action.OPEN_INVENTORY)
            self.assertEqual(int(sent.target_id), 77)
            self.assertIsNone(sent.pos)
        finally:
            containers.close()
            inventory.close()
        self.assertEqual(callbacks, [])
