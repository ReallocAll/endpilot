import asyncio
import unittest
from importlib.util import find_spec

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
