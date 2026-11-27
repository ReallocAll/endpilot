import asyncio
from dataclasses import FrozenInstanceError
import json
import re
import unittest
from unittest.mock import patch

from endpilot import Bot, ChatMessage, DisplayMatch, TextPacketType
from endpilot.protocol import write_batch

from test_scheduled_input import ManualClock, Wire


class DisplayTextPatternTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("DisplayObserver", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.text = b.TextPacket
        self.wire = Wire(self.bot.protocol)
        for packet in (
            b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
            b.StartGamePacket(runtime_id=7, entity_id=17),
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        ):
            self.wire.push(packet)
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    def chat(self, message):
        return self.text(localize=False, body=self.text.AuthorAndMessage(
            type=TextPacketType.CHAT, author="玩家\u00a7r", message=message,
        ))

    def message(self, kind, message, *, localize=False):
        return self.text(localize=localize, body=self.text.MessageOnly(type=kind, message=message))

    def rawtext(self, *nodes, kind=TextPacketType.TEXT_OBJECT_WHISPER):
        return self.message(kind, json.dumps({"rawtext": nodes}, ensure_ascii=False) + "\n")

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        received = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in (*packets, marker)))
        await received

    async def arm(self, awaitable):
        task = asyncio.create_task(awaitable)

        async def cleanup():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        await asyncio.sleep(0)
        return task

    async def test_mixed_ordered_patterns_share_ids_but_keep_chat_and_display_namespaces(self):
        sent = list(self.wire.sent)
        chat_id = self.bot.add_chat_pattern("shared", r"^ready$")
        pattern = (r"^<玩家> (ready)$", re.compile(r"^(  ☃  )(?P<score>37)$"))
        first = self.bot.add_display_pattern("shared", pattern, repeat=False)
        second = self.bot.add_display_pattern("shared", pattern)
        self.assertEqual((first, second), (chat_id + 1, chat_id + 2))
        self.assertEqual(self.bot.remove_chat_pattern(first), 0)
        self.assertEqual(self.bot.remove_display_pattern(chat_id), 0)
        with self.assertRaises(KeyError):
            await self.bot.wait_for_display_match(chat_id, timeout=0)
        with self.assertRaises(KeyError):
            await self.bot.wait_for_chat_match(first, timeout=0)

        display = await self.arm(self.bot.wait_for_display_match("shared", timeout=1))
        another = await self.arm(self.bot.wait_for_display_match(second, timeout=1))
        chat = await self.arm(self.bot.wait_for_chat_match("shared", timeout=1))
        content = await self.arm(self.bot.wait_for_message("<玩家> ready", timeout=1))
        raw = self.rawtext({"text": "  ☃"}, {"rawtext": [{"text": "  "}, {"text": "37"}]})
        await self.deliver(self.chat("ready"), self.rawtext({"text": "intervening"}), raw)
        event = await display
        self.assertIsInstance(event, DisplayMatch)
        self.assertEqual((event.pattern_id, event.name), (first, "shared"))
        self.assertEqual([(source.kind, source.message) for source in event.events], [
            (TextPacketType.CHAT, "ready"), (TextPacketType.TEXT_OBJECT_WHISPER, raw.body.message),
        ])
        self.assertEqual(event.events[0].author, "玩家\u00a7r")
        self.assertEqual([match.string for match in event.matches], ["<玩家> ready", "  ☃  37"])
        self.assertEqual(event.matches[1]["score"], "37")
        self.assertEqual((await another).pattern_id, second)
        self.assertEqual((await chat).messages, (ChatMessage("玩家", "ready"),))
        self.assertEqual(await content, "<玩家> ready")
        with self.assertRaises(FrozenInstanceError):
            event.name = "changed"
        self.assertEqual(self.bot.remove_display_pattern(first), 0)
        self.assertEqual(self.bot.remove_display_pattern("shared"), 1)
        self.assertEqual(self.bot.remove_chat_pattern("shared"), 1)
        self.assertEqual(self.wire.sent, sent)

    async def test_literal_object_opt_in_does_not_change_the_first_match_or_default(self):
        opt_in = await self.arm(self.bot.wait_for_message("  ☃ 37 ", "done", include_text_objects=True, timeout=1))
        default = await self.arm(self.bot.wait_for_message("  ☃ 37 ", "done", timeout=1))
        raw_event = await self.arm(self.bot.wait_for_text(kind=TextPacketType.TEXT_OBJECT, timeout=1))
        for bad_node in (
            {"translate": "unrendered", "with": [{"text": "37"}]}, {"score": {"name": "player", "objective": "count"}},
            {"selector": "@a"}, {"unknown": "hidden"}, {"text": " ", "with": ["37"]}, {"text": 37},
            {"rawtext": "invalid"},
        ):
            await self.deliver(self.rawtext({"text": "  ☃ 37 "}, bad_node))
            self.assertFalse(opt_in.done())
        malformed = ('{"rawtext":[{"text":"  ☃ 37 "}],"rawtext":[{"text":"  ☃ 37 "}]}',
                     '{"rawtext":[{"text":"  ☃ 37 "}', '{"rawtext":["  ☃ 37 "]}')
        await self.deliver(*(self.message(TextPacketType.TEXT_OBJECT_WHISPER, value) for value in malformed),
                           self.message(TextPacketType.TEXT_OBJECT_WHISPER, '{"text":"  ☃ 37 "}', localize=True))
        self.assertFalse(opt_in.done())
        literal = self.rawtext({"text": "  ☃"}, {"rawtext": [{"text": " 37 "}]}, kind=TextPacketType.TEXT_OBJECT)
        await self.deliver(literal, self.message(TextPacketType.RAW, "done"))
        self.assertEqual(await opt_in, "  ☃ 37 ")
        self.assertEqual(await default, "done")
        self.assertEqual((await raw_event).text_object, json.loads(literal.body.message))

    async def test_supported_plain_and_object_kinds_match_exact_literals(self):
        variants = (TextPacketType.RAW, TextPacketType.SYSTEM_MESSAGE, TextPacketType.TIP,
                    TextPacketType.TEXT_OBJECT, TextPacketType.TEXT_OBJECT_WHISPER, TextPacketType.TEXT_OBJECT_ANNOUNCEMENT)
        for kind in variants:
            with self.subTest(kind=kind):
                pattern = self.bot.add_display_pattern("literal", r"^  café ☃ $", repeat=False)
                wait = await self.arm(self.bot.wait_for_display_match(pattern, timeout=1))
                if kind in variants[:3]:
                    await self.deliver(self.message(kind, "  café ☃ ", localize=True))
                    self.assertFalse(wait.done())
                    await self.deliver(self.message(kind, "  café ☃ "))
                else:
                    await self.deliver(self.rawtext({"text": "  café ☃ "}, {"translate": "a.key"}, kind=kind))
                    self.assertFalse(wait.done())
                    await self.deliver(self.rawtext({"text": "  café"}, {"rawtext": [{"text": " ☃ "}]}, kind=kind))
                self.assertEqual((await wait).events[0].kind, kind)
                with self.assertRaises(KeyError):
                    await self.bot.wait_for_display_match(pattern, timeout=0)

    async def test_wait_cancellation_and_timeout_do_not_reset_patterns_and_late_batch_cannot_replace_first(self):
        pattern = self.bot.add_display_pattern("pair", ("^first$", r"^last (\d+)$"))
        await self.deliver(self.message(TextPacketType.TIP, "first"))
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_display_match(pattern, timeout=0)
        canceled = await self.arm(self.bot.wait_for_display_match(pattern, timeout=1))
        first = await self.arm(self.bot.wait_for_display_match(pattern, timeout=1))
        canceled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await canceled
        await self.deliver(self.rawtext({"text": "last 1"}), self.rawtext({"text": "first"}),
                           self.rawtext({"text": "last 2"}))
        self.assertEqual((await first).matches[1][1], "1")

        late = await self.arm(self.bot.wait_for_display_match(pattern, timeout=1))
        await self.deliver(self.rawtext({"text": "last 3"}))
        self.assertFalse(late.done())
        await self.deliver(self.rawtext({"text": "first"}), self.rawtext({"text": "last 4"}))
        self.assertEqual((await late).matches[1][1], "4")

    async def test_invalid_locale_does_not_subscribe_or_allocate_a_pattern_id(self):
        first = self.bot.add_display_pattern("first", "ready")
        for invalid in ("", "fr-FR", "fr_fr", "../fr_FR", False):
            with self.subTest(locale=invalid):
                with self.assertRaisesRegex(ValueError, "Bedrock language code"):
                    self.bot.add_display_pattern("invalid", "ready", locale=invalid)
                with self.assertRaisesRegex(ValueError, "Bedrock language code"):
                    await self.bot.wait_for_message("ready", locale=invalid, timeout=0)
        self.assertEqual(self.bot.remove_display_pattern("invalid"), 0)
        self.assertEqual(self.bot.add_display_pattern("next", "ready", locale="fr_FR"), first + 1)
        self.assertFalse(self.bot._chat._text_observers)
        self.assertFalse(self.bot._chat._content_waits)

    async def test_disconnect_prevents_a_matching_text_object_in_the_same_batch(self):
        pattern = self.bot.add_display_pattern("late", "late", locale="fr_FR")
        waiting = await self.arm(self.bot.wait_for_display_match(pattern, timeout=1))
        content = await self.arm(self.bot.wait_for_message("late", locale="fr_FR", timeout=1))
        await self.deliver(self.b.DisconnectPacket(), self.rawtext({"text": "late"}))
        for task in (waiting, content):
            with self.assertRaises(ConnectionError):
                await task
        with self.assertRaises(ConnectionError):
            self.bot.add_display_pattern("closed", "late")
        with self.assertRaises(ConnectionError):
            await self.bot.wait_for_display_match(pattern, timeout=1)
