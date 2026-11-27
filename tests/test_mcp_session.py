import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
from importlib.util import find_spec
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from endpilot import ResourcePackDecision
from endpilot.login import LoginRefused, SessionEnd
from endpilot.mcp_server import MCPPlayer
from endpilot.protocol import Protocol, read_batch, write_batch
from test_mcp import FakeBot
from test_resource_packs import PackTransport, archive_bytes


async def _accept_raknet(peer, player_id):
    protocol = Protocol(2225)
    binding = protocol.binding
    wire = await peer.accept()

    async def receive(packet_id):
        while True:
            frame = await wire.recv()
            if frame.startswith(b"\xfe") and any(raw.packet_id == packet_id for raw in read_batch(frame[1:])):
                return

    async def send(*packets):
        await wire.send(b"\xfe" + write_batch(protocol.encode(packet) for packet in packets))

    await receive(binding.RequestNetworkSettingsPacket.ID)
    await send(binding.NetworkSettingsPacket(compression_algorithm=binding.PacketCompressionAlgorithm.NONE))
    await receive(binding.LoginPacket.ID)
    await send(binding.StartGamePacket(runtime_id=player_id, entity_id=player_id),
               binding.PlayStatusPacket(status=binding.PlayStatus.PLAYER_SPAWN))
    return wire


class MCPSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.factory = self.enterContext(patch("endpilot.mcp_server.Bot", side_effect=FakeBot))
        self.player = MCPPlayer()
        self.addAsyncCleanup(self.player.disconnect)

    async def test_server_end_releases_ownership_and_waits_for_explicit_reconnect(self):
        player = self.player
        self.assertEqual((await player.session_status())["state"], "idle")
        await player.connect("http://localhost:19132", "Original")
        old = player.connected_bot()
        await player.watch_block(1, 100, 2)
        self.addAsyncCleanup(old.close)
        watching = player._pending[((1, 100, 2), 0)]
        draining, release = asyncio.Event(), asyncio.Event()

        async def cleanup():
            self.assertIs(player.bot, old)
            self.assertFalse(old.closed)
            draining.set()
            await release.wait()

        player.add_connection_cleanup(cleanup)
        end = SessionEnd("server_disconnect", 55, "KICKED", "fixture kick", "filtered")
        old._session_end.finish(end)
        with self.assertRaisesRegex(RuntimeError, "connect a player"):
            player.connected_bot()
        tasks = []
        try:
            async with asyncio.timeout(2):
                await draining.wait()
                status = await player.session_status()
                self.assertEqual((status["state"], status["last_end"]), ("disconnecting", end))
                self.assertIsNone(status["last_join_failure"])
                self.factory.assert_called_once_with("Original")
                reconnecting = asyncio.create_task(player.reconnect())
                tasks.append(reconnecting)
                await asyncio.sleep(0)
                self.assertFalse(reconnecting.done())
                self.factory.assert_called_once_with("Original")
                release.set()
                await reconnecting
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            player._connection_cleanups.remove(cleanup)
        self.assertTrue(old.closed)
        self.assertTrue(watching.cancelled())
        self.assertIsNot(player.connected_bot(), old)
        status = await player.session_status()
        self.assertEqual((status["state"], status["last_end"]), ("connected", end))

    async def test_join_refusal_and_reader_end_are_distinct_and_keep_original_reasons(self):
        player = self.player
        old = FakeBot("Player")
        refused = FakeBot("Player")
        denial = LoginRefused("fixture detail")
        denial.reason_name = "NOT_ALLOWED"
        denial.server_message = "join refused on fixture server"
        refused.join_raknet = AsyncMock(side_effect=denial)
        self.factory.side_effect = [old, refused]
        await player.connect("raknet://localhost:19132", "Player")
        end = SessionEnd("reader_end", error_type="ConnectionResetError")
        old._session_end.finish(end)
        watcher = player._session_watch
        await asyncio.wait_for(watcher, 2)
        self.assertEqual((await player.session_status())["state"], "disconnected")
        self.assertTrue(old.closed)
        with self.assertRaisesRegex(RuntimeError, "NOT_ALLOWED: join refused on fixture server"):
            await player.reconnect()
        status = await player.session_status()
        self.assertEqual((status["state"], status["last_end"]), ("join_failed", end))
        self.assertEqual(status["last_join_failure"], {"error_type": "LoginRefused", "reason_name": "NOT_ALLOWED",
                                                       "message": "join refused on fixture server"})
        self.assertTrue(refused.closed)

    async def test_join_does_not_expose_transport_error_text_or_url_credentials(self):
        player = self.player
        bot = FakeBot("Player")
        bot.join_nethernet = AsyncMock(side_effect=OSError("fixture-private-query"))
        self.factory.side_effect = None
        self.factory.return_value = bot
        with self.assertRaisesRegex(RuntimeError, r"^Join failed \(OSError\)$") as error:
            await player.connect("https://user:fixture-private-password@localhost:8443/a?fixture-private-query")
        self.assertTrue(error.exception.__suppress_context__)
        status = await player.session_status()
        self.assertEqual(status["server"], "https://localhost:8443")
        self.assertEqual(status["last_join_failure"], {"error_type": "OSError", "reason_name": None, "message": None})
        self.assertTrue(bot.closed)

    async def test_switch_account_closes_old_bot_then_uses_new_credentials(self):
        player = self.player
        bot = FakeBot("Original")
        replacement = FakeBot("Original")
        offline = FakeBot("Offline")
        self.factory.side_effect = [bot, replacement, offline]
        bot.join_nethernet = AsyncMock()
        offline.join_nethernet = AsyncMock()
        await player.connect("http://localhost:19132", "Original", accept_resource_packs=True)
        self.assertIs(bot.join_nethernet.call_args.kwargs["on_resource_packs"](None), ResourcePackDecision.ACCEPT)
        credential = AsyncMock()
        provider = Mock(return_value=credential)

        async def join(url, *, credential_provider, on_resource_packs):
            self.assertTrue(bot.closed)
            self.assertEqual(url, "http://localhost:19132")
            self.assertIs(on_resource_packs(None), ResourcePackDecision.ACCEPT)
            await credential_provider("fixture public key")
            replacement.identity = SimpleNamespace(name="Gamertag")

        replacement.join_nethernet = join
        with patch.object(player._accounts, "credential_provider", provider):
            self.assertEqual(await player.switch_account(xuid="123"), {"name": "Gamertag", "player_id": 17})
        provider.assert_called_once_with(replacement, xuid="123")
        credential.assert_awaited_once_with("fixture public key")
        self.assertEqual((await player.session_status())["xuid"], "123")
        self.assertEqual(await player.switch_account(name="Offline"), {"name": "Offline", "player_id": 17})
        self.assertIs(offline.join_nethernet.call_args.kwargs["on_resource_packs"](None), ResourcePackDecision.ACCEPT)
        self.assertTrue(replacement.closed)
        await player.disconnect()
        status = await player.session_status()
        self.assertEqual((status["state"], status["last_end"]), ("disconnected", SessionEnd("local_close")))

    async def test_reconnect_keeps_raknet_pack_choice_and_new_target_defaults_to_refusal(self):
        player = self.player
        bots = [FakeBot("EndPilot") for _ in range(4)]
        for bot in bots:
            bot.join_raknet = AsyncMock()
        self.factory.side_effect = bots

        with self.assertRaisesRegex(TypeError, "must be a boolean"):
            await player.connect("raknet://localhost:19132", accept_resource_packs="true")
        self.factory.assert_not_called()
        await player.connect("raknet://localhost:19132", accept_resource_packs=True)
        await player.reconnect()
        await player.disconnect()
        await player.connect("raknet://localhost:19133")
        await player.reconnect()

        for bot in bots[:2]:
            args, options = bot.join_raknet.await_args
            self.assertEqual(args, ("localhost", 19132))
            self.assertIs(options["on_resource_packs"](None), ResourcePackDecision.ACCEPT)
        for bot in bots[2:]:
            bot.join_raknet.assert_awaited_once_with("localhost", 19133)

    async def test_queued_identity_requests_cannot_use_replacement_target(self):
        for same_address in (False, True):
            with self.subTest(same_address=same_address):
                await self._check_queued_identity_requests(same_address)

    async def _check_queued_identity_requests(self, same_address):
        player = MCPPlayer()
        self.addAsyncCleanup(player.disconnect)
        url = "http://localhost:19132"
        new_url = url if same_address else "http://localhost:19133"
        new_name = "Original" if same_address else "Replacement"
        original, replacement = FakeBot("Original"), FakeBot(new_name)
        replacement.join_nethernet = AsyncMock()
        self.factory.reset_mock()
        self.factory.side_effect = [original, replacement]
        await player.connect(url, "Original")
        draining, release = asyncio.Event(), asyncio.Event()

        async def cleanup():
            draining.set()
            await release.wait()

        player.add_connection_cleanup(cleanup)
        async with asyncio.timeout(3):
            closing = asyncio.create_task(player.disconnect())
            tasks = [closing]
            try:
                await draining.wait()
                pending_join = player.reconnect() if same_address else player.connect(new_url, new_name)
                joining = asyncio.create_task(pending_join)
                tasks.append(joining)
                await asyncio.sleep(0)  # The replacement join queues behind A's blocked cleanup.
                switch = asyncio.create_task(player.switch_account(xuid="123"))
                reconnect = asyncio.create_task(player.reconnect())
                tasks.extend((switch, reconnect))
                await asyncio.sleep(0)  # Both identity requests are admitted against A.
                self.assertTrue(all(not task.done() for task in tasks))
                with patch.object(player._accounts, "credential_provider") as credentials:
                    release.set()
                    self.assertEqual((await joining)["name"], new_name)
                    for stale in (switch, reconnect):
                        with self.assertRaisesRegex(RuntimeError, "connection changed while this request was waiting"):
                            await stale
                    credentials.assert_not_called()
                self.assertIs(player.connected_bot(), replacement)
                self.assertFalse(replacement.closed)
                replacement.join_nethernet.assert_awaited_once_with(new_url)
                self.assertEqual(self.factory.call_count, 2)
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_handoff_leaves_the_transferred_bot_outside_the_mcp_monitor(self):
        player = self.player
        await player.connect("http://localhost:19132")
        watch = player._session_watch
        bot = await player.handoff()
        self.addAsyncCleanup(bot.close)
        bot._session_end.finish(SessionEnd("reader_end"))
        with self.assertRaises(asyncio.CancelledError):
            await watch
        self.assertFalse(bot.closed)
        self.assertEqual((await player.session_status())["state"], "idle")

    @unittest.skipUnless(find_spec("anyio"), "optional MCP dependency unavailable")
    async def test_eof_cancels_authorization_while_bot_cleanup_is_in_progress(self):
        player = self.player
        await player.connect("http://localhost:19132")
        bot = player.connected_bot()
        account_closing, bot_closing = asyncio.Event(), asyncio.Event()

        async def close_account():
            account_closing.set()
            await asyncio.wait_for(bot_closing.wait(), 2)

        async def cleanup_bot():
            bot_closing.set()
            await asyncio.wait_for(account_closing.wait(), 2)

        player.add_connection_cleanup(cleanup_bot)
        with patch.object(player._accounts, "close", side_effect=close_account) as close:
            async with player.lifespan(None):
                self.assertFalse(account_closing.is_set())
        close.assert_awaited_once()
        self.assertTrue(bot.closed)
        self.assertEqual((await player.session_status())["last_end"], SessionEnd("local_close"))


