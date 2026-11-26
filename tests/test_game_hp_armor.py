import unittest

from endpilot._game_hp_armor import damage_after_armor, vanilla_armor_values
from endpilot.item_registry import ItemRegistrySnapshot


class GameHpArmorTests(unittest.TestCase):
    def test_requires_exact_registered_vanilla_armor_name(self):
        registry = ItemRegistrySnapshot({
            -7: "minecraft:iron_chestplate",
            2: "minecraft:elytra",
            3: "minecraft:Iron_chestplate",
            4: "iron_chestplate",
            5: "custom:iron_chestplate",
            6: "minecraft:iron_horse_armor",
            7: "minecraft:shield",
        })
        self.assertEqual(vanilla_armor_values(registry, -7, protocol=2225), (6, 0))
        self.assertEqual(vanilla_armor_values(registry, 2, protocol=2225), (0, 0))
        for item_id in (3, 4, 5, 6, 7, 8):
            with self.subTest(item_id=item_id):
                self.assertIsNone(vanilla_armor_values(registry, item_id, protocol=2225))
        self.assertIsNone(vanilla_armor_values(registry, -7, protocol=2193))
        self.assertIsNone(vanilla_armor_values(None, -7, protocol=2225))

    def test_matches_observed_armor_damage_at_both_distances(self):
        registry = ItemRegistrySnapshot({
            91: "minecraft:iron_chestplate",
            92: "minecraft:diamond_helmet",
            93: "minecraft:diamond_boots",
            94: "minecraft:iron_helmet",
            95: "minecraft:chainmail_leggings",
        })
        # Both rounds on stock BDS 1.26.66.1 reported the same health losses.
        for unarmored, iron_chest, diamond_pair in (
            (7.92434024810791, 7.278402328491211, 6.859767913818359),
            (13.284770965576172, 12.647102355957031, 12.44956111907959),
        ):
            for loadout, expected_totals, expected_damage in (
                ((), (0, 0), unarmored),
                ((91,), (6, 0), iron_chest),
                ((92, 93), (6, 4), diamond_pair),
                ((94, 95), (6, 0), iron_chest),
            ):
                with self.subTest(unarmored=unarmored, loadout=loadout):
                    armor = toughness = 0
                    for item_id in loadout:
                        values = vanilla_armor_values(registry, item_id, protocol=2225)
                        self.assertIsNotNone(values)
                        armor += values[0]
                        toughness += values[1]
                    self.assertEqual((armor, toughness), expected_totals)
                    self.assertAlmostEqual(
                        damage_after_armor(unarmored, armor, toughness), expected_damage, delta=0.000001,
                    )
