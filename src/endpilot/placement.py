"""Submit an ordinary survival placement and observe its world and inventory context."""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from math import isfinite
from typing import Literal

from .blocks import FACE_OFFSETS, BlockChange, Blocks
from .containers import Containers
from .creative_inventory import CreativeInventory
from .entities import Entities, EntitySpawn
from .inventory import HeldItemStack, InventorySlotChange
from .item_registry import ItemRegistrySnapshot
from .protocol import RawPacket


_PlacementReason = Literal[
    "observations_complete", "timed_out", "precondition_failed", "dimension_changed",
    "disconnected", "send_failed", "cancelled",
]

_WATERCRAFT = {
    f"minecraft:{hull}_{prefix}{kind}": (actor_type, variant)
    for hull, kind, variant in (
        ("oak", "boat", 0),
        ("spruce", "boat", 1),
        ("birch", "boat", 2),
        ("jungle", "boat", 3),
        ("acacia", "boat", 4),
        ("dark_oak", "boat", 5),
        ("mangrove", "boat", 6),
        ("bamboo", "raft", 7),
        ("cherry", "boat", 8),
        ("pale_oak", "boat", 9),
        ("poplar", "boat", 10),
    )
    for prefix, actor_type in (("", "minecraft:boat"), ("chest_", "minecraft:chest_boat"))
}


@dataclass(frozen=True, slots=True)
class PlacementInventoryChange:
    """A one-item decrease observed in the submitted hotbar slot."""

    before: HeldItemStack
    after: HeldItemStack


@dataclass(frozen=True, slots=True)
class PlacementResult:
    """Local submission state and independent observations from the server."""

    dimension_id: int
    target: tuple[int, int, int]
    slot: int
    submitted: bool | None
    reason: _PlacementReason
    world_change: BlockChange | None
    inventory_change: PlacementInventoryChange | None


@dataclass(frozen=True, slots=True)
class EntityPlacementResult:
    """Local submission and independent server observations for an entity placement click."""

    dimension_id: int
    position: tuple[int, int, int]
    slot: int
    submitted: bool | None
    reason: _PlacementReason
    spawn: EntitySpawn | None
    inventory_change: PlacementInventoryChange | None


@dataclass(frozen=True, slots=True)
class BoatPlacementResult(EntityPlacementResult):
    """Local submission and independent server observations for an oak boat click."""


class _PreconditionChanged(Exception):
    pass


def _item_identity(stack: HeldItemStack) -> tuple[int, int, int, bytes]:
    return stack.item_id, stack.aux_value, stack.block_runtime_id, stack.user_data_buffer


def _one_item_decrease(before: HeldItemStack, after: HeldItemStack | None) -> bool:
    return (after is not None and after.count == before.count - 1
            and (after.item_id == 0 if before.count == 1 else _item_identity(after) == _item_identity(before)))


async def _wait_for_placement[Result](
    submit: Coroutine[None, None, _PlacementReason | None],
    finished: asyncio.Future[_PlacementReason],
    timeout: float,
    result: Callable[[_PlacementReason], Result],
    *unsubscribe: Callable[[], None],
) -> Result:
    sending = asyncio.create_task(submit)
    try:
        try:
            async with asyncio.timeout(timeout):
                await asyncio.wait((sending, finished), return_when=asyncio.FIRST_COMPLETED)
                if finished.done():
                    return result(finished.result())
                failure = sending.result()
                return result(failure or await finished)
        except TimeoutError:
            return result("timed_out")
        finally:
            for stop in unsubscribe:
                stop()
            sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)
    except asyncio.CancelledError as error:
        error.placement_progress = result("cancelled")
        raise


