import unittest

from bedrock_protocol import nbt

from endpilot.block_data import lookup_vanilla_block_state, vanilla_block_state


VERSION = 18168865


def state(name, states, version=None):
    return nbt.CompoundTag({
        "name": nbt.StringTag(name),
        "states": nbt.CompoundTag(states),
        "version": nbt.IntTag(VERSION) if version is None else version,
    })


class BlockStateLookupTests(unittest.TestCase):
    def test_pumpkin_directions_and_melon_match_the_observed_palette(self):
        for direction, expected in (("south", (11066, 2970556485)), ("north", (11068, 721887527))):
            with self.subTest(direction=direction):
                tag = state("minecraft:pumpkin", {"minecraft:cardinal_direction": nbt.StringTag(direction)})
                self.assertEqual(lookup_vanilla_block_state(tag), expected)
                index, network_hash = expected
                self.assertEqual(vanilla_block_state(index, hashed=False).name, "minecraft:pumpkin")
                self.assertEqual(vanilla_block_state(network_hash, hashed=True).name, "minecraft:pumpkin")
        self.assertEqual(lookup_vanilla_block_state(state("minecraft:melon_block", {})), (2501, 3404388875))

    def test_zero_is_a_valid_sequential_index(self):
        self.assertEqual(lookup_vanilla_block_state(state("minecraft:cyan_terracotta", {})), (0, 973836165))
        self.assertEqual(vanilla_block_state(0, hashed=False).name, "minecraft:cyan_terracotta")

    def test_properties_require_the_exact_keys_types_and_values(self):
        expectations = {"lit": nbt.ByteTag(1), "candles": nbt.IntTag(2)}
        self.assertEqual(lookup_vanilla_block_state(state("minecraft:blue_candle", expectations)), (8, 3164727462))
        for properties in (
            {"lit": nbt.IntTag(1), "candles": nbt.IntTag(2)},
            {"lit": nbt.ByteTag(1), "candles": nbt.ByteTag(2)},
            {"lit": nbt.ByteTag(1), "candles": nbt.IntTag(4)},
            {"lit": nbt.ByteTag(2), "candles": nbt.IntTag(2)},
            {"lit": nbt.ByteTag(1), "other": nbt.IntTag(2)},
            {"lit": nbt.ByteTag(1)},
            {**expectations, "other": nbt.ByteTag(0)},
        ):
            with self.subTest(properties=properties):
                self.assertIsNone(lookup_vanilla_block_state(state("minecraft:blue_candle", properties)))
        self.assertIsNone(lookup_vanilla_block_state(state("minecraft:pumpkin", {})))
        self.assertIsNone(lookup_vanilla_block_state(state("minecraft:pumpkin", {
            "minecraft:cardinal_direction": nbt.StringTag("South"),
        })))

    def test_unknown_names_wrong_versions_and_noncanonical_nbt_do_not_resolve(self):
        for name in ("custom:melon_block", "melon_block", "minecraft:not_registered", "minecraft:unknown"):
            with self.subTest(name=name):
                self.assertIsNone(lookup_vanilla_block_state(state(name, {})))
        for version in (nbt.IntTag(VERSION - 1), nbt.Int64Tag(VERSION), nbt.StringTag(str(VERSION))):
            self.assertIsNone(lookup_vanilla_block_state(state("minecraft:melon_block", {}, version)))

        name = nbt.StringTag("minecraft:melon_block")
        props = nbt.CompoundTag({})
        version = nbt.IntTag(VERSION)
        for malformed in (
            3404388875,
            {"name": name, "states": props, "version": version},
            nbt.CompoundTag({"name": name, "states": props}),
            nbt.CompoundTag({"name": name, "states": version, "version": version}),
            nbt.CompoundTag({"name": version, "states": props, "version": version}),
            nbt.CompoundTag({"name": name, "states": props, "version": version, "extra": version}),
        ):
            self.assertIsNone(lookup_vanilla_block_state(malformed))
