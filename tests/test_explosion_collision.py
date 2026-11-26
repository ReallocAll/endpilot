"""Block-local collision observations for vanilla explosion exposure."""

import unittest

from endpilot.blocks import Blocks
from endpilot.protocol import Protocol

from test_blocks import PacketStream
from test_subchunks import solid_sub_chunk


# Hash and sequential IDs from the Bedrock r26_u6 palette.
AIR = (0xDBF44120, 18894)
STONE = (0x80310E21, 3317)
FLOOR_BUTTON = (0x29BE36C5, 2791)
WALL_BUTTON = (0x226A26B7, 2793)
TOP_SLAB = (0xE782783B, 21904)
FENCE = (0x195BFACD, 14806)
PISTON_ARM = (0xF816F9BD, 114)
WATER = (0x483B8B2A, 14122)
UNKNOWN = (0xFFFFFFFE, 11244)
CUBE = ((0, 0, 0, 1, 1, 1),)


class ExplosionCollisionTests(unittest.TestCase):
    position = (-3, 100, 5)

    def prepare(self, *, hashed=True, custom=False, protocol=2225):
        self.codec = Protocol(protocol)
        self.stream = PacketStream(self.codec)
        self.blocks = Blocks(self.stream, None, None, hashed_runtime_ids=hashed, custom_blocks=custom)
        self.blocks.start()
        self.addCleanup(self.blocks.close)
        self.b = self.codec.binding
        self.id_index = 0 if hashed else 1

    def deliver(self, packet):
        self.stream.emit(self.codec.encode(packet))

    def section(self, ids=None):
        b = self.b
        result = b.SubChunkPacket.SubChunkRequestResult
        self.deliver(b.SubChunkPacket(
            dimension_type=0, center_pos=b.SubChunkPos(x=-1, y=6, z=0),
            sub_chunk_data=[b.SubChunkPacket.SubChunkPacketData(
                sub_chunk_pos_offset=b.SubChunkPacket.SubChunkPosOffset(),
                result=result.SUCCESS_ALL_AIR if ids is None else result.SUCCESS,
                serialized_sub_chunk=None if ids is None else solid_sub_chunk(ids[self.id_index]),
            )],
        ))

    def update(self, ids, *, layer=0):
        x, y, z = self.position
        self.deliver(self.b.UpdateBlockPacket(
            pos=self.b.BlockPos(x=x, y=y, z=z), runtime_id=ids[self.id_index], layer=layer,
        ))

    def collision(self):
        return self.blocks._explosion_collision_at(self.position)

    def test_verified_shapes_in_both_id_modes(self):
        for hashed in (True, False):
            with self.subTest(hashed=hashed):
                self.prepare(hashed=hashed)
                self.assertIsNone(self.collision())
                self.section(STONE)
                self.assertEqual(self.collision(), CUBE)
                self.update(FLOOR_BUTTON)
                self.assertEqual(self.collision(), ())
                self.update(TOP_SLAB)
                self.assertEqual(self.collision(), ((0, 0.5, 0, 1, 1, 1),))
                self.section()
                self.assertEqual(self.collision(), ())
                self.assertEqual(self.stream.sent, [])

    def test_updates_need_both_layers_and_only_establish_dry_cells(self):
        self.prepare()
        self.update(AIR, layer=1)
        self.assertIsNone(self.collision())
        self.update(TOP_SLAB)
        self.assertEqual(self.collision(), ((0, 0.5, 0, 1, 1, 1),))
        self.update(WATER, layer=1)
        self.assertIsNone(self.collision())
        self.update(AIR)
        self.assertIsNone(self.collision())
        self.update(AIR, layer=1)
        self.assertEqual(self.collision(), ())

    def test_main_only_update_and_unresolved_extra_are_not_air(self):
        self.prepare()
        self.update(FLOOR_BUTTON)
        self.assertIsNone(self.collision())
        self.section()
        self.update(UNKNOWN, layer=1)
        self.assertIsNone(self.collision())

    def test_unsupported_states_remain_unresolved(self):
        self.prepare()
        self.section()
        for ids in (WALL_BUTTON, FENCE, PISTON_ARM, WATER, UNKNOWN, (123, 123)):
            with self.subTest(ids=ids):
                self.update(ids)
                self.assertIsNone(self.collision())

    def test_palette_requires_2225_and_verified_sequential_ids(self):
        for options in ({"hashed": False, "custom": True}, {"protocol": 2193}):
            with self.subTest(options=options):
                self.prepare(**options)
                self.section(STONE)
                self.assertIsNone(self.collision())

    def test_unload_dimension_change_and_close_invalidate_observations(self):
        self.prepare()
        self.section(STONE)
        self.assertEqual(self.collision(), CUBE)
        b = self.b
        self.deliver(b.NetworkChunkPublisherUpdatePacket(position=b.BlockPos(x=64, y=100, z=5), radius=0))
        self.assertIsNone(self.collision())
        self.deliver(b.NetworkChunkPublisherUpdatePacket(position=b.BlockPos(x=-3, y=100, z=5), radius=0))
        self.section(STONE)
        self.assertEqual(self.collision(), CUBE)
        self.deliver(b.ChangeDimensionPacket(dimension_id=1))
        self.assertIsNone(self.collision())
        self.deliver(b.ChangeDimensionPacket(dimension_id=0))
        self.section(STONE)
        self.blocks.close()
        self.assertIsNone(self.collision())
