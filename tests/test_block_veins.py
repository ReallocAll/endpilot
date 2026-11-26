from itertools import islice, product
import unittest
from unittest.mock import patch

from endpilot import BlockMatch, Bot
from endpilot import blocks as block_observation
from endpilot.blocks import Blocks, HASHED_AIR_RUNTIME_ID, HASHED_UNKNOWN_RUNTIME_ID
from test_blocks import PacketStream
from test_subchunks import solid_sub_chunk


class BlockVeinTests(unittest.TestCase):
    OAK = 825916963
    OAK_OTHER_AXIS = 4065577688
    STONE = 0x80310E21

    def setUp(self):
        self.bot = Bot("BlockVein", protocol=2225)
        self.stream = PacketStream(self.bot.protocol)
        self.b = self.bot.protocol.binding
        self.blocks = Blocks(self.stream, None, None, hashed_runtime_ids=True)
        self.bot._blocks = self.blocks
        self.blocks.start()

    def tearDown(self):
        self.blocks.close()
        self.assertEqual(self.stream.sent, [])

    def deliver(self, packet):
        self.stream.emit(self.bot.protocol.encode(packet))

    def update(self, position, runtime_id, *, layer=0):
        x, y, z = position
        self.deliver(self.b.UpdateBlockPacket(
            pos=self.b.BlockPos(x=x, y=y, z=z), runtime_id=runtime_id, layer=layer,
        ))

    def section(self, x, y, z, runtime_id=None, *, dimension=0):
        b = self.b
        results = b.SubChunkPacket.SubChunkRequestResult
        self.deliver(b.SubChunkPacket(
            dimension_type=dimension, center_pos=b.SubChunkPos(x=x, y=y, z=z),
            sub_chunk_data=[b.SubChunkPacket.SubChunkPacketData(
                sub_chunk_pos_offset=b.SubChunkPacket.SubChunkPosOffset(x=0, y=0, z=0),
                result=results.SUCCESS_ALL_AIR if runtime_id is None else results.SUCCESS,
                serialized_sub_chunk=None if runtime_id is None else solid_sub_chunk(runtime_id),
            )],
        ))

    def test_connects_faces_edges_corners_and_states_across_negative_sections(self):
        for sx in (-2, -1):
            for sy in (13, 14):
                for sz in (-2, -1):
                    self.section(sx, sy, sz)
        seed, face = (-16, 224, -16), (-17, 224, -16)
        edge, corner = (-16, 225, -17), (-15, 223, -15)
        for position in ((-19, 224, -14), corner, edge, seed):
            self.update(position, self.OAK)
        self.update(face, self.OAK_OTHER_AXIS)
        self.update((-16, 224, -15), self.OAK, layer=1)

        result = self.bot.find_block_vein(seed)
        expected = ((seed, self.OAK, 0), (face, self.OAK_OTHER_AXIS, 1), (edge, self.OAK, 2), (corner, self.OAK, 3))
        self.assertEqual(result.matches, tuple(BlockMatch(p, "minecraft:oak_log", rid, d) for p, rid, d in expected))
        self.assertEqual((result.center, result.dimension_id, result.complete), (seed, 0, True))
        self.assertEqual(self.bot.find_block_vein(seed, max_blocks=4), result)
        limited = self.bot.find_block_vein(seed, max_blocks=2)
        self.assertEqual((limited.matches, limited.complete), (result.matches[:2], False))

    def test_clips_each_step_by_inclusive_manhattan_distance_from_seed(self):
        self.section(0, 6, 0)
        seed, edge = (7, 101, 7), (8, 102, 7)
        later, corner = (9, 102, 7), (8, 102, 8)
        for position in (later, corner, edge, seed):
            self.update(position, self.OAK)
        short = self.bot.find_block_vein(seed, max_distance=2)
        self.assertEqual(tuple(match.position for match in short.matches), (seed, edge))
        self.assertTrue(short.complete)
        extended = self.bot.find_block_vein(seed, max_distance=3)
        self.assertEqual(tuple(match.position for match in extended.matches), (seed, edge, corner, later))
        self.assertTrue(extended.complete)

    def test_flood_radius_and_zero_limits(self):
        self.section(0, 6, 0)
        seed, across_air = (6, 101, 6), (8, 101, 6)
        self.update(seed, self.OAK)
        self.update(across_air, self.OAK)
        for limits in ({}, {"max_distance": 0}, {"flood_radius": 0}):
            with self.subTest(limits=limits):
                result = self.bot.find_block_vein(seed, **limits)
                self.assertEqual(tuple(match.position for match in result.matches), (seed,))
                self.assertTrue(result.complete)
        wider = self.bot.find_block_vein(seed, flood_radius=2)
        self.assertEqual(tuple(match.position for match in wider.matches), (seed, across_air))
        self.assertTrue(wider.complete)
        empty = self.bot.find_block_vein(seed, max_blocks=0)
        self.assertEqual((empty.matches, empty.complete), ((), False))
        air = self.bot.find_block_vein((7, 101, 6), max_blocks=0)
        self.assertEqual((air.matches, air.complete), ((), True))

    def test_only_a_reachable_unknown_frontier_invalidates_completeness(self):
        seed, bridge, disconnected = (5, 101, 5), (6, 101, 5), (7, 101, 5)
        self.update(seed, self.OAK)
        sparse = self.bot.find_block_vein(seed)
        self.assertEqual(tuple(match.position for match in sparse.matches), (seed,))
        self.assertFalse(sparse.complete)
        self.section(0, 6, 0)
        self.update(seed, self.OAK)
        self.update(disconnected, self.OAK)
        self.update(bridge, 0x12345678)
        self.update((10, 101, 5), HASHED_UNKNOWN_RUNTIME_ID)
        unresolved = self.bot.find_block_vein(seed)
        self.assertEqual(tuple(match.position for match in unresolved.matches), (seed,))
        self.assertFalse(unresolved.complete)

        self.update(bridge, self.STONE)
        blocked = self.bot.find_block_vein(seed)
        self.assertEqual(tuple(match.position for match in blocked.matches), (seed,))
        self.assertTrue(blocked.complete)
        self.update(bridge, self.OAK)
        joined = self.bot.find_block_vein(seed)
        self.assertEqual(tuple(match.position for match in joined.matches), (seed, bridge, disconnected))
        self.assertTrue(joined.complete)

    def test_unknown_air_and_dimension_boundaries(self):
        seed = (8, -64, 8)
        for runtime in (None, HASHED_UNKNOWN_RUNTIME_ID, 0x12345678):
            if runtime is not None:
                self.update(seed, runtime)
            result = self.bot.find_block_vein(seed)
            self.assertEqual((result.matches, result.complete), ((), False))
        self.section(0, -4, 0)
        self.assertTrue(self.bot.find_block_vein(seed).complete)
        self.update(seed, self.OAK)
        self.update((8, -65, 8), self.OAK)
        bottom = self.bot.find_block_vein(seed)
        self.assertEqual(tuple(match.position for match in bottom.matches), (seed,))
        self.assertTrue(bottom.complete)

        in_both_dimensions = (8, 5, 8)
        self.update(in_both_dimensions, self.OAK)
        saved = self.bot.find_block_vein(in_both_dimensions, max_distance=0)
        self.deliver(self.b.ChangeDimensionPacket(dimension_id=1))
        self.section(0, 0, 0, self.OAK, dimension=0)
        current = self.bot.find_block_vein(in_both_dimensions, max_distance=0)
        self.assertEqual((current.dimension_id, current.matches, current.complete), (1, (), False))
        self.assertEqual((saved.dimension_id, saved.matches[0].position, saved.complete), (0, in_both_dimensions, True))

    def test_large_sparse_search_uses_only_cached_cells_and_keeps_order_and_limit(self):
        seed, first, second = (0, 100, 0), (-2700, 100, 0), (2700, 100, 0)
        for position in (second, first, seed):
            self.update(position, self.OAK)
        self.update((1, 100, 0), HASHED_AIR_RUNTIME_ID)
        observe = self.blocks.observed_block
        box_mask = block_observation._vein_box_mask
        probes = []
        sections = []

        def bounded_observe(position):
            probes.append(position)
            self.assertLessEqual(len(probes), 5, "wide sparse query visited unobserved cells")
            return observe(position)

        def bounded_box(section, low, high):
            sections.append(section)
            self.assertLessEqual(len(sections), 3, "huge query should only check its three cached section keys")
            return box_mask(section, low, high)

        huge = 10**30
        with (
            patch.object(self.blocks, "observed_block", side_effect=bounded_observe),
            patch.object(block_observation, "_vein_box_mask", new=bounded_box),
        ):
            capped = self.bot.find_block_vein(seed, max_blocks=2, max_distance=huge, flood_radius=huge)
            self.assertEqual(tuple(match.position for match in capped.matches), (seed, first))
            self.assertFalse(capped.complete)
            probes.clear()
            sections.clear()
            all_matches = self.bot.find_block_vein(seed, max_blocks=huge, max_distance=huge, flood_radius=huge)
            self.assertEqual(tuple(match.position for match in all_matches.matches), (seed, first, second))
            self.assertFalse(all_matches.complete)

    def test_wide_neighborhood_still_follows_edges_then_clips_to_seed_distance(self):
        seed, first, second = (0, 100, 0), (12, 100, 0), (22, 100, 0)
        for position in ((23, 100, 0), second, (12, 112, 12), first, seed):
            self.update(position, self.OAK)
        result = self.bot.find_block_vein(seed, flood_radius=12, max_distance=22)
        self.assertEqual(tuple(match.position for match in result.matches), (seed, first, second))
        self.assertFalse(result.complete)

    def test_wide_neighborhood_can_be_complete_without_covering_its_entire_box(self):
        seed = (8, 104, 8)
        for sx in (0, 1):
            for sy in (6, 7):
                for sz in (0, 1):
                    self.section(sx, sy, sz)
        self.update(seed, self.OAK)
        self.update((16, 112, 16), HASHED_UNKNOWN_RUNTIME_ID)
        complete = self.bot.find_block_vein(seed, max_blocks=1, max_distance=8, flood_radius=8)
        self.assertEqual(tuple(match.position for match in complete.matches), (seed,))
        self.assertTrue(complete.complete)
        self.update((16, 104, 8), HASHED_UNKNOWN_RUNTIME_ID)
        partial = self.bot.find_block_vein(seed, max_blocks=1, max_distance=8, flood_radius=8)
        self.assertEqual((partial.matches, partial.complete), (complete.matches, False))

    def test_wide_search_reuses_each_observation_when_distance_exceeds_flood_radius(self):
        for sx, sz in product((0, 1), repeat=2):
            self.section(sx, 6, sz)
        ore = tuple(islice(product(range(8, 13), range(104, 108), range(8, 13)), 99))
        for position in ore:
            self.update(position, self.OAK)

        observe = self.blocks.observed_block
        calls = 0

        def counted_observe(position):
            nonlocal calls
            calls += 1
            self.assertLessEqual(calls, 4 * 16**3, "overlapping neighborhoods reread cached positions")
            return observe(position)

        results = []
        for distance in (35, 36):
            calls = 0
            with patch.object(self.blocks, "observed_block", new=counted_observe):
                results.append(self.bot.find_block_vein(ore[0], max_blocks=100, max_distance=distance, flood_radius=35))
        self.assertEqual(results[0], results[1])
        self.assertEqual(tuple(match.position for match in results[0].matches), ore)
        self.assertFalse(results[0].complete)

    def test_wide_search_checks_new_frontier_after_finding_the_first_neighbor(self):
        for sx, sy, sz in product((0, 1), (6, 7), (0, 1)):
            self.section(sx, sy, sz)
        seed, neighbor, frontier = (8, 104, 8), (16, 104, 8), (17, 104, 8)
        self.update(seed, self.OAK)
        self.update(neighbor, self.OAK)
        result = self.bot.find_block_vein(seed, max_distance=9, flood_radius=8)
        self.assertEqual(tuple(match.position for match in result.matches), (seed, neighbor))
        self.assertTrue(result.complete)
        self.update(frontier, HASHED_UNKNOWN_RUNTIME_ID)
        partial = self.bot.find_block_vein(seed, max_distance=9, flood_radius=8)
        self.assertEqual((partial.matches, partial.complete), (result.matches, False))

    def test_wide_search_checks_local_section_keys_when_many_observations_are_distant(self):
        for section in product((0, 1), (6, 7), (0, 1)):
            self.section(*section)
        ore = tuple(islice(product(range(8, 13), range(104, 108), range(8, 13)), 99))
        for position in ore:
            self.update(position, self.OAK)
        rules = {"max_blocks": 100, "max_distance": 4096, "flood_radius": 8}
        local = self.bot.find_block_vein(ore[0], **rules)
        self.assertEqual(tuple(match.position for match in local.matches), ore)
        self.assertTrue(local.complete)
        for sx, sz in product(range(4, 68), repeat=2):
            self.update((16 * sx, 104, 16 * sz), self.STONE)
        box_mask = block_observation._vein_box_mask
        checked = 0

        def count_box(section, low, high):
            nonlocal checked
            checked += 1
            self.assertLessEqual(checked, 8 * len(ore), "wide query kept scanning distant sections")
            return box_mask(section, low, high)

        with patch.object(block_observation, "_vein_box_mask", new=count_box):
            self.assertEqual(self.bot.find_block_vein(ore[0], **rules), local)

    def test_invalid_arguments_use_the_public_entry(self):
        for parameter in ("max_blocks", "max_distance", "flood_radius"):
            with self.subTest(parameter=parameter):
                with self.assertRaises(TypeError):
                    self.bot.find_block_vein((0, 0, 0), **{parameter: 1.5})
                with self.assertRaises(ValueError):
                    self.bot.find_block_vein((0, 0, 0), **{parameter: -1})
        for position in ((1, 2), (1, 2.5, 3), (0, 1, True), "123"):
            with self.subTest(position=position), self.assertRaises(ValueError):
                self.bot.find_block_vein(position)
