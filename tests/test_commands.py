import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from endpilot.commands import Commands
from endpilot.protocol import Protocol


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_roundtrip_matches_request_id_and_returns_typed_output(self):
        protocol = Protocol(2193)
        b = protocol.binding
        player_uuid = uuid.UUID("01234567-89ab-cdef-fedc-ba9876543210")
        stream = SimpleNamespace(protocol=protocol, send=AsyncMock(), wait_for_packet=AsyncMock())
        commands = Commands(stream, player_uuid, 57)

        async def wait(packet_id, *, match, timeout):
            self.assertEqual(packet_id, b.CommandOutputPacket.ID)
            self.assertEqual(timeout, 5)
            output = b.CommandOutput(
                type=b.CommandOutputType.LAST_OUTPUT,
                success_count=1,
                messages=[b.CommandOutputMessage(
                    message_id="commands.setblock.success", successful=True, params=[],
                )],
                data=None,
            )
            other = b.CommandOriginData(
                type=b.CommandOriginType.PLAYER, uuid=player_uuid,
                request_id="unrelated", player_id=57,
            )
            self.assertFalse(match(protocol.encode(b.CommandOutputPacket(
                origin_data=other, output=output,
            ))))
            # The request ID is generated per invocation; retrieve it from the sent packet.
            while stream.send.await_count == 0:
                await __import__("asyncio").sleep(0)
            sent = stream.send.await_args.args[0]
            self.assertEqual(sent.command, "/setblock 1 99 0 gold_block")
            self.assertEqual(sent.origin.uuid, player_uuid)
            self.assertEqual(sent.origin.player_id, 57)
            self.assertEqual(sent.origin.type, b.CommandOriginType.PLAYER)
            self.assertEqual(sent.version, b.CurrentCmdVersion.LATEST)
            reply = protocol.encode(b.CommandOutputPacket(origin_data=sent.origin, output=output))
            self.assertTrue(match(reply))
            return reply

        stream.wait_for_packet.side_effect = wait
        received = await commands.execute("/setblock 1 99 0 gold_block", timeout=5)
        self.assertIsInstance(received, b.CommandOutputPacket)
        self.assertEqual(received.output.success_count, 1)
        self.assertEqual(received.output.messages[0].message_id, "commands.setblock.success")
