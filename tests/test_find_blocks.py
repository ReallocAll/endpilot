import unittest
from types import SimpleNamespace

from bedrock_protocol import nbt
from endpilot import BlockMatch, Bot
from endpilot.blocks import Blocks

from test_blocks import PacketStream
from test_subchunks import solid_sub_chunk


class BlockSearchTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot("BlockSearch", protocol=2225)
        self.stream = PacketStream(self.bot.protocol)
        self.b = self.bot.protocol.binding
        self.movement = SimpleNamespace(position=(-16.5, 224.62, -16.5))
        self.blocks = Blocks(self.stream, self.movement, None, hashed_runtime_ids=True)
        self.bot._blocks = self.blocks
        self.blocks.start()

    def tearDown(self):
        self.blocks.close()
        self.assertEqual(self.stream.sent, [])

    def deliver(self, packet):
        self.stream.emit(self.bot.protocol.encode(packet))

    def update(self, position, runtime_id, layer=0):
        x, y, z = position
        self.deliver(self.b.UpdateBlockPacket(
            pos=self.b.BlockPos(x=x, y=y, z=z), runtime_id=runtime_id, update_flags=0, layer=layer,
        ))

    def section(self, key, runtime_id=None, dimension=0):
        b = self.b
        x, y, z = key
        results = b.SubChunkPacket.SubChunkRequestResult
        self.deliver(b.SubChunkPacket(
            dimension_type=dimension, center_pos=b.SubChunkPos(x=x, y=y, z=z),
            sub_chunk_data=[b.SubChunkPacket.SubChunkPacketData(
                sub_chunk_pos_offset=b.SubChunkPacket.SubChunkPosOffset(x=0, y=0, z=0),
                result=results.SUCCESS_ALL_AIR if runtime_id is None else results.SUCCESS,
                serialized_sub_chunk=None if runtime_id is None else solid_sub_chunk(runtime_id),
            )],
        ))

    def publish(self, x, z):
        self.deliver(self.b.NetworkChunkPublisherUpdatePacket(
            position=self.b.BlockPos(x=x, y=224, z=z), radius=64,
        ))

    def chest_root(self, position, name=None):
        x, y, z = position
        root = nbt.CompoundTag({"id": nbt.StringTag("Chest"), "x": nbt.IntTag(x),
                                "y": nbt.IntTag(y), "z": nbt.IntTag(z)})
        if name is not None:
            root["CustomName"] = nbt.StringTag(name)
        self.deliver(self.b.BlockActorDataPacket(pos=self.b.BlockPos(x=x, y=y, z=z), data=root))

    def test_sparse_matches_then_complete_geometry_and_stable_limits(self):
        first = (-18, 224, -17)
        second = (-17, 224, -16)
        edge = (-15, 224, -17)
        outer = (-15, 224, -15)
        self.update(second, 825916963)
        sparse = self.bot.find_blocks("oak_log", radius=2)
        self.assertEqual(sparse.center, (-17, 224, -17))
        self.assertEqual([item.position for item in sparse.matches], [second])
        self.assertFalse(sparse.complete)

        for x in (-2, -1):
            for y in (13, 14):
                for z in (-2, -1):
                    self.section((x, y, z))
        empty = self.bot.find_blocks("diamond_block", radius=2)
        self.assertEqual(empty.matches, ())
        self.assertTrue(empty.complete)
        for position, runtime in ((outer, 825916963), (edge, 1881223534),
                                  (second, 825916963), (first, 4065577688)):
            self.update(position, runtime)
        self.update((-17, 224, -17), 927668178, layer=1)
        known = self.bot.find_blocks(names=["minecraft:glass", "minecraft:oak_log"], radius=2)
        self.assertEqual(known.matches, (
            BlockMatch(first, "minecraft:oak_log", 4065577688, 1),
            BlockMatch(second, "minecraft:oak_log", 825916963, 1),
            BlockMatch(edge, "minecraft:oak_log", 1881223534, 4),
        ))
        self.assertTrue(known.complete)
        self.assertEqual(self.bot.find_block(names="oak_log", radius=2).matches, known.matches[:1])
        self.assertEqual(self.bot.find_blocks("oak_log", radius=2, limit=2).matches, known.matches[:2])
        self.assertTrue(self.bot.find_blocks("air", radius=2, limit=0).complete)
        self.assertEqual(self.bot.find_blocks("air", radius=2, limit=0).matches, ())
        self.assertFalse(self.bot.find_blocks("air", radius=10**20, limit=1).complete)
        selected = self.bot.find_blocks([1881223534, 825916963, 4065577688 - (1 << 32)], radius=2, limit=2)
        self.assertEqual(selected.matches, known.matches[:2])
        self.assertTrue(selected.complete)
        exact = self.bot.find_block(1881223534, radius=2)
        self.assertEqual(exact.matches, known.matches[2:])
        self.assertTrue(exact.complete)
        layer_one = self.bot.find_blocks(927668178, radius=0)
        self.assertEqual(layer_one.matches, ())
        self.assertTrue(layer_one.complete)

    def test_unknown_and_custom_states_cannot_certify_air_or_completeness(self):
        center = (-17, 224, -17)
        for runtime_id in (0xFFFFFFFE, 0x12345678):
            self.update(center, runtime_id)
            search = self.bot.find_blocks("air", radius=0)
            self.assertEqual(search.matches, ())
            self.assertFalse(search.complete)
            exact = self.bot.find_blocks(runtime_id, radius=0)
            self.assertEqual(exact.matches, (BlockMatch(center, None, runtime_id, 0),))
            self.assertTrue(exact.complete)
            different = self.bot.find_blocks(927668178, radius=0)
            self.assertEqual(different.matches, ())
            self.assertTrue(different.complete)
        pending = self.bot.find_blocks(0x12345678, radius=0, limit=0, predicate=lambda _: None)
        self.assertEqual(pending.matches, ())
        self.assertFalse(pending.complete)
        self.blocks.hashed_runtime_ids = False
        self.blocks._custom_blocks = True
        self.update(center, 18894)
        self.assertFalse(self.bot.find_blocks("air", radius=0).complete)
        self.section((-2, 14, -2))
        search = self.bot.find_blocks("air", radius=0)
        self.assertTrue(search.complete)
        self.assertEqual(search.matches, (BlockMatch(center, "minecraft:air", None, 0),))

    def test_signed_hash_selects_the_same_id_from_palette_and_sparse_update(self):
        center = (-17, 224, -17)
        runtime_id = 4065577688
        signed_id = runtime_id - (1 << 32)
        self.section((-2, 14, -2), runtime_id)
        expected = (BlockMatch(center, "minecraft:oak_log", runtime_id, 0),)
        palette = self.bot.find_block(signed_id, radius=0)
        self.assertEqual(palette.matches, expected)
        self.assertTrue(palette.complete)
        self.update(center, 825916963)
        replaced = self.bot.find_block(signed_id, radius=0)
        self.assertEqual(replaced.matches, ())
        self.assertTrue(replaced.complete)
        self.update(center, runtime_id)
        self.assertEqual(self.bot.find_block(signed_id, radius=0), palette)

    def test_sequential_runtime_id_does_not_depend_on_the_vanilla_palette(self):
        self.blocks.hashed_runtime_ids = False
        self.blocks._custom_blocks = True
        center = (-17, 224, -17)
        key = (-2, 14, -2)
        self.section(key, 1900)
        selected = self.bot.find_blocks([1900, 0], radius=0)
        self.assertEqual(selected.matches, (BlockMatch(center, None, 1900, 0),))
        self.assertTrue(selected.complete)
        self.section(key)
        no_id = self.bot.find_blocks([1900, 0], radius=0)
        self.assertEqual(no_id.matches, ())
        self.assertFalse(no_id.complete)
        air = self.bot.find_blocks(radius=0, predicate=lambda match: match.name == "minecraft:air")
        self.assertEqual(air.matches, (BlockMatch(center, "minecraft:air", None, 0),))
        self.assertTrue(air.complete)
        self.update(center, 0)
        zero = self.bot.find_blocks(0, radius=0)
        self.assertEqual(zero.matches, (BlockMatch(center, None, 0, 0),))
        self.assertTrue(zero.complete)

    def test_predicate_only_gets_observations_even_when_names_cannot_be_resolved(self):
        center, glass, unknown = (-20, 216, -20), (-21, 216, -20), (-20, 216, -21)
        self.movement.position = (-19.5, 216.62, -19.5)
        self.update(glass, 927668178)
        self.update(unknown, 0x12345678)
        self.update(center, 825916963, layer=1)
        seen = []

        def only_unmapped(match):
            seen.append(match.position)
            return match.name is None

        expected = (BlockMatch(unknown, None, 0x12345678, 1),)
        sparse = self.bot.find_blocks(radius=1, limit=1, predicate=only_unmapped)
        self.assertEqual(sparse.matches, expected)
        self.assertCountEqual(seen, (glass, unknown))
        self.assertFalse(sparse.complete)

        self.section((-2, 13, -2))
        self.update(glass, 927668178)
        self.update(unknown, 0x12345678)
        known = self.bot.find_block(radius=1, predicate=only_unmapped)
        self.assertEqual(known.matches, expected)
        self.assertTrue(known.complete)
        partial = self.bot.find_blocks(radius=1, limit=0, predicate=lambda match: False if match.name else None)
        self.assertEqual(partial.matches, ())
        self.assertFalse(partial.complete)

    def test_invalid_or_empty_selectors_never_run_predicate(self):
        self.update((-17, 224, -17), 4065577688)
        for invalid in (True, [False], [0.0], ["oak_log", 4065577688]):
            with self.subTest(invalid=invalid), self.assertRaises(TypeError):
                self.bot.find_block(invalid, radius=0)
        for invalid in (-(1 << 31) - 1, 1 << 32):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.bot.find_block(invalid, radius=0)
        with self.assertRaises(ValueError):
            self.bot.find_block(radius=0)
        seen = []
        with self.assertRaises(ValueError):
            self.bot.find_blocks([], radius=0, predicate=seen.append)
        self.assertEqual(seen, [])

    def test_predicate_filters_before_limit_preserving_ties_and_unknown_status(self):
        near, first_far, second_far = (-20, 216, -20), (-21, 216, -20), (-20, 216, -21)
        self.movement.position = (-19.5, 216.62, -19.5)
        self.section((-2, 13, -2))
        self.update((-19, 216, -20), 927668178)
        for position in (second_far, near, first_far):
            self.update(position, 741882976)
        for position in (first_far, second_far):
            self.chest_root(position, "Wanted")
        seen = []

        def named(match):
            seen.append(match)
            root = self.bot.observed_block_entity(match.position)
            return None if root is None else "CustomName" in root and root["CustomName"].value == "Wanted"

        unfiltered = self.bot.find_block("chest", radius=2)
        self.assertTrue(unfiltered.complete)
        self.assertEqual(unfiltered.matches[0].position, near)
        expected = (BlockMatch(first_far, "minecraft:chest", 741882976, 1),)
        for limit in (0, 1):
            with self.subTest(limit=limit):
                seen.clear()
                result = self.bot.find_blocks("chest", radius=2, limit=limit, predicate=named)
                self.assertFalse(result.complete)
                self.assertEqual(result.matches, expected if limit else ())
                self.assertCountEqual((match.position for match in seen), (near, first_far, second_far))
        self.chest_root(near)
        known = self.bot.find_block("chest", radius=2, predicate=named)
        self.assertTrue(known.complete)
        self.assertEqual(known.matches, expected)
        none = self.bot.find_blocks("chest", radius=2, predicate=lambda _: False)
        self.assertTrue(none.complete)
        self.assertEqual(none.matches, ())

    def test_predicate_ignores_unresolved_and_other_primary_names(self):
        self.movement.position = (6.5, 216.62, 6.5)
        self.section((0, 13, 0))
        self.update((6, 216, 6), 0x12345678)
        self.update((7, 216, 6), 741882976, layer=1)
        observed = []
        result = self.bot.find_blocks("chest", radius=1, predicate=observed.append)
        self.assertFalse(result.complete)
        self.assertEqual(result.matches, ())
        self.assertEqual(observed, [])

    def test_predicate_errors_propagate_and_observed_root_is_independent(self):
        center = (-17, 224, -17)
        self.update(center, 741882976)
        self.chest_root(center, "Server Name")
        failure = RuntimeError("predicate failure")

        def inspect_copy(match):
            root = self.bot.observed_block_entity(match.position)
            root["CustomName"] = nbt.StringTag("Local Edit")
            raise failure

        initial = self.bot.find_block("chest", radius=0)
        with self.assertRaises(RuntimeError) as raised:
            self.bot.find_block("chest", radius=0, predicate=inspect_copy)
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.bot.find_block("chest", radius=0), initial)
        self.assertEqual(self.bot.observed_block_entity(center)["CustomName"].value, "Server Name")

    def test_invalid_predicate_is_rejected_by_both_public_queries(self):
        center = (-17, 224, -17)
        with self.assertRaisesRegex(TypeError, "predicate must be callable"):
            self.bot.find_block("chest", radius=0, predicate=42)
        self.update(center, 741882976)
        with self.assertRaisesRegex(TypeError, "predicate must return True, False, or None"):
            self.bot.find_blocks("chest", radius=0, limit=0, predicate=lambda _: 1)
        self.assertEqual(self.bot.find_block("chest", radius=0).matches[0].position, center)

    def test_sequential_state_and_invalidation_via_encoded_packets(self):
        self.blocks.hashed_runtime_ids = False
        center = (-17, 224, -17)
        key = (-2, 14, -2)
        self.publish(-17, -17)
        self.section(key, 1900)
        saved = self.bot.find_block("oak_log", radius=0)
        self.assertTrue(saved.complete)
        self.assertEqual(saved.matches, (BlockMatch(center, "minecraft:oak_log", 1900, 0),))
        self.update(center, 17896)
        self.assertTrue(self.bot.find_block("glass", radius=0).matches)
        self.publish(1024, 1024)
        self.section(key, 1900)
        self.publish(-17, -17)
        self.assertFalse(self.bot.find_block("oak_log", radius=0).complete)
        self.section(key, 1901)
        self.assertEqual(self.bot.find_block("oak_log", radius=0).matches[0].runtime_id, 1901)
        self.deliver(self.b.ChangeDimensionPacket(dimension_id=1))
        self.section(key, 1900, dimension=0)
        new = self.bot.find_blocks("oak_log", radius=0)
        self.assertEqual(new.dimension_id, 1)
        self.assertEqual(new.matches, ())
        self.assertFalse(new.complete)
        self.assertEqual(saved.dimension_id, 0)
        self.assertEqual(saved.matches[0].runtime_id, 1900)

    def test_missing_position_and_invalid_arguments(self):
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            self.bot.find_block("oak_log", radius=-1)
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            self.bot.find_blocks("oak_log", limit=-1)
        with self.assertRaises(TypeError):
            self.bot.find_block("oak_log", radius=1.25)
        with self.assertRaises(TypeError):
            self.bot.find_block([0.0])
        with self.assertRaises(ValueError):
            self.bot.find_blocks([])
        self.movement.position = None
        with self.assertRaisesRegex(RuntimeError, "position has not synchronized"):
            self.bot.find_block("oak_log")
