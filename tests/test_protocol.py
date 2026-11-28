import unittest
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bedrock_protocol
from bedrock_protocol import v898, v2168, v2193, v2225
from cramjam import snappy

from endpilot import Bot
from endpilot.compression import PacketConnection
from endpilot.protocol import Protocol, RawPacket, read_batch, write_batch


class ProtocolTests(unittest.TestCase):
    def test_bot_protocol_reports_features_before_join(self):
        for number, expected in ((1001, False), (2193, False), (2225, True)):
            with self.subTest(protocol=number):
                protocol = Bot(protocol=number).protocol
                self.assertIs(protocol.supports_feature("preview_anvil"), expected)
                self.assertIs(protocol.supports_feature("xbox_login"), expected)
                with self.assertRaises(KeyError) as unknown:
                    protocol.supports_feature("unknown")
                self.assertEqual(unknown.exception.args, ("unknown",))

    def test_network_settings_and_unmodelled_packet_in_one_batch(self):
        for protocol, number, wire_version in (
            (Protocol(2193), 2193, "08 91"),
            (Protocol(), 2225, "08 b1"),
        ):
            with self.subTest(protocol=number):
                request = protocol.binding.RequestNetworkSettingsPacket()
                request.client_network_version = protocol.number
                raw = RawPacket(0xFFF, b"custom")
                batch = write_batch([protocol.encode(request), raw])

                # Length, varuint packet ID 193, then the big-endian protocol number.
                self.assertEqual(batch[:7], bytes.fromhex(f"06 c1 01 00 00 {wire_version}"))
                received, extension = read_batch(batch)
                self.assertEqual(protocol.decode(received).client_network_version, number)
                self.assertEqual(extension, raw)

    def test_text_packet_uses_registered_class_and_version_codec(self):
        for native in (v2193, v2225):
            with self.subTest(protocol=native.VERSION):
                protocol = Protocol(native.VERSION)
                binding = protocol.binding
                self.assertIs(binding.packets, native.packets)
                self.assertIs(binding.encode, native.encode)
                self.assertIs(binding.decode, native.decode)
                self.assertIs(binding.TextPacket, v898.TextPacket)
                raw = protocol.encode(binding.TextPacket())
                self.assertIs(type(protocol.decode(raw)), v898.TextPacket)

    def test_packet_from_later_protocol_is_absent(self):
        self.assertIsNone(getattr(Protocol().binding, "ChatPacket", None))

    def test_start_game_accepts_versioned_level_settings(self):
        for protocol, settings_type in (
            (Protocol(2193), v2168.LevelSettings),
            (Protocol(), bedrock_protocol.LevelSettings),
        ):
            with self.subTest(protocol=protocol.number):
                binding = protocol.binding
                self.assertIs(binding.LevelSettings, settings_type)
                start = binding.StartGamePacket(settings=binding.LevelSettings(commands_enabled=True))
                self.assertTrue(start.settings.commands_enabled)

    def test_start_game_round_trips_server_configuration_join_info(self):
        for number in (2193, 2225):
            with self.subTest(protocol=number):
                protocol = Protocol(number)
                binding = protocol.binding
                start = binding.StartGamePacket(server_configuration_join_info=binding.ServerConfigurationJoinInfo())
                decoded = protocol.decode(protocol.encode(start))
                self.assertIs(type(decoded.server_configuration_join_info), v2168.ServerConfigurationJoinInfo)

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
    async def test_negotiated_compression_preserves_raw_packets(self):
        packet = RawPacket(0x3FF, b"valid payload")
        frame = write_batch([packet])
        for wire, algorithm in (
            (b"\x00" + _deflate(frame), 0),
            (b"\xff" + frame, 0),
            (b"\x01\x10\x3c" + frame, 1),  # Raw Snappy: 16-byte output, one literal.
            (b"\xff" + frame, 1),
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

    async def test_snappy_sends_raw_blocks_only_when_smaller(self):
        connection = _connection(b"", 1)
        send = connection.transport.send = AsyncMock()
        for body, header in ((b"x" * 512, 1), (b"x", 255)):
            packet = RawPacket(1, body)
            await connection.send(packet)
            frame, = send.await_args.args
            self.assertEqual(frame[0], header)
            payload = bytes(snappy.decompress_raw(frame[1:])) if header == 1 else frame[1:]
            self.assertEqual(payload, write_batch([packet]))

    async def test_invalid_or_mismatched_snappy_frame_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid compressed batch"):
            await _connection(b"\x01\x10\x3c\x0f", 1).recv_raw()
        with self.assertRaisesRegex(ValueError, "unexpected compression header"):
            await _connection(b"\x01\x02\x04\x01\x01", 0).recv_raw()

    async def test_compressed_batches_reject_expansion_beyond_batch_limit(self):
        frame = write_batch([RawPacket(1, b"x" * 512)])
        for algorithm, wire in ((0, _deflate(frame)), (1, snappy.compress_raw(frame))):
            with patch("endpilot.compression.MAX_DECOMPRESSED_BATCH_SIZE", len(frame) - 1):
                with self.assertRaisesRegex(ValueError, "invalid compressed batch"):
                    await _connection(bytes([algorithm]) + wire, algorithm).recv_raw()
