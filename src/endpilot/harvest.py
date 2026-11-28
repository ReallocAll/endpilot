"""Select and equip tools using the observed block and inventory."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, replace
from math import dist, floor, isfinite
from typing import TYPE_CHECKING

from .block_data import MiningRule, VanillaBlockState
from .block_catalog import vanilla_blocks
from .blocks import BlockObservation, Blocks, _ORDINARY_MINING_REACH
from .containers import Containers, ContainerWindow, _same_slot
from .inventory import (
    HeldItemStack, Inventory, _InventoryMoveNotSubmitted, _MAIN_INVENTORY_SIZE, _PLAYER_CONTAINERS, _stack_snapshot,
)
from .item_user_data import read_item_user_data
from .range_transfer import (
    RangeTransferResult, _check_range, _DOUBLE_CHEST_SIZE, _known_slots, _receipt_slot, _receipt_stack,
    _SINGLE_CONTAINER_SIZE, _transfer_range,
)

if TYPE_CHECKING:
    from .bot import Bot


_WEB_DROP_TOOLS = {
    "minecraft:string": frozenset({
        "minecraft:wooden_sword", "minecraft:stone_sword", "minecraft:golden_sword", "minecraft:copper_sword",
        "minecraft:iron_sword", "minecraft:diamond_sword", "minecraft:netherite_sword",
    }),
    "minecraft:web": frozenset({"minecraft:shears"}),
}
_SILK_TOUCH_ORE_DROPS = {
    "minecraft:diamond_ore": "minecraft:diamond",
    "minecraft:deepslate_diamond_ore": "minecraft:diamond",
    "minecraft:iron_ore": "minecraft:raw_iron",
    "minecraft:deepslate_iron_ore": "minecraft:raw_iron",
}


@dataclass(frozen=True, slots=True)
class _SilkTouchRule(MiningRule):
    silk_touch: bool


def _silk_touch(user_data: bytes, enchant_type) -> bool | None:
    # Even the no-NBT form has a marker and two list counts in a complete descriptor.
    if len(user_data) < 10:
        return None
    try:
        entries = read_item_user_data(user_data, _strict_enchantments=True).get("ench", [])
    except ValueError:
        return None
    levels = {}
    for entry in entries:
        enchantment, level = entry["id"], entry["lvl"]
        if enchantment in levels or level <= 0:
            return None
        levels[enchantment] = level
    silk_level = levels.get(int(enchant_type.SILK_TOUCH))
    if silk_level is None:
        return False
    # The pinned vanilla enchantment catalog makes Silk Touch and Fortune incompatible.
    return True if silk_level == 1 and int(enchant_type.FORTUNE) not in levels else None


def _efficiency_bonus(user_data: bytes, efficiency_id: int) -> int:
    try:
        enchantments = read_item_user_data(user_data).get("ench")
    except ValueError:
        return 0
    if isinstance(enchantments, list):
        for entry in enchantments:
            if isinstance(entry, dict) and type(entry.get("id")) is int and entry["id"] == efficiency_id:
                level = entry.get("lvl")
                if type(level) is int and 1 <= level <= 5:
                    return level**2 + 1
    return 0


@dataclass(frozen=True, slots=True)
class HarvestTool:
    """Tool in a player inventory slot, or bare hands when item_name is None.

    For bare hands, slot is None before equipping and a hotbar slot afterwards.
    speed_multiplier is the unmodified vanilla base speed relative to hands.
    """

    slot: int | None
    item_name: str | None
    speed_multiplier: float


@dataclass(frozen=True, slots=True)
class HarvestToolChest:
    """An observed ordinary chest and the slots authorized for a tool refill."""

    position: tuple[int, int, int]
    chest_slots: range
    inventory_slots: range = range(36)

    def __post_init__(self) -> None:
        if (not isinstance(self.position, tuple) or len(self.position) != 3
                or any(type(axis) is not int or not -(1 << 31) <= axis < (1 << 31) for axis in self.position)):
            raise ValueError("expected three integer block coordinates within signed 32-bit bounds")
        _check_range(self.chest_slots, _DOUBLE_CHEST_SIZE, "chest_slots")
        _check_range(self.inventory_slots, _MAIN_INVENTORY_SIZE, "inventory_slots")


@dataclass(frozen=True, slots=True)
class HarvestToolAcquisition:
    """Outcome, observed chest window, transfer progress and equipped tool."""

    status: str
    window: ContainerWindow | None = None
    transfer: RangeTransferResult | None = None
    tool: HarvestTool | None = None


@dataclass(frozen=True, slots=True)
class HarvestToolSupply:
    """Outcome, ordered chest attempts, and any tool selected during supply."""

    status: str
    attempts: tuple[HarvestToolAcquisition, ...] = ()
    tool: HarvestTool | None = None


class _HarvestEquipUnavailable(RuntimeError):
    pass


async def equip_harvest_tool_from_chest(
    bot: Bot, position: tuple[int, int, int], *, chest: tuple[int, int, int], chest_slots: range,
    inventory_slots: range = range(36), face: int = 1, timeout: float = 15, max_tools: int = 1,
) -> HarvestToolAcquisition:
    """Equip for an observed block, taking up to max_tools from a reachable chest if needed.

    Search the entire main inventory first. Chest slots use the active
    window's order, including the second half of a double chest. Inventory
    slots restrict the take's destination. Normal hotbar selection follows.

    Borrow an already open target window or close the one opened here.
    A different active window is busy. A pending block open is window_busy.
    Missing observations or an
    inconclusive take produce unobserved. A known shortage among the allowed
    chest slots produces tool_unavailable. A full destination or a rejected
    take keeps its range transfer stop reason. A confirmed take can be
    followed by equip_unavailable or window_changed.

    With max_tools greater than one, the call shares a single time budget.
    It equips the best confirmed tool when the limit is reached, the chest
    runs out of eligible tools, or the authorized destination is known full.

    Timeout, cancellation, connection and cleanup errors carry
    harvest_progress, including any confirmed transfer or selection.
    """
    _check_range(chest_slots, _DOUBLE_CHEST_SIZE, "chest_slots")
    _check_range(inventory_slots, _MAIN_INVENTORY_SIZE, "inventory_slots")
    _check_max_tools(max_tools)
    if not isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be finite and nonnegative")
    harvest = bot._harvest_tools
    if harvest is None:
        raise RuntimeError("player has not joined")
    if max_tools > 1:
        supply = await harvest._equip_from_chests(
            bot, position, chests=(HarvestToolChest(chest, chest_slots, inventory_slots),),
            face=face, timeout=timeout, max_tools=max_tools, single=True,
        )
        if supply.attempts:
            attempt, = supply.attempts
            return replace(attempt, status=supply.status)
        return HarvestToolAcquisition(supply.status, tool=supply.tool)
    return await harvest._equip_from_chest(
        bot, position, chest=chest, chest_slots=chest_slots,
        inventory_slots=inventory_slots, face=face, timeout=timeout,
    )


async def equip_harvest_tool_from_chests(
    bot: Bot, position: tuple[int, int, int], *, chests: Sequence[HarvestToolChest],
    face: int = 1, timeout: float = 15, max_tools: int = 1,
) -> HarvestToolSupply:
    """Equip from the main inventory or the first usable chest in caller order.

    Supply currently reachable chests. An empty sequence tries the main
    inventory alone. Advance only after a confirmed shortage and the matching
    close of a chest opened here. Leave a borrowed window to its caller.
    Take up to max_tools from the first chest that can supply them. A
    confirmed take ends the chest search even when fewer tools are available.

    The time budget covers lock acquisition and normal operations. Settling
    already submitted window operations can take longer. An exception carries
    ordered evidence on harvest_supply_progress and keeps harvest_progress
    for the interrupted single acquisition when available.
    """
    if not isinstance(chests, Sequence) or isinstance(chests, (str, bytes, bytearray)):
        raise TypeError("chests must be a sequence of HarvestToolChest")
    snapshot = tuple(chests)
    if any(not isinstance(chest, HarvestToolChest) for chest in snapshot):
        raise TypeError("chests must contain only HarvestToolChest entries")
    _check_max_tools(max_tools)
    if not isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be finite and nonnegative")
    harvest = bot._harvest_tools
    if harvest is None:
        raise RuntimeError("player has not joined")
    return await harvest._equip_from_chests(bot, position, chests=snapshot, face=face, timeout=timeout, max_tools=max_tools)


def _check_max_tools(max_tools):
    if type(max_tools) is not int or max_tools <= 0:
        raise ValueError("max_tools must be a positive integer")


class HarvestTools:
    def __init__(
        self, blocks: Blocks, inventory: Inventory, containers: Containers, item_names: dict[int, str],
        *, ordinary_haste: Callable[[], bool] | None = None,
    ):
        self._blocks = blocks
        self._inventory = inventory
        self._containers = containers
        self._item_names = item_names
        self._ordinary_haste = ordinary_haste
        self._equip_lock = asyncio.Lock()

    def best(
        self, position: tuple[int, int, int], *, require_harvest: bool = True, requested_drop_id: int | None = None,
    ) -> HarvestTool | None:
        return self._best(
            position, self._inventory.slots, require_harvest=require_harvest,
            requested_drop_id=requested_drop_id, strict_drop=True,
        )

    def _best(
        self, position: tuple[int, int, int], slots: dict[int, object], *, requested_drop_id: int | None = None,
        require_harvest: bool = True, strict_drop: bool = False,
    ) -> HarvestTool | None:
        target = self._blocks.observed_block(position)
        rule = self._mining_rule(target, requested_drop_id=requested_drop_id, strict_drop=strict_drop)
        return None if rule is None else self._rank_tools(
            target, rule, slots, require_harvest=require_harvest, keep_current=requested_drop_id is None,
        )

    def _rank_tools(self, target, rule, slots, *, require_harvest, keep_current):
        best = self._best_in_inventory(rule, slots, require_harvest=require_harvest)
        if not keep_current or best is None or best.item_name is None:
            return best
        if (held := self._first_tick_held_tool(target, rule, slots, best)) is not None:
            return held
        return best

    def _first_tick_held_tool(self, target, rule, slots, best) -> HarvestTool | None:
        blocks, inventory = self._blocks, self._inventory
        slot = inventory.selected_slot
        if (slot == best.slot or blocks._custom_blocks or self._ordinary_haste is None
                or not self._ordinary_haste()):
            return None
        current = slots.get(slot)
        if not inventory._has_confirmed_selection(slot, current):
            return None
        name = self._item_names.get(int(current.id), "")
        family = name.rpartition("_")[2]
        if family not in ("pickaxe", "shovel") or not best.item_name.endswith("_" + family):
            return None
        state = blocks._vanilla_state(target.runtime_id)
        known = None if state is None else vanilla_blocks().get(state.name)
        if known is None or not isfinite(known.hardness) or known.hardness <= 0:
            return None
        efficiency_id = int(inventory.packets.protocol.binding.Enchant.Type.EFFICIENCY)
        # Both eligible tools must cross the first-tick threshold in ordinary
        # dry survival. Haste I's 1.44 factor is from the 2225 stock comparison.
        for item, item_name in ((current, name), (slots[best.slot], best.item_name)):
            props = known.tool_properties(item_name)
            if (props is None or props.qualifies_for_drops is not True
                    or (rule.required is not None and item_name not in rule.required)
                    or props.speed_multiplier <= 1 or props.speed_multiplier != rule.speeds.get(item_name)):
                return None
            data = bytes(item.user_data_buffer)
            if len(data) < 10:
                return None
            try:
                nbt_data = read_item_user_data(data, _strict_enchantments=True)
            except ValueError:
                return None
            enchants = nbt_data.get("ench", [])
            enchant = enchants[0] if len(enchants) == 1 else None
            if (nbt_data.keys() - {"ench", "Damage"} or (enchants and (
                enchant is None or enchant["id"] != efficiency_id or not 1 <= enchant["lvl"] <= 5
            ))):
                return None
            speed = props.speed_multiplier + _efficiency_bonus(data, efficiency_id)
            if speed * 1.44 < known.hardness * 30:
                return None
        movement = blocks.movement
        if movement is None or movement._entity_cursor_pose() is None or movement._stationary_mining_pose() is None:
            return None
        motion = movement.state
        observed_feet = movement.observed_feet_position
        if (motion.moving or motion.sneaking or motion.feet_position is None
                or dist(observed_feet, motion.feet_position) > .01):
            return None
        x, y, z = observed_feet
        feet = floor(x), round(y), floor(z)
        # Keep the observed ordinary player body within its known clear column.
        if abs(y - feet[1]) > .01 or any(abs(value - cell - .5) >= .19 for value, cell in ((x, feet[0]), (z, feet[2]))):
            return None
        support = (feet[0], feet[1] - 1, feet[2])
        if (blocks._flat_stance(feet) is not True
                or blocks._stance_air(blocks.observed_block(support, layer=1)) is not True):
            return None
        return HarvestTool(slot, name, rule.speeds[name])

    def _mining_rule(
        self, block: BlockObservation | None, *, requested_drop_id: int | None = None,
        strict_drop: bool = False,
    ) -> MiningRule | None:
        state = None if block is None else self._blocks._vanilla_state(block.runtime_id)
        if state is None:
            return None
        requested_rule = self._requested_drop_rule(state, requested_drop_id)
        if strict_drop and requested_drop_id is not None:
            return requested_rule
        return requested_rule or state.mining_rule

    def _requested_drop_rule(
        self, state: VanillaBlockState | None, requested_drop_id: int | None,
    ) -> MiningRule | None:
        if requested_drop_id is None or state is None or state.mining_rule is None:
            return None
        rule = state.mining_rule
        requested = self._item_names.get(requested_drop_id)
        if state.name == "minecraft:web" and (required := _WEB_DROP_TOOLS.get(requested)) is not None:
            return replace(rule, required=required if rule.required is None else rule.required & required)
        ordinary_drop = _SILK_TOUCH_ORE_DROPS.get(state.name)
        if ordinary_drop is not None and requested in (ordinary_drop, state.name):
            return _SilkTouchRule(rule.required, rule.speeds, silk_touch=requested == state.name)
        return None

    def _matches_drop_enchantment(self, rule: MiningRule, user_data: bytes) -> bool:
        if not isinstance(rule, _SilkTouchRule):
            return True
        enchant_type = self._inventory.packets.protocol.binding.Enchant.Type
        return _silk_touch(bytes(user_data), enchant_type) is rule.silk_touch

    def _has_equipped_drop_tool(self, block: BlockObservation, tool: HarvestTool, requested_drop_id: int) -> bool:
        rule = self._requested_drop_rule(self._blocks._vanilla_state(block.runtime_id), requested_drop_id)
        if rule is None:
            return True
        slot = self._inventory.selected_slot
        if slot != tool.slot:
            return False
        held = self._inventory.item_or_empty(slot)
        held_name = self._item_names.get(int(held.id))
        if (held.id == 0 or held.stack_size <= 0 or held_name != tool.item_name or held_name not in rule.required
                or not self._matches_drop_enchantment(rule, held.user_data_buffer)):
            return False
        equipment = self._inventory._selected_equipment
        # BDS can correct the held item without updating that slot's inventory stack.
        if equipment is None:
            return True
        return (equipment.item_id == int(held.id) and equipment.count > 0
                and self._matches_drop_enchantment(rule, equipment.user_data_buffer))

    def _best_in_inventory(
        self, rule: MiningRule, slots: dict[int, object], *, require_harvest: bool = True,
    ) -> HarvestTool | None:
        return self._best_in_slots(
            rule, {slot: item for slot, item in slots.items() if 0 <= slot < _MAIN_INVENTORY_SIZE},
            selected=self._inventory.selected_slot, require_harvest=require_harvest,
        )

    def _best_in_slots(
        self, rule: MiningRule, slots: dict[int, object], *, selected: int | None = None,
        require_harvest: bool = True,
    ) -> HarvestTool | None:
        required = rule.required
        best = HarvestTool(None, None, 1.0) if required is None or not require_harvest else None
        best_score = 1.0 if require_harvest else (10 if required is None else 3)
        efficiency_id = int(self._inventory.packets.protocol.binding.Enchant.Type.EFFICIENCY)
        # Ties favor bare hands when available, then the selected slot, then the lowest index.
        for slot in sorted(slots, key=lambda index: (index != selected, index)):
            item = slots[slot]
            if int(item.id) == 0 or int(item.stack_size) <= 0:
                continue
            name = self._item_names.get(int(item.id))
            if name is None:
                continue
            harvestable = required is None or name in required
            if require_harvest and not harvestable:
                continue
            if isinstance(rule, _SilkTouchRule) and not self._matches_drop_enchantment(rule, item.user_data_buffer):
                continue
            base_speed = rule.speeds.get(name, 1.0)
            speed = base_speed
            if (base_speed > 1 and (name == "minecraft:shears" or name.endswith(("_pickaxe", "_axe", "_shovel", "_hoe")))
                    and item.user_data_buffer):
                speed += _efficiency_bonus(bytes(item.user_data_buffer), efficiency_id)
            # For the same block, harvestable and non-harvestable breaks use divisors 30 and 100.
            score = speed if require_harvest else speed * (10 if harvestable else 3)
            if best is None or score > best_score:
                best = HarvestTool(slot, name, base_speed)
                best_score = score
        return best

    def _known_item_slots(self, slots, indices: range) -> bool:
        known = _known_slots(slots, indices)
        return (len(known) == len(indices) and all(
            stack.id == 0 or int(stack.id) in self._item_names for stack in known.values()
        ))

    def _inventory_is_observed(self, slots, *, submitting: int | None) -> bool:
        inventory = self._inventory
        indices = range(_MAIN_INVENTORY_SIZE)
        if (not self._known_item_slots(slots, indices)
                or self._inventory_has_pending(indices, submitting=submitting)):
            return False
        predicted = inventory.client_slots()
        return all(_same_slot(slots[slot], predicted.get(slot)) for slot in indices)

    def _inventory_has_pending(self, indices: range | tuple[int, ...], *, submitting: int | None = None) -> bool:
        inventory = self._inventory
        if inventory._unconfirmed_transfer_slots.intersection(indices):
            return True
        own_request = inventory._pending_requests.get(submitting)
        return any(request is not own_request for request in inventory._requests_for_endpoints(
            {(None, slot) for slot in indices}
        ))

    def _observed_chest(self, position: tuple[int, int, int]) -> BlockObservation | None:
        block = self._blocks.observed_block(position)
        state = None if block is None else self._blocks._vanilla_state(block.runtime_id)
        return block if state is not None and state.name == "minecraft:chest" else None

    def _refill_chest(
        self, target: BlockObservation, position: tuple[int, int, int], *, requested_drop_id: int | None = None,
    ) -> BlockObservation | None:
        rule = self._mining_rule(target, requested_drop_id=requested_drop_id)
        slots = self._inventory.slots
        if (rule is not None and rule.required is not None and self._best_in_inventory(rule, slots) is None
                and self._inventory_is_observed(slots, submitting=None)):
            return self._observed_chest(position)
        return None

    def _window_busy(self) -> str | None:
        containers = self._containers
        if containers.window is not None or containers._inventory_open is not None or containers._entity_opening:
            return "busy"
        return "window_busy" if containers._block_open_requests else None

    def _check_session(self, bot: Bot) -> None:
        if bot._harvest_tools is not self or self._inventory._request_stream_ended:
            raise ConnectionError("harvest tool session ended")

    async def _equip_from_chests(self, bot, position, *, chests, face, timeout, max_tools, single=False):
        blocks = self._blocks
        target = blocks.observed_block(position)
        epoch = blocks._mining_epoch
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        attempts = []
        tool = None
        owned_window = None

        def remember_window(window):
            nonlocal owned_window
            owned_window = window

        def result(status):
            return HarvestToolSupply(status, tuple(attempts), tool)

        async def acquire():
            nonlocal owned_window, tool
            async with self._equip_lock:
                self._check_session(bot)
                if target is None:
                    return result("unobserved")
                for chest in (None, *chests):
                    owned_window = None
                    try:
                        acquisition = await self._equip_from_chest(
                            bot, position, chest=None if chest is None else chest.position,
                            chest_slots=range(0) if chest is None else chest.chest_slots,
                            inventory_slots=range(36) if chest is None else chest.inventory_slots,
                            face=face, timeout=max(0, deadline - loop.time()), expected_target=target,
                            expected_epoch=epoch, on_owned_window=remember_window, supply_deadline=deadline,
                            max_tools=max_tools,
                        )
                    except (Exception, asyncio.CancelledError) as error:
                        progress = getattr(error, "harvest_progress", None)
                        if progress is not None:
                            tool = progress.tool
                            if chest is not None:
                                attempts.append(progress)
                        raise
                    tool = acquisition.tool
                    if chest is not None and (acquisition.window is not None or acquisition.status != "equipped"):
                        attempts.append(acquisition)
                    self._check_session(bot)
                    if blocks._mining_epoch != epoch or blocks.observed_block(position) != target:
                        return result("unobserved")
                    if acquisition.status != "tool_unavailable" or acquisition.transfer is not None:
                        return result(acquisition.status)
                    if chest is not None:
                        if owned_window is None or acquisition.window is not owned_window:
                            return result(acquisition.status)
                        if (busy := self._window_busy()) is not None:
                            return result(busy)
                return result("tool_unavailable")

        operation = asyncio.create_task(acquire())
        try:
            async with asyncio.timeout_at(deadline):
                return await asyncio.shield(operation)
        except (Exception, asyncio.CancelledError) as error:
            if not operation.done():
                # Forward the interruption once. Keep the lock with the attempt
                # while it settles its submitted open or close, even on a second cancel.
                operation.cancel()
                while not operation.done():
                    try:
                        await asyncio.wait((operation,))
                    except asyncio.CancelledError:
                        continue
            try:
                operation.result()
            except (Exception, asyncio.CancelledError) as failure:
                if failure is not error:
                    for name in ("harvest_progress", "transfer_progress"):
                        if hasattr(failure, name):
                            setattr(error, name, getattr(failure, name))
                    for note in getattr(failure, "__notes__", ()):
                        error.add_note(note)
            if single:
                if not hasattr(error, "harvest_progress"):
                    error.harvest_progress = attempts[-1] if attempts else HarvestToolAcquisition("unobserved", tool=tool)
            else:
                error.harvest_supply_progress = result("interrupted")
            raise

    async def _close_supply_window(self, *, timeout, _expected_window):
        close = asyncio.create_task(self._containers.close_window(timeout=timeout, _expected_window=_expected_window))
        try:
            return await asyncio.shield(close)
        except asyncio.CancelledError as error:
            try:
                await asyncio.shield(close)
            except (Exception, asyncio.CancelledError) as cleanup_error:
                error.add_note(f"harvest supply close failed: {cleanup_error!r}")
            raise

    async def _equip_from_chest(
        self, bot: Bot, position: tuple[int, int, int], *, chest: tuple[int, int, int] | None, chest_slots: range,
        inventory_slots: range, face: int, timeout: float, expected_target: BlockObservation | None = None,
        expected_chest: BlockObservation | None = None, expected_epoch: int | None = None,
        click_pos: tuple[float, float, float] | None = None,
        on_owned_window: Callable[[ContainerWindow], None] | None = None,
        requested_drop_id: int | None = None, supply_deadline: float | None = None, max_tools: int = 1,
    ) -> HarvestToolAcquisition:
        blocks, inventory, containers = self._blocks, self._inventory, self._containers
        target = blocks.observed_block(position) if expected_target is None else expected_target
        epoch = blocks._mining_epoch if expected_epoch is None else expected_epoch
        rule = self._mining_rule(target, requested_drop_id=requested_drop_id)
        window: ContainerWindow | None = None
        owned_window: ContainerWindow | None = None
        transfer: RangeTransferResult | None = None
        tool: HarvestTool | None = None
        status = "unobserved"
        failure = None
        close_window = containers.close_window if supply_deadline is None else self._close_supply_window

        def current_inventory():
            self._check_session(bot)
            if rule is None or blocks._mining_epoch != epoch or blocks.observed_block(position) != target:
                return None
            return inventory.slots

        def check_supply_deadline():
            if supply_deadline is not None and asyncio.get_running_loop().time() >= supply_deadline:
                raise TimeoutError("harvest tool supply timed out before submission")

        def require_shortage(*, submitting=None, received=None, equipping=False):
            check_supply_deadline()
            slots = current_inventory()
            if slots is None:
                raise _InventoryMoveNotSubmitted("unobserved")
            others = slots
            if received:
                for slot, stack in received.items():
                    if (_stack_snapshot(slots.get(slot)) != stack
                            or _stack_snapshot(inventory.item_or_empty(slot)) != stack):
                        raise _InventoryMoveNotSubmitted("equip_unavailable")
                others = {slot: item for slot, item in slots.items() if slot not in received}
            if self._best_in_inventory(rule, others) is not None:
                raise _InventoryMoveNotSubmitted("inventory_available")
            if not equipping and not self._inventory_is_observed(slots, submitting=submitting):
                raise _InventoryMoveNotSubmitted("unobserved")
            return slots

        def is_target_window(current):
            return (current is not None and current.position == chest and current.container_type == "CONTAINER"
                    and current.entity_unique_id == -1)

        def remember_window(current):
            nonlocal owned_window, window
            window = owned_window = current
            if on_owned_window is not None:
                on_owned_window(current)

        async def equip_inventory(choice, slots, *, confirmed=False, received=None, extra_check=None):
            nonlocal tool

            def require_equip_window():
                self._check_session(bot)
                if window is not None and containers.window is not window:
                    raise _InventoryMoveNotSubmitted("window_changed")

            def before_slot_use(*indices):
                require_equip_window()
                if self._inventory_has_pending(indices):
                    raise _InventoryMoveNotSubmitted("equip_unavailable" if confirmed else "unobserved")

            def before_write():
                check_supply_deadline()
                require_equip_window()
                if current_inventory() is None:
                    raise _InventoryMoveNotSubmitted("unobserved")
                if extra_check is not None:
                    extra_check()

            try:
                if choice.slot is not None:
                    before_slot_use(choice.slot)
                tool = await self._equip_tool(choice, slots, timeout=timeout, confirmed=confirmed,
                                              before_slot_use=before_slot_use,
                                              before_write=before_write if supply_deadline is not None else None,
                                              close_window=close_window, received=received)
            except _InventoryMoveNotSubmitted as stopped:
                return str(stopped)
            except _HarvestEquipUnavailable:
                pass
            self._check_session(bot)
            if window is not None and containers.window is not window:
                return "window_changed"
            return "equip_unavailable" if tool is None else "equipped"

        async def take_one(choice, contents, require_source, *, received=None, time_left):
            chosen = _stack_snapshot(contents[choice.slot])
            moved = None

            def before_take(request_id):
                require_shortage(submitting=request_id, received=received)
                require_source()
                if _stack_snapshot((containers.slots or {}).get(choice.slot)) != chosen:
                    raise _InventoryMoveNotSubmitted("unobserved")

            def confirm(slot, stack):
                nonlocal moved
                moved = slot, stack

            reply = await _transfer_range(
                bot, direction="take", item=choice.item_name, count=1,
                source_slots=range(choice.slot, choice.slot + 1), destination_slots=inventory_slots,
                match=lambda stack: stack == chosen, timeout=time_left, before_write=before_take, on_confirmed=confirm,
            )
            return reply, moved

        async def take_and_equip():
            nonlocal window, transfer
            active = containers.window
            if active is not None and (expected_chest is not None or not is_target_window(active)):
                return "busy"
            chest_block = self._observed_chest(chest)
            if chest_block is None or (expected_chest is not None and chest_block != expected_chest):
                return "unobserved"

            def require_same_chest():
                if blocks.observed_block(chest) != chest_block:
                    raise _InventoryMoveNotSubmitted("unobserved")

            def before_click():
                nonlocal window
                require_shortage()
                require_same_chest()
                if containers.window is not None:
                    if expected_chest is None and is_target_window(containers.window):
                        window = containers.window
                        raise _InventoryMoveNotSubmitted("borrowed_window")
                    raise _InventoryMoveNotSubmitted("busy")
                if containers._inventory_open is not None or containers._entity_opening:
                    raise _InventoryMoveNotSubmitted("busy")
                if click_pos is not None:
                    point = tuple(cell + offset for cell, offset in zip(chest, click_pos, strict=True))
                    hit = blocks.raycast_block_towards(point, _ORDINARY_MINING_REACH)
                    if (hit.position != chest or hit.face != face or hit.click_pos is None
                            or dist(hit.click_pos, click_pos) > 1e-6):
                        raise _InventoryMoveNotSubmitted("unobserved")

            if active is None:
                try:
                    await containers.open(chest, blocks, face=face, timeout=timeout,
                                          _click_pos=click_pos, _before_click=before_click,
                                          _on_observed=remember_window)
                except _InventoryMoveNotSubmitted as stopped:
                    if str(stopped) != "borrowed_window":
                        raise
            else:
                window = active

            chest_items = await containers.wait_for_slots(timeout=timeout, _expected_window=window)
            require_shortage()
            require_same_chest()
            width = containers._observed_window_slot_count(window)
            layout = containers._layout_token
            if (width not in (_SINGLE_CONTAINER_SIZE, _DOUBLE_CHEST_SIZE)
                    or containers._observed_window_block_name(window) != "minecraft:chest"):
                return "unobserved"
            _check_range(chest_slots, width, "chest_slots")
            if max_tools > 1:
                return await take_multiple(layout, require_same_chest)
            if not self._known_item_slots(chest_items, chest_slots):
                return "unobserved"
            choice = self._best_in_slots(rule, {slot: chest_items[slot] for slot in chest_slots})
            if choice is None:
                return "tool_unavailable"
            transfer, received = await take_one(choice, chest_items, require_same_chest, time_left=timeout)
            if transfer.stop_reason == "inventory_available":
                transfer = None
                raise _InventoryMoveNotSubmitted("inventory_available")
            self._check_session(bot)
            if containers.window is not window or containers._layout_token is not layout:
                return "window_changed"
            if received is None:
                return ("unobserved" if transfer.stop_reason in ("unconfirmed", "source_exhausted")
                        else transfer.stop_reason)
            slot, stack = received
            slots = current_inventory()
            if slots is None:
                return "unobserved"
            if _stack_snapshot(slots.get(slot)) != stack:
                return "equip_unavailable"
            return await equip_inventory(replace(choice, slot=slot), slots, confirmed=True)

        async def take_multiple(layout, require_same_chest):
            nonlocal transfer
            steps = []
            received = {}
            loop = asyncio.get_running_loop()

            def require_source():
                if containers.window is not window or containers._layout_token is not layout:
                    raise _InventoryMoveNotSubmitted("window_changed")
                require_same_chest()

            def require_equipment():
                require_source()
                return require_shortage(received=received, equipping=True)

            def record(reason):
                nonlocal transfer
                transfer = RangeTransferResult(max_tools, tuple(steps), reason)

            try:
                for _ in range(max_tools):
                    require_source()
                    require_shortage(received=received)
                    contents = containers.slots or {}
                    if not self._known_item_slots(contents, chest_slots):
                        raise _InventoryMoveNotSubmitted("unobserved")
                    choice = self._best_in_slots(rule, {slot: contents[slot] for slot in chest_slots})
                    if choice is None:
                        if not steps:
                            return "tool_unavailable"
                        record("source_exhausted")
                        break

                    reply, moved = await take_one(
                        choice, contents, require_source, received=received, time_left=max(0, supply_deadline - loop.time()),
                    )
                    steps.extend(reply.steps)
                    record(reply.stop_reason)
                    require_source()
                    if moved is None:
                        if received and reply.stop_reason == "destination_full":
                            break
                        return ("unobserved" if reply.stop_reason in ("unconfirmed", "source_exhausted")
                                else reply.stop_reason)
                    slot, stack = moved
                    received[slot] = stack
                    # Keep partial progress even if the next write has not started.
                    record("unconfirmed")
                else:
                    record("complete")

                slots = require_equipment()
                choice = self._best_in_slots(rule, {slot: slots[slot] for slot in received})
                status = await equip_inventory(choice, slots, confirmed=True, received=received, extra_check=require_equipment)
                if status in ("inventory_available", "equip_unavailable", "window_changed", "unobserved"):
                    record(status)
                return status
            except _InventoryMoveNotSubmitted as stopped:
                if steps:
                    record(str(stopped))
                return str(stopped)
            except (Exception, asyncio.CancelledError) as error:
                pending = getattr(error, "transfer_progress", None)
                if pending is not None:
                    steps.extend(pending.steps)
                    record(pending.stop_reason)
                elif steps and transfer.stop_reason == "unconfirmed" and isinstance(error, asyncio.CancelledError):
                    record("cancelled")
                if transfer is not None:
                    error.transfer_progress = transfer
                raise

        async def acquire():
            slots = current_inventory()
            if slots is None:
                return "unobserved"
            if expected_chest is not None:
                if (busy := self._window_busy()) is not None:
                    return busy
                if self._observed_chest(chest) != expected_chest:
                    return "unobserved"
            choice = self._best_in_inventory(rule, slots)
            if choice is not None:
                return await equip_inventory(choice, slots)
            if not self._inventory_is_observed(slots, submitting=None):
                return "unobserved"
            if chest is None:
                return "tool_unavailable"
            try:
                return await take_and_equip()
            except _InventoryMoveNotSubmitted as stopped:
                if str(stopped) != "inventory_available" or max_tools > 1:
                    return str(stopped)
                if window is not None and containers.window is not window:
                    return "window_changed"
                slots = current_inventory()
                choice = None if slots is None else self._best_in_inventory(rule, slots)
                return "unobserved" if choice is None else await equip_inventory(choice, slots)

        try:
            async with (self._equip_lock if supply_deadline is None else nullcontext()):
                try:
                    status = await acquire()
                except (Exception, asyncio.CancelledError) as error:
                    failure = error
                    transfer = getattr(error, "transfer_progress", transfer)
                    raise
                finally:
                    if owned_window is not None:
                        try:
                            closed = await close_window(timeout=timeout, _expected_window=owned_window)
                            if closed is None and failure is None:
                                status = "window_changed"
                            elif status == "equipped" and expected_chest is not None:
                                status = self._window_busy() or status
                        except (Exception, asyncio.CancelledError) as error:
                            if failure is None:
                                raise
                            failure.add_note(f"harvest chest close failed: {error!r}")
        except (Exception, asyncio.CancelledError) as error:
            error.harvest_progress = HarvestToolAcquisition(status, window, transfer, tool)
            raise
        return HarvestToolAcquisition(status, window, transfer, tool)

    async def equip(
        self, position: tuple[int, int, int], *, timeout: float, requested_drop_id: int | None = None,
        require_harvest: bool = True, strict_drop: bool = False, _keep_equipped: bool = False,
    ) -> HarvestTool | None:
        async with self._equip_lock:
            return await self._equip(
                position, timeout=timeout, requested_drop_id=requested_drop_id, require_harvest=require_harvest,
                strict_drop=strict_drop, keep_equipped=_keep_equipped,
            )

    async def _equip(
        self, position: tuple[int, int, int], *, timeout: float, requested_drop_id: int | None = None,
        require_harvest: bool = True, strict_drop: bool = False, keep_equipped: bool = False,
    ) -> HarvestTool | None:
        blocks, inventory = self._blocks, self._inventory
        target = blocks.observed_block(position)
        rule = self._mining_rule(target, requested_drop_id=requested_drop_id, strict_drop=strict_drop)
        if rule is None:
            return None
        slots = inventory.slots
        ordinary = keep_equipped and requested_drop_id is None
        tool = self._rank_tools(target, rule, slots, require_harvest=require_harvest, keep_current=ordinary)
        if tool is None:
            return None
        if ordinary:
            slot = inventory.selected_slot if tool.slot is None else tool.slot
            item = slots.get(slot)
            if (item is not None and (tool.slot is not None or item.id == 0)
                    and inventory._has_confirmed_selection(slot, item)):
                return replace(tool, slot=slot)
        if not strict_drop or requested_drop_id is None:
            return await self._equip_tool(tool, slots, timeout=timeout)

        if tool.slot is None:
            return None
        epoch = blocks._mining_epoch
        item_id = int(slots[tool.slot].id)

        def before_write():
            if (blocks._mining_epoch != epoch or blocks.observed_block(position) != target
                    or self._mining_rule(target, requested_drop_id=requested_drop_id, strict_drop=True) != rule
                    or self._item_names.get(item_id) != tool.item_name):
                raise _InventoryMoveNotSubmitted("harvest target or drop tool changed")

        try:
            equipped = await self._equip_tool(tool, slots, timeout=timeout, before_write=before_write)
        except _InventoryMoveNotSubmitted:
            return None
        return (equipped if equipped is not None and self._has_equipped_drop_tool(target, equipped, requested_drop_id)
                else None)

    async def _equip_tool(
        self, tool: HarvestTool, slots: dict[int, object], *, timeout: float, confirmed: bool = False,
        before_slot_use: Callable[..., None] | None = None,
        before_write: Callable[[], None] | None = None,
        close_window=None, received: dict[int, HeldItemStack] | None = None,
    ) -> HarvestTool | None:
        inventory = self._inventory
        source = tool.slot
        if source is not None and source < 9:
            return await self._select(tool, source, slots[source], before_slot_use=before_slot_use,
                                      before_write=before_write)

        selected = inventory.selected_slot
        hotbar = sorted((slot for slot in range(9) if slot in slots),
                        key=lambda slot: (slot != selected, slot))
        empty = next((slot for slot in hotbar if int(slots[slot].id) == 0), None)
        if source is None and empty is not None:
            return await self._select(tool, empty, slots[empty], before_slot_use=before_slot_use, before_write=before_write)
        if not hotbar:
            return None

        hotbar_slot = hotbar[0] if empty is None else empty
        if source is None:
            # Stow the current stack before selecting an empty hand.
            destination = next((slot for slot in range(9, 36)
                                if slot in slots and int(slots[slot].id) == 0), None)
            if destination is None:
                return None
            source = hotbar_slot
        else:
            destination = hotbar_slot

        held = await self._transfer(
            source, destination, slots, hotbar_slot=hotbar_slot, timeout=timeout, confirmed=confirmed,
            before_slot_use=before_slot_use, before_write=before_write, close_window=close_window, received=received,
        )
        return await self._select(tool, hotbar_slot, held, before_slot_use=before_slot_use, before_write=before_write)

    def _matches_slot(self, slot: int, item) -> bool:
        inventory = self._inventory
        return (_same_slot(item, inventory.slots.get(slot))
                and _same_slot(item, inventory.item_or_empty(slot)))

    def _require_slot_at_write(self, slot: int, expected: HeldItemStack | None) -> None:
        inventory = self._inventory
        if (inventory._observed_stack(slot) != expected
                or _stack_snapshot(inventory.item_or_empty(slot)) != expected):
            raise _InventoryMoveNotSubmitted(f"harvest inventory slot {slot} changed")

    async def _select(self, tool: HarvestTool, slot: int, item, *, before_slot_use=None, before_write=None) -> HarvestTool:
        if before_slot_use is not None:
            before_slot_use(slot)
        if before_write is not None:
            expected = _stack_snapshot(item)

            def require_selection():
                before_write()
                self._require_slot_at_write(slot, expected)

            require_selection()
            await self._inventory.announce_equipment(slot, item, _before_send=require_selection)
            require_selection()
            return replace(tool, slot=slot)

        if not self._matches_slot(slot, item):
            raise _HarvestEquipUnavailable(f"inventory slot {slot} changed before selecting the harvest tool")
        await self._inventory.select_hotbar(slot)
        if not self._matches_slot(slot, item):
            raise _HarvestEquipUnavailable(f"inventory slot {slot} changed while equipping the harvest tool")
        return replace(tool, slot=slot)

    async def _transfer(
        self, source: int, destination: int, slots: dict[int, object], *, hotbar_slot: int, timeout: float,
        confirmed: bool, before_slot_use: Callable[..., None] | None, before_write: Callable[[], None] | None,
        close_window=None, received: dict[int, HeldItemStack] | None = None,
    ):
        inventory = self._inventory
        containers = self._containers
        if close_window is None:
            close_window = containers.close_window
        owned_window = None
        expected = None if source == hotbar_slot else slots[source]
        check_receipt = confirmed or before_write is not None
        expected_stack = _stack_snapshot(expected) if check_receipt else None
        held = None
        source_after = None
        expected_slots = ({slot: _stack_snapshot(slots[slot]) for slot in (source, destination)}
                          if before_write is not None else {})

        def require_current_slots() -> None:
            before_write()
            for slot, snapshot in expected_slots.items():
                self._require_slot_at_write(slot, snapshot)

        def capture_held_stack() -> None:
            nonlocal held, source_after
            # Use the network ID from this response before later packets or window closure.
            held = inventory.slots.get(hotbar_slot)
            if received is not None:
                source_after = inventory._observed_stack(source)

        def remember_window(window: ContainerWindow) -> None:
            nonlocal owned_window
            owned_window = window

        failure = None
        try:
            window = containers.window
            if window is None:
                # BDS validates player stack requests against an active window.
                open_guard = {"_before_send": require_current_slots} if before_write is not None else {}
                window = await containers.open_inventory_screen(timeout=timeout, _on_observed=remember_window,
                                                                 **open_guard)
                if containers.window is not window:
                    if before_write is not None:
                        raise _InventoryMoveNotSubmitted("harvest inventory window changed")
                    raise _HarvestEquipUnavailable("player inventory window did not open")
            if before_slot_use is not None:
                before_slot_use(source, destination)

            def require_transfer() -> None:
                require_current_slots()
                if containers.window is not window:
                    raise _InventoryMoveNotSubmitted("harvest inventory window changed")

            if before_write is not None:
                require_transfer()
            else:
                for slot in (source, destination):
                    if not self._matches_slot(slot, slots[slot]):
                        raise _HarvestEquipUnavailable(f"inventory slot {slot} changed before harvest tool transfer")

            transfer_guard = {"_at_write": require_transfer} if before_write is not None else {}
            if int(slots[destination].id) == 0:
                response = await inventory.move_item(
                    source, destination, int(slots[source].stack_size), timeout=timeout,
                    _on_confirmed=capture_held_stack, **transfer_guard,
                )
            else:
                response = await inventory.swap_items(
                    source, destination, timeout=timeout, _on_confirmed=capture_held_stack, **transfer_guard,
                )
            if response.result.name != "SUCCESS":
                raise _HarvestEquipUnavailable(f"harvest tool transfer rejected: {response.result.name}")
            if (held is None or int(held.id) != (0 if expected is None else int(expected.id))
                    or (expected is not None and held.user_data_buffer != expected.user_data_buffer)):
                raise _HarvestEquipUnavailable(f"harvest tool transfer did not confirm inventory slot {hotbar_slot}")
            if check_receipt:
                correction = _receipt_slot(response, _PLAYER_CONTAINERS, hotbar_slot)
                if expected_stack is None:
                    net_id = None if correction is None else correction.item_stack_net_id
                    matched = correction is not None and correction.amount == 0 and (net_id is None or net_id.raw_id == 0)
                else:
                    matched = (correction is not None and correction.amount == expected_stack.count
                               and _stack_snapshot(held) == _receipt_stack(expected_stack, correction))
                if not matched:
                    raise _HarvestEquipUnavailable(
                        f"harvest tool transfer did not confirm inventory slot {hotbar_slot}",
                    )
            if received is not None:
                # Update the acquired slots only after both sides of the hotbar
                # move or swap were described by its matching verdict.
                correction = _receipt_slot(response, _PLAYER_CONTAINERS, source)
                previous = expected_slots[destination]
                if previous.item_id:
                    matched = (correction is not None and correction.amount == previous.count
                               and source_after == _receipt_stack(previous, correction))
                else:
                    net_id = None if correction is None else correction.item_stack_net_id
                    matched = (correction is not None and correction.amount == 0
                               and (net_id is None or net_id.raw_id == 0)
                               and source_after is not None and not source_after.item_id and not source_after.count)
                if not matched:
                    raise _HarvestEquipUnavailable(f"harvest tool transfer did not confirm inventory slot {source}")
                del received[source]
                if destination in received:
                    received[source] = source_after
                received[destination] = _stack_snapshot(held)
            return held
        except BaseException as error:
            failure = error
            raise
        finally:
            if owned_window is not None:
                try:
                    await close_window(timeout=timeout, _expected_window=owned_window)
                except Exception as error:
                    if failure is None or (before_write is not None and isinstance(failure, _InventoryMoveNotSubmitted)):
                        raise
                    failure.add_note(f"player inventory close failed: {error!r}")
