import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scripts import bedrock_mining


class MiningRuleInputTests(TestCase):
    def setUp(self):
        self.block_types = [
            {"name": f"minecraft:{name}", "requires_correct_tool_for_drops": name in {"ore", "bedrock"}}
            for name in ("ore", "dirt", "water", "bedrock")
        ]
        self.items = [{"name": f"minecraft:{name}"} for name in ("iron_pickaxe", "golden_pickaxe", "stone_shovel")]
        self.blocks = [
            {"name": entry["name"], "destroy_speed": hardness}
            for entry, hardness in zip(self.block_types, (3, 0.5, 100, -1), strict=True)
        ]
        self.qualification = {
            "reference_commit": "40570f03ef64e37c71a81388b52fd55da5d73521",
            "schema": 1,
            "scope": "default_permutations_unenchanted",
            "tools": [None, *(item["name"] for item in self.items)],
            "by_block": {
                "minecraft:ore": "IEII",
                "minecraft:dirt": "DDDD",
                "minecraft:water": "EEEE",
                "minecraft:bedrock": "IIII",
            },
        }
        self.speeds = {
            "minecraft:ore": {"minecraft:iron_pickaxe": 6, "minecraft:golden_pickaxe": 12},
            "minecraft:dirt": {"minecraft:stone_shovel": 4},
        }

    def rules(self):
        return bedrock_mining.build_rules(
            self.block_types, self.items, self.blocks, qualification=self.qualification, speeds=self.speeds,
        )

    def test_tool_eligibility_and_speed_are_independent(self):
        rules = self.rules()

        self.assertEqual(rules["minecraft:ore"], (
            frozenset({"minecraft:iron_pickaxe"}),
            {"minecraft:iron_pickaxe": 6, "minecraft:golden_pickaxe": 12},
        ))
        self.assertEqual(rules["minecraft:dirt"], (None, {"minecraft:stone_shovel": 4}))
        self.assertIsNone(rules["minecraft:water"])
        self.assertIsNone(rules["minecraft:bedrock"])

    def test_bubble_columns_can_be_mined_by_hand_in_water(self):
        name = "minecraft:bubble_column"
        self.block_types.append({"name": name, "requires_correct_tool_for_drops": False})
        self.blocks.extend(
            {"name": name, "destroy_speed": 0, "states": f"{{drag_down:{value}b}}"}
            for value in (0, 1)
        )
        self.qualification["by_block"][name] = "EEEE"
        self.speeds[name] = {}

        rules = self.rules()
        self.assertEqual(rules[name], (None, {}))
        self.assertIsNone(rules["minecraft:water"])

    def test_join_rejects_missing_observation_and_unknown_tool(self):
        ore_speeds = self.speeds.pop("minecraft:ore")
        with self.assertRaisesRegex(ValueError, "missing mining speed observations.*minecraft:ore"):
            self.rules()

        ore_speeds["minecraft:invented_pickaxe"] = 5
        self.speeds["minecraft:ore"] = ore_speeds
        with self.assertRaisesRegex(ValueError, "unknown items.*minecraft:invented_pickaxe"):
            self.rules()

    def test_name_rule_rejects_states_with_different_breakability(self):
        self.blocks.append({"name": "minecraft:ore", "destroy_speed": -1})
        with self.assertRaisesRegex(ValueError, "need per-state observations.*minecraft:ore"):
            self.rules()

    def test_missing_entries_cannot_establish_tool_eligibility(self):
        for block, row in (("ore", "????"), ("ore", "IAII"), ("ore", "AAAA"), ("dirt", "DAAA")):
            name = f"minecraft:{block}"
            with self.subTest(block=block, row=row), patch.dict(self.qualification["by_block"], {name: row}):
                with self.assertRaisesRegex(ValueError, f"unresolved mining qualification for {name}"):
                    self.rules()

    def test_all_missing_entries_can_use_published_no_tool_requirement(self):
        self.qualification["by_block"]["minecraft:dirt"] = "AAAA"
        self.assertEqual(self.rules()["minecraft:dirt"], (None, {"minecraft:stone_shovel": 4}))

    def test_bare_hand_cannot_override_published_tool_requirement(self):
        self.qualification["by_block"]["minecraft:ore"] = "EEEE"
        with self.assertRaisesRegex(ValueError, "requires_correct_tool_for_drops.*minecraft:ore"):
            self.rules()

    def test_generator_entry_binds_observations_to_build_and_palette(self):
        record = {
            "bedrock_data_revision": "40570f03ef64e37c71a81388b52fd55da5d73521",
            "bds_version": "1.26.66.1",
            "native_build": 53696239,
            "qualification_build": 53696234,
            "protocol": 2225,
            "qualification": {key: value for key, value in self.qualification.items() if key != "by_block"},
            "speed_profiles": [self.speeds["minecraft:ore"], self.speeds["minecraft:dirt"], {}],
            "profiles": [[row, speed] for row, speed in (("IEII", 0), ("DDDD", 1), ("EEEE", 2), ("IIII", 2))],
            "blocks": {entry["name"]: index for index, entry in enumerate(self.block_types)},
        }
        with TemporaryDirectory() as directory, patch.object(
            bedrock_mining, "_OBSERVATIONS", Path(directory) / "observations.json",
        ):
            bedrock_mining._OBSERVATIONS.write_text(json.dumps(record), encoding="utf-8")
            rules = bedrock_mining.mining_rules(self.block_types, self.items, self.blocks)
            self.assertIsNone(rules["minecraft:water"])

            record["native_build"] = record["qualification_build"]
            bedrock_mining._OBSERVATIONS.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "different BDS build or palette"):
                bedrock_mining.mining_rules(self.block_types, self.items, self.blocks)
