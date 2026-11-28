"""RakNet envelope and encryption contracts against the transport's wire boundary."""

import asyncio
import socket
import threading
import unittest
from unittest.mock import patch

import raknet.asyncio
from raknet import _peer

from endpilot import Bot
from endpilot.protocol import Protocol, read_batch, write_batch
from endpilot.transport.raknet import RakNetTransport, advertised_protocol, connect


class Peer:
    def __init__(self):
        self.frames = asyncio.Queue()
        self.sent = []

    async def send(self, data, **options):
        self.sent.append((data, options))

    async def recv(self):
        return await self.frames.get()

    async def close(self):
        pass


class RakNetTests(unittest.IsolatedAsyncioTestCase):
    def assert_peer_released(self, peer, port):
        self.assertFalse(peer._pump.is_alive())
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("0.0.0.0", port))

    async def test_cancelled_native_startup_releases_ping_and_dial_peers(self):
        original_init = _peer.Peer.__init__
        original_close = raknet.asyncio.Peer.close
        loop = asyncio.get_running_loop()
        for operation in (advertised_protocol, connect):
            with self.subTest(operation=operation.__name__):
                startup_entered, constructed, close_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
                resume_startup, resume_close = threading.Event(), asyncio.Event()
                peers = []

                def delayed_init(peer, *args, **kwargs):
                    loop.call_soon_threadsafe(startup_entered.set)
                    if not resume_startup.wait(5):
                        raise TimeoutError("test did not release native startup")
                    original_init(peer, *args, **kwargs)
                    peers.append((peer, peer.local_address[1]))
                    loop.call_soon_threadsafe(constructed.set)

                async def delayed_close(peer, timeout=0.5):
                    close_entered.set()
                    await resume_close.wait()
                    await original_close(peer, timeout)

                with (
                    patch.object(_peer.Peer, "__init__", delayed_init),
                    patch.object(raknet.asyncio.Peer, "close", delayed_close),
                ):
                    running = asyncio.create_task(operation("127.0.0.1", 9))
                    try:
                        async with asyncio.timeout(8):
                            await startup_entered.wait()
                            running.cancel("cancelled during startup")
                            await asyncio.sleep(0)
                            self.assertFalse(running.done())
                            resume_startup.set()
                            await close_entered.wait()
                            running.cancel("cancelled again during cleanup")
                            await asyncio.sleep(0)
                            self.assertFalse(running.done())
                            resume_close.set()
                            with self.assertRaisesRegex(asyncio.CancelledError, "cancelled during startup"):
                                await running
                            (peer, port), = peers
                            self.assert_peer_released(peer, port)
                    finally:
                        resume_startup.set()
                        resume_close.set()
                        await asyncio.gather(running, return_exceptions=True)
                        await asyncio.wait_for(constructed.wait(), 5)
                        for peer, _ in peers:
                            await original_close(peer, 0)

    async def test_candidate_deadline_during_probe_cleanup_stops_before_dial(self):
        original_init = _peer.Peer.__init__
        loop = asyncio.get_running_loop()
        started, deadline_elapsed = asyncio.Event(), asyncio.Event()
        resume = threading.Event()
        peers = []
        bot = Bot(protocol=None)
        self.addAsyncCleanup(bot.close)

        def delayed_init(peer, *args, **kwargs):
            loop.call_soon_threadsafe(started.set)
            if not resume.wait(5):
                raise TimeoutError("test did not release native startup")
            original_init(peer, *args, **kwargs)
            peers.append((peer, peer.local_address[1]))

        with (
            patch.object(_peer.Peer, "__init__", delayed_init),
            patch("endpilot.transport.raknet.connect") as dial,
        ):
            joining = asyncio.create_task(bot.join_raknet("127.0.0.1", 9, timeout=0.3))
            try:
                async with asyncio.timeout(6):
                    await started.wait()
                    loop.call_later(0.4, deadline_elapsed.set)
                    await deadline_elapsed.wait()
                    self.assertFalse(joining.done())
                    resume.set()
                    with self.assertRaises(TimeoutError):
                        await joining
                dial.assert_not_awaited()
                self.assertEqual(bot.protocol.number, 2225)
                (peer, port), = peers
                self.assert_peer_released(peer, port)
            finally:
                resume.set()
                await asyncio.gather(joining, return_exceptions=True)
                for peer, _ in peers:
                    await peer.close(0)

    async def test_native_loopback_transfer_gives_transport_ownership_of_dial_peer(self):
        announcement = b"MCPE;Loopback;2193;1.26.52"
        async with asyncio.timeout(8), await raknet.asyncio.create_server(
            ("127.0.0.1", 0), offline_ping_response=announcement,
        ) as server:
            address = server.local_address
            self.assertEqual(await advertised_protocol(*address), 2193)
            client = await connect(*address)
            peer = client._peer
            port = peer.local_address[1]
            try:
                async with await server.accept() as incoming:
                    await client.send(b"bedrock batch")
                    self.assertEqual(await incoming.recv(), b"\xfebedrock batch")
                    self.assertTrue(peer._pump.is_alive())
            finally:
                await client.close()
            self.assert_peer_released(peer, port)

    async def test_repeated_join_cancellation_releases_unpublished_session(self):
        bot = Bot(protocol=None)
        loop = asyncio.get_running_loop()
        installed = []
        cancellations = []
        spawns = []
        ends = []
        peer_done = asyncio.Event()
        tail_read = asyncio.Event()

        async def serve(server, number):
            protocol = Protocol(number)
            binding = protocol.binding
            async with await server.accept() as connection:
                wire = RakNetTransport(connection)
                raw, = read_batch(await wire.recv())
                request = protocol.decode(raw)
                self.assertIsInstance(request, binding.RequestNetworkSettingsPacket)
                self.assertEqual(request.client_network_version, number)
                settings = binding.NetworkSettingsPacket(compression_algorithm=binding.PacketCompressionAlgorithm.NONE)
                await wire.send(write_batch([protocol.encode(settings)]))
                raw, = read_batch(await wire.recv())
                login = protocol.decode(raw)
                self.assertIsInstance(login, binding.LoginPacket)
                self.assertEqual(login.client_network_version, number)
                start = binding.StartGamePacket(runtime_id=13, entity_id=27)
                spawn = binding.PlayStatusPacket(status=binding.PlayStatus.PLAYER_SPAWN)
                tail = binding.SetHealthPacket(health=17)
                await wire.send(write_batch(protocol.encode(packet) for packet in (start, spawn, tail)))
                await peer_done.wait()

        def observe_spawn(kind):
            packets = bot.packets
            spawns.append((kind, bot.protocol, packets.protocol))
            health_id = packets.protocol.binding.SetHealthPacket.ID

            def observe_tail(raw):
                if raw.packet_id == health_id:
                    spawns.append(("tail", packets.protocol.decode(raw).health))
                    tail_read.set()

            packets.subscribe_raw(observe_tail)

        start_session = bot._start_session

        def cancel(message):
            cancellations.append(joining.cancel(message))

        def cancel_before_publish(session, **options):
            start_session(session, **options)
            with self.assertRaisesRegex(RuntimeError, "player has not joined"):
                _ = bot.packets
            packets = session.packets
            peer = packets.transport._peer
            tasks = (packets._reader_task, bot._movement._ticker, session.clock._task)
            installed.append((bot.protocol.number, packets.protocol.number, peer, peer.local_address[1],
                              tasks))
            loop.call_soon(cancel, "first cancellation")
            loop.call_soon(loop.call_soon, cancel, "second cancellation")

        async with asyncio.timeout(12), await raknet.asyncio.create_server(
            ("127.0.0.1", 0), offline_ping_response=b"MCPE;Loopback;2225;1.26.66",
        ) as server, asyncio.TaskGroup() as tasks:
            try:
                tasks.create_task(serve(server, 2225))
                await bot.join_raknet(*server.local_address)
                published = bot.protocol
                await bot.close()
                bot.subscribe_spawns(observe_spawn)
                bot.subscribe_session_end(ends.append)
                server.raw.set_offline_ping_response(b"MCPE;Loopback;2193;1.26.52")
                tasks.create_task(serve(server, 2193))
                with patch.object(bot, "_start_session", side_effect=cancel_before_publish):
                    joining = asyncio.create_task(bot.join_raknet(*server.local_address))
                    with self.assertRaises(asyncio.CancelledError) as caught:
                        await joining

                (before, selected, peer, port, background), = installed
                self.assertEqual((before, selected), (2225, 2193))
                self.assertEqual(cancellations, [True, True])
                self.assert_peer_released(peer, port)
                self.assertTrue(all(task.done() for task in background))
                self.assertEqual(caught.exception.args, ("first cancellation",))
                self.assertIs(bot.protocol, published)
                self.assertEqual(spawns, [])
                self.assertEqual(ends, [])
                with self.assertRaisesRegex(RuntimeError, "player has not joined"):
                    _ = bot.packets

                tasks.create_task(serve(server, 2193))
                await bot.join_raknet(*server.local_address)
                await tail_read.wait()
                self.assertEqual(bot.protocol.number, 2193)
                self.assertEqual(spawns, [("initial", bot.protocol, bot.protocol), ("tail", 17)])
            finally:
                peer_done.set()
                await bot.close()

    async def test_game_frame_and_post_handshake_cipher(self):
        # Simulate two endpoints with the same Bedrock session key and opposite counters.
        a, b = Peer(), Peer()
        client, server = RakNetTransport(a), RakNetTransport(b)
        await client.send(b"settings")
        self.assertEqual(a.sent[0][0], b"\xfe" + b"settings")
        await b.frames.put(a.sent[0][0])
        self.assertEqual(await server.recv(), b"settings")

        key = bytes(range(32))
        client.enable_encryption(key)
        server.enable_encryption(key)
        await client.send(b"login-finished")
        self.assertNotIn(b"login-finished", a.sent[-1][0])
        await b.frames.put(a.sent[-1][0])
        self.assertEqual(await server.recv(), b"login-finished")
        await server.send(b"spawn")
        await a.frames.put(b.sent[-1][0])
        self.assertEqual(await client.recv(), b"spawn")

    async def test_corrupted_ciphertext_is_rejected(self):
        a, b = Peer(), Peer()
        sender, receiver = RakNetTransport(a), RakNetTransport(b)
        key = bytes(range(32))
        sender.enable_encryption(key)
        receiver.enable_encryption(key)
        await sender.send(b"payload")
        modified = bytearray(a.sent[-1][0])
        modified[-1] ^= 1
        await b.frames.put(bytes(modified))
        with self.assertRaisesRegex(ConnectionError, "checksum"):
            await receiver.recv()
