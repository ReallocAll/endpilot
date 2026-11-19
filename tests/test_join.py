import asyncio
import base64
import json
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from endpilot.authentication import OfflineIdentity
from endpilot.compression import PacketConnection
from endpilot.login import JoinSession
from endpilot.protocol import Protocol, RawPacket, read_batch, write_batch


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.received = asyncio.Queue()

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        return await self.received.get()

    async def close(self):
        self.closed = True


class JoinTests(unittest.IsolatedAsyncioTestCase):
    def test_offline_identity_signature(self):
        identity = OfflineIdentity("EndPilotTest")
        auth_length = struct.unpack_from("<i", identity.login_request("server"), 0)[0]
        auth = json.loads(identity.login_request("server")[4:4 + auth_length])
        token = auth["Token"].split(".")
        header = json.loads(base64.urlsafe_b64decode(token[0] + "=="))
        payload = json.loads(base64.urlsafe_b64decode(token[1] + "=="))
        sig = base64.urlsafe_b64decode(token[2] + "==")
        key = serialization.load_der_public_key(base64.b64decode(header["x5u"]))
        key.verify(
            encode_dss_signature(int.from_bytes(sig[:48], "big"), int.from_bytes(sig[48:], "big")),
            ".".join(token[:2]).encode(), ec.ECDSA(hashes.SHA384()),
        )
        self.assertEqual(payload["xname"], "EndPilotTest")

    async def test_join_timeout_includes_signaling_connection(self):
        async def never_connect(*args, **kwargs):
            await asyncio.Event().wait()

        with patch("endpilot.transport.nethernet.connect", side_effect=never_connect):
            with self.assertRaises(TimeoutError):
                await JoinSession.connect_nethernet(
                    "http://127.0.0.1:19132", OfflineIdentity("EndPilotTest"), object(), timeout=0.01
                )

    async def test_player_spawn_finishes_loading_screen(self):
        protocol = Protocol(2193)
        binding = protocol.binding

        class SpawnPackets:
            def __init__(self):
                self.protocol = protocol
                self.sent = []

            async def recv_raw(self):
                return RawPacket(binding.PlayStatusPacket.ID, b"")

            async def send(self, packet):
                self.sent.append(packet)

        packets = SpawnPackets()
        with patch.object(protocol, "decode", return_value=SimpleNamespace(status=3)):
            session = JoinSession(packets, OfflineIdentity("EndPilotTest"), "")
            await session.join()

        self.assertTrue(session.spawned)
        self.assertIsInstance(packets.sent[0], binding.LoginPacket)
        self.assertEqual(
            [int(packet.serverbound_loading_screen_packet_type) for packet in packets.sent[1:]],
            [int(binding.ServerboundLoadingScreenPacketType.START_LOADING_SCREEN),
             int(binding.ServerboundLoadingScreenPacketType.END_LOADING_SCREEN)],
        )

    async def test_compressed_typed_raw_roundtrip(self):
        protocol = Protocol(2193)
        settings = protocol.binding.NetworkSettingsPacket()
        settings.compression_algorithm = protocol.binding.PacketCompressionAlgorithm.ZLIB
        settings.compression_threshold = 1
        fake = FakeTransport()
        stream = PacketConnection(fake, protocol, settings)
        packet = protocol.binding.RequestNetworkSettingsPacket()
        packet.client_network_version = protocol.number
        await stream.send(packet)
        self.assertIn(fake.sent[0][0], (0, 255))
        await fake.received.put(fake.sent[0])
        raw = await stream.recv_raw()
        self.assertEqual(protocol.decode(raw).client_network_version, protocol.number)
        await stream.send(RawPacket(header=5, body=b"x" * 1024))
        self.assertEqual(fake.sent[-1][0], 0)  # Repetitive payload is worth compressing.
        # A batch can carry multiple packets without dropping the next one.
        payload = write_batch([raw, raw])
        await fake.received.put(b"\xff" + payload)
        self.assertEqual((await stream.recv_raw()).packet_id, raw.packet_id)
        self.assertEqual((await stream.recv_raw()).packet_id, raw.packet_id)

    async def test_failed_observer_does_not_stop_live_packets(self):
        protocol = Protocol(2193)
        settings = protocol.binding.NetworkSettingsPacket()
        settings.compression_algorithm = protocol.binding.PacketCompressionAlgorithm.ZLIB
        settings.compression_threshold = 1
        fake = FakeTransport()
        stream = PacketConnection(fake, protocol, settings)
        observed = []

        def broken(_raw):
            raise ValueError("extension callback failed")

        stream.subscribe_raw(broken)
        stream.subscribe_raw(observed.append)
        with patch.object(asyncio.get_running_loop(), "call_exception_handler") as report:
            stream.start_receiving()
            for raw in (RawPacket(5, b"first"), RawPacket(5, b"second")):
                receiving = asyncio.create_task(stream.recv_raw())
                await asyncio.sleep(0)  # Register the live waiter before the packet arrives.
                await fake.received.put(b"\xff" + write_batch([raw]))
                self.assertEqual(await asyncio.wait_for(receiving, 1), raw)

            self.assertEqual([packet.body for packet in observed], [b"first", b"second"])
            self.assertEqual(report.call_count, 2)

        await stream.close()
        self.assertTrue(fake.closed)

    async def test_reader_failure_still_closes_transport(self):
        protocol = Protocol(2193)
        settings = protocol.binding.NetworkSettingsPacket()
        settings.compression_algorithm = protocol.binding.PacketCompressionAlgorithm.ZLIB
        settings.compression_threshold = 1
        fake = FakeTransport()
        stream = PacketConnection(fake, protocol, settings)
        stream.start_receiving()
        await fake.received.put(b"\x01invalid-compression-header")
        await asyncio.sleep(0)

        with self.assertRaises(ValueError):
            await stream.close()
        self.assertTrue(fake.closed)
