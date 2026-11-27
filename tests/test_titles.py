import asyncio
from dataclasses import FrozenInstanceError
import gc
import unittest
import weakref
from unittest.mock import patch

from bedrock_protocol import DecodeError

from endpilot import Bot, RecoverableError, TextPacketType, TitleEvent
from endpilot.compression import PacketConnection
from endpilot.login import JoinSession
from endpilot.protocol import RawPacket, write_batch


class BatchTransport:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []

    async def recv(self):
        return await self.incoming.get()

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        pass


class ErrorSubscriptionLifetimeTests(unittest.TestCase):
    def test_retained_unsubscribe_does_not_keep_other_observers_alive(self):
        bot = Bot(protocol=2225)
        bot_ref = weakref.ref(bot)
        unsubscribe = bot.subscribe_error(lambda _: None)

        def observer(_):
            self.fail("unexpected diagnostic")

        observer_ref = weakref.ref(observer)
        bot.subscribe_error(observer)
        del observer
        self.assertIsNotNone(observer_ref())

        del bot
        gc.collect()
        self.assertIsNone(bot_ref())
        self.assertIsNone(observer_ref())
        unsubscribe()
        unsubscribe()


class TitleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("EndPilotTitle", protocol=2225)
        self.binding = self.bot.protocol.binding
        self.addAsyncCleanup(self.bot.close)
        await self.join()

    async def join(self):
        self.transport = BatchTransport()
        binding = self.binding
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
        raw = [packet if isinstance(packet, RawPacket) else self.bot.protocol.encode(packet) for packet in packets]
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

    async def test_subscriptions_project_each_title_once_in_wire_order(self):
        t = self.title_type
        packet = self.packet
        text = self.binding.TextPacket
        tip = text(body=text.MessageOnly(type=TextPacketType.TIP, message="same display region"))
        raw_title = '{"rawtext":[{"translate":"title.test"}]}\n'
        raw_subtitle = '{"rawtext":[{"text":"subtitle"}]}\n'
        raw_actionbar = '{"rawtext":[{"text":"status"}]}\n'
        raw_filtered = '{"rawtext":[{"text":"filtered"}]}\n'
        updates = (
            packet(type=t.TITLE, title_text="Top", filtered_title_text="Filtered"),
            packet(type=t.SUBTITLE, title_text="§aSide\n"),
            packet(type=t.ACTIONBAR, title_text=""),
            packet(type=t.ACTIONBAR, title_text=""),
            packet(type=t.TITLE_TEXT_OBJECT, title_text=raw_title),
            packet(type=t.SUBTITLE_TEXT_OBJECT, title_text=raw_subtitle),
            packet(type=t.ACTIONBAR_TEXT_OBJECT, title_text=raw_actionbar, filtered_title_text=raw_filtered),
            packet(type=t.TIMES, fade_in_time=0, stay_time=45, fade_out_time=6),
            packet(type=t.CLEAR),
            packet(type=t.RESET),
        )
        expected = [
            TitleEvent("title", text="Top", filtered_text="Filtered"),
            TitleEvent("subtitle", text="§aSide\n"),
            TitleEvent("actionbar", text=""),
            TitleEvent("actionbar", text=""),
            TitleEvent("title", text=raw_title, text_object=True),
            TitleEvent("subtitle", text=raw_subtitle, text_object=True),
            TitleEvent("actionbar", text=raw_actionbar, filtered_text=raw_filtered, text_object=True),
            TitleEvent("times", fade_in_ticks=0, stay_ticks=45, fade_out_ticks=6),
            TitleEvent("clear"),
            TitleEvent("reset"),
        ]
        seen, actionbars, tips = [], [], []
        sends_before = list(self.transport.sent)
        with patch.object(self.bot.protocol, "decode", wraps=self.bot.protocol.decode) as decode:
            history = asyncio.create_task(self.bot.packets.wait_for_packet(self.packet.ID, timeout=1))
            await asyncio.sleep(0)
            self.deliver(packet(type=t.TITLE, title_text="history"))
            await history
            self.assertFalse(any(call.args[0].packet_id == self.packet.ID for call in decode.call_args_list))
            waiting = asyncio.create_task(self.bot.wait_for_title(kind="actionbar", timeout=1))
            await asyncio.sleep(0)
            self.bot.subscribe_titles(seen.append)
            self.bot.subscribe_titles(actionbars.append, kind="actionbar")
            self.bot.subscribe_text(tips.append, kind=TextPacketType.TIP)
            self.deliver(tip, *updates)
            self.assertEqual(await waiting, expected[2])

        self.assertEqual(seen, expected)
        self.assertEqual(actionbars, [expected[2], expected[3], expected[6]])
        self.assertEqual([event.message for event in tips], ["same display region"])
        self.assertEqual(sum(call.args[0].packet_id == self.packet.ID for call in decode.call_args_list), len(updates))
        self.assertEqual(self.transport.sent, sends_before)

    async def test_subscription_added_by_raw_observer_starts_with_the_next_packet(self):
        current = self.packet(type=self.title_type.TITLE, title_text="current")
        following = self.packet(type=self.title_type.TITLE, title_text="following")
        for history in ("first", "removed", "active"):
            with self.subTest(history=history):
                if history != "first":
                    await self.bot.close()
                    await self.join()
                previous, added = [], []

                def register(raw):
                    if raw.packet_id == self.packet.ID:
                        self.bot.subscribe_titles(added.append)
                        remove_registrar()

                remove_registrar = self.bot.packets.subscribe_raw(register)
                if history != "first":
                    unsubscribe = self.bot.subscribe_titles(previous.append)
                    if history == "removed":
                        unsubscribe()
                self.deliver(current, following)
                first = await self.bot.wait_for_title(timeout=1)
                second = TitleEvent("title", text="following")
                self.assertEqual(first, TitleEvent("title", text="current"))
                self.assertEqual(added, [second])
                self.assertEqual(previous, [first, second] if history == "active" else [])

    async def test_malformed_title_is_decoded_and_reported_once_then_delivery_continues(self):
        waiting = asyncio.create_task(self.bot.wait_for_title(timeout=1))
        await asyncio.sleep(0)
        seen, other, diagnostics = [], [], []
        self.bot.subscribe_error(diagnostics.append)
        self.bot.subscribe_titles(seen.append)
        self.bot.subscribe_titles(other.append, kind="title")
        malformed = RawPacket(self.packet.ID, b"")
        valid = self.packet(type=self.title_type.TITLE, title_text="after malformed")

        with (
            patch.object(self.bot.protocol, "decode", wraps=self.bot.protocol.decode) as decode,
            patch.object(asyncio.get_running_loop(), "call_exception_handler") as report,
        ):
            self.deliver(malformed, valid)
            with self.assertRaises(DecodeError) as failed:
                await waiting

        self.assertEqual([call.args[0] for call in decode.call_args_list], [
            malformed, self.bot.protocol.encode(valid),
        ])
        report.assert_called_once()
        diagnostic = report.call_args.args[0]["exception"]
        self.assertIsInstance(diagnostic, DecodeError)
        for error in (failed.exception, diagnostic):
            self.assertEqual((error.reason, error.path, error.offset), ("truncated", ("type",), 0))
        self.assertEqual(seen, [TitleEvent("title", text="after malformed")])
        self.assertEqual(other, seen)
        expected = RecoverableError("titles", self.packet.ID, "DecodeError", "could not decode SetTitle")
        self.assertEqual(diagnostics, [expected])
        with self.assertRaises(FrozenInstanceError):
            diagnostics[0].message = "changed"
        self.assertIsNone(self.bot.session_end)

    async def test_error_observer_does_not_decode_titles_without_a_title_consumer(self):
        diagnostics = []
        self.bot.subscribe_error(diagnostics.append)
        malformed = RawPacket(self.packet.ID, b"")
        with patch.object(self.bot.protocol, "decode", wraps=self.bot.protocol.decode) as decode:
            history = asyncio.create_task(self.bot.packets.wait_for_packet(self.packet.ID, timeout=1))
            await asyncio.sleep(0)
            self.deliver(malformed)
            self.assertEqual(await history, malformed)

        self.assertFalse(any(call.args[0].packet_id == self.packet.ID for call in decode.call_args_list))
        self.assertEqual(diagnostics, [])

    async def test_title_waiters_share_decode_error_when_error_observer_closes_bot(self):
        loop = asyncio.get_running_loop()
        closing, diagnostics = [], []
        old_session = self.bot._session
        old_titles = self.bot._titles
        old_titles_ref = weakref.ref(old_titles)
        old_publisher = old_titles._publish_error
        malformed = RawPacket(self.packet.ID, b"")

        def close_bot(diagnostic):
            diagnostics.append(diagnostic)
            closing.append(asyncio.eager_task_factory(loop, self.bot.close()))

        self.bot.subscribe_error(close_bot)
        waiting = [asyncio.create_task(self.bot.wait_for_title(timeout=1)) for _ in range(2)]
        await asyncio.sleep(0)

        with (
            patch.object(loop, "call_exception_handler") as report,
            patch.object(self.bot.protocol, "decode", wraps=self.bot.protocol.decode) as decode,
        ):
            self.deliver(malformed)
            try:
                failures = await asyncio.gather(*waiting, return_exceptions=True)
            finally:
                await asyncio.gather(*closing)

        self.assertEqual([call.args[0] for call in decode.call_args_list], [malformed])
        self.assertIsNot(failures[0], failures[1])
        for error in failures:
            self.assertIsInstance(error, DecodeError)
            self.assertEqual((error.reason, error.path, error.offset), ("truncated", ("type",), 0))
        expected = RecoverableError("titles", self.packet.ID, "DecodeError", "could not decode SetTitle")
        self.assertEqual(diagnostics, [expected])
        self.assertEqual(len(closing), 1)
        self.assertEqual(self.bot.session_end.kind, "local_close")
        report.assert_not_called()
        self.assertIsNone(old_titles._last_event)

        closing.clear()
        del old_titles, failures, error, waiting
        await self.join()
        self.assertIsNot(self.bot._session, old_session)
        old_publisher(expected)
        self.assertEqual(diagnostics, [expected])
        self.assertIsNone(self.bot.session_end)
        del old_session
        await asyncio.sleep(0)
        gc.collect()
        self.assertIsNone(old_titles_ref())

    async def test_error_subscriptions_follow_bot_and_isolate_callback_failures(self):
        seen, removed = [], []
        observer_failure = RuntimeError("error observer failed on purpose")

        def fail(_event):
            remove_observer()
            raise observer_failure

        remove_failure = self.bot.subscribe_error(fail)
        remove_observer = self.bot.subscribe_error(removed.append)
        unsubscribe = self.bot.subscribe_error(seen.append)
        old_session = self.bot._session
        old_publisher = self.bot._titles._publish_error
        malformed = RawPacket(self.packet.ID, b"")

        async def wait_for_failure():
            waiting = asyncio.create_task(self.bot.wait_for_title(timeout=1))
            await asyncio.sleep(0)
            self.deliver(malformed)
            with self.assertRaises(DecodeError):
                await waiting

        with patch.object(asyncio.get_running_loop(), "call_exception_handler") as report:
            await wait_for_failure()
        report.assert_called_once()
        self.assertIs(report.call_args.args[0]["exception"], observer_failure)
        self.assertEqual(len(seen), 1)
        first = seen[0]
        self.assertEqual(removed, [])
        remove_observer()
        remove_failure()

        await self.bot.close()
        await self.join()
        self.assertIsNot(self.bot._session, old_session)
        old_publisher(first)
        self.assertEqual(seen, [first])
        with patch.object(asyncio.get_running_loop(), "call_exception_handler") as report:
            await wait_for_failure()
        report.assert_not_called()
        self.assertEqual(seen, [first, first])
        self.assertIsNot(seen[0], seen[1])
        unsubscribe()
        unsubscribe()
        await wait_for_failure()
        self.assertEqual(len(seen), 2)

    async def test_unsubscribe_is_immediate_and_observer_errors_are_isolated(self):
        seen = []
        listener = seen.append
        failure = ValueError("observer failed")
        cancellation = asyncio.CancelledError("observer cancelled itself")

        def remove_and_fail(event):
            if event.text == "second":
                unsubscribe()
                unsubscribe()
                raise failure

        def raise_cancellation(event):
            if event.text == "second":
                raise cancellation

        self.bot.subscribe_titles(remove_and_fail)
        unsubscribe = self.bot.subscribe_titles(listener)
        self.bot.subscribe_titles(raise_cancellation)
        self.bot.subscribe_titles(listener)
        waiting = asyncio.create_task(self.bot.wait_for_title(kind="reset", timeout=1))
        await asyncio.sleep(0)
        with patch.object(asyncio.get_running_loop(), "call_exception_handler") as report:
            self.deliver(
                *(self.packet(type=self.title_type.TITLE, title_text=text) for text in ("first", "second", "third")),
                self.packet(type=self.title_type.RESET),
            )
            await waiting

        self.assertEqual([event.text for event in seen], ["first", "first", "second", "third", None])
        self.assertEqual([call.args[0]["exception"] for call in report.call_args_list], [failure, cancellation])

    async def test_disconnect_ends_subscriptions_before_trailing_packets_and_rejoin(self):
        seen = []
        unsubscribe = self.bot.subscribe_titles(seen.append)
        pending = asyncio.create_task(self.bot.wait_for_title(kind="reset", timeout=1))
        await asyncio.sleep(0)
        self.deliver(
            self.packet(type=self.title_type.TITLE, title_text="before"),
            self.binding.DisconnectPacket(),
            self.packet(type=self.title_type.RESET),
        )
        self.assertEqual(await pending, TitleEvent("reset"))
        self.assertEqual(seen, [TitleEvent("title", text="before")])
        self.assertIsNone(self.bot._titles._last_event)
        pending = asyncio.create_task(self.bot.wait_for_title(kind="clear", timeout=1))
        await asyncio.sleep(0)
        self.deliver(self.packet(type=self.title_type.CLEAR))
        self.assertEqual(await pending, TitleEvent("clear"))
        self.assertIsNone(self.bot._titles._last_event)
        with self.assertRaises(ConnectionError):
            self.bot.subscribe_titles(seen.append)
        await self.bot.close()
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            self.bot.subscribe_titles(seen.append)

        await self.join()
        fresh = []
        self.bot.subscribe_titles(fresh.append)
        unsubscribe()
        packet = self.packet(type=self.title_type.ACTIONBAR, title_text="fresh")
        self.deliver(packet)
        event = await self.bot.wait_for_title(timeout=1)
        self.assertEqual(fresh, [event])
        self.assertEqual(seen, [TitleEvent("title", text="before")])

    async def test_first_subscription_rejects_after_disconnect(self):
        self.deliver(self.binding.DisconnectPacket())
        await self.bot.wait_for_server_disconnect(timeout=1)
        self.assertFalse(self.bot.packets._reader_end_result()[0])
        with self.assertRaises(ConnectionError):
            self.bot.subscribe_titles(lambda _: None)

    async def test_reader_failure_releases_listener(self):
        diagnostics = []
        self.bot.subscribe_error(diagnostics.append)

        def listener(_event):
            self.fail("unexpected title event")

        reference = weakref.ref(listener)
        unsubscribe = self.bot.subscribe_titles(listener)
        del listener
        ended = asyncio.Event()
        self.bot.packets.subscribe_reader_end(lambda _: ended.set())
        self.transport.incoming.put_nowait(b"\xff\x02\x00")
        await asyncio.wait_for(ended.wait(), timeout=1)
        self.assertEqual((await self.bot.wait_for_session_end(timeout=1)).kind, "reader_end")
        self.assertEqual(diagnostics, [])

        self.assertIsNone(reference())
        unsubscribe()
        with self.assertRaises(ConnectionError):
            self.bot.subscribe_titles(lambda _: None)
        with self.assertRaisesRegex(ValueError, "truncated packet in batch"):
            await self.bot.close()
