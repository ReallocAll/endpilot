import asyncio
import base64
from hashlib import sha256
import json
import struct
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from endpilot import Bot, LoginRefused
from endpilot.authentication import _sign
from endpilot.compression import PacketConnection
from endpilot.login import request_network_settings
from endpilot.protocol import Protocol, read_batch, write_batch
from endpilot.qa import PacketTimeline
from endpilot.transport.raknet import RakNetTransport
from test_scheduled_input import Wire


class SentPacketTests(unittest.IsolatedAsyncioTestCase):
    async def test_typed_success_raw_and_handshake_keep_numeric_subscriptions(self):
        protocol = Protocol(2225)
        b = protocol.binding
        wire = Wire(protocol)
        timeline = PacketTimeline(max_kept=0)
        settings = b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE)
        packets = PacketConnection(wire, protocol, settings, timeline=timeline)
        self.addAsyncCleanup(packets.close)
        observed = []
        unsubscribe = packets.subscribe_sent(observed.append)
        typed = b.InteractPacket(action=b.InteractPacket.Action.OPEN_INVENTORY, target_id=7, pos=None)
        raw = protocol.encode(typed)

        async def fail():
            raise OSError("send failed")

        entered = asyncio.Event()

        async def block():
            entered.set()
            await asyncio.Future()

        wire.before_send = fail
        with self.assertRaisesRegex(OSError, "send failed"):
            await packets.send(typed)
        wire.before_send = block
        pending = asyncio.create_task(packets.send(typed))
        await asyncio.wait_for(entered.wait(), timeout=1)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(timeline.sent_count("InteractPacket"), 0)
        self.assertEqual(observed, [])

        wire.before_send = None
        await packets.send(typed)
        await packets.send(raw)
        handshake = b.ClientToServerHandshakePacket()
        await packets.send(handshake)
        await packets.send(protocol.encode(handshake))
        self.assertEqual(timeline.sent_count("InteractPacket"), 1)
        self.assertEqual(timeline.sent_count("ClientToServerHandshakePacket"), 1)
        self.assertEqual(timeline.sent_count("interactpacket"), 0)
        self.assertEqual(timeline.sent_count(f"#{raw.packet_id}"), 0)
        self.assertEqual(timeline.next_index, 0)
        self.assertEqual(observed, [raw.packet_id, raw.packet_id] + [handshake.ID] * 2)
        unsubscribe()
        await packets.send(typed)
        self.assertEqual(timeline.sent_count("InteractPacket"), 2)
        self.assertEqual(len(observed), 4)

    async def test_first_request_counts_only_after_transport_success(self):
        protocol = Protocol(2225)
        b = protocol.binding
        wire = Wire(protocol)
        timeline = PacketTimeline()
        self.addAsyncCleanup(wire.close)

        async def fail():
            raise OSError("settings send failed")

        entered = asyncio.Event()

        async def block():
            entered.set()
            await asyncio.Future()

        wire.before_send = fail
        with self.assertRaisesRegex(OSError, "settings send failed"):
            await request_network_settings(wire, protocol, timeline=timeline)
        wire.before_send = block
        pending = asyncio.create_task(request_network_settings(wire, protocol, timeline=timeline))
        await asyncio.wait_for(entered.wait(), timeout=1)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(timeline.sent_count("RequestNetworkSettingsPacket"), 0)

        wire.before_send = None
        settings = b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE)
        wire.push(settings)
        await request_network_settings(wire, protocol, timeline=timeline)
        self.assertEqual(timeline.sent_count("RequestNetworkSettingsPacket"), 1)
        self.assertEqual(timeline.sent_count("NetworkSettingsPacket"), 0)
        self.assertEqual(timeline.received_count("NetworkSettingsPacket"), 1)

    async def test_timeline_counts_survive_close_and_a_new_timeline_starts_empty(self):
        protocol = Protocol(2225)
        b = protocol.binding
        bot = Bot("SentCount", protocol=2225)
        self.addAsyncCleanup(bot.close)

        def ready_wire():
            wire = Wire(protocol)
            wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
            wire.push(b.StartGamePacket(runtime_id=7, entity_id=11))
            wire.push(b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
            return wire

        async def join(wire, timeline):
            with patch("endpilot.transport.nethernet.connect", return_value=wire):
                await bot.join_nethernet("http://127.0.0.1:19132", timeline=timeline)

        def counts(timeline):
            return tuple(timeline.sent_count(name) for name in (
                "RequestNetworkSettingsPacket", "LoginPacket", "TextPacket", "ClientToServerHandshakePacket",
            ))

        timeline = PacketTimeline()
        self.assertEqual(counts(timeline), (0, 0, 0, 0))
        first = ready_wire()
        await join(first, timeline)
        self.assertEqual(counts(timeline), (1, 1, 0, 0))
        await bot.send_chat("first attempt")
        self.assertEqual(counts(timeline), (1, 1, 1, 0))
        await bot.close()
        self.assertTrue(first.closed)
        self.assertEqual(counts(timeline), (1, 1, 1, 0))

        rejected = Wire(protocol)
        rejected.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        rejected.push(b.DisconnectPacket())
        rejected_timeline = PacketTimeline()
        with self.assertRaises(LoginRefused):
            await join(rejected, rejected_timeline)
        self.assertTrue(rejected.closed)
        self.assertEqual(counts(rejected_timeline), (1, 1, 0, 0))
        await bot.close()
        self.assertEqual(counts(rejected_timeline), (1, 1, 0, 0))

        fresh_timeline = PacketTimeline()
        await join(ready_wire(), fresh_timeline)
        self.assertEqual(counts(fresh_timeline), (1, 1, 0, 0))
        self.assertEqual(counts(timeline), (1, 1, 1, 0))

    async def test_timeline_counts_the_encrypted_bedrock_handshake(self):
        protocol = Protocol(2225)
        b = protocol.binding
        bot = Bot("EncryptedCount", protocol=2225)
        self.addAsyncCleanup(bot.close)
        timeline = PacketTimeline()
        to_client, to_server = asyncio.Queue(), asyncio.Queue()

        class LocalPeer:
            def __init__(self, incoming, outgoing):
                self.incoming, self.outgoing = incoming, outgoing

            async def recv(self):
                return await self.incoming.get()

            async def send(self, data, **_options):
                self.outgoing.put_nowait(data)

            async def close(self):
                pass

        client = LocalPeer(to_client, to_server)
        server = RakNetTransport(LocalPeer(to_server, to_client))
        server_key = ec.generate_private_key(ec.SECP384R1())
        salt = b"local sent-count handshake"

        async def send(packet):
            await server.send(write_batch([protocol.encode(packet)]))

        async def receive():
            raw, = read_batch(await server.recv())
            return protocol.decode(raw)

        async def handshake():
            request = await receive()
            self.assertIsInstance(request, b.RequestNetworkSettingsPacket)
            await send(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
            login = await receive()
            self.assertIsInstance(login, b.LoginPacket)
            length, = struct.unpack_from("<i", login.connection_request)
            auth = json.loads(login.connection_request[4:4 + length])
            self.assertEqual(auth["AuthenticationType"], 2)
            payload = auth["Token"].split(".")[1]
            identity = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            client_key = serialization.load_der_public_key(base64.b64decode(identity["cpk"]))
            token = _sign({"salt": base64.b64encode(salt).decode()}, server_key)
            await send(b.ServerToClientHandshakePacket(token=token))
            server.enable_encryption(sha256(salt + server_key.exchange(ec.ECDH(), client_key)).digest())
            self.assertIsInstance(await receive(), b.ClientToServerHandshakePacket)
            await send(b.StartGamePacket(runtime_id=7, entity_id=11))
            await send(b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))

        with patch("raknet.asyncio.create_connection", return_value=client) as connect:
            async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
                tasks.create_task(handshake())
                await bot.join_raknet("127.0.0.1", 19132, timeline=timeline)
            connect.assert_awaited_once_with(("127.0.0.1", 19132))
        self.assertEqual(timeline.sent_count("RequestNetworkSettingsPacket"), 1)
        self.assertEqual(timeline.sent_count("LoginPacket"), 1)
        self.assertEqual(timeline.sent_count("ClientToServerHandshakePacket"), 1)
        self.assertEqual(timeline.received_count("ServerToClientHandshakePacket"), 1)
        self.assertEqual(timeline.sent_count("ServerToClientHandshakePacket"), 0)
