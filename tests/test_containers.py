import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from endpilot.blocks import BlockObservation
from endpilot.containers import Containers, ContainerWindow
from endpilot.inventory import Inventory
from endpilot.protocol import Protocol, RawPacket


class ContainerTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_uses_selected_item_and_waits_for_server_window(self):
        codec = Protocol(2193)
        b = codec.binding
        callbacks = []
        response = SimpleNamespace(
            container_id=5, type=SimpleNamespace(name="CONTAINER"),
            pos=b.BlockPos(x=1, y=100, z=0), entity_unique_id=-1,
        )
        raw = RawPacket(b.ContainerOpenPacket.ID, b"window")
        protocol = SimpleNamespace(
            binding=b, encode=codec.encode,
            decode=lambda packet: response if packet.body == b"window" else codec.decode(packet),
        )

        async def wait(packet_id, *, timeout, match):
            self.assertEqual(packet_id, b.ContainerOpenPacket.ID)
            self.assertTrue(match(raw))
            return raw

        stream = SimpleNamespace(
            protocol=protocol,
            subscribe_raw=lambda cb: callbacks.append(cb) or (lambda: callbacks.remove(cb)),
            send=AsyncMock(), wait_for_packet=wait,
        )
        movement = SimpleNamespace(wait_for_position_sync=AsyncMock(return_value=(0.5, 101.62, 0.5)))
        inventory = Inventory(stream, 7)
        containers = Containers(stream, movement, inventory)
        blocks = SimpleNamespace(observed_block=lambda pos: BlockObservation(pos, 1234, 0, False))
        try:
            item = b.SerializedNetworkItemStackDescriptor(
                id=42, stack_size=1, aux_value=0, net_id_variant=None,
                block_runtime_id=0, user_data_buffer=b"",
            )
            inventory._set_slot(2, item)
            await inventory.select_hotbar(2)
            stream.send.reset_mock()
            opened = await containers.open((1, 100, 0), blocks, face=4)
            self.assertEqual(opened, ContainerWindow(5, "CONTAINER", (1, 100, 0), -1))
            self.assertEqual(stream.send.await_count, 1)
            sent = codec.decode(codec.encode(stream.send.await_args.args[0]))
            self.assertIsInstance(sent, b.InventoryTransactionPacket)
            tx = sent.variant_transaction
            self.assertEqual(tx.action_type, b.ItemUseInventoryTransaction.ActionType.PLACE)
            self.assertEqual(tx.trigger_type, b.ItemUseInventoryTransaction.TriggerType.PLAYER_INPUT)
            self.assertEqual(int(tx.face), 4)
            self.assertEqual((tx.click_pos.x, tx.click_pos.y, tx.click_pos.z), (0.0, 0.5, 0.5))
            self.assertEqual((tx.pos.x, tx.pos.y, tx.pos.z), (1, 100, 0))
            self.assertEqual((tx.from_pos.x, tx.from_pos.z), (0.5, 0.5))
            self.assertAlmostEqual(tx.from_pos.y, 101.62, places=4)
            self.assertEqual(int(tx.target_block_id), 1234)
            self.assertEqual(int(tx.slot), 2)
            self.assertEqual(int(tx.item.id), 42)
            self.assertEqual(tx.transaction.actions, [])
            callbacks[1](raw)
            self.assertEqual(containers.window, opened)
            # Server close should clear only the same window, not a different ID.
            close = SimpleNamespace(container_id=5)
            stream.protocol.decode = lambda packet: close
            callbacks[1](RawPacket(b.ContainerClosePacket.ID, b"close"))
            self.assertIsNone(containers.window)
        finally:
            containers.close()
            inventory.close()
        self.assertEqual(callbacks, [])

    async def test_unknown_target_and_empty_hand_are_valid(self):
        codec = Protocol(2193)
        b = codec.binding
        raw = RawPacket(b.ContainerOpenPacket.ID, b"window")
        response = SimpleNamespace(
            container_id=7, type=SimpleNamespace(name="CONTAINER"),
            pos=b.BlockPos(x=1, y=100, z=0), entity_unique_id=-1,
        )
        stream = SimpleNamespace(
            protocol=SimpleNamespace(
                binding=b, decode=lambda _: response,
            ),
            subscribe_raw=lambda cb: (lambda: None),
            send=AsyncMock(),
            wait_for_packet=AsyncMock(return_value=raw),
        )
        inventory = Inventory(stream, 7)
        containers = Containers(stream, SimpleNamespace(
            wait_for_position_sync=AsyncMock(return_value=(0.5, 101.62, 0.5)),
        ), inventory)
        try:
            await containers.open((1, 100, 0), SimpleNamespace(observed_block=lambda _: None))
            packet = codec.decode(codec.encode(stream.send.await_args.args[0]))
            self.assertEqual(int(packet.variant_transaction.target_block_id), 0)
            self.assertEqual(int(packet.variant_transaction.item.id), 0)
        finally:
            containers.close()
            inventory.close()
