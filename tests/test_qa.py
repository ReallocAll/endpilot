import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bedrock_protocol import TextPacketType
from endpilot import Bot, LoginRefused
from endpilot.compression import PacketConnection
from endpilot.protocol import read_batch, write_batch
from endpilot.qa import PacketTimeline
from test_scheduled_input import ManualClock


class QATests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Bot("PacketObserver", protocol=2225)
        self.binding = self.bot.protocol.binding
        self.incoming = asyncio.Queue()
        self.transport = SimpleNamespace(recv=self.incoming.get, send=AsyncMock(), close=AsyncMock())
        self.addAsyncCleanup(self.bot.close)

    def deliver(self, *packets):
        self.incoming.put_nowait(write_batch(map(self.bot.protocol.encode, packets)))

    async def test_timeline_preserves_negotiation_login_and_live_reply_without_waiting_on_send(self):
        b = self.binding
        timeline = PacketTimeline(max_kept=3)
        self.deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.deliver(
            b.StartGamePacket(runtime_id=4, entity_id=17),
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        )
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeline=timeline)

        initial = timeline.received()
        self.assertEqual([packet.name for packet in initial], [
            "NetworkSettingsPacket", "StartGamePacket", "PlayStatusPacket",
        ])
        self.assertEqual([packet.index for packet in initial], [0, 1, 2])
        self.assertEqual([packet.protocol for packet in initial], [2225] * 3)
        cursor = timeline.next_index

        self.assertIsNone(await asyncio.wait_for(self.bot.send_command("say hello"), timeout=1))
        sent, = read_batch(self.transport.send.await_args.args[0])
        cmd = self.bot.protocol.decode(sent)
        self.assertIsInstance(cmd, b.CommandRequestPacket)
        self.assertEqual(cmd.command, "/say hello")
        self.assertEqual(cmd.origin.type, b.CommandOriginType.PLAYER)
        self.assertEqual(cmd.origin.uuid, self.bot.identity.uuid)
        self.assertEqual(cmd.origin.player_id, 17)

        reply = b.TextPacket(body=b.TextPacket.MessageOnly(type=TextPacketType.RAW, message="hello"))
        pending = asyncio.create_task(self.bot.packets.wait_for_packet(reply.ID, timeout=1))
        await asyncio.sleep(0)
        self.deliver(reply)
        raw = await pending
        observed, = timeline.received("TextPacket", since=cursor)
        self.assertEqual((observed.index, observed.raw), (cursor, raw))
        self.assertGreaterEqual(observed.received_at, initial[-1].received_at)
        self.assertEqual(self.bot.protocol.decode(observed.raw).body.message, "hello")
        self.assertEqual(timeline.received("NetworkSettingsPacket"), ())
        self.assertEqual(timeline.received_count("NetworkSettingsPacket"), 1)
        self.assertEqual(timeline.received(since=cursor + 1), ())
        await self.bot.close()
        self.assertEqual(timeline.received("TextPacket"), (observed,))

    async def test_replayed_startup_columns_keep_their_original_timeline_entries(self):
        b = self.binding
        timeline = PacketTimeline()
        decoded = self.enterContext(patch.object(self.bot.protocol, "decode", wraps=self.bot.protocol.decode))

        def column(x):
            return b.LevelChunkPacket(
                pos=b.ChunkPos(x=x, z=7), dimension_id=0, sub_chunks_count=0,
                client_request_sub_chunk_limit=1, cache_enabled=False, cache_metadata=[],
                serialized_chunk=b"\x01\x02" + b"\xff" * 23 + b"\x00", is_client_biome_update=False,
            )

        first, second, after_spawn = (column(x) for x in (1, 2, 3))
        dimensions = b.DimensionDataPacket()
        publisher = b.NetworkChunkPublisherUpdatePacket(radius=128)
        self.deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.deliver(
            b.StartGamePacket(runtime_id=4, entity_id=17), dimensions, publisher, first, second,
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN), after_spawn,
        )
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet(
                "http://127.0.0.1:19132", timeline=timeline, retain_initial_chunks=True,
            )

        login = timeline.received()
        self.assertEqual([packet.name for packet in login], [
            "NetworkSettingsPacket", "StartGamePacket", "DimensionDataPacket", "NetworkChunkPublisherUpdatePacket",
            "LevelChunkPacket", "LevelChunkPacket", "PlayStatusPacket",
        ])
        first_raw, second_raw, live_raw = map(self.bot.protocol.encode, (first, second, after_spawn))
        dimensions_raw, publisher_raw = map(self.bot.protocol.encode, (dimensions, publisher))
        self.assertEqual([packet.raw for packet in login[2:6]], [dimensions_raw, publisher_raw, first_raw, second_raw])
        self.assertEqual(timeline.received_count("LevelChunkPacket"), 2)
        cursor = timeline.next_index

        delivered = []
        ready = asyncio.Event()

        def observe(raw):
            if raw.packet_id in (b.DimensionDataPacket.ID, b.NetworkChunkPublisherUpdatePacket.ID, b.LevelChunkPacket.ID):
                delivered.append(raw)
                if len(delivered) == 6:
                    ready.set()

        unsubscribe = self.bot.packets.subscribe_raw(observe)
        self.deliver(first)  # A separate wire packet can have the same bytes as a retained column.
        try:
            await asyncio.wait_for(ready.wait(), timeout=1)
        finally:
            unsubscribe()

        self.assertEqual(delivered, [dimensions_raw, publisher_raw, first_raw, second_raw, live_raw, first_raw])
        self.assertEqual(timeline.received()[:cursor], login)
        live = timeline.received(since=cursor)
        self.assertEqual([packet.index for packet in live], [cursor, cursor + 1])
        self.assertEqual([packet.raw for packet in live], [live_raw, first_raw])
        self.assertEqual(timeline.received_count("LevelChunkPacket"), 4)
        self.assertTrue(all(self.bot.column_coverage((x, 7)).announced for x in (1, 2, 3)))
        # Timeline decoding happens at receipt. Blocks decodes each startup
        # dimension definition once and reuses it during replay.
        self.assertEqual([call.args[0] for call in decoded.call_args_list
                          if call.args[0].packet_id == b.DimensionDataPacket.ID],
                         [dimensions_raw, dimensions_raw])
        # Blocks also decodes each delivered column once for coverage.
        self.assertEqual([call.args[0] for call in decoded.call_args_list
                          if call.args[0].packet_id == b.LevelChunkPacket.ID],
                         [first_raw, second_raw, first_raw, second_raw, live_raw, live_raw, first_raw, first_raw])

    async def test_raw_receive_without_opt_in_history_does_not_decode(self):
        b = self.binding
        packets = PacketConnection(self.transport, self.bot.protocol, b.NetworkSettingsPacket(
            compression_algorithm=b.PacketCompressionAlgorithm.NONE,
        ))
        self.addAsyncCleanup(packets.close)
        packet = b.TextPacket(body=b.TextPacket.MessageOnly(type=TextPacketType.RAW, message="local sample"))
        self.deliver(packet)
        with patch.object(self.bot.protocol, "decode", wraps=self.bot.protocol.decode) as decoded:
            self.assertEqual(await packets.recv_raw(), self.bot.protocol.encode(packet))
            decoded.assert_not_called()

    async def test_packet_pending_during_loading_keeps_its_batch_receive_tick(self):
        b = self.binding
        clock = ManualClock()
        timeline = PacketTimeline()
        initializing, resume = asyncio.Event(), asyncio.Event()

        async def send(batch):
            if any(raw.packet_id == b.SetLocalPlayerAsInitializedPacket.ID for raw in read_batch(batch)):
                initializing.set()
                await resume.wait()

        self.transport.send.side_effect = send
        with clock.patch_scheduler(), patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            joining = asyncio.create_task(self.bot.join_nethernet("http://127.0.0.1:19132", timeline=timeline))
            try:
                await clock.wake_at(50)
                self.deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
                await asyncio.sleep(0)
                await clock.wake_at(100)
                self.deliver(
                    b.StartGamePacket(runtime_id=4, entity_id=17),
                    b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
                    b.TextPacket(body=b.TextPacket.MessageOnly(type=TextPacketType.RAW, message="same batch")),
                )
                await asyncio.wait_for(initializing.wait(), 1)
                self.assertFalse(timeline.received("TextPacket"))
                await clock.wake_at(150)
                self.assertEqual(self.bot.get_tick(), 3)
                resume.set()
                await asyncio.wait_for(joining, 1)
                await asyncio.sleep(0)
                self.assertEqual([(entry.name, entry.tick) for entry in timeline.received()], [
                    ("NetworkSettingsPacket", 1), ("StartGamePacket", 2),
                    ("PlayStatusPacket", 2), ("TextPacket", 2),
                ])
            finally:
                joining.cancel()
                await asyncio.gather(joining, return_exceptions=True)
                await self.bot.close()

    async def test_raknet_login_refusal_is_retained_and_transport_closes(self):
        b = self.binding
        timeline = PacketTimeline()
        self.deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        rejected = b.PlayStatusPacket(status=b.PlayStatus.LOGIN_FAILED_SERVER_FULL_SUB_CLIENT)
        self.deliver(rejected)
        with patch("endpilot.transport.raknet.connect", return_value=self.transport):
            with self.assertRaises(LoginRefused):
                await self.bot.join_raknet("127.0.0.1", timeline=timeline)
        self.transport.close.assert_awaited_once()
        self.assertEqual([packet.name for packet in timeline.received()], ["NetworkSettingsPacket", "PlayStatusPacket"])
        observed, = timeline.received("PlayStatusPacket", since=1)
        self.assertEqual(self.bot.protocol.decode(observed.raw).status, rejected.status)
        self.assertEqual(timeline.next_index, 2)
