import struct
import unittest

from endpilot.anvil import AnvilPreview, _estimate_anvil
from endpilot.inventory import HeldItemStack


def _string(value):
    encoded = value.encode()
    return struct.pack("<H", len(encoded)) + encoded


def _tag(tag, name, payload):
    return bytes((tag,)) + _string(name) + payload


def _int(name, value):
    return _tag(3, name, struct.pack("<i", value))


def _name(value):
    return _tag(10, "display", _tag(8, "Name", _string(value)) + b"\0")


def _ench(*entries):
    compounds = b"".join(
        _tag(2, "id", struct.pack("<h", kind)) + _tag(2, "lvl", struct.pack("<h", level)) + b"\0"
        for kind, level in entries
    )
    return _tag(9, "ench", struct.pack("<Bi", 10, len(entries)) + compounds)


def _stack(*tags, count=1, item_id=1, aux_value=0, data=None):
    if data is None:
        data = b"\xff\xff\x01\x0a\0\0" + b"".join(tags) + bytes(9) if tags else bytes(10)
    return HeldItemStack(item_id, count, aux_value, 1, 0, data)


class AnvilRuleTests(unittest.TestCase):
    def test_rename_uses_observed_prior_work_and_custom_name(self):
        sword = _stack(_int("RepairCost", 1), _name("Old"))
        name = "minecraft:diamond_sword"
        self.assertEqual(_estimate_anvil(sword, name, None, None, "New"), AnvilPreview(2, 0))
        self.assertIsNone(_estimate_anvil(sword, name, None, None, "Old"))
        self.assertIsNone(_estimate_anvil(sword, name, None, None, None))

    def test_unnamed_item_uses_its_default_name_and_accepts_whitespace(self):
        sword = "minecraft:diamond_sword"
        self.assertIsNone(_estimate_anvil(_stack(), sword, None, None, "Diamond Sword"))
        self.assertEqual(_estimate_anvil(_stack(), sword, None, None, "   "), AnvilPreview(1, 0))
        self.assertEqual(_estimate_anvil(_stack(), sword, None, None, "鑽石劍"), AnvilPreview(1, 0))
        self.assertIsNone(_estimate_anvil(_stack(aux_value=1), sword, None, None, "New"))
        self.assertEqual(
            _estimate_anvil(_stack(_name("Old")), sword, None, None, "Diamond Sword"), AnvilPreview(1, 0),
        )

    def test_material_repair_uses_explicit_name_or_charges_for_removal(self):
        primary = _stack(_int("Damage", 125), _name("Keep this name"))
        diamond_sword = "minecraft:diamond_sword"
        diamond = _stack(item_id=2)
        for name, cost in ((None, 2), ("Keep this name", 1)):
            with self.subTest(name=name):
                self.assertEqual(_estimate_anvil(primary, diamond_sword, diamond, "minecraft:diamond", name),
                                 AnvilPreview(cost, 1))

    def test_no_nbt_is_distinct_from_missing_item_data(self):
        name = "minecraft:diamond_sword"
        self.assertEqual(_estimate_anvil(_stack(), name, None, None, "New"), AnvilPreview(1, 0))
        for data in (b"", b"\0\0", b"\xff" * 15):
            with self.subTest(data=data):
                self.assertIsNone(_estimate_anvil(_stack(data=data), name, None, None, "New"))

    def test_material_repair_uses_only_as_many_units_as_needed(self):
        primary_name, material_name = "minecraft:iron_pickaxe", "minecraft:iron_ingot"
        for damage, available, used in ((8, 4, 1), (62, 4, 1), (125, 1, 1), (125, 4, 3)):
            with self.subTest(damage=damage, available=available):
                result = _estimate_anvil(
                    _stack(_int("Damage", damage)), primary_name,
                    _stack(count=available, item_id=2), material_name, None,
                )
                self.assertEqual(result, AnvilPreview(used, used))

    def test_material_repair_adds_both_prior_work_costs_and_rename(self):
        result = _estimate_anvil(
            _stack(_int("Damage", 125), _int("RepairCost", 3)), "minecraft:iron_pickaxe",
            _stack(_int("RepairCost", 1), count=4, item_id=2), "minecraft:iron_ingot", "New",
        )
        self.assertEqual(result, AnvilPreview(8, 3))

    def test_same_item_consumes_one_and_requires_durability_gain(self):
        sword = "minecraft:diamond_sword"
        damaged = _stack(_int("Damage", 1400))
        self.assertEqual(_estimate_anvil(damaged, sword, damaged, sword, None), AnvilPreview(2, 1))
        self.assertIsNone(_estimate_anvil(_stack(), sword, damaged, sword, None))
        self.assertIsNone(_estimate_anvil(damaged, sword, damaged, "minecraft:iron_sword", None))

    def test_survival_cost_limit_clamps_a_rename_and_rejects_expensive_repairs(self):
        pickaxe = "minecraft:iron_pickaxe"
        primary = _stack(_int("Damage", 200), _int("RepairCost", 31))
        materials = (_stack(_int("Damage", 100), _int("RepairCost", 6)),
                     _stack(_int("Damage", 100), _int("RepairCost", 7)))
        self.assertEqual(_estimate_anvil(primary, pickaxe, materials[0], pickaxe, None), AnvilPreview(39, 1))
        self.assertIsNone(_estimate_anvil(primary, pickaxe, materials[1], pickaxe, None))
        self.assertIsNone(_estimate_anvil(primary, pickaxe, materials[0], pickaxe, "New"))
        rename = _stack(_int("Damage", 63), _int("RepairCost", 63))
        self.assertEqual(_estimate_anvil(rename, pickaxe, None, None, "Anvil cap control"), AnvilPreview(39, 0))

    def test_staged_order_controls_enchantment_cost_in_addition_to_repair(self):
        sword = "minecraft:diamond_sword"
        sharpness = _stack(_int("Damage", 1400), _ench((9, 2)))
        knockback = _stack(_int("Damage", 1400), _ench((12, 2)))
        self.assertEqual(_estimate_anvil(sharpness, sword, knockback, sword, None), AnvilPreview(6, 1))
        self.assertEqual(_estimate_anvil(knockback, sword, sharpness, sword, None), AnvilPreview(4, 1))

    def test_book_and_same_item_enchantments_charge_for_gained_levels_only(self):
        sword = "minecraft:diamond_sword"
        book = "minecraft:enchanted_book"
        sharpness = _stack(_ench((9, 2)))
        knockback = _stack(_ench((12, 2)))
        self.assertEqual(_estimate_anvil(sharpness, sword, sharpness, sword, None), AnvilPreview(1, 1))
        self.assertEqual(_estimate_anvil(sharpness, sword, knockback, sword, None), AnvilPreview(4, 1))
        self.assertEqual(_estimate_anvil(sharpness, sword, knockback, book, None), AnvilPreview(2, 1))
        self.assertIsNone(_estimate_anvil(sharpness, sword, _stack(_ench((9, 1))), sword, None))
        capped = _stack(_ench((9, 5)))
        self.assertIsNone(_estimate_anvil(capped, sword, capped, sword, "Changed"))

    def test_enchantments_must_fit_the_item_and_the_effective_enchantment_set(self):
        sword = "minecraft:diamond_sword"
        book = "minecraft:enchanted_book"
        sharpness = _stack(_ench((9, 2)))
        self.assertIsNone(_estimate_anvil(sharpness, sword, _stack(_ench((10, 2))), sword, None))
        self.assertIsNone(_estimate_anvil(sharpness, sword, _stack(_ench((0, 1))), book, None))
        self.assertEqual(_estimate_anvil(sharpness, sword, _stack(_ench((10, 2), (12, 2))), sword, None),
                         AnvilPreview(4, 1))
        self.assertEqual(_estimate_anvil(_stack(), sword, _stack(_ench((9, 2), (10, 2))), book, None),
                         AnvilPreview(2, 1))

    def test_invalid_rule_metadata_has_no_estimate(self):
        for tag in (_int("Damage", -1), _int("Damage", 251), _int("RepairCost", -1),
                    _tag(8, "RepairCost", _string("3"))):
            with self.subTest(tag=tag):
                self.assertIsNone(_estimate_anvil(
                    _stack(tag), "minecraft:iron_pickaxe", _stack(item_id=2), "minecraft:iron_ingot", None,
                ))
        damaged = _stack(_int("Damage", 1400))
        sword = "minecraft:diamond_sword"
        self.assertIsNone(_estimate_anvil(_stack(_int("display", 0)), sword, None, None, "New"))
        self.assertIsNone(_estimate_anvil(damaged, sword, _stack(_int("ench", 0)), sword, None))
        self.assertIsNone(_estimate_anvil(damaged, sword, _stack(_ench((32767, 1))), sword, None))
