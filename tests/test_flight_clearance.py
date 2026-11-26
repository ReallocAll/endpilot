"""Swept creative-flight clearance with native block observations."""

import unittest

from endpilot.blocks import Blocks
from endpilot.protocol import Protocol

from test_blocks import PacketStream


# Hash and sequential IDs from the protocol 2225 Bedrock palette.
AIR = (0xDBF44120, 18894)
STONE = (0x80310E21, 3317)
OAK_FENCE = (0x195BFACD, 14806)
OAK_FENCE_EAST_NORTH = (0x3E8C7661, 14809)
COBBLESTONE_WALL = (0xB17EB040, 5277)
CLOSED_FENCE_GATE = (0x905BA240, 303)
OPEN_FENCE_GATE = (0xCF42558B, 307)
BORDER_BLOCK = (0x361DD750, 12763)
PITCHER_LOWER = (0xF330127F, 1653)
PITCHER_UPPER = (0xB4495F34, 1654)
BOTTOM_SLAB = (0xA3BE7E64, 10165)
PISTON_ARM = (0xF816F9BD, 114)
WATER = (0x483B8B2A, 14122)
UNKNOWN = (0xFFFFFFFE, 11244)


class FlightClearanceTests(unittest.TestCase):
    def prepare(self, *, hashed=True, section=7):
        self.codec = Protocol(2225)
        self.stream = PacketStream(self.codec)
        self.blocks = Blocks(self.stream, None, None, hashed_runtime_ids=hashed)
        self.blocks.start()
        self.addCleanup(self.blocks.close)
        self.id_index = 0 if hashed else 1
        self.air_section(section)

    def air_section(self, section):
        b = self.codec.binding
        self.stream.emit(self.codec.encode(b.SubChunkPacket(
            dimension_type=0, center_pos=b.SubChunkPos(x=0, y=section, z=0),
            sub_chunk_data=[b.SubChunkPacket.SubChunkPacketData(
                sub_chunk_pos_offset=b.SubChunkPacket.SubChunkPosOffset(),
                result=b.SubChunkPacket.SubChunkRequestResult.SUCCESS_ALL_AIR,
            )],
        )))

    def install(self, position, ids, *, layer=0):
        x, y, z = position
        b = self.codec.binding
        self.stream.emit(self.codec.encode(b.UpdateBlockPacket(
            pos=b.BlockPos(x=x, y=y, z=z), runtime_id=ids[self.id_index], layer=layer,
        )))

    def segment(self, x, y, z1=4, z2=7):
        return self.blocks._flight_clearance((x, y, z1), (x, y, z2))

    def test_fence_overhang_blocks_at_121_25_and_contact_at_the_top_is_clear(self):
        for hashed in (True, False):
            with self.subTest(hashed=hashed):
                self.prepare(hashed=hashed)
                self.install((3, 120, 5), OAK_FENCE)
                self.assertIs(self.segment(3.5, 121.25, 4, 4), True)
                self.assertIs(self.segment(3.5, 121.25, 7, 7), True)
                self.assertIs(self.segment(3.5, 121.25), False)
                self.assertIs(self.segment(3.5, 121.25, 7, 4), False)
                self.assertIs(self.segment(3.5, 121.5), True)
                self.assertIs(self.segment(3.5, 121.6), True)
                self.assertIs(self.segment(3.05, 121.25), True)
                self.assertIs(self.segment(3.10, 121.25), False)

    def test_connected_fence_checks_each_arm_and_preserves_the_corner_gap(self):
        self.prepare()
        self.install((3, 120, 5), OAK_FENCE_EAST_NORTH)
        self.assertIs(self.blocks._flight_clearance((2.4, 121.25, 5), (4.5, 121.25, 5)), False)
        self.assertIs(self.segment(3.99, 121.25, 5.05, 5.05), True)
        self.assertIs(self.segment(3.99, 121.25, 5.50, 5.50), False)

    def test_wall_gate_and_border_use_their_published_collision_bounds(self):
        self.prepare()
        # A gate's select box persists after opening, but its collision does not.
        for name, ids, expected in (
            ("wall", COBBLESTONE_WALL, False), ("closed gate", CLOSED_FENCE_GATE, False),
            ("open gate", OPEN_FENCE_GATE, True), ("border", BORDER_BLOCK, False),
        ):
            with self.subTest(block=name):
                self.install((3, 120, 5), ids)
                self.assertIs(self.segment(3.5, 121.25), expected)
                self.assertIs(self.segment(3.5, 121.6), True)

    def test_pitcher_crop_overhang_can_hit_the_head_from_the_cell_above(self):
        self.prepare()
        self.install((3, 124, 5), PITCHER_LOWER)
        self.assertIs(self.segment(3.5, 122.13), True)
        self.assertIs(self.segment(3.5, 122.15), False)
        self.install((3, 124, 5), PITCHER_UPPER)
        self.assertIs(self.segment(3.5, 122.3), True)

    def test_floor_contact_and_slab_clearance_are_distinct_from_overlap(self):
        self.prepare()
        self.install((3, 120, 5), STONE)
        self.assertIs(self.segment(3.5, 121), True)
        self.assertIs(self.segment(3.5, 120.99999), True)
        self.assertIs(self.segment(3.5, 120.99), False)
        self.install((3, 120, 5), BOTTOM_SLAB)
        self.assertIs(self.segment(3.5, 120.5), True)
        self.assertIs(self.segment(3.5, 120.4), False)

    def test_swept_head_and_side_include_only_simultaneous_overlap(self):
        self.prepare()
        self.install((4, 123, 5), STONE)
        self.assertIs(self.segment(3.7, 121.25), True)
        self.assertIs(self.segment(3.75, 121.19), True)
        self.assertIs(self.segment(3.75, 121.25), False)
        # The body's x and z ranges visit the voxel at disjoint times.
        self.assertIs(self.blocks._flight_clearance((3, 121.25, 5.3), (4.3, 121.25, 4)), True)

    def test_unknown_cells_in_the_protrusion_range_remain_unknown(self):
        self.prepare(section=8)
        self.assertIsNone(self.segment(3.5, 128.25))
        self.assertIs(self.segment(3.5, 128.5), True)
        self.air_section(7)
        self.assertIs(self.segment(3.5, 128.25), True)
        self.install((3, 127, 5), UNKNOWN, layer=1)
        self.assertIsNone(self.segment(3.5, 128.25))
        self.install((3, 127, 5), OAK_FENCE, layer=1)
        self.assertIs(self.segment(3.5, 128.25), False)

    def test_unobserved_cell_above_the_head_can_contain_a_downward_protrusion(self):
        self.prepare()
        self.assertIs(self.segment(3.5, 126.13), True)
        self.assertIsNone(self.segment(3.5, 126.15))
        self.air_section(8)
        self.assertIs(self.segment(3.5, 126.15), True)
        self.install((3, 128, 5), PITCHER_LOWER)
        self.assertIs(self.segment(3.5, 126.15), False)

    def test_liquid_and_unknown_extra_layer_are_conservative(self):
        self.prepare()
        for ids in (WATER, UNKNOWN):
            with self.subTest(ids=ids):
                self.install((3, 121, 5), ids, layer=1)
                self.assertIsNone(self.segment(3.5, 121.25))
        self.install((3, 121, 5), STONE)
        self.assertIs(self.segment(3.5, 121.25), False)

    def test_dynamic_piston_arm_below_the_feet_remains_unknown(self):
        self.prepare()
        self.install((3, 120, 5), PISTON_ARM)
        self.assertIsNone(self.segment(3.5, 121.25))
