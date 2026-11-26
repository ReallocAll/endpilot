import asyncio
import unittest
from unittest.mock import patch

from endpilot import Bot, TitleEvent
from endpilot.compression import PacketConnection
from endpilot.login import JoinSession
from endpilot.protocol import write_batch


class BatchTransport:
    def __init__(self):
        self.incoming = asyncio.Queue()

    async def recv(self):
        return await self.incoming.get()

    async def send(self, data):
        pass

    async def close(self):
        pass


class TitleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("EndPilotTitle", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.transport = BatchTransport()
        binding = self.bot.protocol.binding
        settings = binding.NetworkSettingsPacket(
            compression_algorithm=binding.PacketCompressionAlgorithm.ZLIB,
            compression_threshold=1,
        )
        self.packet = binding.packets[88]
        self.title_type = self.packet.TitleType
        packets = PacketConnection(self.transport, self.bot.protocol, settings)
        session = JoinSession(packets, self.bot.identity, "http://127.0.0.1:19132")
        session.player_id, session.player_unique_id = 42, 7
        with patch.object(JoinSession, "connect_nethernet", return_value=session):
            await self.bot.join_nethernet("http://127.0.0.1:19132")

    def deliver(self, *packets):
        raw = [self.bot.protocol.encode(packet) for packet in packets]
        self.transport.incoming.put_nowait(b"\xff" + write_batch(raw))

    async def test_dispatches_adjacent_kinds_to_independent_waiters(self):
        kinds = (None, "title", "subtitle", "actionbar", "times", "clear", "reset")
        waits = [asyncio.create_task(self.bot.wait_for_title(kind=kind, timeout=1)) for kind in kinds]
        await asyncio.sleep(0)

        self.deliver(
            self.packet(type=self.title_type.SUBTITLE, title_text="§aSide"),
            self.packet(type=self.title_type.TIMES, fade_in_time=0, stay_time=45, fade_out_time=6),
            self.packet(type=self.title_type.TITLE, title_text="Top", fade_in_time=9),
            self.packet(type=self.title_type.ACTIONBAR, title_text=""),
            self.packet(type=self.title_type.CLEAR),
            self.packet(type=self.title_type.RESET),
        )

        self.assertEqual(await asyncio.gather(*waits), [
            TitleEvent("subtitle", text="§aSide"),
            TitleEvent("title", text="Top"),
            TitleEvent("subtitle", text="§aSide"),
            TitleEvent("actionbar", text=""),
            TitleEvent("times", fade_in_ticks=0, stay_ticks=45, fade_out_ticks=6),
            TitleEvent("clear"),
            TitleEvent("reset"),
        ])

    async def test_preserves_each_text_object_and_filtered_text(self):
        waits = [asyncio.create_task(self.bot.wait_for_title(kind=kind, timeout=1))
                 for kind in ("title", "subtitle", "actionbar")]
        await asyncio.sleep(0)
        title = '{"rawtext":[{"translate":"title.test"}]}'
        subtitle = '{"rawtext":[{"text":"subtitle"}]}'
        actionbar = '{"rawtext":[{"text":"status"}]}'
        filtered = '{"rawtext":[{"text":"filtered"}]}'
        self.deliver(
            self.packet(type=self.title_type.TITLE_TEXT_OBJECT, title_text=title, filtered_title_text=filtered),
            self.packet(type=self.title_type.SUBTITLE_TEXT_OBJECT, title_text=subtitle),
            self.packet(type=self.title_type.ACTIONBAR_TEXT_OBJECT, title_text=actionbar),
        )

        self.assertEqual(await asyncio.gather(*waits), [
            TitleEvent("title", text=title, filtered_text=filtered, text_object=True),
            TitleEvent("subtitle", text=subtitle, text_object=True),
            TitleEvent("actionbar", text=actionbar, text_object=True),
        ])

    async def test_timeout_ignores_other_kinds_and_past_packets(self):
        pending = asyncio.create_task(self.bot.wait_for_title(kind="title", timeout=0.05))
        await asyncio.sleep(0)
        self.deliver(self.packet(type=self.title_type.SUBTITLE, title_text="past"))
        with self.assertRaises(TimeoutError):
            await pending
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_title(timeout=0.05)

        pending = asyncio.create_task(self.bot.wait_for_title(timeout=1))
        await asyncio.sleep(0)
        self.deliver(self.packet(type=self.title_type.TITLE, title_text="new", filtered_title_text="new filtered"))
        self.assertEqual(await pending, TitleEvent("title", text="new", filtered_text="new filtered"))

    async def test_close_releases_pending_wait(self):
        pending = asyncio.create_task(self.bot.wait_for_title(timeout=15))
        await asyncio.sleep(0)
        await self.bot.close()
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(pending, timeout=1)
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            await self.bot.wait_for_title()
