"""Check the external form controller's observable request/response contract."""

import asyncio
import json
import os
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryFile
from unittest.mock import patch

import endpilot
from endpilot.login import SessionEnd, _SessionEndState
from endpilot.mcp_server import MCPPlayer
from endpilot.protocol import Protocol, read_batch, write_batch
from endpilot_form_extension.mcp_server import FormPlayer, register_tools


ACTION = {"type": "form", "title": "Hello", "buttons": [{"text": "No"}, {"text": "Yes"}]}
CHAT_MESSAGE = "Could I see the menu?"


class FakePackets:
    def __init__(self):
        self.incoming = asyncio.Future()
        self.watched = []

    async def wait_for_packet(self, packet_id, *, timeout):
        self.watched.append((packet_id, timeout))
        return await asyncio.wait_for(self.incoming, timeout)


class FakeBot:
    def __init__(self, name):
        self.player_id = 17
        self._session_end = _SessionEndState()
        self.protocol = Protocol(2193)
        self.packets = FakePackets()
        self.chat = []
        self.answers = []
        self.closed = False

    async def join_nethernet(self, url):
        self.url = url

    async def join_raknet(self, host, port):
        self.url = (host, port)

    async def send_chat(self, text):
        self.chat.append(text)

    def get_pending_form(self, request):
        if request is not self.packets.incoming.result():
            return None
        packet = self.protocol.decode(request)
        return {"form_id": packet.form_id, "form": json.loads(packet.form_json)}

    async def answer_form(self, selection, request):
        self.answers.append((selection, request))

    async def close(self):
        self.closed = True
        self._session_end.finish(SessionEnd("local_close"))

    @property
    def session_end(self):
        return self._session_end.result

    async def wait_for_session_end(self, timeout=15):
        return await self._session_end.wait(timeout)


class FormMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_watch_trigger_read_and_respond_use_public_bot_apis(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = FormPlayer()
            await player.connect("raknet://127.0.0.1:19132")
            bot = player.bot
            await player.watch_form()
            self.assertEqual(bot.packets.watched, [(bot.protocol.binding.ModalFormRequestPacket.ID, 15)])
            await player.send_chat(CHAT_MESSAGE)
            self.assertEqual(bot.chat, [CHAT_MESSAGE])
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
            self.assertEqual(bot.answers, [])
            answer = await player.answer_form(19, 1)
            self.assertEqual(answer["submitted"], True)
            choice, request = bot.answers[0]
            self.assertEqual(choice, 1)
            self.assertIs(request, raw)
            await player.disconnect()
            self.assertTrue(bot.closed)

    async def test_disconnect_cancels_live_watcher(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = FormPlayer()
            await player.connect("http://127.0.0.1:19132")
            await player.watch_form()
            await player.disconnect()
            self.assertIsNone(player.bot)
            self.assertIsNone(player._waiting)

    async def test_read_without_watch_is_rejected(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = FormPlayer()
            await player.connect("http://127.0.0.1:19132")
            with self.assertRaisesRegex(RuntimeError, "watch_form"):
                await player.read_form()
            await player.disconnect()

    async def test_disconnect_drains_in_flight_answer_before_closing(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = FormPlayer()
            await player.connect("http://127.0.0.1:19132")
            bot = player.bot
            self.addAsyncCleanup(player.disconnect)
            started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def send_answer(_button_index, _request):
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
                    await release.wait()

            bot.answer_form = send_answer
            await player.watch_form()
            bot.packets.incoming.set_result(bot.protocol.encode(bot.protocol.binding.ModalFormRequestPacket(
                form_id=19, form_json=json.dumps(ACTION),
            )))
            await player.read_form()
            answer = asyncio.create_task(player.answer_form(19, 1))
            await asyncio.wait_for(started.wait(), 1)
            disconnect = asyncio.create_task(player.disconnect())
            try:
                await asyncio.wait_for(cancelled.wait(), 1)
                self.assertFalse(disconnect.done())
                self.assertFalse(bot.closed)
                release.set()
                self.assertEqual(await disconnect, {"disconnected": True})
                with self.assertRaisesRegex(RuntimeError, "connect a player"):
                    await answer
                self.assertTrue(bot.closed)
            finally:
                release.set()
                await asyncio.gather(answer, disconnect, return_exceptions=True)

    async def test_handoff_drains_a_late_form_wait_before_reconnect(self):
        tools = {}

        class ToolRegistry:
            def tool(self):
                def register(tool):
                    tools[tool.__name__] = tool
                return register

        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            register_tools(ToolRegistry(), player)
            await player.connect("http://127.0.0.1:19132")
            bot = player.connected_bot()
            self.addAsyncCleanup(bot.close)
            self.addAsyncCleanup(player.disconnect)
            draining, release = asyncio.Event(), asyncio.Event()
            request = bot.protocol.encode(bot.protocol.binding.ModalFormRequestPacket(
                form_id=19, form_json=json.dumps(ACTION),
            ))

            async def late_form(_packet_id, *, timeout):
                self.assertEqual(timeout, 15)
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    draining.set()
                    await release.wait()
                    bot.packets.incoming.set_result(request)
                    return request

            bot.packets.wait_for_packet = late_form
            await tools["watch_form"]()
            reading = asyncio.create_task(tools["read_form"]())
            await asyncio.sleep(0)
            handoff = asyncio.create_task(player.handoff())
            tasks = [reading, handoff]
            try:
                await asyncio.wait_for(draining.wait(), 1)
                reconnect = asyncio.create_task(player.connect("http://127.0.0.1:19132", "Replacement"))
                tasks.append(reconnect)
                await asyncio.sleep(0)
                self.assertFalse(handoff.done())
                self.assertFalse(reconnect.done())
                with self.assertRaisesRegex(RuntimeError, "connect a player"):
                    await tools["watch_form"]()
                release.set()
                self.assertIs(await handoff, bot)
                self.assertFalse(bot.closed)
                self.assertEqual((await reconnect)["name"], "Replacement")
                with self.assertRaisesRegex(RuntimeError, "connect a player|connection changed"):
                    await reading
                with self.assertRaisesRegex(RuntimeError, "matching"):
                    await tools["answer_form"](19, 1)
                self.assertEqual(bot.answers, [])
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)


@unittest.skipUnless(find_spec("mcp") and find_spec("raknet"), "optional MCP and RakNet dependencies unavailable")
class FormMCPStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_join_serves_builtin_and_form_tools_until_disconnect(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from raknet import ConnectionClosedOK
        from raknet.asyncio import create_server

        protocol = Protocol(2225)
        binding = protocol.binding
        source = Path(endpilot.__file__).resolve().parents[1]
        extension_source = Path(__file__).resolve().parents[1] / "src"
        script = """
import sys
from endpilot.mcp_server import create_server
from endpilot_form_extension.mcp_server import register_tools

def register(server, player):
    async def fail_cleanup():
        if player.bot.player_id == 43:
            raise OSError("stdio extension cleanup failed")

    async def finish_cleanup():
        if player.bot.player_id == 43:
            print("second stdio cleanup finished", file=sys.stderr)

    player.add_connection_cleanup(fail_cleanup)
    player.add_connection_cleanup(finish_cleanup)
    register_tools(server, player)

create_server(register).run(transport="stdio")
"""
        params = StdioServerParameters(
            command=sys.executable,
            args=["-B", "-c", script],
            env={"PYTHONPATH": os.pathsep.join(map(str, (source, extension_source)))},
        )

        async def receive(wire, packet_id):
            while True:
                frame = await wire.recv()
                if frame.startswith(b"\xfe"):
                    for raw in read_batch(frame[1:]):
                        if raw.packet_id == packet_id:
                            return protocol.decode(raw)

        async def send(wire, *packets):
            await wire.send(b"\xfe" + write_batch(protocol.encode(packet) for packet in packets))

        async def accept_player(peer, player_id):
            wire = await peer.accept()
            settings = await receive(wire, binding.RequestNetworkSettingsPacket.ID)
            self.assertEqual(settings.client_network_version, protocol.number)
            await send(wire, binding.NetworkSettingsPacket(compression_algorithm=binding.PacketCompressionAlgorithm.NONE))
            await receive(wire, binding.LoginPacket.ID)
            await send(wire, binding.StartGamePacket(runtime_id=player_id, entity_id=player_id),
                       binding.PlayStatusPacket(status=binding.PlayStatus.PLAYER_SPAWN))
            return wire

        async def assert_closed(wire):
            with self.assertRaises(ConnectionClosedOK):
                while True:
                    await wire.recv()

        async with asyncio.timeout(20):
            async with await create_server(("127.0.0.1", 0)) as peer:
                url = f"raknet://127.0.0.1:{peer.local_address[1]}"
                with TemporaryFile(mode="w+", encoding="utf-8") as stderr:
                    async with stdio_client(params, errlog=stderr) as (incoming, outgoing), ClientSession(
                        incoming, outgoing, read_timeout_seconds=5,
                    ) as session:
                        await session.initialize()
                        names = [tool.name for tool in (await session.list_tools()).tools]
                        self.assertEqual(names.count("connect"), 1)
                        self.assertEqual(names.count("disconnect"), 1)
                        expected = {"observed_block", "watch_form", "send_chat", "read_form", "answer_form"}
                        self.assertTrue(expected.issubset(names), names)

                        async def call(tool, **arguments):
                            result = await session.call_tool(tool, arguments)
                            self.assertFalse(result.is_error, result)
                            return result.structured_content or json.loads(result.content[0].text)

                        async def connect(player_id):
                            async with asyncio.TaskGroup() as tasks:
                                connection = tasks.create_task(accept_player(peer, player_id))
                                joined = await call("connect", url=url, name="SharedForm")
                                self.assertEqual(joined["player_id"], player_id)
                            return connection.result()

                        wire = await connect(42)
                        self.assertFalse((await call("observed_block", x=0, y=0, z=0))["observed"])
                        await call("watch_form")
                        await call("send_chat", message=CHAT_MESSAGE)
                        chat = await receive(wire, binding.TextPacket.ID)
                        self.assertEqual(chat.body.message, CHAT_MESSAGE)
                        request = binding.ModalFormRequestPacket(form_id=19, form_json=json.dumps(ACTION))
                        await send(wire, request)
                        form = await call("read_form")
                        self.assertEqual((form["form_id"], form["button_count"]), (19, 2))
                        self.assertTrue((await call("answer_form", form_id=19, button_index=1))["submitted"])
                        response = await receive(wire, binding.ModalFormResponsePacket.ID)
                        self.assertEqual((response.form_id, response.json_response), (19, "1"))
                        self.assertEqual(peer.connections, [wire])

                        await call("watch_form")
                        await send(wire, request)
                        await call("read_form")
                        self.assertTrue((await call("disconnect"))["disconnected"])
                        await assert_closed(wire)

                        wire = await connect(43)
                        stale = await session.call_tool("answer_form", {"form_id": 19, "button_index": 1})
                        self.assertTrue(stale.is_error)
                        await call("watch_form", timeout=60)
                    stderr.seek(0)
                    output = stderr.read()
                    self.assertIn("second stdio cleanup finished", output)
                    self.assertIn("OSError: stdio extension cleanup failed", output)
                await assert_closed(wire)
