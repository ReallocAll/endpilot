"""Observable distinctions for unknown, air, solid and dimension changes."""

import asyncio
import struct
import unittest
from types import SimpleNamespace

from endpilot.blocks import BlockObservation, Blocks, HASHED_AIR_RUNTIME_ID
from endpilot.subchunks import parse_sub_chunk


def signed_varint(value):
    number = ((value << 1) ^ (value >> 31)) & 0xFFFFFFFF
    out = bytearray()
    while number >= 128:
        out.append((number & 127) | 128)
        number >>= 7
    return bytes(out + bytes([number]))


def solid_sub_chunk(value):
    return bytes([9, 1, 6, 1]) + signed_varint(
        value - (1 << 32) if value >= (1 << 31) else value
    )


class Raw:
    def __init__(self, packet_id, payload):
        self.packet_id = packet_id
        self.payload = payload


class FakePackets:
    def __init__(self):
        names = ("ChangeDimensionPacket", "UpdateBlockPacket",
                 "UpdateSubChunkBlocksPacket", "SubChunkPacket", "LevelChunkPacket", "DimensionDataPacket",
                 "BlockActorDataPacket")
        binding = SimpleNamespace(**{name: SimpleNamespace(ID=i) for i, name in enumerate(names)})
        self.protocol = SimpleNamespace(binding=binding, decode=lambda raw: raw.payload)
        self.observer = None

    def subscribe_raw(self, observer):
        self.observer = observer
        return lambda: setattr(self, "observer", None)

    def subscribe_reader_end(self, observer):
        return lambda: None

    def emit(self, packet_id, payload):
        self.observer(Raw(packet_id, payload))


def response(center, offset, result, data=None, dimension=0):
    return SimpleNamespace(
        dimension_type=dimension,
        center_pos=SimpleNamespace(x=center[0], y=center[1], z=center[2]),
        sub_chunk_data=[SimpleNamespace(
            sub_chunk_pos_offset=SimpleNamespace(x=offset[0], y=offset[1], z=offset[2]),
            result=SimpleNamespace(name=result), serialized_sub_chunk=data,
        )],
    )


class BlockObservationTests(unittest.TestCase):
    def setUp(self):
        self.stream = FakePackets()
        self.blocks = Blocks(self.stream, None, None, hashed_runtime_ids=True)
        self.blocks.start()

    def tearDown(self):
        self.blocks.close()
        self.assertIsNone(self.stream.observer)

    def test_unknown_air_solid_and_negative_coordinate_sections(self):
        self.assertIsNone(self.blocks.observed_block((-1, 100, 0)))
        b = self.stream.protocol.binding
        self.stream.emit(b.SubChunkPacket.ID, response((-1, 0, 0), (0, 6, 0),
                                                      "SUCCESS_ALL_AIR"))
        self.assertEqual(self.blocks.observed_block((-1, 100, 0)),
                         BlockObservation((-1, 100, 0), HASHED_AIR_RUNTIME_ID, 0, True))
        self.assertIsNone(self.blocks.observed_block((-1, 116, 0)))
        self.stream.emit(b.SubChunkPacket.ID, response((0, 0, 0), (0, 6, 0),
                                                      "SUCCESS", solid_sub_chunk(33)))
        self.assertEqual(self.blocks.observed_block((2, 100, 3)),
                         BlockObservation((2, 100, 3), 33, 0, False))

    def test_update_precedence_refresh_and_dimension_clear(self):
        b = self.stream.protocol.binding
        pos = SimpleNamespace(x=2, y=100, z=3)
        self.stream.emit(b.UpdateBlockPacket.ID,
                         SimpleNamespace(pos=pos, runtime_id=20, layer=0))
        self.assertEqual(self.blocks.observed_block((2, 100, 3)).runtime_id, 20)
        self.stream.emit(b.SubChunkPacket.ID, response((0, 0, 0), (0, 6, 0),
                                                      "SUCCESS", solid_sub_chunk(33)))
        self.assertEqual(self.blocks.observed_block((2, 100, 3)).runtime_id, 33)
        self.stream.emit(b.UpdateBlockPacket.ID,
                         SimpleNamespace(pos=pos, runtime_id=44, layer=0))
        self.assertEqual(self.blocks.observed_block((2, 100, 3)).runtime_id, 44)
        self.stream.emit(b.ChangeDimensionPacket.ID, SimpleNamespace(dimension_id=1))
        self.assertIsNone(self.blocks.observed_block((2, 100, 3)))
        self.stream.emit(b.SubChunkPacket.ID, response((0, 0, 0), (0, 6, 0),
                                                      "SUCCESS", solid_sub_chunk(33), dimension=0))
        self.assertIsNone(self.blocks.observed_block((2, 100, 3)))

    def test_all_air_is_known_without_hashed_id(self):
        self.blocks.hashed_runtime_ids = False
        b = self.stream.protocol.binding
        self.stream.emit(b.SubChunkPacket.ID, response((0, 0, 0), (0, 6, 0),
                                                      "SUCCESS_ALL_AIR"))
        self.assertEqual(self.blocks.observed_block((0, 100, 0)),
                         BlockObservation((0, 100, 0), None, 0, True))


class SubChunkParserTests(unittest.TestCase):
    def test_palette_packing_and_odd_bit_width(self):
        bits = 3
        per_word = 32 // bits
        words = [0] * ((4096 + per_word - 1) // per_word)
        words[0] = 2 | (4 << bits)
        blob = (bytes([8, 1, bits * 2 + 1])
                + b"".join(struct.pack("<I", word) for word in words)
                + signed_varint(5)
                + b"".join(signed_varint(n) for n in (10, 11, 12, 13, 14)))
        section = parse_sub_chunk(blob)
        self.assertEqual(section.get_block(0, 0, 0), 12)
        self.assertEqual(section.get_block(0, 1, 0), 14)
        self.assertEqual(section.get_block(0, 2, 0), 10)
        self.assertIsNone(section.get_block(0, 0, 0, layer=1))

    def test_malformed_palette_is_not_treated_as_known_air(self):
        with self.assertRaises(ValueError):
            parse_sub_chunk(bytes([9, 1, 6, 1]))
