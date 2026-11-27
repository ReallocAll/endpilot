import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from endpilot import Bot, CursorObservation, HeldItemStack
from endpilot.protocol import write_batch
from test_scheduled_input import ManualClock, Wire


class CursorObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("CursorObservation", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = self.bot.protocol.binding

    async def join(self, *before_spawn):
        b = self.b
        self.wire = Wire(self.bot.protocol)
        for packet in (b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
                       b.StartGamePacket(runtime_id=7, entity_id=17), *before_spawn,
                       b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN)):
            self.wire.push(packet)
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        received = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in (*packets, marker)))
        await received

    def item(self, count, net_id=None, *, data=b""):
        return self.b.SerializedNetworkItemStackDescriptor(
            id=318 if count else 0, stack_size=count, net_id_variant=net_id, user_data_buffer=data,
        )

    def content(self, *items, inventory="PLAYER_ONLY_UI", **fields):
        return self.b.InventoryContentPacket(inventory_id=self.b.ContainerID[inventory], slots=items, **fields)

    def slot(self, index, item, *, inventory="PLAYER_ONLY_UI"):
        return self.b.InventorySlotPacket(inventory_id=self.b.ContainerID[inventory], slot=index, item=item)

    def verdict(self, count, net_id=None, *, slot=0, container="CURSOR_CONTAINER", result="SUCCESS"):
        b = self.b
        return b.ItemStackResponseInfo(
            client_request_id=b.ItemStackRequestId(raw_id=-17), result=b.ItemStackNetResult[result],
            containers=[b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName[container]),
                slots=[b.ItemStackResponseSlotInfo(slot=slot, requested_slot=0, amount=count,
                    item_stack_net_id=None if net_id is None else b.ItemStackNetId(raw_id=net_id))],
            )],
        )

    def responses(self, *reports):
        return self.b.ItemStackResponsePacket(responses=reports)

    async def test_explicit_descriptors_and_unmatched_verdicts_follow_receive_order(self):
        await self.join(self.content(self.item(0)))
        self.assertEqual(self.bot.last_cursor_observation, CursorObservation("inventory_content", 0, None, None))
        sent_before = list(self.wire.sent)
        nbt = bytes.fromhex("ffff010a000003060044616d616765780500000000000000000000")
        item = self.item(3, 6, data=nbt)
        await self.deliver(self.responses(self.verdict(2, 6)), self.content(item))
        expected = HeldItemStack(318, 3, 0, 6, 0, nbt)
        self.assertEqual(self.bot.last_cursor_observation, CursorObservation("inventory_content", 3, 6, expected))

        await self.deliver(self.slot(0, item), self.responses(self.verdict(1, 9), self.verdict(2, 6)))
        self.assertEqual(self.bot.last_cursor_observation, CursorObservation("item_stack_response", 2, 6, None))
        await self.deliver(self.responses(self.verdict(1, 6)), self.slot(0, self.item(1, data=nbt)))
        self.assertEqual(self.bot.last_cursor_observation, CursorObservation(
            "inventory_slot", 1, None, HeldItemStack(318, 1, 0, None, 0, nbt),
        ))
        await self.deliver(self.responses(self.verdict(0)))
        self.assertEqual(self.bot.last_cursor_observation, CursorObservation("item_stack_response", 0, None, None))
        self.assertEqual(self.wire.sent, sent_before)

    async def test_snapshot_is_independent_and_non_cursor_updates_preserve_it(self):
        b = self.b
        await self.join()
        self.assertIsNone(self.bot.last_cursor_observation)
        packet = self.slot(0, self.item(2, 23))
        raw = self.bot.protocol.encode(packet)
        decode = self.bot.protocol.decode
        exposed = SimpleNamespace(id=318, stack_size=2, aux_value=4, net_id_variant=23, block_runtime_id=5,
                                  user_data_buffer=bytearray(b"cursor item data"))
        source = SimpleNamespace(inventory_id=b.ContainerID.PLAYER_ONLY_UI, slot=0, item=exposed,
                                 full_container_name=None, storage_item=None)
        with patch.object(self.bot.protocol, "decode", side_effect=lambda incoming: (
                source if incoming == raw else decode(incoming))):
            await self.deliver(packet)
        sample = self.bot.last_cursor_observation
        exposed.stack_size = 1
        exposed.user_data_buffer[0] = 0
        self.assertEqual(sample, CursorObservation("inventory_slot", 2, 23,
            HeldItemStack(318, 2, 4, 23, 5, b"cursor item data")))
        with self.assertRaises(FrozenInstanceError):
            sample.count = 0
        with self.assertRaises(FrozenInstanceError):
            sample.observed_item.item_id = 0

        await self.deliver(
            self.content(self.item(1, 23), inventory="INVENTORY"),
            self.slot(0, self.item(1, 23), inventory="OFFHAND"),
            self.content(storage_item=self.item(2, 23)),
            self.slot(1, self.item(0)),
            self.slot(0, b.SerializedNetworkItemStackDescriptor(id=318, stack_size=0)),
            self.responses(self.verdict(0, slot=1), self.verdict(0, container="HOTBAR_CONTAINER"),
                           self.verdict(0, result="ERROR")),
            b.ContainerOpenPacket(container_id=2, type=b.ContainerType.WORKBENCH),
            b.ContainerClosePacket(container_id=2, container_type=b.ContainerType.WORKBENCH,
                                   server_initiated_close=True),
        )
        self.assertIs(self.bot.last_cursor_observation, sample)

    async def test_disconnect_reader_end_and_reconnect_clear_per_connection(self):
        self.assertIsNone(self.bot.last_cursor_observation)
        await self.join()
        await self.deliver(self.responses(self.verdict(1, 8)))
        ended_samples = []
        self.bot.subscribe_session_end(lambda _: ended_samples.append(self.bot.last_cursor_observation))
        old_wire = self.wire
        await self.deliver(self.b.DisconnectPacket(), self.responses(self.verdict(2, 9)))
        self.assertIsNone(self.bot.last_cursor_observation)
        self.assertEqual(ended_samples, [None])
        await self.bot.close()
        await self.join()
        self.assertIsNone(self.bot.last_cursor_observation)
        await self.deliver(self.responses(self.verdict(1, 10)))
        current = self.bot.last_cursor_observation
        old_wire.push(self.responses(self.verdict(1, 9)))
        await self.deliver(self.b.SetTimePacket())
        self.assertIs(self.bot.last_cursor_observation, current)

        self.wire.incoming.put_nowait(ConnectionError("reader ended"))
        self.assertEqual((await self.bot.wait_for_session_end(timeout=1)).kind, "reader_end")
        self.assertIsNone(self.bot.last_cursor_observation)
        self.assertEqual(ended_samples, [None, None])
        with self.assertRaisesRegex(ConnectionError, "reader ended"):
            await self.bot.close()
        await self.join(self.slot(0, self.item(0)))
        self.assertEqual(self.bot.last_cursor_observation, CursorObservation("inventory_slot", 0, None, None))
        await self.bot.close()
        self.assertEqual(ended_samples, [None, None, None])
        self.assertIsNone(self.bot.last_cursor_observation)

    async def test_finished_reader_clears_public_sample_before_end_callbacks(self):
        loop = asyncio.get_running_loop()
        reading = loop.create_future()
        cancellation = asyncio.CancelledError()
        original_recv = Wire.recv
        ended = []

        def read_sample():
            sample = self.bot.last_cursor_observation
            reading.set_result((list(ended), sample))

        async def recv(wire):
            try:
                return await original_recv(wire)
            except asyncio.CancelledError as error:
                if error is cancellation:
                    loop.call_soon(read_sample)
                raise

        with patch.object(Wire, "recv", recv):
            await self.join(self.content(self.item(0)))
            self.bot.subscribe_session_end(ended.append)
            self.assertIsNotNone(self.bot.last_cursor_observation)
            self.wire.incoming.put_nowait(cancellation)
            self.assertEqual(await asyncio.wait_for(reading, 1), ([], None))
