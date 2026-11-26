import asyncio
import unittest
from types import SimpleNamespace

from endpilot import Bot, FurnaceProgress
from endpilot.compression import PacketConnection
from endpilot.containers import Containers
from endpilot.inventory import Inventory
from endpilot.protocol import write_batch

from test_blocks import PacketStream
from test_furnace_transfer import item
from test_join import FakeTransport


class FurnaceProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("FurnaceProgress", protocol=2225)
        self.stream = PacketStream(self.bot.protocol)
        self.b = self.bot.protocol.binding
        self.inventory = Inventory(self.stream, 7)
        self.containers = Containers(self.stream, None, self.inventory)
        self.bot._inventory = self.inventory
        self.bot._containers = self.containers

    async def asyncTearDown(self):
        self.containers.close()
        self.inventory.close()

    def deliver(self, packet):
        self.stream.emit(self.bot.protocol.encode(packet))

    def open_window(self, container_id=2, kind="FURNACE"):
        b = self.b
        self.deliver(b.ContainerOpenPacket(
            container_id=container_id, type=b.ContainerType[kind],
            pos=b.BlockPos(x=8, y=220, z=10), entity_unique_id=-1,
        ))
        return self.bot.open_window

    def close_window(self, container_id=2, kind="FURNACE"):
        b = self.b
        self.deliver(b.ContainerClosePacket(
            container_id=container_id, container_type=b.ContainerType[kind],
            server_initiated_close=False,
        ))

    def data(self, container_id, data_id, value):
        self.deliver(self.b.ContainerSetDataPacket(container_id=container_id, id=data_id, value=value))

    def slots(self, container_id, ingredient):
        b = self.b
        self.deliver(b.InventoryContentPacket(
            inventory_id=container_id, slots=[item(b, ingredient, 4, 11), item(b), item(b)],
        ))

    async def test_fields_are_observed_independently_for_each_furnace_kind(self):
        for kind in ("FURNACE", "SMOKER", "BLAST_FURNACE"):
            with self.subTest(kind=kind):
                window = self.open_window(kind=kind)
                initial = self.bot.furnace_progress
                self.assertEqual(initial, FurnaceProgress(window))
                self.data(2, 0, 0)
                self.data(2, 2, 800)
                self.data(2, 3, 99)
                self.data(3, 1, 444)
                self.assertEqual(self.bot.furnace_progress, FurnaceProgress(window, 0, None, 800))
                pending = asyncio.create_task(self.bot.wait_for_furnace_progress(timeout=1))
                await asyncio.sleep(0)
                self.data(2, 1, 731)
                self.assertEqual(await pending, FurnaceProgress(window, 0, 731, 800))
                self.assertEqual(initial, FurnaceProgress(window))
                self.close_window(kind=kind)
                self.assertIsNone(self.bot.furnace_progress)

    async def test_wait_is_bound_to_the_open_instance_and_old_ids_are_ignored(self):
        original = self.open_window()
        self.data(2, 0, 93)
        saved = self.bot.furnace_progress
        pending = asyncio.create_task(self.bot.wait_for_furnace_progress(timeout=1))
        await asyncio.sleep(0)
        self.close_window()
        self.data(2, 1, 900)
        newer = self.open_window(container_id=3, kind="SMOKER")
        self.data(2, 1, 900)
        self.assertEqual(self.bot.furnace_progress, FurnaceProgress(newer))
        with self.assertRaisesRegex(RuntimeError, "closed or changed"):
            await pending
        self.assertEqual(saved, FurnaceProgress(original, 93))

        current_wait = asyncio.create_task(self.bot.wait_for_furnace_progress(timeout=1))
        await asyncio.sleep(0)
        self.close_window(container_id=3, kind="SMOKER")
        recycled = self.open_window(container_id=3, kind="SMOKER")
        self.assertEqual(recycled, newer)
        self.assertIsNot(recycled, newer)
        self.data(3, 0, 5)
        with self.assertRaisesRegex(RuntimeError, "closed or changed"):
            await current_wait
        self.assertEqual(self.bot.furnace_progress, FurnaceProgress(recycled, 5))

        self.open_window(container_id=4, kind="BREWING_STAND")
        self.data(4, 0, 99)
        self.assertIsNone(self.bot.furnace_progress)
        with self.assertRaisesRegex(RuntimeError, "no furnace"):
            await self.bot.wait_for_furnace_progress()

    async def test_transfer_scheduled_for_replaced_window_is_never_sent(self):
        self.open_window(kind="SMOKER")
        self.slots(2, 273)
        pending = asyncio.create_task(self.bot.take_from_container(0, 2))
        await asyncio.sleep(0)
        self.open_window(kind="SMOKER")
        self.slots(2, 517)
        with self.assertRaisesRegex(RuntimeError, "closed or changed"):
            await pending
        self.assertEqual(self.stream.sent, [])
        self.assertEqual(self.bot.container_slots[0].id, 517)

    async def test_late_verdict_affects_player_at_most_once_and_preserves_reopened_slots(self):
        b = self.b
        self.open_window(kind="BLAST_FURNACE")
        self.slots(2, 517)
        pending = asyncio.create_task(self.bot.take_from_container(0, 2))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        request, = self.stream.sent[0].requests
        self.assertEqual(request.actions[0].source.full_container_name.name.name,
                         "BLAST_FURNACE_INGREDIENT_CONTAINER")
        self.close_window(kind="BLAST_FURNACE")
        self.open_window(kind="BLAST_FURNACE")
        self.slots(2, 516)

        def correction(name, slot, count, net_id):
            return b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName[name]),
                slots=[b.ItemStackResponseSlotInfo(
                    requested_slot=slot, slot=slot, amount=count, item_stack_net_id=b.ItemStackNetId(raw_id=net_id),
                )],
            )

        verdict = b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            result=b.ItemStackNetResult.SUCCESS, client_request_id=request.client_request_id,
            containers=[correction("BLAST_FURNACE_INGREDIENT_CONTAINER", 0, 3, 11),
                        correction("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 2, 1, 49)],
        )])
        self.deliver(verdict)
        self.assertEqual((await pending).result.name, "SUCCESS")
        self.assertEqual((self.bot.container_slots[0].id, self.bot.container_slots[0].stack_size), (516, 4))
        self.assertEqual((self.bot.inventory_slots[2].id, self.bot.inventory_slots[2].stack_size), (517, 1))
        self.deliver(b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=2, item=item(b, 517, 7, 49)))
        self.deliver(verdict)
        self.assertEqual(self.bot.inventory_slots[2].stack_size, 7)


class FurnaceReaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("FurnaceReader", protocol=2225)
        self.b = self.bot.protocol.binding

        class Transport(FakeTransport):
            async def recv(self):
                value = await super().recv()
                if isinstance(value, Exception):
                    raise value
                return value

        self.transport = Transport()
        self.connection = PacketConnection(self.transport, self.bot.protocol, SimpleNamespace(
            compression_algorithm=0, compression_threshold=1,
        ))
        self.bot._containers = Containers(self.connection, None, None)
        self.reader = self.connection.start_receiving()
        self.reader_failure = None
        b = self.b
        opened = asyncio.create_task(self.connection.wait_for_packet(b.ContainerOpenPacket.ID, timeout=1))
        await asyncio.sleep(0)
        self.deliver(
            b.ContainerOpenPacket(container_id=2, type=b.ContainerType.FURNACE,
                                  pos=b.BlockPos(x=8, y=220, z=10), entity_unique_id=-1),
            b.InventoryContentPacket(inventory_id=2, slots=[item(b), item(b), item(b)]),
        )
        await opened
        self.window = self.bot.open_window

    async def asyncTearDown(self):
        self.bot._containers.close()
        if self.reader_failure is None:
            await self.connection.close()
        else:
            error_type, message = self.reader_failure
            with self.assertRaisesRegex(error_type, message):
                await self.connection.close()
        self.assertTrue(self.transport.closed)
        self.assertEqual((self.connection._subscribers, self.connection._reader_end_subscribers), ([], []))

    def deliver(self, *packets):
        batch = write_batch(self.bot.protocol.encode(packet) for packet in packets)
        self.transport.received.put_nowait(b"\xff" + batch)

    def cook(self, ticks):
        return self.b.ContainerSetDataPacket(container_id=2, id=0, value=ticks)

    async def assert_raw_eof_clears_window(self, source):
        with self.assertRaises(EOFError) as caught:
            await self.connection.recv_raw()
        self.assertIs(caught.exception, source)
        self.assertTrue(self.reader.done())
        self.assertIsNone(self.bot.open_window)
        self.assertIsNone(self.bot.container_slots)
        self.assertIsNone(self.bot.furnace_progress)

    async def test_pending_raw_receive_observes_cleared_window_on_eof(self):
        source = EOFError("end of transport")
        self.reader_failure = EOFError, str(source)
        self.transport.received.put_nowait(source)
        await self.assert_raw_eof_clears_window(source)

    async def test_raw_receive_after_reader_done_observes_cleared_window(self):
        source = EOFError("end of transport")
        self.reader_failure = EOFError, str(source)

        async def read_after_reader():
            self.assertTrue(self.reader.done())
            await self.assert_raw_eof_clears_window(source)

        self.transport.received.put_nowait(source)
        await asyncio.create_task(read_after_reader())

    async def test_each_waiter_receives_first_packet_in_a_batch(self):
        first = asyncio.create_task(self.bot.wait_for_furnace_progress(timeout=1))
        second = asyncio.create_task(self.bot.wait_for_furnace_progress(timeout=1))
        await asyncio.sleep(0)
        self.deliver(self.cook(1), self.cook(2))
        a, b = await asyncio.gather(first, second)
        self.assertEqual(a, FurnaceProgress(self.window, 1))
        self.assertEqual(b, a)
        self.assertIsNot(a, b)
        self.assertEqual(self.bot.furnace_progress, FurnaceProgress(self.window, 2))

    async def test_transport_eof_fails_pending_wait_and_clears_window(self):
        pending = asyncio.create_task(self.bot.wait_for_furnace_progress(timeout=1))
        await asyncio.sleep(0)
        self.reader_failure = ConnectionError, "empty compressed batch"
        self.transport.received.put_nowait(b"")
        with self.assertRaisesRegex(ConnectionError, "before a furnace progress update") as caught:
            await pending
        self.assertIsInstance(caught.exception.__cause__, ConnectionError)
        self.assertEqual(str(caught.exception.__cause__), "empty compressed batch")
        self.assertIsNone(self.bot.open_window)
        self.assertIsNone(self.bot.container_slots)
        self.assertIsNone(self.bot.furnace_progress)

    async def test_transport_error_is_preserved_when_progress_wait_fails(self):
        pending = asyncio.create_task(self.bot.wait_for_furnace_progress(timeout=1))
        await asyncio.sleep(0)
        source = ValueError("failed transport receive")
        self.reader_failure = ValueError, str(source)
        self.transport.received.put_nowait(source)
        with self.assertRaises(ConnectionError) as caught:
            await pending
        self.assertIs(caught.exception.__cause__, source)
        self.assertIsNone(self.bot.open_window)
        self.assertIsNone(self.bot.container_slots)
        self.assertIsNone(self.bot.furnace_progress)

    async def test_matched_update_survives_later_disconnect_in_the_same_batch(self):
        pending = asyncio.create_task(self.bot.wait_for_furnace_progress(timeout=1))
        await asyncio.sleep(0)
        self.deliver(self.cook(1), self.b.DisconnectPacket(), self.cook(2))
        self.assertEqual(await pending, FurnaceProgress(self.window, 1))
        self.assertIsNone(self.bot.open_window)
        self.assertIsNone(self.bot.furnace_progress)

    async def test_matched_update_survives_later_reader_failure(self):
        pending = asyncio.create_task(self.bot.wait_for_furnace_progress(timeout=1))
        await asyncio.sleep(0)
        self.reader_failure = EOFError, "end of transport"
        self.deliver(self.cook(1), self.cook(2))
        self.transport.received.put_nowait(EOFError("end of transport"))
        self.assertEqual(await pending, FurnaceProgress(self.window, 1))
        with self.assertRaisesRegex(EOFError, "end of transport"):
            await self.reader
        self.assertIsNone(self.bot.furnace_progress)

    async def test_normal_close_keeps_its_window_error_and_reader(self):
        pending = asyncio.create_task(self.bot.wait_for_furnace_progress(timeout=1))
        await asyncio.sleep(0)
        self.deliver(self.b.ContainerClosePacket(
            container_id=2, container_type=self.b.ContainerType.FURNACE, server_initiated_close=False,
        ))
        with self.assertRaisesRegex(RuntimeError, "furnace window closed or changed"):
            await pending
        self.assertFalse(self.reader.done())
        self.assertIsNone(self.bot.open_window)
