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
            close = SimpleNamespace(container_id=5, server_initiated_close=False)
            stream.protocol.decode = lambda packet: close
            callbacks[1](RawPacket(b.ContainerClosePacket.ID, b"close"))
            self.assertIsNone(containers.window)
        finally:
            containers.close()
            inventory.close()
        self.assertEqual(callbacks, [])

    async def test_server_initiated_close_acknowledges_only_active_window(self):
        codec = Protocol(2193)
        b = codec.binding
        callbacks = []
        close_packets = {
            b"other": SimpleNamespace(
                container_id=9, container_type=b.ContainerType.CONTAINER,
                server_initiated_close=True,
            ),
            b"server": SimpleNamespace(
                container_id=5, container_type=b.ContainerType.CONTAINER,
                server_initiated_close=True,
            ),
            b"client": SimpleNamespace(
                container_id=5, container_type=b.ContainerType.CONTAINER,
                server_initiated_close=False,
            ),
        }
        stream = SimpleNamespace(
            protocol=SimpleNamespace(
                binding=b, decode=lambda raw: close_packets[raw.body],
            ),
            subscribe_raw=lambda cb: callbacks.append(cb) or (lambda: callbacks.remove(cb)),
            send=AsyncMock(),
        )
        containers = Containers(stream, None, None)
        opened = ContainerWindow(5, "CONTAINER", (1, 100, 0), -1)
        try:
            containers.window = opened
            callbacks[0](RawPacket(b.ContainerClosePacket.ID, b"other"))
            self.assertEqual(containers.window, opened)
            await asyncio.sleep(0)
            stream.send.assert_not_awaited()

            callbacks[0](RawPacket(b.ContainerClosePacket.ID, b"server"))
            self.assertIsNone(containers.window)
            self.assertIsNone(containers.slots)
            await asyncio.sleep(0)
            self.assertEqual(stream.send.await_count, 1)
            sent = codec.decode(codec.encode(stream.send.await_args.args[0]))
            self.assertIsInstance(sent, b.ContainerClosePacket)
            self.assertEqual(int(sent.container_id), 5)
            self.assertEqual(sent.container_type, b.ContainerType.CONTAINER)
            self.assertTrue(sent.server_initiated_close)

            callbacks[0](RawPacket(b.ContainerClosePacket.ID, b"server"))
            await asyncio.sleep(0)
            self.assertEqual(stream.send.await_count, 1)

            containers.window = opened
            callbacks[0](RawPacket(b.ContainerClosePacket.ID, b"client"))
            self.assertIsNone(containers.window)
            await asyncio.sleep(0)
            self.assertEqual(stream.send.await_count, 1)
        finally:
            containers.close()
        self.assertEqual(callbacks, [])

    async def test_close_waits_for_matching_server_window_and_serializes_fields(self):
        codec = Protocol(2193)
        b = codec.binding
        callbacks = []
        close_raw = RawPacket(b.ContainerClosePacket.ID, b"close")
        different_raw = RawPacket(b.ContainerClosePacket.ID, b"other")
        response = SimpleNamespace(container_id=5, server_initiated_close=False)
        other = SimpleNamespace(container_id=9)
        protocol = SimpleNamespace(
            binding=b, encode=codec.encode,
            decode=lambda packet: response if packet.body == b"close" else other,
        )

        async def wait(packet_id, *, timeout, match):
            self.assertEqual(packet_id, b.ContainerClosePacket.ID)
            self.assertEqual(timeout, 3)
            self.assertFalse(match(different_raw))
            self.assertTrue(match(close_raw))
            callbacks[0](different_raw)
            self.assertEqual(containers.window, opened)
            callbacks[0](close_raw)
            return close_raw

        stream = SimpleNamespace(
            protocol=protocol,
            subscribe_raw=lambda cb: callbacks.append(cb) or (lambda: callbacks.remove(cb)),
            send=AsyncMock(), wait_for_packet=wait,
        )
        containers = Containers(stream, None, None)
        opened = ContainerWindow(5, "CONTAINER", (1, 100, 0), -1)
        containers.window = opened
        try:
            self.assertEqual(await containers.close_window(timeout=3), opened)
            self.assertIsNone(containers.window)
            sent = codec.decode(codec.encode(stream.send.await_args.args[0]))
            self.assertIsInstance(sent, b.ContainerClosePacket)
            self.assertEqual(int(sent.container_id), 5)
            self.assertEqual(sent.container_type, b.ContainerType.CONTAINER)
            self.assertFalse(sent.server_initiated_close)
            stream.send.reset_mock()
            self.assertIsNone(await containers.close_window())
            stream.send.assert_not_awaited()
        finally:
            containers.close()

    async def test_window_slots_are_server_reported_and_scoped_to_window(self):
        codec = Protocol(2193)
        b = codec.binding
        callbacks = []
        first = SimpleNamespace(id=3, stack_size=4)
        updated = SimpleNamespace(id=3, stack_size=2)
        transacted = SimpleNamespace(id=3, stack_size=1)
        messages = {
            b"open": SimpleNamespace(
                container_id=5, type=SimpleNamespace(name="CONTAINER"),
                pos=b.BlockPos(x=1, y=100, z=0), entity_unique_id=-1,
            ),
            b"other": SimpleNamespace(inventory_id=0, slots=[first]),
            b"contents": SimpleNamespace(inventory_id=5, slots=[first]),
            b"slot": SimpleNamespace(inventory_id=5, slot=0, item=updated),
            b"transaction": SimpleNamespace(variant_transaction=SimpleNamespace(
                transaction=SimpleNamespace(actions=[SimpleNamespace(
                    source=SimpleNamespace(
                        type=SimpleNamespace(name="CONTAINER_INVENTORY"), container_id=5,
                    ),
                    slot=0, to_item_descriptor=transacted,
                )]),
            )),
            b"other_transaction": SimpleNamespace(variant_transaction=SimpleNamespace(
                transaction=SimpleNamespace(actions=[SimpleNamespace(
                    source=SimpleNamespace(
                        type=SimpleNamespace(name="CONTAINER_INVENTORY"), container_id=0,
                    ),
                    slot=0, to_item_descriptor=transacted,
                )]),
            )),
            b"close": SimpleNamespace(container_id=5, server_initiated_close=False),
        }
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: messages[raw.body]),
            subscribe_raw=lambda cb: callbacks.append(cb) or (lambda: callbacks.remove(cb)),
        )
        containers = Containers(packets, None, None)

        def inbound(packet_type, body):
            callbacks[0](RawPacket(packet_type.ID, body))

        try:
            inbound(b.ContainerOpenPacket, b"open")
            self.assertIsNone(containers.slots)
            pending = asyncio.create_task(containers.wait_for_slots(timeout=1))
            await asyncio.sleep(0)
            inbound(b.InventoryContentPacket, b"other")
            inbound(b.InventoryTransactionPacket, b"transaction")
            self.assertIsNone(containers.slots)  # Initial contents are not yet complete.
            self.assertFalse(pending.done())
            inbound(b.InventoryContentPacket, b"contents")
            self.assertEqual((await pending)[0].stack_size, 4)

            snapshot = containers.slots
            snapshot.clear()
            self.assertEqual(containers.slots[0].stack_size, 4)
            inbound(b.InventoryTransactionPacket, b"other_transaction")
            self.assertEqual(containers.slots[0].stack_size, 4)
            inbound(b.InventoryTransactionPacket, b"transaction")
            self.assertEqual(containers.slots[0].stack_size, 1)
            inbound(b.InventorySlotPacket, b"slot")
            self.assertEqual(containers.slots[0].stack_size, 2)

            inbound(b.ContainerClosePacket, b"close")
            self.assertIsNone(containers.window)
            self.assertIsNone(containers.slots)
            with self.assertRaises(RuntimeError):
                await containers.wait_for_slots(timeout=1)

            inbound(b.ContainerOpenPacket, b"open")
            waiting = asyncio.create_task(containers.wait_for_slots(timeout=1))
            await asyncio.sleep(0)
            inbound(b.ContainerClosePacket, b"close")
            inbound(b.ContainerOpenPacket, b"open")  # Server reuses numeric window ID.
            inbound(b.InventoryContentPacket, b"contents")
            with self.assertRaises(RuntimeError):
                await waiting
            self.assertEqual(containers.slots[0].stack_size, 4)
        finally:
            containers.close()
        self.assertEqual(callbacks, [])

    async def test_close_timeout_keeps_last_observed_window(self):
        codec = Protocol(2193)
        b = codec.binding
        stream = SimpleNamespace(
            protocol=SimpleNamespace(binding=b),
            subscribe_raw=lambda cb: (lambda: None),
            send=AsyncMock(),
            wait_for_packet=AsyncMock(side_effect=TimeoutError),
        )
        containers = Containers(stream, None, None)
        opened = ContainerWindow(2, "CONTAINER", (1, 100, 0), -1)
        containers.window = opened
        try:
            with self.assertRaises(TimeoutError):
                await containers.close_window()
            self.assertEqual(containers.window, opened)
            self.assertEqual(stream.send.await_count, 1)
        finally:
            containers.close()

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


    async def test_wait_for_close_observes_server_packet_without_requesting_close(self):
        codec = Protocol(2193)
        b = codec.binding
        callbacks = []
        packets_by_body = {
            b"other": SimpleNamespace(
                container_id=9, container_type=b.ContainerType.CONTAINER,
                server_initiated_close=True,
            ),
            b"server": SimpleNamespace(
                container_id=5, container_type=b.ContainerType.CONTAINER,
                server_initiated_close=True,
            ),
        }
        unrelated = RawPacket(b.ContainerClosePacket.ID, b"other")
        matching = RawPacket(b.ContainerClosePacket.ID, b"server")

        async def wait(packet_id, *, timeout, match):
            self.assertEqual(packet_id, b.ContainerClosePacket.ID)
            self.assertEqual(timeout, 4)
            self.assertFalse(match(unrelated))
            callbacks[0](unrelated)
            self.assertIs(containers.window, opened)
            self.assertTrue(match(matching))
            callbacks[0](matching)
            return matching

        stream = SimpleNamespace(
            protocol=SimpleNamespace(
                binding=b, decode=lambda raw: packets_by_body[raw.body],
            ),
            subscribe_raw=lambda cb: callbacks.append(cb) or (lambda: callbacks.remove(cb)),
            send=AsyncMock(), wait_for_packet=wait,
        )
        containers = Containers(stream, None, None)
        opened = ContainerWindow(5, "CONTAINER", (1, 100, 0), -1)
        containers.window = opened
        try:
            received = await containers.wait_for_close(timeout=4)
            self.assertEqual(int(received.container_id), 5)
            self.assertTrue(received.server_initiated_close)
            self.assertIsNone(containers.window)
            await asyncio.sleep(0)
            # Only the existing server-close acknowledgement was sent.
            self.assertEqual(stream.send.await_count, 1)
            acknowledged = codec.decode(codec.encode(stream.send.await_args.args[0]))
            self.assertTrue(acknowledged.server_initiated_close)
            self.assertEqual(int(acknowledged.container_id), 5)
            with self.assertRaisesRegex(RuntimeError, "no container window"):
                await containers.wait_for_close()
        finally:
            containers.close()
        self.assertEqual(callbacks, [])

    async def test_wait_for_close_timeout_does_not_send_or_clear_window(self):
        b = Protocol(2193).binding
        stream = SimpleNamespace(
            protocol=SimpleNamespace(binding=b),
            subscribe_raw=lambda _: (lambda: None),
            send=AsyncMock(),
            wait_for_packet=AsyncMock(side_effect=TimeoutError),
        )
        containers = Containers(stream, None, None)
        opened = ContainerWindow(5, "TRADE", None, 42)
        containers.window = opened
        try:
            with self.assertRaises(TimeoutError):
                await containers.wait_for_close(timeout=0.01)
            self.assertIs(containers.window, opened)
            stream.send.assert_not_awaited()
        finally:
            containers.close()
