"""Read protocol 2225 block and tool properties without a session."""

from dataclasses import FrozenInstanceError
import unittest

from endpilot import VanillaBlock, VanillaBlockToolProperties, vanilla_blocks


class BlockCatalogTests(unittest.TestCase):
    def test_catalog_uses_exact_names_and_keeps_registered_unknown(self):
        catalog = vanilla_blocks(protocol=2225)
        self.assertIs(catalog, vanilla_blocks())
        self.assertEqual(len(catalog), 1480)
        self.assertTrue(all(name == block.name and name.startswith("minecraft:") for name, block in catalog.items()))

        unknown = catalog["minecraft:unknown"]
        self.assertIsInstance(unknown, VanillaBlock)
        self.assertEqual(unknown.hardness, 0.0)
        self.assertIsNone(unknown.tool_properties("minecraft:copper_pickaxe"))
        self.assertIsNone(catalog.get("unknown"))
        self.assertIsNone(catalog.get("minecraft:not_a_registered_block"))

        for protocol in (2193, "2225", 2225.0):
            with self.subTest(protocol=protocol), self.assertRaisesRegex(ValueError, "unsupported vanilla block protocol"):
                vanilla_blocks(protocol)

    def test_raw_speed_and_drop_eligibility_are_independent(self):
        catalog = vanilla_blocks()
        ore = catalog["minecraft:iron_ore"]
        self.assertEqual(ore.hardness, 3.0)
        self.assertEqual(catalog["minecraft:deepslate_iron_ore"].hardness, 4.5)
        self.assertEqual(ore.tool_properties("minecraft:copper_pickaxe"), VanillaBlockToolProperties(5.0, True))
        self.assertEqual(ore.tool_properties("minecraft:golden_pickaxe"), VanillaBlockToolProperties(12.0, False))
        self.assertEqual(ore.tool_properties("minecraft:brush"), VanillaBlockToolProperties(1.0, False))
        self.assertEqual(
            catalog["minecraft:glass"].tool_properties("minecraft:copper_pickaxe"), VanillaBlockToolProperties(1.0, True),
        )

    def test_measured_items_are_distinct_from_items_outside_the_tool_domain(self):
        ore = vanilla_blocks()["minecraft:iron_ore"]
        materials = ("wooden", "stone", "copper", "iron", "golden", "diamond", "netherite")
        tools = {f"{material}_{kind}" for material in materials for kind in ("axe", "hoe", "pickaxe", "shovel", "spear", "sword")}
        tools.update(("brush", "fishing_rod", "flint_and_steel", "mace", "shears", "trident"))
        self.assertEqual(len(tools), 48)
        for tool in sorted(tools):
            with self.subTest(tool=tool):
                self.assertIsInstance(ore.tool_properties(f"minecraft:{tool}"), VanillaBlockToolProperties)
        for item in ("minecraft:stick", "minecraft:invented_pickaxe", "copper_pickaxe", "test:copper_pickaxe"):
            with self.subTest(item=item):
                self.assertIsNone(ore.tool_properties(item))

    def test_unknown_eligibility_and_absent_mining_rules_have_distinct_results(self):
        catalog = vanilla_blocks()
        tool = "minecraft:copper_pickaxe"
        for name, speed in (("icicle", 5.0), ("ice_crystal", 5.0), ("disc_press", 1.0)):
            with self.subTest(block=name):
                self.assertEqual(catalog[f"minecraft:{name}"].tool_properties(tool), VanillaBlockToolProperties(speed, None))
        for name, hardness in (("bedrock", -1.0), ("water", 100.0)):
            with self.subTest(block=name):
                block = catalog[f"minecraft:{name}"]
                self.assertEqual(block.hardness, hardness)
                self.assertIsNone(block.tool_properties(tool))

    def test_catalog_records_and_tool_results_are_immutable(self):
        catalog = vanilla_blocks()
        ore = catalog["minecraft:iron_ore"]
        copper = ore.tool_properties("minecraft:copper_pickaxe")
        with self.assertRaises(TypeError):
            catalog["minecraft:iron_ore"] = catalog["minecraft:bedrock"]
        with self.assertRaises(FrozenInstanceError):
            ore.hardness = 0.0
        with self.assertRaises(FrozenInstanceError):
            copper.qualifies_for_drops = False