async def observe_placement(
    containers: Containers,
    blocks: Blocks,
    creative: CreativeInventory,
    against: tuple[int, int, int],
    *,
    face: int,
    expected_runtime_id: int | None = None,
    expected_block_name: str | None = None,
    replace: bool = False,
    click_pos: tuple[float, float, float] | None = None,
    timeout: float = 15,
) -> PlacementResult:
    """Send one click, then wait for both observations or an observation boundary."""
    if face not in FACE_OFFSETS:
        raise ValueError("placement face must be between 0 and 5")
    if (expected_runtime_id is None) == (expected_block_name is None):
        raise ValueError("specify exactly one of expected_runtime_id and expected_block_name")
    if expected_runtime_id is not None and (type(expected_runtime_id) is not int or not 0 <= expected_runtime_id < 2**32):
        raise ValueError("placement target requires an unsigned block runtime ID")
    if expected_block_name is not None and (
        not isinstance(expected_block_name, str) or not expected_block_name.startswith("minecraft:")
        or len(expected_block_name) == len("minecraft:")
    ):
        raise ValueError("placement target requires a fully qualified vanilla block name")
    if click_pos is not None and (len(click_pos) != 3 or not all(0 <= value <= 1 for value in click_pos)):
        raise ValueError("placement click point must have three block-local coordinates")
    if not isfinite(timeout) or timeout < 0:
        raise ValueError("placement timeout must be a finite nonnegative number")

    inventory, movement, packets = containers.inventory, containers.movement, containers.packets
    binding = packets.protocol.binding
    dx, dy, dz = FACE_OFFSETS[face]
    x, y, z = against
    target = (x, y, z) if replace else (x + dx, y + dy, z + dz)
    dimension = blocks.dimension_id
    held = inventory.held_item
    slot, before = held.slot, held.observed_item
    selection_revision = inventory.selection_revision
    clicked = blocks.observed_block(against)
    baseline = clicked if replace else blocks.observed_block(target)
    submitted = False
    world_change = None
    inventory_change = None
    inventory_baseline = None
    last_slot_descriptor = None

    def result(reason: _PlacementReason) -> PlacementResult:
        return PlacementResult(dimension, target, slot, submitted, reason, world_change, inventory_change)

    if blocks._closed:
        return result("disconnected")
    if movement._changing_dimension:
        return result("dimension_changed")
    if (creative.game_mode != binding.GameType.SURVIVAL
        or before is None or before.item_id == 0 or before.count <= 0 or held.predicted_count is not None
        or clicked is None or clicked.is_air is not False or clicked.runtime_id is None
        or baseline is None or (not replace and baseline.is_air is not True)
        or (expected_runtime_id is not None and baseline.runtime_id == expected_runtime_id)):
        return result("precondition_failed")
    clicked_state = blocks._vanilla_state(clicked.runtime_id)
    is_short_grass = clicked_state is not None and clicked_state.name == "minecraft:short_grass"
    if replace != is_short_grass:
        return result("precondition_failed")
    if timeout == 0:
        return result("timed_out")

    finished = asyncio.get_running_loop().create_future()

    def finish(reason: _PlacementReason) -> None:
        if not finished.done():
            finished.set_result(reason)

    def matches_expected(runtime_id: int | None) -> bool:
        if expected_runtime_id is not None:
            return runtime_id == expected_runtime_id
        if runtime_id is None:
            return False
        state = blocks._vanilla_state(runtime_id)
        return state is not None and state.name == expected_block_name

    def on_block(change: BlockChange) -> None:
        nonlocal world_change
        if submitted is not True or finished.done() or world_change is not None:
            return
        if replace:
            if change.before != baseline or change.after.is_air is not False:
                return
        elif change.before is None or change.before.is_air is not True or change.after.is_air is True:
            return
        if matches_expected(change.after.runtime_id):
            world_change = change
            if inventory_change is not None:
                finish("observations_complete")

    inventory_packets = (binding.InventoryContentPacket.ID, binding.InventorySlotPacket.ID,
                         binding.InventoryTransactionPacket.ID, binding.ItemStackResponsePacket.ID)

    def on_packet(raw) -> None:
        nonlocal inventory_change, last_slot_descriptor
        if raw.packet_id == binding.ChangeDimensionPacket.ID:
            finish("dimension_changed")
        elif raw.packet_id == binding.DisconnectPacket.ID:
            finish("disconnected")
        elif (submitted is True and not finished.done() and inventory_change is None
              and inventory_baseline is not None and raw.packet_id in inventory_packets):
            descriptor = inventory._slots.get(slot)
            # Inventory replaces the descriptor only when it applies a report for this slot.
            if descriptor is last_slot_descriptor:
                return
            last_slot_descriptor = descriptor
            after = inventory._observed_stack(slot)
            if _one_item_decrease(inventory_baseline, after):
                inventory_change = PlacementInventoryChange(inventory_baseline, after)
                if world_change is not None:
                    finish("observations_complete")

    def before_send() -> None:
        nonlocal submitted
        if movement._changing_dimension or blocks.dimension_id != dimension:
            finish("dimension_changed")
        if blocks._closed:
            finish("disconnected")
        current = inventory.held_item
        if (finished.done() or creative.game_mode != binding.GameType.SURVIVAL
            or inventory.selection_revision != selection_revision
            or current.slot != slot or current.observed_item != before or current.predicted_count is not None
            or blocks.observed_block(against) != clicked
            or (not replace and blocks.observed_block(target) != baseline)):
            raise _PreconditionChanged
        submitted = None

    async def submit() -> _PlacementReason | None:
        nonlocal submitted, inventory_baseline, last_slot_descriptor
        try:
            await movement.wait_for_position_sync(timeout=None)
        except ConnectionError:
            return "disconnected"
        if movement.input_position is None:
            return "precondition_failed"
        try:
            await containers.click_block(against, blocks, face=face, click_pos=click_pos, _before_send=before_send)
        except _PreconditionChanged:
            return "precondition_failed"
        except Exception:
            if submitted is False:
                raise
            return "send_failed"
        last_slot_descriptor = inventory._slots.get(slot)
        observed = inventory._observed_stack(slot)
        if observed is not None and observed.count > 0 and _item_identity(observed) == _item_identity(before):
            inventory_baseline = observed
        submitted = True
        return None

    unsubscribe_block = blocks.subscribe_block_changes(on_block, dimension_id=dimension, position=target, layer=0)
    unsubscribe_packet = packets.subscribe_raw(on_packet)
    unsubscribe_end = packets.subscribe_reader_end(lambda _cause: finish("disconnected"))
    return await _wait_for_placement(
        submit(), finished, timeout, result, unsubscribe_block, unsubscribe_packet, unsubscribe_end,
    )


