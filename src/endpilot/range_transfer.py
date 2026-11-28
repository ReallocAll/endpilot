"""Transfer named items between observed slot ranges."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from inspect import isawaitable, iscoroutine
from math import isfinite
from typing import TYPE_CHECKING, Literal

from ._stack_limits_2225 import STACK_LIMITS
from .containers import _ENDER_CHEST, _PLAYER_CONTAINERS, _same_slot, _storage_window_domain
from .inventory import HeldItemStack, _InventoryMoveNotSubmitted, _stack_snapshot

if TYPE_CHECKING:
    from .bot import Bot


_SINGLE_CONTAINER_SIZE = 27
_DOUBLE_CHEST_SIZE = 54
_TRANSFER_BLOCKS = ("minecraft:chest", "minecraft:barrel")
_UTILITY_BLOCK_LAYOUTS = {
    "DISPENSER": ("minecraft:dispenser", 9),
    "DROPPER": ("minecraft:dropper", 9),
    "HOPPER": ("minecraft:hopper", 5),
}
_PLAYER_SIZE = 36
_MAX_STEP = 255


@dataclass(frozen=True, slots=True)
class RangeTransferStep:
    """Requested movement and its matching server evidence, when available.

    After a wait times out or is cancelled, a request ID without a verdict
    identifies a request that may still complete. It does not confirm a write.
    """

    source_slot: int
    destination_slot: int
    requested: int
    confirmed: int | None
    request_id: int | None
    verdict: str | None


@dataclass(frozen=True, slots=True)
class RangeTransferResult:
    """Attempted steps through completion or the first terminal outcome."""

    requested: int
    steps: tuple[RangeTransferStep, ...]
    stop_reason: str

    @property
    def confirmed_count(self) -> int:
        """Total quantity established by the matching server verdicts."""
        return sum(step.confirmed for step in self.steps if step.confirmed is not None)


def _check_range(slots: range, limit: int, name: str) -> None:
    if not isinstance(slots, range) or slots.step != 1 or not 0 <= slots.start <= slots.stop <= limit:
        raise ValueError(f"{name} must be a forward half-open range within 0..{limit}")


def _known_slots(slots, indices):
    known = {}
    for index in indices:
        stack = slots.get(index)
        if stack is None:
            continue
        if (stack.id == 0 and stack.stack_size == 0
                or stack.id and stack.net_id_variant and 0 < stack.stack_size <= _MAX_STEP):
            known[index] = stack
    return known


def _merge_key(stack):
    return stack.id, stack.aux_value, stack.block_runtime_id, bytes(stack.user_data_buffer)


def _plan_step(source, destination, item_id, remaining, limit, source_slots, destination_slots, *,
               match=None, window_is_current=None):
    candidates = []
    for index, stack in source.items():
        if stack.id != item_id:
            continue
        if match is not None:
            selected = match(_stack_snapshot(stack))
            if isawaitable(selected):
                if iscoroutine(selected):
                    selected.close()
                raise TypeError("match must be a synchronous predicate")
            selected = bool(selected)
            if window_is_current is not None and not window_is_current():
                return "window_changed"
            if not selected:
                continue
        candidates.append((index, _merge_key(stack)))
    source_unknown = len(source) < len(source_slots)
    if not candidates:
        return "unobserved" if source_unknown else "source_exhausted"

    empty = None
    capacity_unknown = False
    for target, stack in destination.items():
        if stack.id == 0:
            if empty is None:
                empty = target
            continue
        key = _merge_key(stack)
        for origin, source_key in candidates:
            if key != source_key:
                continue
            if limit is None:
                capacity_unknown = True
                break
            spare = limit - stack.stack_size
            if spare > 0:
                return origin, target, min(remaining, source[origin].stack_size, spare)
            break

    if empty is not None:
        origin = candidates[0][0]
        return origin, empty, min(remaining, source[origin].stack_size)
    if source_unknown or len(destination) < len(destination_slots) or capacity_unknown:
        return "unobserved"
    return "destination_full"


def _receipt_slot(response, names, index: int):
    matches = [slot for group in response.containers or ()
               if group.full_container_name.name.name in names
               for slot in group.slots if slot.slot == index]
    return matches[0] if len(matches) == 1 else None


def _receipt_count(response, names, index: int) -> int | None:
    slot = _receipt_slot(response, names, index)
    return None if slot is None else int(slot.amount)


def _receipt_stack(stack: HeldItemStack, slot) -> HeldItemStack:
    net_id = slot.item_stack_net_id
    return replace(stack, count=int(slot.amount), net_id=stack.net_id if net_id is None else int(net_id.raw_id))


async def transfer_range(
    bot: Bot,
    *,
    direction: Literal["take", "put"],
    item: str,
    count: int,
    source_slots: range,
    destination_slots: range,
    match: Callable[[HeldItemStack], bool] | None = None,
    timeout: float = 15,
) -> RangeTransferResult:
    """Move a registered item between the open storage window and main inventory.

    A double chest uses the 54-slot order reported by its active window.
    Chest minecarts and chest watercraft require matching 27-slot windows.
    Hopper minecarts require matching 5-slot windows.
    Dispensers and droppers require matching 9-slot windows. A block hopper
    requires a matching 5-slot window and an observed powered state.
    Optional match is a pure synchronous predicate on immutable source stacks
    with the requested item ID. It can run again before planning each step.
    A window change during matching stops the transfer before the next send.
    Fill compatible observed stacks first, then use observed empty slots.
    A step overlapping an in-flight transfer stops with unconfirmed progress.
    A predicate error carries transfer_progress for prior steps on the original
    exception. A wait error also carries progress from replies already applied
    by the reader. A step with confirmed=None may still receive its server verdict.
    """
    return await _transfer_range(
        bot, direction=direction, item=item, count=count, source_slots=source_slots,
        destination_slots=destination_slots, match=match, timeout=timeout,
    )


async def transfer_item(
    bot: Bot,
    *,
    direction: Literal["take", "put"],
    item: str,
    count: int,
    match: Callable[[HeldItemStack], bool] | None = None,
    timeout: float = 15,
) -> RangeTransferResult:
    """Move up to count of a registered item between the active container and main inventory.

    Use the same storage windows as transfer_range. Wait for the open window's
    contents, then use its full slot range and all 36 main inventory slots.
    The timeout covers the content wait and all item requests. A replacement
    window stops the operation, and a zero count completes immediately.
    The optional match and server-confirmed result follow transfer_range.
    """
    return await _transfer_range(
        bot, direction=direction, item=item, count=count, match=match, timeout=timeout, whole_window=True,
    )


async def _transfer_range(
    bot: Bot, *, direction: Literal["take", "put"], item: str, count: int,
    source_slots: range | None = None, destination_slots: range | None = None,
    match: Callable[[HeldItemStack], bool] | None, timeout: float,
    whole_window: bool = False,
    before_write: Callable[[int], None] | None = None,
    on_confirmed: Callable[[int, HeldItemStack], None] | None = None,
) -> RangeTransferResult:
    if direction not in ("take", "put"):
        raise ValueError("direction must be take or put")
    if not isinstance(item, str) or not item:
        raise ValueError("item must be a nonempty registered name")
    if type(count) is not int or count < 0:
        raise ValueError("count must be a nonnegative integer")
    if not isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be finite and nonnegative")
    if match is not None and not callable(match):
        raise TypeError("match must be callable")
    into_container = direction == "put"
    if not whole_window:
        _check_range(source_slots, _PLAYER_SIZE if into_container else _DOUBLE_CHEST_SIZE, "source_slots")
        _check_range(destination_slots, _DOUBLE_CHEST_SIZE if into_container else _PLAYER_SIZE, "destination_slots")
    if count == 0:
        return RangeTransferResult(0, (), "complete")

    steps = []

    def result(reason):
        return RangeTransferResult(count, tuple(steps), reason)

    def mark_unconfirmed():
        nonlocal overlapped
        overlapped = True

    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if whole_window else None
        window = bot.open_window
        if window is None or window.container_type not in ("CONTAINER", *_UTILITY_BLOCK_LAYOUTS):
            raise RuntimeError("open a supported storage container before transferring items")
        containers, inventory = bot._containers, bot._inventory
        entity = containers._entity_container
        entity_slot_count = None if entity is None else entity.slot_count(window)
        utility_layout = _UTILITY_BLOCK_LAYOUTS.get(window.container_type)
        if window.container_type == "HOPPER" and window.entity_unique_id != -1:
            if entity_slot_count is None:
                raise RuntimeError("the active hopper window must be bound to a hopper minecart")
            if not entity.is_visible():
                return result("window_changed")
            utility_layout = None
        block_slots, player_slots = (
            (destination_slots, source_slots) if into_container else (source_slots, destination_slots)
        )
        container_range_name = "destination_slots" if into_container else "source_slots"

        if utility_layout is not None:
            utility_name, utility_width = utility_layout
            if window.entity_unique_id != -1 or window.position is None or entity is not None:
                raise RuntimeError("the active window must identify a block storage container")
            if not whole_window:
                _check_range(block_slots, utility_width, container_range_name)
            observed = containers._observed_window_block_state(window)
            if observed is None:
                return result("unobserved")
            if observed.name != utility_name:
                raise RuntimeError(f"the active window must match an observed {utility_name} block")
            if utility_name == "minecraft:hopper":
                powered = observed.properties.get("toggle_bit")
                if powered == 0:
                    raise RuntimeError("the block hopper must be powered before transferring items")
                if powered != 1:
                    return result("unobserved")

        def utility_is_current():
            if containers._entity_container is not None:
                return False
            observed = containers._observed_window_block_state(window)
            return (observed is not None and observed.name == utility_name
                    and (utility_name != "minecraft:hopper" or observed.properties.get("toggle_bit") == 1))

        slot_count = containers._observed_window_slot_count(window)
        if whole_window and slot_count is None:
            try:
                await containers.wait_for_slots(timeout=deadline - loop.time(), _expected_window=window)
            except _InventoryMoveNotSubmitted as stopped:
                return result(str(stopped))
            if bot._containers is not containers or bot._inventory is not inventory or containers.window is not window:
                return result("window_changed")
            if utility_layout is not None and not utility_is_current():
                return result("window_changed")
            slot_count = containers._observed_window_slot_count(window)
        tracked_pair = containers._observed_paired_chest_halves(window) is not None
        if tracked_pair and not containers._paired_chest_window_is_current(window):
            return result("window_changed")
        if utility_layout is not None:
            if slot_count is None:
                return result("unobserved")
            if slot_count != utility_width:
                raise RuntimeError(f"the active {utility_name} window has an unsupported slot count")
        elif window.entity_unique_id == -1:
            if window.position is None:
                raise RuntimeError("open an ordinary chest or barrel before transferring items")
            if not whole_window and slot_count == _SINGLE_CONTAINER_SIZE:
                _check_range(block_slots, slot_count, container_range_name)
            block_name = containers._observed_window_block_name(window)
            if block_name is None or slot_count is None:
                return result("unobserved")
            if block_name not in _TRANSFER_BLOCKS:
                raise RuntimeError("the active window must be an ordinary chest or barrel")
            if slot_count != _SINGLE_CONTAINER_SIZE and (block_name != "minecraft:chest"
                                                          or slot_count != _DOUBLE_CHEST_SIZE):
                raise RuntimeError("the active chest or barrel has an unsupported slot count")
        else:
            if entity is not None and (containers._entity_container is not entity or not entity.is_visible()):
                return result("window_changed")
            if entity_slot_count is None:
                raise RuntimeError("open an ordinary chest or barrel, or a bound chest minecart, hopper minecart, or chest boat")
            if not whole_window:
                _check_range(block_slots, entity_slot_count, container_range_name)
            if slot_count is None:
                return result("unobserved")
            if slot_count != entity_slot_count:
                raise RuntimeError("the active entity container has an unsupported slot count")
        if whole_window:
            block_slots, player_slots = range(slot_count), range(_PLAYER_SIZE)
            source_slots, destination_slots = (
                (player_slots, block_slots) if into_container else (block_slots, player_slots)
            )
        layout = containers._layout_token
        # The active double chest can gain a more precise physical pairing
        # without a new window or slot count. Capture each half before matching.
        paired_endpoints = ()
        if match is not None and entity is None and slot_count == _DOUBLE_CHEST_SIZE:
            paired_endpoints = tuple((slot, containers._block_transfer_endpoint(slot))
                                     for slot in (0, _SINGLE_CONTAINER_SIZE))

        def window_is_current():
            return (bot._containers is containers and bot._inventory is inventory
                    and containers.window is window and containers._layout_token is layout
                    and (not tracked_pair or containers._paired_chest_window_is_current(window))
                    and (utility_layout is None or utility_is_current())
                    and (entity is None or (containers._entity_container is entity and entity.is_visible())))

        registry = bot.item_registry
        item_id = None if registry is None else registry.id_for_name(item)
        if not item_id:
            return result("unobserved")

        remaining = count
        limit = STACK_LIMITS.get(item) if bot.protocol.number == 2225 else None
        if deadline is None:
            deadline = loop.time() + timeout
        while remaining:
            if not window_is_current():
                return result("window_changed")
            block = bot.container_slots
            if block is None:
                return result("unobserved")
            player = _known_slots(bot.inventory_slots, player_slots)
            predicted = inventory.client_slots()
            player = {index: stack for index, stack in player.items() if _same_slot(stack, predicted.get(index))}
            block = _known_slots(block, block_slots)
            source, destination = (player, block) if into_container else (block, player)
            plan = _plan_step(source, destination, item_id, remaining, limit, source_slots, destination_slots,
                              match=match, window_is_current=window_is_current)
            if any(containers._block_transfer_endpoint(slot) != endpoint for slot, endpoint in paired_endpoints):
                return result("window_changed")
            if isinstance(plan, str):
                return result(plan)
            origin, target, amount = plan
            time_left = deadline - loop.time()
            if time_left <= 0:
                raise TimeoutError("range transfer timed out before the next step")

            source_stack, destination_stack = source[origin], destination[target]
            source_before = source_stack.stack_size
            destination_before = destination_stack.stack_size
            identity = None if on_confirmed is None else _stack_snapshot(
                destination_stack if destination_before else source_stack,
            )
            container_name = containers._container_slot_name(window, target if into_container else origin)
            step = RangeTransferStep(origin, target, amount, None, None, None)
            steps.append(step)
            overlapped = False

            def record_reply(reply):
                observed = replace(step, request_id=int(reply.client_request_id.raw_id), verdict=reply.result.name)
                steps[-1] = observed
                if observed.verdict != "SUCCESS":
                    steps[-1] = replace(observed, confirmed=0)
                    return None
                if overlapped:
                    return None

                # Receipt counts belong to the request, independently of current slots.
                block_names = (container_name,)
                source_names, destination_names = (
                    (_PLAYER_CONTAINERS, block_names) if into_container else (block_names, _PLAYER_CONTAINERS)
                )
                source_after = _receipt_count(reply, source_names, origin)
                destination_receipt = _receipt_slot(reply, destination_names, target)
                if source_after is None or destination_receipt is None:
                    return None
                moved = source_before - source_after
                if not 0 < moved <= amount or moved != int(destination_receipt.amount) - destination_before:
                    return None
                steps[-1] = replace(observed, confirmed=moved)
                return moved, destination_receipt

            try:
                reply = await containers._transfer(
                    origin, target, amount, into_container=into_container, timeout=time_left,
                    block_container=container_name if entity is None and utility_layout is None else None,
                    _expected_slots=(source_stack, destination_stack),
                    _expected_slot_count=slot_count,
                    _on_unconfirmed=mark_unconfirmed,
                    _range_window_current=(window_is_current if tracked_pair or whole_window or utility_layout is not None
                                           or entity is not None or match is not None or before_write is not None else None),
                    _before_range_write=before_write,
                )
            except _InventoryMoveNotSubmitted as stopped:
                steps.pop()
                return result(str(stopped))
            except (TimeoutError, asyncio.CancelledError, ConnectionError) as error:
                receipt = getattr(error, "_item_stack_response", None)
                if receipt is not None:
                    record_reply(receipt)
                elif (request_id := getattr(error, "_item_stack_request_id", None)) is not None:
                    steps[-1] = replace(step, request_id=request_id)
                raise
            if reply is None:
                # The single-step path declined the conflicting request before sending.
                steps.pop()
                return result("unconfirmed")
            confirmed = record_reply(reply)
            if confirmed is None:
                return result("rejected" if steps[-1].confirmed == 0 else "unconfirmed")
            moved, destination_receipt = confirmed
            if on_confirmed is not None:
                # The matched response establishes identity independently of later slot updates.
                on_confirmed(target, _receipt_stack(identity, destination_receipt))
            remaining -= moved
        return result("complete")
    except (Exception, asyncio.CancelledError) as error:
        error.transfer_progress = result("cancelled" if isinstance(error, asyncio.CancelledError) else "unconfirmed")
        raise


async def transfer_inventory_range(
    bot: Bot,
    *,
    item: str,
    count: int,
    source_slots: range,
    destination_slots: range,
    match: Callable[[HeldItemStack], bool] | None = None,
    timeout: float = 15,
) -> RangeTransferResult:
    """Move a registered item between two ranges in the open player inventory.

    Optional match is a pure synchronous predicate on immutable source stacks
    with the requested item ID. It can run again before planning each step.
    Fill compatible observed stacks first, then use observed empty slots.
    Results count matching server corrections to both endpoints. A predicate
    or wait error carries transfer_progress on the original exception.
    """
    return await _transfer_within(
        bot, item=item, count=count, source_slots=source_slots, destination_slots=destination_slots,
        match=match, timeout=timeout, in_container=False,
    )


async def transfer_container_range(
    bot: Bot,
    *,
    item: str,
    count: int,
    source_slots: range,
    destination_slots: range,
    match: Callable[[HeldItemStack], bool] | None = None,
    timeout: float = 15,
) -> RangeTransferResult:
    """Move a registered item between disjoint ranges in an open storage window.

    Use an observed 27-slot ordinary chest, trapped chest, barrel, vanilla
    shulker box or personal ender chest, or an ordinary double chest with
    both halves observed and paired. Observe the ender chest block before
    opening the window. Use the server's 54-slot order for double chests.
    Fill compatible stacks before empty slots and count only moves
    confirmed by matching corrections to both slots. The optional
    synchronous match and transfer_progress follow transfer_inventory_range.
    """
    return await _transfer_within(
        bot, item=item, count=count, source_slots=source_slots, destination_slots=destination_slots,
        match=match, timeout=timeout, in_container=True,
    )


async def _transfer_within(bot, *, item, count, source_slots, destination_slots, match, timeout, in_container):
    if not isinstance(item, str) or not item:
        raise ValueError("item must be a nonempty registered name")
    if type(count) is not int or count < 0:
        raise ValueError("count must be a nonnegative integer")
    if not isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be finite and nonnegative")
    if match is not None and not callable(match):
        raise TypeError("match must be callable")
    slot_limit = _DOUBLE_CHEST_SIZE if in_container else _PLAYER_SIZE
    _check_range(source_slots, slot_limit, "source_slots")
    _check_range(destination_slots, slot_limit, "destination_slots")
    if (source_slots and destination_slots and source_slots.start < destination_slots.stop
            and destination_slots.start < source_slots.stop):
        if in_container:
            raise ValueError("container transfer ranges must not overlap")
        raise ValueError("inventory transfer ranges must not overlap")
    if not count:
        return RangeTransferResult(0, (), "complete")

    steps = []

    def result(reason):
        return RangeTransferResult(count, tuple(steps), reason)

    def mark_unconfirmed():
        nonlocal overlapped
        overlapped = True

    stop_watching = None
    submitted_reply = None

    def retain_watch_until(reply):
        nonlocal submitted_reply
        submitted_reply = reply

    try:
        inventory, containers = bot._inventory, bot._containers
        window = bot.open_window
        kind = "CONTAINER" if in_container else "INVENTORY"
        if inventory is None or containers is None or window is None or window.container_type != kind:
            target = "a block storage window" if in_container else "the player inventory"
            raise RuntimeError(f"open {target} before transferring items")
        name = domain = None
        paired = False
        if in_container:
            if window.position is None or window.entity_unique_id != -1:
                raise RuntimeError("open a block storage window before transferring items")
            if containers._window_chest_pair_changed or (
                containers._tracks_ender_chest(window) and containers._storage_block_replaced
            ):
                return result("window_changed")
            name = containers._observed_window_block_name(window)
            current_name = containers._current_window_block_name(window)
            slot_count = containers._observed_window_slot_count(window)
            if name is None or slot_count is None or current_name is None:
                return result("unobserved")
            domain = _storage_window_domain(name)
            paired = name == "minecraft:chest" and slot_count == _DOUBLE_CHEST_SIZE
            if (domain is None or (slot_count != _SINGLE_CONTAINER_SIZE and not paired)
                    or (name != "minecraft:chest" and bot.protocol.number != 2225)):
                raise RuntimeError("open a supported storage block with 27 slots or an ordinary double chest")
            _check_range(source_slots, slot_count, "source_slots")
            _check_range(destination_slots, slot_count, "destination_slots")
            if current_name != name or containers._storage_block_replaced:
                return result("window_changed")
            if name != "minecraft:chest" and containers._storage_block_replaced is None:
                return result("unobserved")
            if paired:
                if containers._window_chest_halves is None:
                    return result("unobserved")
                if not containers._paired_chest_window_is_current(window):
                    return result("window_changed")
        layout = containers._layout_token

        target_replaced = False

        def observe_target(current_name):
            nonlocal target_replaced
            if current_name is not None and current_name != name:
                target_replaced = True

        def window_is_current():
            return (bot._inventory is inventory and bot._containers is containers
                    and containers.window is window and containers._layout_token is layout
                    and not target_replaced
                    and (not paired or containers._paired_chest_window_is_current(window))
                    and (not in_container or (not containers._storage_block_replaced
                                              and containers._current_window_block_name(window) == name)))

        registry = bot.item_registry
        item_id = None if registry is None else registry.id_for_name(item)
        if not item_id:
            return result("unobserved")
        if in_container and name != _ENDER_CHEST:
            stop_watching = containers._watch_transfer_block(window.position, observe_target)
        limit = STACK_LIMITS.get(item) if bot.protocol.number == 2225 else None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        remaining = count
        while remaining:
            if not window_is_current():
                return result("window_changed")
            if in_container:
                slots = containers.slots

                def observed(indices):
                    return {index: stack for index, stack in _known_slots(slots, indices).items()
                            if containers._can_plan_container_slot(index)}
            else:
                slots = inventory.slots
                predicted = inventory.client_slots()

                def observed(indices):
                    return {index: stack for index, stack in _known_slots(slots, indices).items()
                            if index not in inventory._unconfirmed_transfer_slots
                            and _same_slot(stack, predicted.get(index))}

            source, destination = observed(source_slots), observed(destination_slots)
            plan = _plan_step(source, destination, item_id, remaining, limit, source_slots, destination_slots,
                              match=match, window_is_current=window_is_current if in_container else None)
            if isinstance(plan, str):
                return result(plan)
            origin, target, amount = plan
            time_left = deadline - loop.time()
            if time_left <= 0:
                raise TimeoutError("range transfer timed out before the next step")

            step = RangeTransferStep(origin, target, amount, None, None, None)
            steps.append(step)
            overlapped = False
            container_confirmed = None

            def confirm_container(moved):
                nonlocal container_confirmed
                container_confirmed = moved

            def record_reply(reply):
                observed = replace(step, request_id=int(reply.client_request_id.raw_id), verdict=reply.result.name,
                                   confirmed=container_confirmed)
                steps[-1] = observed
                if not window_is_current():
                    return "window_changed"
                if observed.verdict != "SUCCESS":
                    steps[-1] = replace(observed, confirmed=0)
                    return "rejected"
                if in_container:
                    return "unconfirmed" if container_confirmed is None else None
                if overlapped:
                    return "unconfirmed"

                source_after = _receipt_count(reply, _PLAYER_CONTAINERS, origin)
                destination_after = _receipt_count(reply, _PLAYER_CONTAINERS, target)
                if source_after is None or destination_after is None:
                    return "unconfirmed"
                moved = source[origin].stack_size - source_after
                if not 0 < moved <= amount or moved != destination_after - destination[target].stack_size:
                    return "unconfirmed"
                steps[-1] = replace(observed, confirmed=moved)
                return None

            try:
                expected = source[origin], destination[target]
                if in_container:
                    reply = await containers._move_within_container(
                        window, origin, target, amount, domain=domain, timeout=time_left, expected=expected,
                        window_is_current=window_is_current, on_submitted=retain_watch_until,
                        on_confirmed=confirm_container,
                    )
                else:
                    reply = await inventory.move_item(
                        origin, target, amount, timeout=time_left, _window_is_current=window_is_current,
                        _expected_slots=tuple(map(_stack_snapshot, expected)), _on_unconfirmed=mark_unconfirmed,
                    )
            except _InventoryMoveNotSubmitted as stopped:
                steps.pop()
                return result(str(stopped))
            except (TimeoutError, asyncio.CancelledError, ConnectionError) as error:
                receipt = getattr(error, "_item_stack_response", None)
                if receipt is not None:
                    record_reply(receipt)
                elif (request_id := getattr(error, "_item_stack_request_id", None)) is not None:
                    steps[-1] = replace(step, request_id=request_id)
                raise
            if reply is None:
                steps.pop()
                return result("unconfirmed")

            stop = record_reply(reply)
            if stop is not None:
                return result(stop)
            remaining -= steps[-1].confirmed
        return result("complete")
    except (Exception, asyncio.CancelledError) as error:
        error.transfer_progress = result("cancelled" if isinstance(error, asyncio.CancelledError) else "unconfirmed")
        raise
    finally:
        if stop_watching is not None:
            if submitted_reply is None or submitted_reply.done():
                stop_watching()
            else:
                # The window also detaches its watches when it closes.
                # A submitted request can still apply a verdict after its caller leaves.
                submitted_reply.add_done_callback(lambda _: stop_watching())
