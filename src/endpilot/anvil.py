"""Estimate survival anvil operations from observed item descriptors."""

from dataclasses import dataclass
from functools import cache
from hashlib import sha256
from importlib.resources import files
import json
from typing import Any, TypedDict

from .inventory import HeldItemStack
from .item_user_data import read_item_user_data


@dataclass(frozen=True, slots=True)
class AnvilPreview:
    """The local level cost and number of material items the result consumes."""

    level_cost: int
    material_count: int


_ENCHANTED_BOOK = "minecraft:enchanted_book"
_MAX_LEVEL_COST = 39


class _ItemRules(TypedDict):
    durability: int
    default_name_sha256: str
    repair_materials: list[str]
    applicable_enchantments: list[int]


class _EnchantmentRules(TypedDict):
    max_level: int
    item_cost: int
    book_cost: int
    incompatible: list[int]


@cache
def _anvil_catalogue() -> tuple[dict[str, _ItemRules], dict[int, _EnchantmentRules]]:
    data = json.loads(files("endpilot").joinpath("data", "anvil-2225.json").read_bytes())
    return data["items"], {int(kind): rules for kind, rules in data["enchantments"].items()}


def _item_data(item: HeldItemStack) -> dict[str, Any]:
    # A complete descriptor has an NBT marker and two list counts, even with no NBT.
    if item.count <= 0 or len(item.user_data_buffer) < 10:
        raise ValueError("missing anvil item data")
    data = read_item_user_data(item.user_data_buffer)
    repair_cost = data.get("RepairCost", 0)
    if type(repair_cost) is not int or repair_cost < 0:
        raise ValueError("invalid anvil repair cost")
    return data


def _custom_name(data: dict[str, Any]) -> str | None:
    display = data.get("display", {})
    if not isinstance(display, dict):
        raise ValueError("invalid item display data")
    name = display.get("Name")
    if name is not None and not isinstance(name, str):
        raise ValueError("invalid item display name")
    return name


def _rename_cost(
    primary: HeldItemStack, data: dict[str, Any], rules: _ItemRules, new_name: str | None,
) -> int:
    current_name = _custom_name(data)
    if current_name is not None or new_name is None:
        return int(new_name != current_name)
    default_name = rules["default_name_sha256"]
    if primary.aux_value != 0 or not default_name:
        raise ValueError("unknown item display name")
    return int(sha256(new_name.encode()).hexdigest() != default_name)


def _damage(item: HeldItemStack, data: dict[str, Any], durability: int) -> int:
    damage = data.get("Damage", item.aux_value)
    if type(damage) is not int or not 0 <= damage <= durability:
        raise ValueError("invalid item damage")
    return damage


def _enchantments(data: dict[str, Any], rules: dict[int, _EnchantmentRules]) -> dict[int, int]:
    entries = data.get("ench", [])
    if not isinstance(entries, list):
        raise ValueError("invalid item enchantments")
    levels = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid item enchantment")
        enchantment, level = entry.get("id"), entry.get("lvl")
        if type(enchantment) is not int or enchantment not in rules or enchantment in levels:
            raise ValueError("invalid item enchantment ID")
        if type(level) is not int or not 0 < level <= rules[enchantment]["max_level"]:
            raise ValueError("invalid item enchantment level")
        levels[enchantment] = level
    return levels


def _enchantment_cost(
    primary_data: dict[str, Any], material_data: dict[str, Any], applicable: list[int],
    rules: dict[int, _EnchantmentRules], *, book: bool,
) -> int:
    donated = _enchantments(material_data, rules)
    if not donated:
        return 0
    levels = _enchantments(primary_data, rules)
    cost = 0
    for kind, level in donated.items():
        enchantment = rules[kind]
        if kind not in applicable or any(other in levels for other in enchantment["incompatible"]):
            continue
        old_level = levels.get(kind, 0)
        new_level = min(level + 1, enchantment["max_level"]) if level == old_level else level
        if new_level > old_level:
            levels[kind] = new_level
            cost += (new_level - old_level) * enchantment["book_cost" if book else "item_cost"]
    return cost


def _estimate_anvil(
    primary: HeldItemStack, primary_name: str, material: HeldItemStack | None,
    material_name: str | None, new_name: str | None,
) -> AnvilPreview | None:
    if material is None and new_name is None:
        return None
    items, enchantments = _anvil_catalogue()
    rules = items.get(primary_name)
    if rules is None:
        return None
    try:
        primary_data = _item_data(primary)
        rename_cost = _rename_cost(primary, primary_data, rules, new_name)
        prior_work = primary_data.get("RepairCost", 0)
        if material is None:
            if rename_cost:
                return AnvilPreview(min(prior_work + rename_cost, _MAX_LEVEL_COST), 0)
            return None

        material_data = _item_data(material)
        durability = rules["durability"]
        book = material_name == _ENCHANTED_BOOK
        if material_name in rules["repair_materials"] and durability >= 4:
            damage = _damage(primary, primary_data, durability)
            per_unit = durability // 4
            used = min(material.count, (damage + per_unit - 1) // per_unit)
            operation_cost = used
        elif material_name == primary_name or book:
            used = 1
            operation_cost = _enchantment_cost(
                primary_data, material_data, rules["applicable_enchantments"], enchantments, book=book,
            )
            if durability and not book:
                damage = _damage(primary, primary_data, durability)
                other_damage = _damage(material, material_data, durability)
                repair_bonus = durability * 12 // 100
                repaired_damage = max(0, damage + other_damage - durability - repair_bonus)
                if repaired_damage < damage:
                    operation_cost += 2
        else:
            return None
        if not operation_cost:
            return None
        prior_work += material_data.get("RepairCost", 0)
        level_cost = prior_work + operation_cost + rename_cost
        return AnvilPreview(level_cost, used) if level_cost <= _MAX_LEVEL_COST else None
    except ValueError:
        return None
