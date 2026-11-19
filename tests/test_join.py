import asyncio
import base64
import json
import struct
import unittest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from endpilot.authentication import OfflineIdentity
from endpilot.compression import PacketConnection
from endpilot.protocol import Protocol, RawPacket, read_batch, write_batch


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.received = asyncio.Queue()

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        return await self.received.get()


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
        self.assertEqual(fake.sent[0][0], 0)  # Raw-deflate above the 1-byte threshold.
        await fake.received.put(fake.sent[0])
        raw = await stream.recv_raw()
        self.assertEqual(protocol.decode(raw).client_network_version, protocol.number)
        await stream.send(RawPacket(header=5, body=b"abc"))
        self.assertIsInstance(fake.sent[-1], bytes)
        # A batch can carry multiple packets without dropping the next one.
        payload = write_batch([raw, raw])
        await fake.received.put(b"\xff" + payload)
        self.assertEqual((await stream.recv_raw()).packet_id, raw.packet_id)
        self.assertEqual((await stream.recv_raw()).packet_id, raw.packet_id)
