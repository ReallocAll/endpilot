"""Read-only estimate for an observed Bedrock player and terrain."""

from math import isfinite
from typing import TYPE_CHECKING

from ._game_hp_armor import damage_after_armor, vanilla_armor_values
from .entities import _player_flag
from .explosion_damage import _COLLISION_LIMITS, _estimate_game_base_damage, _float32
from .item_user_data import read_item_user_data

if TYPE_CHECKING:
    from .bot import Bot


# The standing position on the wire includes the player Y reference and its epsilon.
_STANDING_Y_OFFSET = 1.62001


def _standing_feet(position):
    x, y, z = position
    return x, _float32(y - _STANDING_Y_OFFSET), z


def _local_target(movement, state, binding):
    if movement.observed_feet_position is None or movement._observed_ground is not True:
        return None
    flags = binding.ActorFlags
    if any(movement._actor_flag(flag) is not False for flag in
           (flags.SNEAKING, flags.RIDING, flags.GLIDING, flags.SWIMMING, flags.SLEEPING, flags.CRAWLING)):
        return None
    if None in state._collision_size:
        return None
    return _standing_feet(movement.position), state._collision_size


def _remote_target(entities, runtime_id, binding):
    state = entities.get(runtime_id)
    if state is None or state.actor_type != "minecraft:player" or entities._actor_instance(runtime_id) is None:
        return None
    feet, standing = entities._cursor_feet(state, binding)
    if not standing or runtime_id in entities._unidentified_equipment_since:
        return None
    view = entities._player_views[runtime_id]
    body = entities._cursor_bodies.get(runtime_id)
    if body is None or None in body.size or _player_flag(view.flags, binding.ActorFlags.SNEAKING):
        return None
    # AddPlayer announces feet directly. MovePlayer includes the standing Y reference.
    return (feet if feet == state.position else _standing_feet(state.position)), body.size


def _armor_totals(inventory, registry):
    observed = inventory._observed_damage_equipment()
    if observed is None:
        return None
    armor, hands = observed
    for item in (*armor, *hands):
        if item.item_id == 0:
            continue
        name = None if registry is None else registry.name_for_id(item.item_id)
        if name is None or not name.startswith("minecraft:") or name == "minecraft:shield":
            return None
        try:
            data = read_item_user_data(item.user_data_buffer)
        except ValueError:
            return None
        if data.get("ench", []) != [] or data.get("StoredEnchantments", []) != []:
            return None
    armor_points = toughness = 0
    for slot, (item, suffix) in enumerate(zip(armor, ("_helmet", "_chestplate", "_leggings", "_boots"), strict=True)):
        if item.item_id == 0:
            continue
        name = registry.name_for_id(item.item_id)
        if not (name.endswith(suffix) or (slot == 1 and name == "minecraft:elytra")):
            return None
        values = vanilla_armor_values(registry, item.item_id, protocol=2225)
        if values is None:
            return None
        armor_points += values[0]
        toughness += values[1]
    return armor_points, toughness


def _estimate_sourceless_explosion_damage(
    bot: "Bot", center: tuple[float, float, float], radius: float, *,
    target_runtime_id: int | None, raw: bool,
) -> float | None:
    if target_runtime_id is not None and type(target_runtime_id) is not int:
        raise TypeError("target_runtime_id must be an int or None")
    if type(raw) is not bool:
        raise TypeError("raw must be a bool")
    session = bot._session
    if (bot.protocol.number != 2225 or session is None or not session.spawned
            or bot.session_end is not None or bot.is_changing_dimension):
        return None
    state, blocks = bot._player_state, bot._blocks
    binding = bot.protocol.binding
    local = target_runtime_id is None or target_runtime_id == session.player_id
    if local:
        target = _local_target(bot._movement, state, binding)
    else:
        target = _remote_target(bot._entities, target_runtime_id, binding)
    if target is None:
        return None
    feet, (width, height) = target
    half = (width / 2, height / 2, width / 2)
    aabb_center = (feet[0], _float32(feet[1] + half[1]), feet[2])

    def collision_at(position):
        boxes = blocks._explosion_collision_at(position)
        return ((), _COLLISION_LIMITS) if boxes is None else (boxes, None)

    damage = _estimate_game_base_damage(feet, aabb_center, half, center, radius, collision_at)
    if raw or damage is None or damage == 0:
        return damage
    if (not local or bot.game_mode != binding.GameType.SURVIVAL or bot.world_difficulty not in (2, 3)
            or state.health is None or not isfinite(state.health) or state.health <= 0
            or state.absorption != 0 or state.status_effects()):
        return None
    totals = _armor_totals(bot._inventory, session.item_registry)
    if totals is None:
        return None
    return damage_after_armor(damage, *totals)
