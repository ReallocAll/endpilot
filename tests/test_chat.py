import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bedrock_protocol import TextPacketType
from endpilot.chat import Chat, ChatMessage
from endpilot.protocol import Protocol


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_typed_chat_send_and_filtered_server_observation(self):
        protocol = Protocol(2193)
        stream = SimpleNamespace(
            protocol=protocol, send=AsyncMock(), wait_for_packet=AsyncMock()
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