@unittest.skipUnless(find_spec("mcp") and find_spec("raknet"), "optional MCP and RakNet dependencies unavailable")
class MCPSessionStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_close_without_disconnect_packet_reports_reader_end(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from raknet.asyncio import create_server

        source = Path(__file__).resolve().parents[1] / "src"
        params = StdioServerParameters(command=sys.executable, args=["-B", "-m", "endpilot.mcp_server"],
                                       env={"PYTHONPATH": str(source)})
        async with asyncio.timeout(15):
            async with await create_server(("127.0.0.1", 0)) as peer:
                async with stdio_client(params) as (incoming, outgoing), ClientSession(
                    incoming, outgoing, read_timeout_seconds=5,
                ) as session:
                    await session.initialize()
                    async with asyncio.TaskGroup() as tasks:
                        connection = tasks.create_task(_accept_raknet(peer, 42))
                        url = f"raknet://127.0.0.1:{peer.local_address[1]}"
                        joined = await session.call_tool("connect", {"url": url, "accept_resource_packs": True})
                        self.assertFalse(joined.is_error, joined.content)
                    await connection.result().close()
                    while True:
                        status = await session.call_tool("session_status")
                        self.assertFalse(status.is_error, status.content)
                        report = status.structured_content
                        if report["state"] == "disconnected":
                            break
                        await asyncio.sleep(0.025)
                    self.assertEqual(report["last_end"]["kind"], "reader_end")
                    self.assertIsNotNone(report["last_end"]["error_type"])
                    self.assertIsNone(report["last_join_failure"])
                    self.assertEqual(peer.connections, [])


@unittest.skipUnless(find_spec("mcp"), "optional MCP dependency unavailable")
class MCPStandaloneShutdownTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def host(self, *args):
        from mcp_types import LATEST_PROTOCOL_VERSION

        source = Path(__file__).resolve().parents[1] / "src"
        with TemporaryDirectory() as directory:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", *args, cwd=directory,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PYTHONPATH": str(source), "LOCALAPPDATA": directory, "XDG_STATE_HOME": directory},
            )
            next_id = 0

            async def send(method, params, request_id=None):
                message = {"jsonrpc": "2.0", "method": method, "params": params}
                if request_id is not None:
                    message["id"] = request_id
                process.stdin.write(json.dumps(message).encode() + b"\n")
                await process.stdin.drain()
                if request_id is not None:
                    response = await asyncio.wait_for(process.stdout.readline(), 8)
                    self.assertTrue(response.endswith(b"\n"), response)
                    frame = json.loads(response)
                    self.assertEqual(frame["id"], request_id)
                    return frame

            async def call(tool, *, error=False, request_id=None, **arguments):
                nonlocal next_id
                next_id += 1
                frame = await send("tools/call", {"name": tool, "arguments": arguments},
                                   next_id if request_id is None else request_id)
                result = frame["result"]
                self.assertIs(result.get("isError", False), error, result)
                if error:
                    return result["content"][0]["text"]
                return result.get("structuredContent") or json.loads(result["content"][0]["text"])

            try:
                await send("initialize", {"protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": {},
                                          "clientInfo": {"name": "shutdown-test", "version": "1"}}, 0)
                await send("notifications/initialized", {})
                yield process, call, send, Path(directory)
            finally:
                try:
                    await asyncio.wait_for(process.communicate(), 4)
                except TimeoutError:
                    process.kill()
                    await process.communicate()

    @unittest.skipUnless(find_spec("raknet"), "optional RakNet dependency unavailable")
    async def test_shutdown_flushes_reply_with_stdin_open_and_drains_worker_and_player(self):
        from raknet import ConnectionClosedOK
        from raknet.asyncio import create_server

        async def require_closed(wire):
            async with asyncio.timeout(5):
                with self.assertRaises(ConnectionClosedOK):
                    while True:
                        await wire.recv()

        async with asyncio.timeout(30), await create_server(("127.0.0.1", 0)) as peer:
            async with self.host("-m", "endpilot.mcp_server") as (process, call, send, _):
                self.assertIn("does not accept arguments", await call("shutdown", error=True, unexpected=False))
                malformed = await send("tools/call", {"name": "shutdown", "arguments": []}, "invalid")
                self.assertIn("error", malformed)
                self.assertEqual((await call("session_status"))["state"], "idle")
                url = f"raknet://127.0.0.1:{peer.local_address[1]}"

                async with asyncio.TaskGroup() as tasks:
                    first = tasks.create_task(_accept_raknet(peer, 71))
                    self.assertEqual((await call("connect", url=url))["player_id"], 71)
                self.assertEqual(await call("disconnect"), {"disconnected": True})
                await require_closed(first.result())
                async with asyncio.TaskGroup() as tasks:
                    second = tasks.create_task(_accept_raknet(peer, 72))
                    self.assertEqual((await call("reconnect"))["player_id"], 72)

                active = await call("run_script", source="""
import socket

async def main(bot):
    with socket.socket() as fixture:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            fixture.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        fixture.bind(('127.0.0.1', 0))
        print(fixture.getsockname()[1])
        while True:
            pass
""")
                async with asyncio.timeout(8):
                    while True:
                        progress = await call("observe_script", run_id=active["run_id"])
                        self.assertEqual(progress["state"], "running", progress)
                        output = "".join(chunk["text"] for chunk in progress["output"])
                        if output.endswith("\n"):
                            port = int(output)
                            break
                        await asyncio.sleep(0.025)
                with socket.socket() as probe, self.assertRaises(OSError):
                    probe.bind(("127.0.0.1", port))

                self.assertEqual(await call("shutdown", request_id="final-response"), {"shutdown": True})
                # The parent leaves stdin open through the response and process exit.
                self.assertEqual(await asyncio.wait_for(process.wait(), 8), 0)
                self.assertEqual(await process.stdout.read(), b"")
                await require_closed(second.result())
                self.assertEqual(peer.connections, [])
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", port))

    @unittest.skipIf(os.name == "nt" and find_spec("pywintypes") is None, "optional Windows Xbox dependency unavailable")
    async def test_shutdown_drains_pending_device_authorization(self):
        entry = """
import asyncio
import os
from pathlib import Path
from unittest.mock import patch

from endpilot.mcp_server import _run_stdio

async def authorize(self, public_key, *, add):
    assert os.read(0, 1) == b''
    os.write(1, b'fixture-direct-output\\n')
    self._on_device_code('https://example.invalid/', 'FIXTURE')
    try:
        await asyncio.Future()
    finally:
        Path('auth.drained').write_text('cancelled', encoding='utf-8')

with patch('endpilot.xbox.XboxCredentialProvider._credentials_for_account', authorize):
    asyncio.run(_run_stdio())
"""
        async with self.host("-c", entry) as (process, call, _, base):
            pending = await call("account_add")
            async with asyncio.timeout(8):
                while True:
                    status = await call("account_authorization_status", authorization_id=pending["authorization_id"])
                    self.assertEqual(status["status"], "pending", status)
                    if status.get("challenge"):
                        break
                    await asyncio.sleep(0.02)
            self.assertEqual(await call("shutdown"), {"shutdown": True})
            self.assertEqual(await asyncio.wait_for(process.wait(), 8), 0)
            self.assertIn(b"fixture-direct-output", await process.stderr.read())
            self.assertEqual((base / "auth.drained").read_text(encoding="utf-8"), "cancelled")


