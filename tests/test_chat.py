import asyncio
import re
import unittest
import zlib
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bedrock_protocol import TextPacketType
from endpilot import Bot, ResourcePackIndex, TextEvent
from endpilot.chat import Chat, ChatMessage
from endpilot.compression import PacketConnection
from endpilot.login import JoinSession
from endpilot.protocol import Protocol, read_batch, write_batch


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_typed_chat_send_and_filtered_server_observation(self):
        protocol = Protocol(2193)
        stream = SimpleNamespace(
            protocol=protocol, send=AsyncMock(), wait_for_packet=AsyncMock(),
            subscribe_raw=lambda _: lambda: None, subscribe_reader_end=lambda _: lambda: None,
        )
        chat = Chat(stream, "EndPilotTest")
        await chat.send("hello from player")
        sent = stream.send.await_args.args[0]
        encoded = protocol.encode(sent)
        decoded = protocol.decode(encoded)
        self.assertEqual(encoded.packet_id, 9)
        self.assertEqual(decoded.body.type, TextPacketType.CHAT)
        self.assertEqual(decoded.body.author, "EndPilotTest")
        self.assertEqual(decoded.body.message, "hello from player")

        text = protocol.binding.packets[9]
        def incoming(author, message, kind=TextPacketType.CHAT):
            packet = text(
                localize=False,
                body=text.AuthorAndMessage(type=kind, author=author, message=message),
                xuid="", platform_id="", filtered_message=None,
            )
            return protocol.encode(packet)

        async def wait(packet_id, *, match, timeout):
            self.assertEqual(packet_id, 9)
            self.assertEqual(timeout, 5)
            self.assertFalse(match(incoming("AnotherPlayer", "not this player")))
            self.assertTrue(match(incoming("EndPilotTest\u00a7r", "relay confirmed")))
            return incoming("EndPilotTest\u00a7r", "relay confirmed")

        stream.wait_for_packet.side_effect = wait
        observed = await chat.wait_for_message(author="EndPilotTest", timeout=5)
        self.assertEqual(observed, ChatMessage("EndPilotTest", "relay confirmed"))


class PublicChatPatternTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("PatternObserver")
        self.protocol = self.bot.protocol
        self.addAsyncCleanup(self.bot.close)
        await self.join()

    async def join(self, *, locale="en_US", resource_pack_index=None):
        self.incoming = asyncio.Queue()
        self.transport = SimpleNamespace(recv=self.incoming.get, send=AsyncMock(), close=AsyncMock())
        settings = self.protocol.binding.NetworkSettingsPacket(
            compression_algorithm=self.protocol.binding.PacketCompressionAlgorithm.ZLIB,
            compression_threshold=1,
        )
        packets = PacketConnection(self.transport, self.protocol, settings)
        session = JoinSession(packets, self.bot.identity, "http://127.0.0.1:19132")
        session.player_id = session.player_unique_id = 1
        session.resource_pack_index = resource_pack_index
        with patch.object(JoinSession, "connect_nethernet", return_value=session):
            await self.bot.join_nethernet(session.address, locale=locale)

    def player_chat(self, message, author="Player\u00a7r"):
        text = self.protocol.binding.packets[9]
        return text.AuthorAndMessage(type=TextPacketType.CHAT, author=author, message=message)

    async def deliver(self, *bodies):
        text = self.protocol.binding.packets[9]
        raw = [self.protocol.encode(text(
            localize=False, body=body, xuid="", platform_id="", filtered_message=None,
        )) for body in bodies]
        self.incoming.put_nowait(b"\xff" + write_batch(raw))
        await asyncio.sleep(0)

    async def arm(self, awaitable):
        task = asyncio.create_task(awaitable)

        async def cleanup():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        await asyncio.sleep(0)
        return task

    async def test_group_advances_in_wire_order_and_ordinary_chat_can_observe_same_packet(self):
        group = self.bot.add_chat_pattern("order", (r"^start (?P<number>\d+)", r"finish (red|blue)$"))
        one_shot = self.bot.add_chat_pattern("start", re.compile(r"^start (?P<number>\d+)"), repeat=False)
        first = await self.arm(self.bot.wait_for_chat_match(group, timeout=1))
        single = await self.arm(self.bot.wait_for_chat_match("start", timeout=1))
        ordinary = await self.arm(self.bot.wait_for_chat(author="Player", timeout=1))

        system = self.protocol.binding.packets[9].MessageOnly(type=TextPacketType.SYSTEM_MESSAGE, message="start 0")
        await self.deliver(system, self.player_chat("finish blue", author="OtherPlayer"))
        for waiting in (first, single, ordinary):
            self.assertFalse(waiting.done())

        start = "start 42 finish blue"
        await self.deliver(self.player_chat(start))
        self.assertEqual(await ordinary, ChatMessage("Player", start))
        observed = await single
        self.assertEqual((observed.pattern_id, observed.name), (one_shot, "start"))
        self.assertEqual(observed.matches[0].groupdict(), {"number": "42"})
        self.assertEqual(self.bot.remove_chat_pattern(one_shot), 0)
        self.assertFalse(first.done())

        await self.deliver(self.player_chat("noise"), self.player_chat("finish red"), self.player_chat("after"))
        observed = await first
        self.assertEqual([match.groups() for match in observed.matches], [("42",), ("red",)])
        self.assertEqual(observed.messages, (ChatMessage("Player", start), ChatMessage("Player", "finish red")))

        await self.deliver(self.player_chat("start 73"))
        second = await self.arm(self.bot.wait_for_chat_match("order", timeout=1))
        await self.deliver(self.player_chat("finish blue"))
        self.assertEqual([match.groups() for match in (await second).matches], [("73",), ("blue",)])
        self.assertEqual(observed.matches[0].group("number"), "42")

    async def test_registration_removal_by_id_and_name(self):
        first = self.bot.add_chat_pattern("duplicate", "same")
        second = self.bot.add_chat_pattern("duplicate", "same")
        third = self.bot.add_chat_pattern("duplicate", "same")
        self.assertEqual(len({first, second, third}), 3)
        self.assertEqual(self.bot.remove_chat_pattern(second), 1)
        with self.assertRaises(KeyError):
            await self.bot.wait_for_chat_match(second, timeout=1)

        waiting = await self.arm(self.bot.wait_for_chat_match("duplicate", timeout=1))
        await self.deliver(self.player_chat("same"))
        self.assertEqual((await waiting).pattern_id, first)
        self.assertEqual(self.bot.remove_chat_pattern("duplicate"), 2)
        with self.assertRaises(KeyError):
            await self.bot.wait_for_chat_match("duplicate", timeout=1)

    async def test_timeout_and_cancel_leave_progress_and_stream_usable(self):
        group = self.bot.add_chat_pattern("pair", ("first", "second"))
        await self.deliver(self.player_chat("first"))
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_chat_match(group, timeout=0)

        canceled = await self.arm(self.bot.wait_for_chat_match(group, timeout=1))
        other = await self.arm(self.bot.wait_for_chat_match(group, timeout=1))
        canceled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await canceled
        await self.deliver(self.player_chat("second"))
        self.assertEqual([msg.message for msg in (await other).messages], ["first", "second"])

        waiting = await self.arm(self.bot.wait_for_chat_match(group, timeout=1))
        self.incoming.put_nowait(b"")
        with self.assertRaisesRegex(ConnectionError, "packet stream lost"):
            await waiting
        with self.assertRaisesRegex(ConnectionError, "empty compressed batch"):
            await self.bot.close()

    async def test_text_wait_preserves_packet_types_and_variant_fields(self):
        text = self.protocol.binding.TextPacket
        raw_object = '{"rawtext":[{"translate":"example.key","with":["raw argument"]}]}\n'
        authored = (TextPacketType.CHAT, TextPacketType.WHISPER, TextPacketType.ANNOUNCEMENT)
        parameterized = (TextPacketType.TRANSLATE, TextPacketType.POPUP, TextPacketType.JUKEBOX_POPUP)
        objects = (TextPacketType.TEXT_OBJECT, TextPacketType.TEXT_OBJECT_WHISPER,
                   TextPacketType.TEXT_OBJECT_ANNOUNCEMENT)
        messages = (TextPacketType.RAW, TextPacketType.SYSTEM_MESSAGE, TextPacketType.TIP)
        packets = []
        expected = []
        for kind in authored:
            body = text.AuthorAndMessage(type=kind, author="Peer\u00a7r", message="\u00a7aOriginal  text")
            metadata = {"localize": False, "xuid": "123", "platform_id": "platform", "filtered_message": "filtered"}
            packets.append(text(body=body, **metadata))
            expected.append(TextEvent(kind, "\u00a7aOriginal  text", author="Peer\u00a7r", **metadata))
        for kind in parameterized:
            body = text.MessageAndParams(type=kind, message="%example.key", params=["A", "B"])
            packets.append(text(body=body, localize=True))
            expected.append(TextEvent(kind, "%example.key", localize=True, params=("A", "B")))
        for kind in objects + messages:
            message = raw_object if kind in objects else "literal"
            packets.append(text(body=text.MessageOnly(type=kind, message=message), localize=False))
            expected.append(TextEvent(kind, message, localize=False))

        waits = [await self.arm(self.bot.wait_for_text(kind=event.kind, timeout=1)) for event in expected]
        whisper = await self.arm(self.bot.wait_for_text(kind=TextPacketType.WHISPER, author="Peer", timeout=1))
        self.incoming.put_nowait(b"\xff" + write_batch(self.protocol.encode(packet) for packet in packets))
        events = await asyncio.gather(*waits)
        self.assertEqual(events, expected)
        self.assertEqual(await whisper, expected[1])
        for event in events:
            expected_object = {"rawtext": [{"translate": "example.key", "with": ["raw argument"]}]}
            self.assertEqual(event.text_object, expected_object if event.kind in objects else None)

    async def test_text_wait_is_independent_of_player_chat_and_pattern_waiters(self):
        pattern_id = self.bot.add_chat_pattern("signal", r"^(signal)$")
        pattern = await self.arm(self.bot.wait_for_chat_match(pattern_id, timeout=1))
        chat = await self.arm(self.bot.wait_for_chat(author="Player", timeout=1))
        direct = await self.arm(self.bot.wait_for_text(kind=TextPacketType.WHISPER, author="Player", timeout=1))
        authored = self.protocol.binding.TextPacket.AuthorAndMessage
        await self.deliver(authored(type=TextPacketType.WHISPER, author="Player\u00a7r", message="signal"))
        self.assertEqual((await direct).message, "signal")
        self.assertFalse(chat.done())
        self.assertFalse(pattern.done())

        general = await self.arm(self.bot.wait_for_text(timeout=1))
        await self.deliver(self.player_chat("signal"))
        self.assertEqual((await general).kind, TextPacketType.CHAT)
        self.assertEqual(await chat, ChatMessage("Player", "signal"))
        self.assertEqual((await pattern).matches[0].groups(), ("signal",))

    async def test_text_wait_times_out_and_close_ends_pending_wait(self):
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_text(timeout=0)

        waiting = await self.arm(self.bot.wait_for_text(timeout=15))
        await self.bot.close()
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(waiting, timeout=1)
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            await self.bot.wait_for_text()

    async def test_text_wait_matches_exact_wire_body_and_retains_kind_and_author_filters(self):
        kind = TextPacketType.WHISPER
        author = "Player\u00a7r"
        body = " \u00a7aSignal  1 "
        text = self.protocol.binding.TextPacket
        sent_before = self.transport.send.await_count
        waiting = await self.arm(self.bot.wait_for_text(kind=kind, author="Player", message=body, timeout=1))

        await self.deliver(
            text.AuthorAndMessage(type=kind, author=author, message=body.lower()),
            text.AuthorAndMessage(type=kind, author=author, message=body + "suffix"),
            text.AuthorAndMessage(type=kind, author=author, message=body.strip()),
            text.AuthorAndMessage(type=kind, author="Other", message=body),
            self.player_chat(body),
        )
        self.assertFalse(waiting.done())
        await self.deliver(text.AuthorAndMessage(type=kind, author=author, message=body))
        self.assertEqual(await waiting, TextEvent(kind, body, localize=False, author=author))
        self.assertEqual(self.transport.send.await_count, sent_before)

    async def test_text_wait_searches_the_wire_body_even_when_localized(self):
        text = self.protocol.binding.TextPacket
        kind = TextPacketType.TRANSLATE
        message = "%example.signal_42"
        waiting = await self.arm(self.bot.wait_for_text(message=re.compile(r"signal_\d+"), timeout=1))
        packet = text(
            body=text.MessageAndParams(type=kind, message=message, params=["argument"]), localize=True,
        )
        self.incoming.put_nowait(b"\xff" + write_batch([self.protocol.encode(packet)]))
        self.assertEqual(await waiting, TextEvent(kind, message, localize=True, params=("argument",)))

    async def test_text_wait_rejects_invalid_message_matchers(self):
        for message in (1, b"body", re.compile(b"body"), ["body"]):
            with self.subTest(message=message), self.assertRaisesRegex(TypeError, "compiled string regex"):
                await self.bot.wait_for_text(message=message, timeout=0)

    async def test_text_wait_with_message_releases_waiter_on_timeout_or_cancellation(self):
        subscribers = self.bot._chat.packets._subscribers
        before = tuple(subscribers)
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_text(message="unseen", timeout=0)
        self.assertEqual(tuple(subscribers), before)

        canceled = await self.arm(self.bot.wait_for_text(message="unseen"))
        canceled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await canceled
        self.assertEqual(tuple(subscribers), before)

        empty = await self.arm(self.bot.wait_for_text(message="", timeout=1))
        text = self.protocol.binding.TextPacket
        await self.deliver(
            text.MessageOnly(type=TextPacketType.RAW, message="other"),
            text.MessageOnly(type=TextPacketType.RAW, message=""),
        )
        self.assertEqual(await empty, TextEvent(TextPacketType.RAW, "", localize=False))

    async def test_message_wait_takes_the_first_matching_packet_in_a_batch(self):
        text = self.protocol.binding.TextPacket
        server = text.MessageOnly(type=TextPacketType.RAW, message="server pearl")
        player = self.player_chat("player pearl")
        player_match = re.compile(r"> player pearl$")

        first = await self.arm(self.bot.wait_for_message(player_match, ["server pearl"], timeout=1))
        await self.deliver(server, player)
        self.assertEqual(await first, "server pearl")

        first = await self.arm(self.bot.wait_for_message("server pearl", (player_match,), timeout=1))
        await self.deliver(player, server)
        self.assertEqual(await first, "<Player> player pearl")
        self.assertFalse(self.bot._chat._text_observers)
        self.assertFalse(self.bot._chat._content_waits)

    async def test_message_wait_skips_nonmatches_and_unrendered_text_variants(self):
        text = self.protocol.binding.TextPacket
        raw_object = '{"rawtext":[{"text":"literal"}]}'
        structured = []
        self.bot.subscribe_text(structured.append)
        waiting = await self.arm(self.bot.wait_for_message(
            "literal", "<Player> literal", raw_object, "%key", timeout=1,
        ))
        packets = (
            text(body=text.MessageOnly(type=TextPacketType.RAW, message="prefix literal"), localize=False),
            text(body=text.MessageOnly(type=TextPacketType.RAW, message="Literal"), localize=False),
            text(body=text.MessageOnly(type=TextPacketType.RAW, message="literal"), localize=True),
            text(body=self.player_chat("literal"), localize=True),
            text(body=text.MessageOnly(type=TextPacketType.TEXT_OBJECT, message=raw_object), localize=False),
            text(body=text.MessageAndParams(type=TextPacketType.TRANSLATE, message="%key", params=["v"]),
                 localize=False),
            text(body=text.AuthorAndMessage(type=TextPacketType.WHISPER, author="Player", message="literal"),
                 localize=False),
            text(body=text.AuthorAndMessage(type=TextPacketType.ANNOUNCEMENT, author="Player", message="literal"),
                 localize=False),
        )
        self.incoming.put_nowait(b"\xff" + write_batch(self.protocol.encode(packet) for packet in packets))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        self.assertEqual(len(structured), len(packets))
        self.assertTrue(structured[2].localize)
        self.assertEqual(structured[3].author, "Player\u00a7r")
        self.assertEqual(structured[4].text_object, {"rawtext": [{"text": "literal"}]})
        self.assertEqual((structured[5].message, structured[5].params), ("%key", ("v",)))
        await self.deliver(text.MessageOnly(type=TextPacketType.SYSTEM_MESSAGE, message="literal"))
        self.assertEqual(await waiting, "literal")
        self.assertEqual(len(self.bot._chat._text_observers), 1)

        waiting = await self.arm(self.bot.wait_for_message("", timeout=1))
        await self.deliver(text.MessageOnly(type=TextPacketType.TIP, message=""))
        self.assertEqual(await waiting, "")

    async def test_message_wait_rejects_invalid_matchers_before_subscribing(self):
        for matchers in ((), ([],), ((), [])):
            with self.subTest(matchers=matchers), self.assertRaisesRegex(ValueError, "at least one"):
                await self.bot.wait_for_message(*matchers)
        for matchers in ((None,), (1,), (b"",), (bytearray(b"no"),), (re.compile(b"no"),),
                         ("valid", [object()]), (["valid", ["nested"]],)):
            with self.subTest(matchers=matchers), self.assertRaisesRegex(TypeError, "compiled string regexes"):
                await self.bot.wait_for_message(*matchers)
        self.assertFalse(self.bot._chat._text_observers)
        self.assertFalse(self.bot._chat._content_waits)

    async def test_message_wait_cleans_up_on_timeout_cancel_and_close(self):
        chat = self.bot._chat
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_message("timeout", timeout=0)
        self.assertFalse(chat._text_observers)
        self.assertFalse(chat._content_waits)

        canceled = await self.arm(self.bot.wait_for_message("unseen", timeout=1))
        other = await self.arm(self.bot.wait_for_message(re.compile("remains"), timeout=1))
        canceled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await canceled
        self.assertEqual((len(chat._text_observers), len(chat._content_waits)), (1, 1))
        await self.deliver(self.player_chat("still remains here"))
        self.assertEqual(await other, "<Player> still remains here")

        closing = await self.arm(self.bot.wait_for_message("unseen"))
        await self.bot.close()
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(closing, timeout=1)
        self.assertFalse(chat._text_observers)
        self.assertFalse(chat._content_waits)
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            await self.bot.wait_for_message("unseen")

    async def test_message_wait_disconnects_before_matching_a_trailing_packet(self):
        chat = self.bot._chat
        waiting = await self.arm(self.bot.wait_for_message("<Player> late", timeout=15))
        binding = self.protocol.binding
        packet = binding.TextPacket(body=self.player_chat("late"), localize=False)
        self.incoming.put_nowait(b"\xff" + write_batch(
            self.protocol.encode(value) for value in (binding.DisconnectPacket(), packet)
        ))
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(waiting, timeout=1)
        self.assertFalse(chat.packets._reader_task.done())
        self.assertFalse(chat._text_observers)
        self.assertFalse(chat._content_waits)
        with self.assertRaises(ConnectionError):
            await self.bot.wait_for_message("<Player> late")

        await self.bot.close()
        await self.join()
        waiting = await self.arm(self.bot.wait_for_message("<Player> fresh", timeout=1))
        await self.deliver(self.player_chat("fresh"))
        self.assertEqual(await waiting, "<Player> fresh")

    async def test_continuous_callbacks_preserve_delivery_and_pattern_order_within_one_batch(self):
        pattern = self.bot.add_chat_pattern("pair", (r"^start (\d+)$", r"^end (red|blue)$"))
        events = []
        whispers = []
        completions = []
        self.bot.subscribe_text(lambda event: events.append(("text", event.message)))
        self.bot.subscribe_text(whispers.append, kind=TextPacketType.WHISPER, author="Player")
        self.bot.subscribe_chat(lambda event: events.append(("chat", event.message)), author="Player")
        self.bot.subscribe_chat_match("pair", completions.append)
        self.bot.subscribe_chat_match(pattern, lambda event: events.append(("match", event.matches[0].group(1))))
        waiting = await self.arm(self.bot.wait_for_chat_match(pattern, timeout=1))
        ordinary = await self.arm(self.bot.wait_for_chat(author="Player", timeout=1))
        private = self.protocol.binding.TextPacket.AuthorAndMessage(
            type=TextPacketType.WHISPER, author="Player\u00a7r", message="start 0",
        )
        await self.deliver(
            self.player_chat("start 1"), private, self.player_chat("end red"),
            self.player_chat("start 2"), self.player_chat("end blue"),
        )
        self.assertEqual(events, [
            ("text", "start 1"), ("chat", "start 1"), ("text", "start 0"), ("chat", "start 0"),
            ("text", "end red"), ("chat", "end red"), ("match", "1"),
            ("text", "start 2"), ("chat", "start 2"),
            ("text", "end blue"), ("chat", "end blue"), ("match", "2"),
        ])
        self.assertEqual([(event.author, event.message) for event in whispers], [("Player\u00a7r", "start 0")])
        self.assertEqual(await ordinary, ChatMessage("Player", "start 1"))
        self.assertEqual(await waiting, completions[0])
        self.assertEqual([[match.group(1) for match in event.matches] for event in completions],
                         [["1", "red"], ["2", "blue"]])
        with self.assertRaises(FrozenInstanceError):
            completions[0].messages = ()

    async def test_history_keeps_wire_order_and_repeats_without_synthesizing_hud_or_death(self):
        b = self.protocol.binding
        text = b.TextPacket
        history, authored, wire = [], [], []
        self.bot.subscribe_chat(history.append)
        self.bot.subscribe_chat(authored.append, author="Player")
        self.bot.subscribe_text(wire.append)

        def packet(kind, message, *, author=None, params=None, localize=False):
            if author is not None:
                body = text.AuthorAndMessage(type=kind, author=author, message=message)
            elif params is not None:
                body = text.MessageAndParams(type=kind, message=message, params=params)
            else:
                body = text.MessageOnly(type=kind, message=message)
            return text(body=body, localize=localize)

        public = text(body=self.player_chat("same"))
        whisper = packet(TextPacketType.WHISPER, "same", author="Player")
        server = packet(TextPacketType.ANNOUNCEMENT, "[Server] notice", author="Server")
        joined = packet(TextPacketType.TRANSLATE, "§e%multiplayer.player.joined", params=["Jo"], localize=True)
        death = packet(TextPacketType.TRANSLATE, "death.attack.lava", params=["Jo"], localize=True)
        rawtext = packet(TextPacketType.TEXT_OBJECT, '{"rawtext":[{"text":"server object"}]}')
        excluded = [packet(TextPacketType.TIP, "tip"), packet(TextPacketType.POPUP, "pop", params=[]),
                    packet(TextPacketType.JUKEBOX_POPUP, "song", params=[])]
        titles = [b.SetTitlePacket(type=kind, title_text="display only")
                  for kind in (b.SetTitlePacket.TitleType.TITLE, b.SetTitlePacket.TitleType.SUBTITLE,
                               b.SetTitlePacket.TitleType.ACTIONBAR)]
        death_info = b.DeathInfoPacket(death_cause_message=b.DeathInfoPacket.DeathCauseMessage(
            attack_name="death.attack.lava", message_list=["Jo"],
        ))
        packets = [public, *excluded, whisper, joined, death_info, rawtext, server, *titles, death, public]
        self.incoming.put_nowait(b"\xff" + write_batch(self.protocol.encode(value) for value in packets))
        await asyncio.sleep(0)

        self.assertEqual(history, [ChatMessage("Player", "same"), ChatMessage("Player", "same", "whisper"),
                                   ChatMessage(None, None, "join", "§e%multiplayer.player.joined", ("Jo",)),
                                   ChatMessage(None, "server object", "server"),
                                   ChatMessage("Server", "[Server] notice", "server"),
                                   ChatMessage(None, None, "death", "death.attack.lava", ("Jo",)),
                                   ChatMessage("Player", "same")])
        self.assertEqual(authored, [history[0], history[1], history[-1]])
        self.assertEqual([event.kind for event in wire], [value.body.type for value in packets if isinstance(value, text)])
        with self.assertRaises(FrozenInstanceError):
            history[2].params = ()

    async def test_history_uses_actual_join_locale_and_current_resource_pack(self):
        await self.bot.close()
        self.bot = Bot("PatternObserver", chat_languages={
            "en_US": b"death.attack.lava=English %s\n",
            "fr_FR": "death.attack.lava=Le joueur %s\nmultiplayer.player.left=%s a quitté la partie\n".encode(),
        })
        self.addAsyncCleanup(self.bot.close)
        self.protocol = self.bot.protocol

        async def show(overlay):
            pack = ResourcePackIndex((), {"texts/fr_FR.lang": overlay})
            await self.join(locale="fr_FR", resource_pack_index=pack)
            entries = []
            self.bot.subscribe_chat(entries.append)
            text = self.protocol.binding.TextPacket
            messages = [
                text(body=text.MessageAndParams(type=TextPacketType.TRANSLATE, message="death.attack.lava",
                                                params=["Player"]), localize=True),
                text(body=text.MessageAndParams(type=TextPacketType.TRANSLATE, message="death.attack.lava",
                                                params=[]), localize=True),
                text(body=text.MessageOnly(type=TextPacketType.TEXT_OBJECT,
                                           message='{"rawtext":[{"translate":"§e%multiplayer.player.left",'
                                                   '"with":{"rawtext":[{"text":"Player"}]}}]}')),
            ]
            self.incoming.put_nowait(b"\xff" + write_batch(self.protocol.encode(value) for value in messages))
            await asyncio.sleep(0)
            await self.bot.close()
            return entries

        old = await show(b"death.attack.lava=Overlay A %s\n")
        new = await show(b"death.attack.lava=Overlay B %s\n")
        common = [ChatMessage(None, None, "death", "death.attack.lava", ()),
                  ChatMessage(None, "§ePlayer a quitté la partie", "leave", "§e%multiplayer.player.left", ("Player",))]
        self.assertEqual(old, [ChatMessage(None, "Overlay A Player", "death", "death.attack.lava", ("Player",)), *common])
        self.assertEqual(new, [ChatMessage(None, "Overlay B Player", "death", "death.attack.lava", ("Player",)), *common])

    async def test_callback_exceptions_do_not_interrupt_other_callbacks_or_waiters(self):
        pattern = self.bot.add_chat_pattern("signal", "^signal")
        recovered = []
        reported = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: reported.append(context["exception"]))

        def bad_text(_event):
            raise RuntimeError("text callback")

        def bad_message(_event):
            raise asyncio.CancelledError("chat callback")

        def bad_match(_event):
            raise ValueError("pattern callback")

        try:
            self.bot.subscribe_text(bad_text)
            self.bot.subscribe_text(lambda event: recovered.append(("text", event.message)))
            self.bot.subscribe_chat(bad_message)
            self.bot.subscribe_chat(lambda event: recovered.append(("chat", event.message)))
            self.bot.subscribe_chat_match(pattern, bad_match)
            self.bot.subscribe_chat_match(pattern, lambda event: recovered.append(("match", event.messages[0].message)))
            waiting = await self.arm(self.bot.wait_for_chat_match(pattern, timeout=1))
            await self.deliver(self.player_chat("signal 1"), self.player_chat("signal 2"))
            self.assertEqual((await waiting).messages[0].message, "signal 1")
        finally:
            loop.set_exception_handler(previous)
        self.assertEqual(recovered, [(kind, f"signal {i}") for i in (1, 2) for kind in ("text", "chat", "match")])
        self.assertEqual([type(error) for error in reported], [RuntimeError, asyncio.CancelledError, ValueError] * 2)

    async def test_unsubscribe_is_idempotent_for_duplicate_callbacks_and_pattern_filters(self):
        values = []
        callback = values.append
        first = self.bot.subscribe_chat(callback)
        second = self.bot.subscribe_chat(callback)
        patterns = []
        with self.assertRaises(KeyError):
            self.bot.subscribe_chat_match("missing", patterns.append)
        matcher = self.bot.add_chat_pattern("one", "report", repeat=False)
        unsubscribe_match = self.bot.subscribe_chat_match(matcher, patterns.append)
        first()
        first()
        await self.deliver(self.player_chat("report"))
        self.assertEqual(values, [ChatMessage("Player", "report")])
        self.assertEqual(len(patterns), 1)
        second()
        unsubscribe_match()
        await self.deliver(self.player_chat("report"))
        self.assertEqual(len(values), 1)
        self.assertEqual(len(patterns), 1)

    async def test_disconnect_ends_subscriptions_before_trailing_text_and_rejoin_is_independent(self):
        old_text, old_chat, old_matches = [], [], []
        pattern = self.bot.add_chat_pattern("before", "trailing")
        old_unsubscribe = self.bot.subscribe_text(old_text.append)
        self.bot.subscribe_chat(old_chat.append)
        self.bot.subscribe_chat_match(pattern, old_matches.append)
        binding = self.protocol.binding
        packet = binding.TextPacket(body=self.player_chat("trailing"))
        self.incoming.put_nowait(b"\xff" + write_batch(
            self.protocol.encode(value) for value in (binding.DisconnectPacket(), packet)
        ))
        await asyncio.sleep(0)
        self.assertEqual((old_text, old_chat, old_matches), ([], [], []))
        with self.assertRaises(ConnectionError):
            self.bot.subscribe_chat(old_chat.append)
        await self.bot.close()

        await self.join()
        new = []
        self.bot.subscribe_chat(new.append)
        old_unsubscribe()
        await self.deliver(self.player_chat("new"))
        self.assertEqual(new, [ChatMessage("Player", "new")])
        self.assertEqual(old_chat, [])

    async def test_first_subscription_after_disconnect_rejects_before_reader_exits(self):
        chat_events, text_events, match_events = [], [], []
        errors = []
        reader_was_running = []
        trailing_text = asyncio.Event()
        binding = self.protocol.binding
        packets = self.bot.packets

        def inspect(raw):
            if raw.packet_id == binding.DisconnectPacket.ID:
                reader_was_running.append(not packets._reader_task.done())
                for subscribe in (
                    lambda: self.bot.subscribe_chat(chat_events.append),
                    lambda: self.bot.subscribe_text(text_events.append),
                    lambda: self.bot.subscribe_chat_match("unregistered", match_events.append),
                ):
                    try:
                        subscribe()
                    except ConnectionError as error:
                        errors.append(error)
            elif raw.packet_id == binding.TextPacket.ID:
                trailing_text.set()

        unsubscribe = packets.subscribe_raw(inspect)
        self.addCleanup(unsubscribe)
        late = binding.TextPacket(body=self.player_chat("late after disconnect"))
        self.incoming.put_nowait(b"\xff" + write_batch(
            self.protocol.encode(packet) for packet in (binding.DisconnectPacket(), late)
        ))
        await asyncio.wait_for(trailing_text.wait(), timeout=1)
        self.assertEqual(reader_was_running, [True])
        self.assertEqual((chat_events, text_events, match_events), ([], [], []))
        self.assertEqual(len(errors), 3)

    async def test_reader_failure_ends_each_subscription(self):
        seen = []
        self.bot.subscribe_chat(seen.append)
        ended = asyncio.Event()
        self.bot.packets.subscribe_reader_end(lambda _cause: ended.set())
        self.incoming.put_nowait(b"")
        await asyncio.wait_for(ended.wait(), timeout=1)
        with self.assertRaises(ConnectionError):
            self.bot.subscribe_text(seen.append)
        self.assertEqual(seen, [])
        with self.assertRaisesRegex(ConnectionError, "empty compressed batch"):
            await self.bot.close()

    async def test_whisper_serializes_one_tell_with_a_literal_recipient_and_exact_text(self):
        self.transport.send.reset_mock()
        message = ' Cafe\u0301 \U0001f469\u200d\U0001f680 user@example.com @ @a1  \\@a "@a" \'@a\' \\"@a\\" '
        await self.bot.send_whisper('Player(Two)\' "Q"\\', message)

        self.transport.send.assert_awaited_once()
        batch = self.transport.send.await_args.args[0]
        self.assertIn(batch[0], (0, 255))
        body = zlib.decompress(batch[1:], wbits=-15) if batch[0] == 0 else batch[1:]
        raw, = read_batch(body)
        binding = self.protocol.binding
        packet = self.protocol.decode(raw)
        self.assertIsInstance(packet, binding.CommandRequestPacket)
        self.assertEqual(packet.command, f'/tell "Player(Two)\' \\"Q\\"\\\\" {message}')
        self.assertEqual(packet.origin.type, binding.CommandOriginType.PLAYER)
        self.assertEqual(packet.origin.uuid, self.bot.identity.uuid)
        self.assertEqual(packet.origin.player_id, 1)
        self.assertEqual(packet.version, binding.CurrentCmdVersion.LATEST)
        self.assertFalse(packet.internal_source)

    async def test_whisper_rejects_active_selectors_and_command_breaks(self):
        self.transport.send.reset_mock()
        attempts = (
            ("@a", "literal"), ("@a[name=Peer]", "literal"), ("Peer\nOther", "literal"), ("", "literal"),
            ("Peer", ""), ("Peer", "   "), ("Peer", "literal @a"), ("Peer", "literal (@a)"),
            ("Peer", "operator@a.io"), ("Peer", "literal @a[name=Peer]"),
            ("Peer", "word @a_ suffix"), ("Peer", "word @n suffix"), ("Peer", "couldn't @a today' done"),
            ("Peer", "literal 'word @a"), ("Peer", "quote 'foo @a after' done"),
            ("Peer", 'quote "@a" @a then'),
            ("Peer", "line\nbreak"), ("Peer", "line\rbreak"), ("Peer", "nul\0byte"),
        )
        for recipient, message in attempts:
            with self.subTest(recipient=recipient, message=message), self.assertRaises(ValueError):
                await self.bot.send_whisper(recipient, message)
        self.transport.send.assert_not_awaited()
