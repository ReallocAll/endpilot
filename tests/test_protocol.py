import unittest
import zlib
from types import SimpleNamespace

from endpilot.compression import MAX_DECOMPRESSED_BATCH_SIZE, PacketConnection

from endpilot.protocol import Protocol, RawPacket, read_batch, write_batch


class ProtocolTests(unittest.TestCase):
    def test_network_settings_and_unmodelled_packet_in_one_batch(self):
        protocol = Protocol(2193)
        request = protocol.binding.RequestNetworkSettingsPacket()
        request.client_network_version = 2193
        raw = RawPacket(0xFFF, b"custom")
        batch = write_batch([protocol.encode(request), raw])

        # Length, varuint packet ID 193, then big-endian protocol 2193.
        self.assertEqual(batch[:7], bytes.fromhex("06 c1 01 00 00 08 91"))
        received, extension = read_batch(batch)
        self.assertEqual(protocol.decode(received).client_network_version, 2193)
        self.assertEqual(extension, raw)

    def test_partial_frame_cannot_consume_the_next_packet_header(self):
        with self.assertRaises(ValueError):
            list(read_batch(bytes.fromhex("01 80 01 00")))


class _IncomingWire:
    def __init__(self, *messages):
        self.messages = iter(messages)

    async def recv(self):
        return next(self.messages)


def _deflate(data):
    compressor = zlib.compressobj(wbits=-15)
    return compressor.compress(data) + compressor.flush()


def _connection(message, algorithm=0):
    settings = SimpleNamespace(compression_algorithm=algorithm, compression_threshold=1)
    return PacketConnection(_IncomingWire(message), None, settings)


class CompressedBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_zlib_and_none_batches_preserve_raw_packets(self):
        packet = RawPacket(0x3FF, b"valid payload")
        frame = write_batch([packet])
        for wire, algorithm in (
            (b"\x00" + _deflate(frame), 0),
            (b"\xff" + frame, 0),
            (frame, 65535),
        ):
            with self.subTest(algorithm=algorithm, prefix=wire[:1]):
                self.assertEqual(await _connection(wire, algorithm).recv_raw(), packet)

    async def test_zlib_rejects_truncated_corrupt_and_trailing_streams(self):
        frame = write_batch([RawPacket(0x37, b"valid payload")])
        compressed = _deflate(frame)
        for wire in (
            b"\x00" + compressed[:-1],
            b"\x00" + compressed + b"extra",
            b"\x00" + b"not deflate",
        ):
            with self.subTest(wire=wire[:20]):
                with self.assertRaises(ValueError):
                    await _connection(wire).recv_raw()

    async def test_zlib_rejects_expansion_beyond_batch_limit(self):
        frame = write_batch([RawPacket(1, b"x" * MAX_DECOMPRESSED_BATCH_SIZE)])
        with self.assertRaisesRegex(ValueError, "invalid compressed batch"):
            await _connection(b"\x00" + _deflate(frame)).recv_raw()
