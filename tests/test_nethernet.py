import asyncio
import hashlib
import unittest
from importlib.util import find_spec

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from endpilot.login import request_network_settings
from endpilot.protocol import Protocol, read_batch, write_batch


@unittest.skipUnless(find_spec("nethernet"), "install the nethernet extra")
class NetherNetTests(unittest.IsolatedAsyncioTestCase):
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
                peer, host="127.0.0.1", identity_signer=IdentitySigner(key, domain="self")
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
