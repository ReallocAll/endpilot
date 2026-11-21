"""Check the external form controller's observable request/response contract."""

import asyncio
import json
import unittest
from unittest.mock import patch

from endpilot.protocol import Protocol
from endpilot_form_extension.mcp_server import FormPlayer


class FakePackets:
    def __init__(self):
        self.incoming = asyncio.Future()
        self.sent = []
        self.watched = []

    async def wait_for_packet(self, packet_id, *, timeout):
        self.watched.append((packet_id, timeout))
        return await asyncio.wait_for(self.incoming, timeout)

    async def send(self, packet):
        self.sent.append(packet)


class FakeBot:
    def __init__(self, name):
        self.player_id = 17
        self.protocol = Protocol(2193)
        self.packets = FakePackets()
        self.chat = []
        self.closed = False

    async def join_nethernet(self, url):
        self.url = url

    async def join_raknet(self, host, port):
        self.url = (host, port)

    async def send_chat(self, text):
        self.chat.append(text)

    async def close(self):
        self.closed = True


class FormMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_watch_trigger_read_and_respond_use_public_packets(self):
        with patch("endpilot_form_extension.mcp_server.Bot", FakeBot):
            player = FormPlayer()
            await player.connect("raknet://127.0.0.1:19132")
            bot = player.bot
            await player.watch_form()
            self.assertEqual(bot.packets.watched, [(bot.protocol.binding.ModalFormRequestPacket.ID, 15)])
            await player.send_chat("endpilot-form-probe")
            self.assertEqual(bot.chat, ["endpilot-form-probe"])
            raw = bot.protocol.encode(bot.protocol.binding.ModalFormRequestPacket(
                form_id=19,
                form_json=json.dumps({
                    "type": "form", "title": "Hello",
                    "elements": [{"type": "label", "text": "Prompt"},
                                 {"type": "button", "text": "No"},
                                 {"type": "button", "text": "Yes"}],
                }),
            ))
            bot.packets.incoming.set_result(raw)
            form = await player.read_form()
            self.assertEqual((form["form_id"], form["button_count"]), (19, 2))
            with self.assertRaisesRegex(RuntimeError, "matching"):
                await player.answer_form(20, 1)
            with self.assertRaisesRegex(ValueError, "button index"):
                await player.answer_form(19, 2)
            self.assertEqual(bot.packets.sent, [])
            answer = await player.answer_form(19, 1)
            self.assertEqual(answer["submitted"], True)
            reply = bot.protocol.decode(bot.protocol.encode(bot.packets.sent[0]))
            self.assertEqual((int(reply.form_id), reply.json_response, reply.form_cancel_reason),
                             (19, "1", None))
            await player.disconnect()
            self.assertTrue(bot.closed)

    async def test_disconnect_cancels_live_watcher(self):
        with patch("endpilot_form_extension.mcp_server.Bot", FakeBot):
            player = FormPlayer()
            await player.connect("http://127.0.0.1:19132")
            await player.watch_form()
            await player.disconnect()
            self.assertTrue(player.bot is None)
            self.assertTrue(player._waiting is None)

    async def test_read_without_watch_is_rejected(self):
        with patch("endpilot_form_extension.mcp_server.Bot", FakeBot):
            player = FormPlayer()
            await player.connect("http://127.0.0.1:19132")
            with self.assertRaisesRegex(RuntimeError, "watch_form"):
                await player.read_form()
            await player.disconnect()