async def observe_entity_placement[Result: EntityPlacementResult](
    containers: Containers,
    blocks: Blocks,
    creative: CreativeInventory,
    entities: Entities,
    get_item_registry: Callable[[], ItemRegistrySnapshot | None],
    position: tuple[int, int, int],
    *,
    result_type: type[Result],
    timeout: float = 15,
) -> Result:
    """Submit one top-face click and wait for entity and inventory observations."""
    if not isfinite(timeout) or timeout < 0:
        raise ValueError("placement timeout must be a finite nonnegative number")

    inventory, movement, packets = containers.inventory, containers.movement, containers.packets
    binding = packets.protocol.binding
    x, y, z = position
    position = (x, y, z)
    dimension = blocks.dimension_id
    held = inventory.held_item
    slot, before = held.slot, held.observed_item
    selection_revision = inventory.selection_revision
    submitted = False
    spawn = None
    inventory_change = None
    inventory_baseline = None

    def result(reason: _PlacementReason) -> Result:
        return result_type(dimension, position, slot, submitted, reason, spawn, inventory_change)

    def registry_name(item_id: int) -> str | None:
        registry = get_item_registry()
        return None if registry is None else registry.name_for_id(item_id)

    if blocks._closed or packets._reader_end_result()[0]:
        return result("disconnected")
    if movement._changing_dimension:
        return result("dimension_changed")
    if (creative.game_mode != binding.GameType.SURVIVAL
        or before is None or before.item_id == 0 or before.count <= 0 or held.predicted_count is not None):
        return result("precondition_failed")

    item_name = registry_name(before.item_id)
    legacy_oak_boat = result_type is BoatPlacementResult
    if legacy_oak_boat and item_name != "minecraft:oak_boat":
        return result("precondition_failed")

    clicked = blocks.observed_block_state(position)
    if clicked is None:
        return result("precondition_failed")

    variant = None
    clearance = ()
    match item_name:
        case _ if item_name in _WATERCRAFT:
            depth = clicked.properties.get("liquid_depth")
            if clicked.name != "minecraft:water" or type(depth) is not int or depth != 0:
                return result("precondition_failed")
            actor_type, hull_variant = _WATERCRAFT[item_name]
            if not legacy_oak_boat:
                variant = hull_variant
            click_pos = spawn_offset = (0.5, 0.875, 0.5)
        case "minecraft:minecart" | "minecraft:chest_minecart":
            direction = clicked.properties.get("rail_direction")
            if clicked.name != "minecraft:rail" or type(direction) is not int or direction != 0:
                return result("precondition_failed")
            actor_type = item_name
            click_pos = (0.5, 0.125, 0.5)
            spawn_offset = (0.5, 0.5, 0.5)
        case _:
            actor_type = {
                "minecraft:armor_stand": "minecraft:armor_stand",
                "minecraft:end_crystal": "minecraft:ender_crystal",
                "minecraft:cow_spawn_egg": "minecraft:cow",
                "minecraft:pig_spawn_egg": "minecraft:pig",
                "minecraft:zombie_spawn_egg": "minecraft:zombie",
                "minecraft:villager_spawn_egg": "minecraft:villager_v2",
            }.get(item_name)
            if actor_type is None:
                return result("precondition_failed")
            supported_blocks = (("minecraft:obsidian", "minecraft:bedrock") if item_name == "minecraft:end_crystal"
                                else ("minecraft:stone",))
            if clicked.name not in supported_blocks:
                return result("precondition_failed")
            clearance = (
                blocks.observed_block((x, y + 1, z)),
                blocks.observed_block((x, y + 2, z)),
                *(blocks.observed_block((x, y + offset, z), layer=1) for offset in (0, 1, 2)),
            )
            if any(block is None or block.is_air is not True for block in clearance):
                return result("precondition_failed")
            click_pos = spawn_offset = (0.5, 1.0, 0.5)

    if timeout == 0:
        return result("timed_out")

    # AddActor carries native float32 coordinates, including when a half block rounds away.
    wire_spawn = binding.Vec3(x=x + spawn_offset[0], y=y + spawn_offset[1], z=z + spawn_offset[2])
    world_spawn = (wire_spawn.x, wire_spawn.y, wire_spawn.z)
    seen_ids = {actor.entity_id for actor in entities.snapshot()}
    finished = asyncio.get_running_loop().create_future()

    def finish(reason: _PlacementReason) -> None:
        if not finished.done():
            finished.set_result(reason)

    def matches_variant(actor) -> bool:
        if variant is None:
            return True
        payloads = [entry.payload for entry in actor.data.data if int(entry.id) == int(binding.ActorDataIDs.VARIANT)]
        return (len(payloads) == 1 and isinstance(payloads[0], binding.DataItemIntPayload)
                and payloads[0].value == variant)

    def on_inventory(change: InventorySlotChange) -> None:
        nonlocal inventory_change
        if (submitted is not True or finished.done() or inventory_change is not None
            or inventory_baseline is None or change.before is None):
            return
        if (change.before.count == inventory_baseline.count
            and _item_identity(change.before) == _item_identity(inventory_baseline)
            and _one_item_decrease(change.before, change.after)):
            inventory_change = PlacementInventoryChange(change.before, change.after)
            if spawn is not None:
                finish("observations_complete")

    def on_packet(raw: RawPacket) -> None:
        nonlocal spawn
        if raw.packet_id == binding.ChangeDimensionPacket.ID:
            finish("dimension_changed")
        elif raw.packet_id == binding.DisconnectPacket.ID:
            finish("disconnected")
        elif raw.packet_id == binding.AddActorPacket.ID and not finished.done() and spawn is None:
            actor = packets.protocol.decode(raw)
            entity_id = int(actor.entity_id)
            if entity_id in seen_ids:
                return
            # A retransmission of an earlier actor cannot establish a new spawn after send.
            seen_ids.add(entity_id)
            if submitted is True and actor.actor_type == actor_type:
                location = (float(actor.pos.x), float(actor.pos.y), float(actor.pos.z))
                if location == world_spawn and matches_variant(actor):
                    spawn = EntitySpawn(entity_id, int(actor.runtime_id), actor.actor_type, location)
                    if inventory_change is not None:
                        finish("observations_complete")

    def before_send() -> None:
        nonlocal submitted
        if movement._changing_dimension or blocks.dimension_id != dimension:
            finish("dimension_changed")
        if blocks._closed:
            finish("disconnected")
        current = inventory.held_item
        if (finished.done() or creative.game_mode != binding.GameType.SURVIVAL
            or inventory.selection_revision != selection_revision
            or current.slot != slot or current.observed_item != before or current.predicted_count is not None
            or registry_name(before.item_id) != item_name or blocks.observed_block_state(position) != clicked
            or any(blocks.observed_block(block.position, layer=block.layer) != block for block in clearance)):
            raise _PreconditionChanged
        submitted = None

    async def submit() -> _PlacementReason | None:
        nonlocal submitted, inventory_baseline
        try:
            await movement.wait_for_position_sync(timeout=None)
        except ConnectionError:
            return "disconnected"
        if movement.input_position is None:
            return "precondition_failed"
        try:
            await containers.click_block(position, blocks, face=1, click_pos=click_pos, _before_send=before_send)
        except _PreconditionChanged:
            return "precondition_failed"
        except Exception:
            if submitted is not False:
                return "send_failed"
            if packets._reader_end_result()[0]:
                return "disconnected"
            raise
        observed = inventory._observed_stack(slot)
        if observed is not None and observed.count > 0 and _item_identity(observed) == _item_identity(before):
            inventory_baseline = observed
        submitted = True
        return None

    try:
        unsubscribe_inventory = inventory.subscribe_inventory_changes(on_inventory, slot=slot)
    except ConnectionError:
        return result("disconnected")
    unsubscribe_packet = packets.subscribe_raw(on_packet)
    unsubscribe_end = packets.subscribe_reader_end(lambda _cause: finish("disconnected"))
    return await _wait_for_placement(
        submit(), finished, timeout, result, unsubscribe_inventory, unsubscribe_packet, unsubscribe_end,
    )
