"""Fixed vanilla armor fields and ordinary armor damage reduction."""

from ._armor_values_2225 import ARMOR_VALUES
from .item_registry import ItemRegistrySnapshot


def vanilla_armor_values(
    registry: ItemRegistrySnapshot | None,
    item_id: int,
    *,
    protocol: int,
) -> tuple[int, int] | None:
    """Return `(armor_value, toughness_value)` for an exact registered vanilla armor name.

    Callers identify empty slots and verify the equipment state separately.
    """
    if protocol != 2225 or registry is None:
        return None
    name = registry.name_for_id(item_id)
    return ARMOR_VALUES.get(name) if name is not None else None


def damage_after_armor(damage: float, armor_value: int, toughness_value: int) -> float:
    """Apply ordinary armor reduction to nonnegative damage with known equipment totals.

    Uses the rule observed for source-free vanilla script explosions on normal
    difficulty. The caller establishes the supported damage and equipment conditions.
    """
    effective_armor = max(armor_value / 5, armor_value - damage / (2 + toughness_value / 4))
    return damage * (1 - min(20, effective_armor) / 25)
