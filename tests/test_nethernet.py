import asyncio
import hashlib
import json
import unittest
from contextlib import asynccontextmanager, nullcontext
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from endpilot import Bot, TerrainStorage
from endpilot.login import _prepare_block_palette, request_network_settings
from endpilot.protocol import Protocol, read_batch, write_batch


@unittest.skipUnless(find_spec("nethernet"), "install the nethernet extra")
class NetherNetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from aiortc import RTCIceGatherer

        # NetherNet falls back to aiortc's default STUN server for an empty list.
        self.enterContext(patch.object(RTCIceGatherer, "getDefaultIceServers", return_value=[]))

    @asynccontextmanager
    async def _connected_peer(self):
        from nethernet import serve_http

        from endpilot.transport.nethernet import connect

        accepted = asyncio.Queue(maxsize=1)
        finished = asyncio.Event()

        async def peer(connection):
            accepted.put_nowait(connection)
            await finished.wait()

        with patch("aioice.ice.get_host_addresses", return_value=["127.0.0.1"]):
            async with asyncio.timeout(30), serve_http(peer, host="127.0.0.1") as server:
                async with await connect(f"http://127.0.0.1:{server.bound_port}") as client:
                    try:
                        yield client, await accepted.get()
                    finally:
                        finished.set()

    async def test_reliable_channel_end_drains_received_messages(self):
        from nethernet import ConnectionClosed, SessionError, SessionState

        async with self._connected_peer() as (client, peer):
            read = asyncio.create_task(client.recv())
            await asyncio.sleep(0)
            read.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await read

            messages = (b"first complete message", b"second complete message")
            queued = asyncio.Event()

            # Wait until the native receive handlers have enqueued both complete messages.
            @client._session._pc._reliable.on("message")
            def on_message(_fragment):
                if client._recv_queue.qsize() == len(messages):
                    queued.set()

            for message in messages:
                await peer.send(message)
            await queued.wait()

            peer._session._pc._reliable.close()
            async with asyncio.timeout(10):
                self.assertIs(await client.wait_closed(), SessionError.DATA_CHANNEL_CLOSED)
                self.assertIs(peer.state, SessionState.CONNECTED)
                for message in messages:
                    self.assertEqual(await client.recv(), message)
                with self.assertRaises(ConnectionClosed) as closed:
                    await client.recv()
                self.assertEqual(closed.exception.error, 32)

                await client.close(SessionError.GENERIC_FAILURE)
                with self.assertRaises(ConnectionClosed) as closed_again:
                    await client.recv()
                self.assertIs(closed_again.exception.error, SessionError.DATA_CHANNEL_CLOSED)

    async def test_reliable_channel_end_releases_pending_inventory_request(self):
        from nethernet import ConnectionClosed, SessionError

        from endpilot.compression import PacketConnection
        from endpilot.inventory import Inventory

        protocol = Protocol(2225)
        binding = protocol.binding
        settings = binding.NetworkSettingsPacket(compression_algorithm=65535, compression_threshold=0)
        async with self._connected_peer() as (client, peer):
            packets = PacketConnection(client, protocol, settings)
            peer_packets = PacketConnection(peer, protocol, settings)
            inventory = Inventory(packets, 7)
            reader = packets.start_receiving()
            try:
                await peer_packets.send(binding.InventorySlotPacket(
                    inventory_id=binding.ContainerID.INVENTORY, slot=9,
                    item=binding.SerializedNetworkItemStackDescriptor(id=389, stack_size=2, net_id_variant=77),
                ))
                self.assertEqual((await inventory.wait_for_item(9)).stack_size, 2)
                move = asyncio.create_task(inventory.move_item(9, 18, timeout=60))
                try:
                    packet = await peer_packets.recv()
                    self.assertIsInstance(packet, binding.ItemStackRequestPacket)
                    request, = packet.requests
                    self.assertEqual(set(inventory._pending_requests), {int(request.client_request_id.raw_id)})
                    self.assertFalse(move.done())

                    peer._session._pc._reliable.close()
                    async with asyncio.timeout(10):
                        with self.assertRaisesRegex(ConnectionError, "connection ended before an item-stack verdict") as lost:
                            await asyncio.shield(move)
                        with self.assertRaises(ConnectionClosed) as closed:
                            await asyncio.shield(reader)
                    self.assertIs(lost.exception.__cause__, closed.exception)
                    self.assertIs(closed.exception.error, SessionError.DATA_CHANNEL_CLOSED)
                    self.assertFalse(inventory._pending_requests)
                    self.assertEqual(inventory.slots[9].stack_size, 2)
                    self.assertNotIn(18, inventory.slots)
                finally:
                    move.cancel()
                    await asyncio.gather(move, return_exceptions=True)
            finally:
                inventory.close()
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

    async def test_reads_protocol_from_http_advertisement(self):
        from aiohttp import web

        from endpilot.transport.nethernet import advertised_protocol

        async def advertisement(_request):
            return web.json_response({"protocol": 2193})

        app = web.Application()
        app.router.add_get("/v1/join", advertisement)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = runner.addresses[0][1]
        self.assertEqual(await advertised_protocol(f"http://127.0.0.1:{port}"), 2193)

    async def test_opening_exchange(self):
        from nethernet import serve_http

        from endpilot.transport.nethernet import connect

        protocol = Protocol(2193)

        async def peer(connection):
            packet, = read_batch(await connection.recv())
            request = protocol.decode(packet)
            self.assertEqual(request.client_network_version, 2193)
            response = protocol.binding.NetworkSettingsPacket()
            response.compression_threshold = 256
            await connection.send(write_batch([protocol.encode(response)]))
            await connection.recv()  # Keep the connection open until the client closes.

        async with asyncio.timeout(30), serve_http(peer, host="127.0.0.1") as server:
            client = await connect(f"http://127.0.0.1:{server.bound_port}")
            try:
                settings = await request_network_settings(client, protocol)
                self.assertEqual(settings.compression_threshold, 256)
            finally:
                await client.close()

    async def test_join_reads_protocol_from_the_existing_signaling_get(self):
        from aiohttp import ClientResponse, web
        from nethernet import serve_http
        from nethernet.signaling.http import HttpSignalingServer

        directory = self.enterContext(TemporaryDirectory())
        storage = TerrainStorage.sqlite(Path(directory) / "terrain.sqlite3", world_key="protocol-fixture")
        version_text = " 1.26.52-fixture+wire "
        for route, configured, announcement, expected in (
            ("nethernet", None, 2193, 2193),
            ("bare", None, "2193", 2193),
            ("url", None, None, 2225),
            ("nethernet", 2225, 2193, 2225),
        ):
            with self.subTest(route=route, configured=configured, announcement=announcement):
                protocol = Protocol(expected)
                binding = protocol.binding
                bot = Bot("ProtocolProbe", protocol=configured)
                initial = bot.protocol
                requests = []
                spawns = []
                opening = []
                initialized = asyncio.Event()
                bot.subscribe_spawns(lambda kind: spawns.append((kind, bot.protocol, bot.reported_server_version)))

                async def announce(_server, request):
                    requests.append(request.method)
                    return web.Response(status=204) if announcement is None else web.json_response(
                        {"protocol": announcement},
                    )

                async def peer(connection):
                    async def receive():
                        raw, = read_batch(await connection.recv())
                        return protocol.decode(raw)

                    async def send(*packets):
                        await connection.send(write_batch(protocol.encode(packet) for packet in packets))

                    opening.append(await receive())
                    await send(binding.NetworkSettingsPacket(compression_algorithm=binding.PacketCompressionAlgorithm.NONE))
                    opening.append(await receive())
                    await send(
                        binding.StartGamePacket(runtime_id=13, entity_id=27, server_version=version_text),
                        binding.PlayStatusPacket(status=binding.PlayStatus.PLAYER_SPAWN),
                    )
                    while not isinstance(packet := await receive(), binding.SetLocalPlayerAsInitializedPacket):
                        pass
                    opening.append(packet)
                    initialized.set()
                    async for _ in connection:
                        pass

                with (
                    patch.object(HttpSignalingServer, "_handle_capability", announce),
                    patch("aioice.ice.get_host_addresses", return_value=["127.0.0.1"]),
                    patch("endpilot.login._prepare_block_palette", wraps=_prepare_block_palette) as prepare,
                    patch.object(ClientResponse, "json", side_effect=AssertionError(
                        "fixed protocol must not read discovery metadata",
                    )) if configured is not None else nullcontext(),
                ):
                    async with asyncio.timeout(12), serve_http(peer, host="127.0.0.1") as server:
                        address = f"127.0.0.1:{server.bound_port}"
                        try:
                            if route == "nethernet":
                                await bot.join_nethernet(f"http://{address}", terrain_storage=storage)
                            else:
                                await bot.join_address(
                                    address if route == "bare" else f"http://{address}", terrain_storage=storage,
                                )
                            await initialized.wait()
                            request, login, player_initialized = opening
                            self.assertEqual(requests, ["GET"])
                            self.assertIsInstance(request, binding.RequestNetworkSettingsPacket)
                            self.assertIsInstance(login, binding.LoginPacket)
                            self.assertEqual((request.client_network_version, login.client_network_version),
                                             (expected, expected))
                            auth_length = int.from_bytes(login.connection_request[:4], "little")
                            auth = json.loads(login.connection_request[4:4 + auth_length])
                            self.assertEqual(auth["AuthenticationType"], 2)
                            self.assertEqual(player_initialized.player_id, 13)
                            self.assertEqual(bot.protocol.number, expected)
                            self.assertIs(bot.protocol, bot.packets.protocol)
                            self.assertEqual(bot.protocol.supports_feature("xbox_login"), expected == 2225)
                            self.assertEqual(bot.protocol.supports_feature("preview_anvil"), expected == 2225)
                            self.assertEqual(storage._writer.protocol, expected)
                            self.assertEqual(spawns, [("initial", bot.protocol, version_text)])
                            self.assertEqual(prepare.await_count, int(configured is None and expected == 2225))
                            if configured is not None:
                                self.assertIs(bot.protocol, initial)
                            active = bot.protocol
                        finally:
                            await bot.close()
                        self.assertIs(bot.protocol, active)
                        self.assertIsNone(bot.reported_server_version)

    async def test_optional_server_identity_pinning(self):
        from nethernet import IdentitySigner, InvalidIdentity, serve_http

        from endpilot.transport.nethernet import connect

        key = ec.generate_private_key(ec.SECP384R1())
        pin = hashlib.sha256(key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )).hexdigest()

        async def peer(connection):
            async for _ in connection:
                pass

        async with asyncio.timeout(30):
            async with serve_http(
                peer, host="127.0.0.1", identity_signer=IdentitySigner(key)
            ) as server:
                url = f"http://127.0.0.1:{server.bound_port}"
                connection = await connect(url, server_key_sha256=pin)
                await connection.close()
                with self.assertRaises(InvalidIdentity):
                    await connect(url, server_key_sha256="0" * 64)

            async with serve_http(peer, host="127.0.0.1") as server:
                url = f"http://127.0.0.1:{server.bound_port}"
                connection = await connect(url)  # No pin: no identity assertion required.
                await connection.close()
                with self.assertRaises(InvalidIdentity):
                    await connect(url, server_key_sha256=pin)
