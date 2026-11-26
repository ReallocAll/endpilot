import asyncio
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bedrock_protocol import TextPacketType
from endpilot.commands import Commands
from endpilot.protocol import Protocol


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_sends_typed_request_and_observes_bds_text_feedback(self):
        protocol = Protocol(2193)
        b = protocol.binding
        player_uuid = uuid.UUID("01234567-89ab-cdef-fedc-ba9876543210")
        stream = SimpleNamespace(protocol=protocol, send=AsyncMock(), wait_for_packet=AsyncMock())
        commands = Commands(stream, player_uuid, 57)
        text = b.TextPacket

        def incoming(kind, message):
            return protocol.encode(text(
                localize=False,
                body=text.MessageOnly(type=kind, message=message),
                xuid="", platform_id="", filtered_message=None,
            ))

        async def wait(packet_id, *, match, timeout):
            self.assertEqual(packet_id, (text.ID, b.DisconnectPacket.ID))
            self.assertEqual(timeout, 5)
            self.assertFalse(match(incoming(TextPacketType.SYSTEM_MESSAGE, "unrelated system text")))
            self.assertFalse(match(incoming(TextPacketType.RAW, "unrelated raw text")))
            while stream.send.await_count == 0:
                await asyncio.sleep(0)
            sent = stream.send.await_args.args[0]
            self.assertEqual(sent.command, "/setblock 1 99 0 gold_block")
            self.assertEqual(sent.origin.uuid, player_uuid)
            self.assertEqual(sent.origin.player_id, 57)
            self.assertEqual(sent.origin.type, b.CommandOriginType.PLAYER)
            self.assertEqual(sent.version, b.CurrentCmdVersion.LATEST)
            self.assertEqual(protocol.decode(protocol.encode(sent)).origin.request_id, sent.origin.request_id)
            reply = incoming(TextPacketType.TEXT_OBJECT, '{"rawtext":[{"translate":"commands.setblock.success"}]}')
            self.assertTrue(match(reply))
            return reply

        stream.wait_for_packet.side_effect = wait
        received = await commands.execute("/setblock 1 99 0 gold_block", timeout=5)
        self.assertIsInstance(received, b.TextPacket)
        self.assertEqual(received.body.type, TextPacketType.TEXT_OBJECT)
        self.assertIn("commands.setblock.success", received.body.message)
