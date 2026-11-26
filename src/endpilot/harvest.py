"""Select and equip tools using the observed block and inventory."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from math import dist, isfinite
from typing import TYPE_CHECKING

from .block_data import MiningRule
from .blocks import BlockObservation, Blocks, _ORDINARY_MINING_REACH
from .containers import Containers, ContainerWindow, _same_slot
from .inventory import (
    Inventory, _InventoryMoveNotSubmitted, _MAIN_INVENTORY_SIZE, _PLAYER_CONTAINERS, _stack_snapshot,
)
from .item_user_data import read_item_user_data
from .range_transfer import (
    RangeTransferResult, _check_range, _DOUBLE_CHEST_SIZE, _known_slots, _receipt_slot, _receipt_stack,
    _SINGLE_CONTAINER_SIZE, _transfer_range,
)

if TYPE_CHECKING:
    from .bot import Bot


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


class _HarvestEquipUnavailable(RuntimeError):
    pass


async def equip_harvest_tool_from_chest(
    bot: Bot, position: tuple[int, int, int], *, chest: tuple[int, int, int], chest_slots: range,
    inventory_slots: range = range(36), face: int = 1, timeout: float = 15,
) -> HarvestToolAcquisition:
    """Equip for an observed block, taking one eligible tool from a reachable chest if needed.

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

    Timeout, cancellation, connection and cleanup errors carry
    harvest_progress, including any confirmed transfer or selection.
    """
    _check_range(chest_slots, _DOUBLE_CHEST_SIZE, "chest_slots")
    _check_range(inventory_slots, _MAIN_INVENTORY_SIZE, "inventory_slots")
    if not isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be finite and nonnegative")
    harvest = bot._harvest_tools
    if harvest is None:
        raise RuntimeError("player has not joined")
    return await harvest._equip_from_chest(
        bot, position, chest=chest, chest_slots=chest_slots,
        inventory_slots=inventory_slots, face=face, timeout=timeout,
    )


class HarvestTools:
    def __init__(
        self, blocks: Blocks, inventory: Inventory, containers: Containers, item_names: dict[int, str],
    ):
        self._blocks = blocks
        self._inventory = inventory
        self._containers = containers
        self._item_names = item_names
        self._equip_lock = asyncio.Lock()

    def best(self, position: tuple[int, int, int]) -> HarvestTool | None:
        return self._best(position, self._inventory.slots)

    def _best(self, position: tuple[int, int, int], slots: dict[int, object]) -> HarvestTool | None:
        rule = self._mining_rule(self._blocks.observed_block(position))
        return None if rule is None else self._best_in_inventory(rule, slots)

    def _mining_rule(self, block: BlockObservation | None) -> MiningRule | None:
        state = None if block is None else self._blocks._vanilla_state(block.runtime_id)
        return None if state is None else state.mining_rule

    def _best_in_inventory(self, rule: MiningRule, slots: dict[int, object]) -> HarvestTool | None:
        return self._best_in_slots(
            rule, {slot: item for slot, item in slots.items() if 0 <= slot < _MAIN_INVENTORY_SIZE},
            selected=self._inventory.selected_slot,
        )

    def _best_in_slots(
        self, rule: MiningRule, slots: dict[int, object], *, selected: int | None = None,
    ) -> HarvestTool | None:
        best = HarvestTool(None, None, 1.0) if rule.required is None else None
        best_speed = 1.0
        efficiency_id = int(self._inventory.packets.protocol.binding.Enchant.Type.EFFICIENCY)
        # Equal speeds favor the selected player slot, then the lowest index.
        for slot in sorted(slots, key=lambda index: (index != selected, index)):
            item = slots[slot]
            if int(item.id) == 0 or int(item.stack_size) <= 0:
                continue
            name = self._item_names.get(int(item.id))
            if name is None or (rule.required is not None and name not in rule.required):
                continue
            base_speed = rule.speeds.get(name, 1.0)
            speed = base_speed
            if base_speed > 1 and name.endswith(("_pickaxe", "_axe", "_shovel", "_hoe")) and item.user_data_buffer:
                speed += _efficiency_bonus(bytes(item.user_data_buffer), efficiency_id)
            if best is None or speed > best_speed:
                best = HarvestTool(slot, name, base_speed)
                best_speed = speed
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

    def _refill_chest(self, target: BlockObservation, position: tuple[int, int, int]) -> BlockObservation | None:
        rule = self._mining_rule(target)
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

    async def _equip_from_chest(
        self, bot: Bot, position: tuple[int, int, int], *, chest: tuple[int, int, int], chest_slots: range,
        inventory_slots: range, face: int, timeout: float, expected_target: BlockObservation | None = None,
        expected_chest: BlockObservation | None = None, expected_epoch: int | None = None,
        click_pos: tuple[float, float, float] | None = None,
    ) -> HarvestToolAcquisition:
        blocks, inventory, containers = self._blocks, self._inventory, self._containers
        target = blocks.observed_block(position) if expected_target is None else expected_target
        epoch = blocks._mining_epoch if expected_epoch is None else expected_epoch
        rule = self._mining_rule(target)
        window: ContainerWindow | None = None
        owned_window: ContainerWindow | None = None
        transfer: RangeTransferResult | None = None
        tool: HarvestTool | None = None
        status = "unobserved"
        failure = None

        def check_session():
            if bot._harvest_tools is not self or inventory._request_stream_ended:
                raise ConnectionError("harvest tool session ended")

        def current_inventory():
            check_session()
            if rule is None or blocks._mining_epoch != epoch or blocks.observed_block(position) != target:
                return None
            return inventory.slots

        def require_shortage(*, submitting=None):
            slots = current_inventory()
            if slots is None:
                raise _InventoryMoveNotSubmitted("unobserved")
            if self._best_in_inventory(rule, slots) is not None:
                raise _InventoryMoveNotSubmitted("inventory_available")
            if not self._inventory_is_observed(slots, submitting=submitting):
                raise _InventoryMoveNotSubmitted("unobserved")

        def is_target_window(current):
            return (current is not None and current.position == chest and current.container_type == "CONTAINER"
                    and current.entity_unique_id == -1)

        def remember_window(current):
            nonlocal owned_window, window
            window = owned_window = current

        async def equip_inventory(choice, slots, *, confirmed=False):
            nonlocal tool

            def before_slot_use(*indices):
                check_session()
                if window is not None and containers.window is not window:
                    raise _InventoryMoveNotSubmitted("window_changed")
                if self._inventory_has_pending(indices):
                    raise _InventoryMoveNotSubmitted("equip_unavailable" if confirmed else "unobserved")

            try:
                if choice.slot is not None:
                    before_slot_use(choice.slot)
                tool = await self._equip_tool(choice, slots, timeout=timeout, confirmed=confirmed,
                                              before_slot_use=before_slot_use)
            except _InventoryMoveNotSubmitted as stopped:
                return str(stopped)
            except _HarvestEquipUnavailable:
                pass
            check_session()
            if window is not None and containers.window is not window:
                return "window_changed"
            return "equip_unavailable" if tool is None else "equipped"

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
            if not self._known_item_slots(chest_items, chest_slots):
                return "unobserved"
            choice = self._best_in_slots(rule, {slot: chest_items[slot] for slot in chest_slots})
            if choice is None:
                return "tool_unavailable"
            chosen = _stack_snapshot(chest_items[choice.slot])
            received = None

            def before_take(request_id):
                require_shortage(submitting=request_id)
                require_same_chest()
                contents = containers.slots or {}
                if _stack_snapshot(contents.get(choice.slot)) != chosen:
                    raise _InventoryMoveNotSubmitted("unobserved")

            def confirm(slot, stack):
                nonlocal received
                received = slot, stack

            transfer = await _transfer_range(
                bot, direction="take", item=choice.item_name, count=1,
                source_slots=range(choice.slot, choice.slot + 1), destination_slots=inventory_slots,
                match=lambda stack: stack == chosen, timeout=timeout, before_write=before_take, on_confirmed=confirm,
            )
            if transfer.stop_reason == "inventory_available":
                transfer = None
                raise _InventoryMoveNotSubmitted("inventory_available")
            check_session()
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
            try:
                return await take_and_equip()
            except _InventoryMoveNotSubmitted as stopped:
                if str(stopped) != "inventory_available":
                    return str(stopped)
                if window is not None and containers.window is not window:
                    return "window_changed"
                slots = current_inventory()
                choice = None if slots is None else self._best_in_inventory(rule, slots)
                return "unobserved" if choice is None else await equip_inventory(choice, slots)

        try:
            async with self._equip_lock:
                try:
                    status = await acquire()
                except (Exception, asyncio.CancelledError) as error:
                    failure = error
                    transfer = getattr(error, "transfer_progress", transfer)
                    raise
                finally:
                    if owned_window is not None:
                        try:
                            closed = await containers.close_window(timeout=timeout, _expected_window=owned_window)
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

    async def equip(self, position: tuple[int, int, int], *, timeout: float) -> HarvestTool | None:
        async with self._equip_lock:
            return await self._equip(position, timeout=timeout)

    async def _equip(self, position: tuple[int, int, int], *, timeout: float) -> HarvestTool | None:
        slots = self._inventory.slots
        tool = self._best(position, slots)
        if tool is None:
            return None
        return await self._equip_tool(tool, slots, timeout=timeout)

    async def _equip_tool(
        self, tool: HarvestTool, slots: dict[int, object], *, timeout: float, confirmed: bool = False,
        before_slot_use: Callable[..., None] | None = None,
    ) -> HarvestTool | None:
        inventory = self._inventory
        source = tool.slot
        if source is not None and source < 9:
            return await self._select(tool, source, slots[source], before_slot_use=before_slot_use)

        selected = inventory.selected_slot
        hotbar = sorted((slot for slot in range(9) if slot in slots),
                        key=lambda slot: (slot != selected, slot))
        empty = next((slot for slot in hotbar if int(slots[slot].id) == 0), None)
        if source is None and empty is not None:
            return await self._select(tool, empty, slots[empty], before_slot_use=before_slot_use)
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
            before_slot_use=before_slot_use,
        )
        return await self._select(tool, hotbar_slot, held, before_slot_use=before_slot_use)

    def _matches_slot(self, slot: int, item) -> bool:
        inventory = self._inventory
        return (_same_slot(item, inventory.slots.get(slot))
                and _same_slot(item, inventory.item_or_empty(slot)))

    async def _select(self, tool: HarvestTool, slot: int, item, *, before_slot_use=None) -> HarvestTool:
        if before_slot_use is not None:
            before_slot_use(slot)
        if not self._matches_slot(slot, item):
            raise _HarvestEquipUnavailable(f"inventory slot {slot} changed before selecting the harvest tool")
        await self._inventory.select_hotbar(slot)
        if not self._matches_slot(slot, item):
            raise _HarvestEquipUnavailable(f"inventory slot {slot} changed while equipping the harvest tool")
        return replace(tool, slot=slot)

    async def _transfer(
        self, source: int, destination: int, slots: dict[int, object], *, hotbar_slot: int, timeout: float,
        confirmed: bool, before_slot_use: Callable[..., None] | None,
    ):
        inventory = self._inventory
        containers = self._containers
        owned_window = None
        expected = None if source == hotbar_slot else slots[source]
        expected_stack = _stack_snapshot(expected) if confirmed else None
        held = None

        def capture_held_stack() -> None:
            nonlocal held
            # Use the network ID from this response before later packets or window closure.
            held = inventory.slots.get(hotbar_slot)

        def remember_window(window: ContainerWindow) -> None:
            nonlocal owned_window
            owned_window = window

        failure = None
        try:
            if containers.window is None:
                # BDS validates player stack requests against an active window.
                opened = await containers.open_inventory_screen(timeout=timeout, _on_observed=remember_window)
                if containers.window is not opened:
                    raise _HarvestEquipUnavailable("player inventory window did not open")
            if before_slot_use is not None:
                before_slot_use(source, destination)
            for slot in (source, destination):
                if not self._matches_slot(slot, slots[slot]):
                    raise _HarvestEquipUnavailable(f"inventory slot {slot} changed before harvest tool transfer")
            if int(slots[destination].id) == 0:
                response = await inventory.move_item(
                    source, destination, int(slots[source].stack_size), timeout=timeout,
                    _on_confirmed=capture_held_stack,
                )
            else:
                response = await inventory.swap_items(
                    source, destination, timeout=timeout, _on_confirmed=capture_held_stack,
                )
            if response.result.name != "SUCCESS":
                raise _HarvestEquipUnavailable(f"harvest tool transfer rejected: {response.result.name}")
            if (held is None or int(held.id) != (0 if expected is None else int(expected.id))
                    or (expected is not None and held.user_data_buffer != expected.user_data_buffer)):
                raise _HarvestEquipUnavailable(f"harvest tool transfer did not confirm inventory slot {hotbar_slot}")
            if confirmed:
                correction = _receipt_slot(response, _PLAYER_CONTAINERS, hotbar_slot)
                if (correction is None or correction.amount != expected_stack.count
                        or _stack_snapshot(held) != _receipt_stack(expected_stack, correction)):
                    raise _HarvestEquipUnavailable(
                        f"harvest tool transfer did not confirm inventory slot {hotbar_slot}",
                    )
            return held
        except BaseException as error:
            failure = error
            raise
        finally:
            if owned_window is not None:
                try:
                    await containers.close_window(timeout=timeout, _expected_window=owned_window)
                except Exception as error:
                    if failure is None:
                        raise
                    failure.add_note(f"player inventory close failed: {error!r}")
