"""RakNet envelope and encryption contracts against the transport's wire boundary."""

import asyncio
import unittest

from endpilot.transport.raknet import RakNetTransport


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
