from dataclasses import FrozenInstanceError, replace
from struct import pack, unpack
import unittest

from endpilot.entities import PaintingSnapshot
from endpilot.paintings import PaintingGeometry, painting_geometry
from endpilot.protocol import Protocol, RawPacket


# Vanilla BDS 1.26.66.1 AddPainting bodies, observed sizes, outward steps and
# inclusive wall bounds from geometry-0534b06-04 and painting-f473934-01.
CASES = (
    ("f3ffffff1f0600008cc100805e43008000c10405417a746563",
     (1, 1), (0, 0, -1), (-18, 222, -8), (-18, 222, -8)),
    ("f1ffffff1f070000c1c000805e43000000c10204506f6f6c",
     (2, 1), (-1, 0, 0), (-6, 222, -9), (-6, 222, -8)),
    ("efffffff1f080000d04000005f430000dfc0000c707261697269655f72696465",
     (1, 2), (0, 0, 1), (6, 222, -8), (6, 223, -8)),
    ("edffffff1f090040884100006043000008c106086261636b79617264",
     (3, 4), (1, 0, 0), (16, 222, -10), (16, 225, -8)),
    ("ebffffff1f0a000040c100805f430000bf40040a446f6e6b65794b6f6e67",
     (4, 3), (0, 0, -1), (-14, 222, 6), (-11, 224, 6)),
    ("e9ffffff1f0b0000904000805f43008010410007626f7571756574",
     (3, 3), (0, 0, 1), (3, 222, 8), (5, 224, 8)),
    ("e7ffffff1f0c00807f4100005f4300008040020668756d626c65",
     (2, 2), (-1, 0, 0), (16, 222, 3), (16, 223, 4)),
    ("e5ffffff1f0d000080c000005f4300801041000766696e64696e67",
     (4, 2), (0, 0, 1), (-6, 222, 8), (-3, 223, 8)),
    ("edffffff1f090000004100005f4300805f410408756e7061636b6564",
     (4, 4), (0, 0, -1), (6, 221, 14), (9, 224, 14)),
    ("ebffffff1f0a00806f4100005f430000104102085069677363656e65",
     (4, 4), (-1, 0, 0), (15, 221, 7), (15, 224, 10)),
)
NORTH = PaintingSnapshot(-4294967290, 6, (-17.5, 222.5, -8.03125), 2, "Aztec")


class PaintingGeometryTests(unittest.TestCase):
    def test_archived_packets_cover_all_sizes_and_directions(self):
        protocol = Protocol(2225)
        for body, size, outward, low, high in CASES:
            packet = protocol.decode(RawPacket(protocol.binding.AddPaintingPacket.ID, bytes.fromhex(body)))
            position = packet.pos.x, packet.pos.y, packet.pos.z
            painting = PaintingSnapshot(packet.entity_id, packet.runtime_id, position, packet.dir, packet.motif)
            with self.subTest(motif=painting.motif):
                wall = tuple(
                    (x, y, z)
                    for y in range(low[1], high[1] + 1)
                    for x in range(low[0], high[0] + 1)
                    for z in range(low[2], high[2] + 1)
                )
                dx, dy, dz = outward
                hanging = tuple((x + dx, y + dy, z + dz) for x, y, z in wall)
                self.assertEqual(painting_geometry(painting), PaintingGeometry(*size, wall, hanging))
                self.assertEqual(painting.position, position)

    def test_geometry_is_immutable(self):
        geometry = painting_geometry(NORTH)
        with self.assertRaises(FrozenInstanceError):
            geometry.width = 4

    def test_unknown_protocol_direction_and_case_sensitive_motif(self):
        self.assertIsNone(painting_geometry(NORTH, protocol=2193))
        for painting in (replace(NORTH, dir=-1), replace(NORTH, dir=4),
                         replace(NORTH, motif="aztec"), replace(NORTH, motif="minecraft:Aztec"),
                         replace(NORTH, motif="raw_1x1")):
            with self.subTest(painting=painting):
                self.assertIsNone(painting_geometry(painting))

    def test_invalid_grid_positions(self):
        for position in ((-17.0, 222.5, -8.03125), (-17.5, 222.0, -8.03125),
                         (-17.5, 222.5, -8.0), (-17.5, 222.5, -7.96875),
                         (-17.5, 222.5, float("inf")), (float("nan"), 222.5, -8.03125)):
            with self.subTest(position=position):
                self.assertIsNone(painting_geometry(replace(NORTH, position=position)))

    def test_quantized_wall_offset_still_identifies_a_boundary(self):
        boundary = -(2**20)
        observed = unpack("<f", pack("<f", boundary - 1 / 32))[0]
        self.assertEqual(observed, boundary)
        painting = replace(NORTH, position=(-17.5, 222.5, observed))
        self.assertEqual(painting_geometry(painting), PaintingGeometry(
            1, 1, ((-18, 222, boundary),), ((-18, 222, boundary - 1),),
        ))

    def test_half_cell_with_only_one_float32_preimage(self):
        limit = 2**23
        # At -limit, the in-bound half-cell is still exactly represented. The
        # out-bound half-cell rounds to -limit and is its only candidate.
        observed = unpack("<f", pack("<f", -limit - 0.5))[0]
        self.assertEqual(observed, -limit)
        painting = replace(NORTH, position=(observed, 222.5, -8.03125))
        self.assertEqual(painting_geometry(painting), PaintingGeometry(
            1, 1, ((-limit - 1, 222, -8),), ((-limit - 1, 222, -9),),
        ))

    def test_ambiguous_half_cells_and_lost_integer_precision(self):
        center = 2**23 + 2
        self.assertEqual(unpack("<ff", pack("<ff", center - 0.5, center + 0.5)), (center, center))
        for position in ((center, 222.5, -8.03125), (-17.5, -center, -8.03125)):
            with self.subTest(position=position):
                self.assertIsNone(painting_geometry(replace(NORTH, position=position)))

        center = 2**24
        self.assertEqual(unpack("<ff", pack("<ff", center, center + 1)), (center, center))
        painting = replace(NORTH, position=(center, 222.5, -8.03125), motif="Pool")
        self.assertIsNone(painting_geometry(painting))