class MCPResourcePackTests(unittest.IsolatedAsyncioTestCase):
    async def test_required_pack_is_downloaded_and_verified_only_on_opt_in(self):
        protocol = Protocol(2225)
        binding = protocol.binding
        pack_id = UUID("7ff41856-bc81-466a-89f6-a0f005bf8e91")
        resource_name = f"{pack_id}_1.0.0"
        data = archive_bytes(pack_id, b"MCP pack fixture")
        response = binding.ResourcePackClientResponsePacket
        offer = binding.ResourcePacksInfoPacket(resource_pack_required=True, resource_packs=[
            binding.PackInfoData(pack_id_version=binding.PackIdVersion(
                id=pack_id, version=binding.SemVersion(version="1.0.0")), pack_size=len(data)),
        ])
        player = MCPPlayer()
        self.addAsyncCleanup(player.disconnect)

        for mode in ("default", "bad_hash", "verified"):
            with self.subTest(mode=mode):
                wire = PackTransport(protocol)
                wire.deliver(binding.NetworkSettingsPacket(
                    compression_algorithm=binding.PacketCompressionAlgorithm.ZLIB, compression_threshold=0,
                ), compressed_connection=False)
                wire.deliver(offer)
                with patch("endpilot.transport.nethernet.connect", return_value=wire):
                    async with asyncio.timeout(3), asyncio.TaskGroup() as tasks:
                        options = {} if mode == "default" else {"accept_resource_packs": True}

                        async def join():
                            if mode == "verified":
                                return await player.connect("http://localhost:19132", **options)
                            reason = "MISSING_CLIENT" if mode == "default" else "resource-pack SHA-256 does not match"
                            with self.assertRaisesRegex(RuntimeError, reason):
                                await player.connect("http://localhost:19132", **options)

                        joining = tasks.create_task(join())
                        decision = (await wire.outgoing.get()).response
                        if mode == "default":
                            self.assertIsInstance(decision, response.Cancel)
                            reason = binding.DisconnectFailReason.MISSING_CLIENT
                            wire.deliver(binding.DisconnectPacket(reason=reason))
                        else:
                            self.assertEqual(decision.downloading_packs, [resource_name])
                            hash_data = b"incorrect archive" if mode == "bad_hash" else data
                            wire.deliver(binding.ResourcePackDataInfoPacket(
                                resource_name=resource_name, file_size=len(data), chunk_size=len(data),
                                nb_chunks=1, pack_type=binding.PackType.RESOURCES, file_hash=sha256(hash_data).digest(),
                            ))
                            request = await wire.outgoing.get()
                            self.assertEqual((request.resource_name, request.chunk), (resource_name, 0))
                            wire.deliver(binding.ResourcePackChunkDataPacket(
                                resource_name=resource_name, chunk_id=0, byte_offset=0, data=data,
                            ))
                            if mode == "verified":
                                self.assertIsInstance((await wire.outgoing.get()).response, response.DownloadingFinished)
                                wire.deliver(binding.ResourcePackStackPacket(texture_pack_ids_and_versions=[
                                    binding.PackInstanceId(pack_id=str(pack_id), version="1.0.0"),
                                ]))
                                self.assertIsInstance((await wire.outgoing.get()).response,
                                                      response.ResourcePackStackFinished)
                                wire.deliver(binding.StartGamePacket(runtime_id=42, entity_id=42),
                                             binding.PlayStatusPacket(status=binding.PlayStatus.PLAYER_SPAWN))
                if mode == "verified":
                    self.assertEqual(joining.result()["player_id"], 42)
                    self.assertEqual(player.connected_bot().resource_pack_index.read("texts/en_US.lang"), b"MCP pack fixture")
                else:
                    self.assertTrue(wire.closed)
                    self.assertTrue(wire.outgoing.empty())
                    status = await player.session_status()
                    self.assertEqual(status["state"], "join_failed")
                    if mode == "default":
                        self.assertEqual(status["last_join_failure"]["reason_name"], "MISSING_CLIENT")
                    else:
                        self.assertEqual(status["last_join_failure"]["message"],
                                         "resource-pack SHA-256 does not match the received archive")
