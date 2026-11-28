"""Store one caller-authorized inventory stack during block collection."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from math import dist
from typing import TYPE_CHECKING

from .inventory import _InventoryMoveNotSubmitted, _PLAYER_CONTAINERS
from .range_transfer import (
    RangeTransferResult, _check_range, _DOUBLE_CHEST_SIZE, _PLAYER_SIZE, _receipt_slot, _receipt_stack,
    _SINGLE_CONTAINER_SIZE, _TRANSFER_BLOCKS, _transfer_range,
)

if TYPE_CHECKING:
    from .bot import Bot
    from .containers import ContainerWindow


@dataclass(frozen=True, slots=True)
class CollectionStorage:
    """Authorize one registered item and explicit slots in ordinary chests."""

    position: tuple[int, int, int]
    item: int | str
    inventory_slots: range = field(kw_only=True)
    chest_slots: range = field(kw_only=True)
    additional_positions: tuple[tuple[int, int, int], ...] = field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        from .collection import _position

        object.__setattr__(self, "position", _position(self.position))
        positions = dict.fromkeys(_position(value) for value in self.additional_positions)
        positions.pop(self.position, None)
        object.__setattr__(self, "additional_positions", tuple(positions))
        if not (type(self.item) is int and self.item != 0 or isinstance(self.item, str) and self.item):
            raise ValueError("item must be a nonempty registered name or its transferable network ID")
        for name, limit in (("inventory_slots", _PLAYER_SIZE), ("chest_slots", _DOUBLE_CHEST_SIZE)):
            slots = getattr(self, name)
            _check_range(slots, limit, name)
            if not slots:
                raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True, slots=True)
class CollectionStorageResult:
    """Outcome and confirmed or pending transfer steps for one storage attempt."""

    stop_reason: str
    transfer: RangeTransferResult | None = None


def _registered_storage_item(registry, item: int | str) -> tuple[int, str]:
    if registry is None:
        raise RuntimeError("the session item registry is unavailable")
    item_name = registry.name_for_id(item) if type(item) is int else item
    item_id = registry.id_for_name(item_name) if isinstance(item_name, str) else None
    if not item_id or (type(item) is int and item_id != item):
        raise ValueError("item must be an exact registered name or its transferable network ID")
    return item_id, item_name


def _target_window(window: ContainerWindow, chest: tuple[int, int, int]) -> bool:
    return (window.position == chest and window.container_type == "CONTAINER"
            and window.entity_unique_id == -1)


async def _close_owned_window(containers, window, timeout, failure):
    closing = asyncio.create_task(containers.close_window(timeout=timeout, _expected_window=window))
    cancellation = None
    closed = None
    # Keep the close request and its observer together if the caller stops the task.
    while True:
        try:
            await asyncio.wait((closing,))
            break
        except asyncio.CancelledError as error:
            if failure is None and cancellation is None:
                cancellation = error
            if closing.done():
                break
    try:
        closed = closing.result()
    except (Exception, asyncio.CancelledError) as error:
        primary = failure if failure is not None else cancellation
        if primary is None:
            raise
        primary.add_note(f"collection chest close failed: {error!r}")
    if cancellation is not None:
        raise cancellation
    return closed


async def _store_at_chest(
    bot: Bot, chest: tuple[int, int, int], count: int, *,
    containers, blocks, registry, item_id: int, item_name: str, epoch: int,
    inventory_slots: range, chest_slots: range | None, ticks: int, search_radius: int, timeout: float,
    update=None, dimension_id: int | None = None, authorize: Callable[[int | None], None] | None = None,
    record: Callable[[CollectionStorageResult], None] | None = None, multi_chest: bool = False,
) -> CollectionStorageResult:
    strict = authorize is not None
    owned_window: ContainerWindow | None = None
    chest_block = None
    chest_changed = False
    transfer = None
    close_observed = False
    client_close_observed = False
    closed = None
    unsubscribe_chest = None
    unsubscribe_close = None
    inventory = bot._inventory
    source_slot = inventory_slots.start
    source_before = inventory._observed_stack(source_slot) if strict else None
    last_reply = None

    def require_world() -> None:
        if bot._containers is not containers or containers._unsubscribe is None:
            raise ConnectionError("collection session ended before storage completed")
        if strict and (bot._blocks is not blocks or bot._inventory is not inventory):
            raise ConnectionError("collection session ended before storage completed")
        if blocks._mining_epoch != epoch or (strict and blocks.dimension_id != dimension_id):
            raise _InventoryMoveNotSubmitted("world_changed")

    def opening_pending() -> bool:
        return (containers._inventory_open is not None or containers._entity_opening
                or containers._block_open_requests != 0)

    def foreign_window_pending() -> bool:
        return containers.window is not None or opening_pending()

    def observe_chest_change(_change) -> None:
        nonlocal chest_changed
        chest_changed = True

    def require_same_chest() -> None:
        if chest_changed or blocks.observed_block(chest) != chest_block:
            raise _InventoryMoveNotSubmitted("chest_changed")

    def remember_window(window: ContainerWindow) -> None:
        nonlocal owned_window, unsubscribe_close
        owned_window = window
        if not strict:
            return
        packets = containers.packets
        close_id = packets.protocol.binding.ContainerClosePacket.ID

        def observe_close(raw):
            nonlocal close_observed, client_close_observed
            if containers.window is window:
                return
            # The container subscriber has already applied this packet. The first
            # transition away from this generation decides whether it closed.
            if raw.packet_id == close_id and containers.window is None:
                packet = packets.protocol.decode(raw)
                # Vanilla BDS acknowledges client closes with type NONE.
                close_observed = (int(packet.container_id) == window.container_id
                                  and packet.container_type.name in ("NONE", window.container_type))
                client_close_observed = close_observed and not packet.server_initiated_close
            unsubscribe_close()

        unsubscribe_close = packets.subscribe_raw(observe_close)

    def source_matches(stack) -> bool:
        if last_reply is None:
            return stack == source_before
        # Range transfer only proceeds to another write after its matching receipt.
        receipt = _receipt_slot(last_reply.result(), _PLAYER_CONTAINERS, source_slot)
        return receipt is not None and stack == _receipt_stack(source_before, receipt)

    def publish(**changes) -> None:
        if update is not None:
            update(**changes)

    async def store() -> str:
        nonlocal chest_block, transfer, unsubscribe_chest
        require_world()
        if strict:
            if foreign_window_pending():
                return "window_busy"
            if source_before is None or source_before.item_id != item_id or source_before.count != count:
                return "unobserved"
        window = containers.window
        if window is None:
            chest_block = blocks.observed_block(chest)
            chest_state = None if chest_block is None else blocks._vanilla_state(chest_block.runtime_id)
            allowed_blocks = ("minecraft:chest",) if strict else _TRANSFER_BLOCKS
            if chest_state is None or chest_state.name not in allowed_blocks:
                return "unobserved"
            if strict:
                unsubscribe_chest = blocks.subscribe_block_changes(
                    observe_chest_change, position=chest, layer=0, dimension_id=dimension_id,
                )

            publish(phase="approaching")
            async with asyncio.timeout(timeout):
                approach = await bot.approach_block(chest, ticks=ticks, search_radius=search_radius)
            publish(approach=approach, notify=False)
            require_world()
            require_same_chest()
            if strict and foreign_window_pending():
                return "window_busy"
            window = containers.window
            if window is None:
                if approach.status != "reached":
                    return approach.status
                assert approach.face is not None and approach.stand_position is not None

                def before_click() -> None:
                    require_world()
                    require_same_chest()
                    if (containers.window is not None or containers._inventory_open is not None
                            or containers._entity_opening):
                        raise _InventoryMoveNotSubmitted("window_busy")
                    movement = containers.movement
                    if multi_chest and movement.observed_feet_position is None:
                        raise _InventoryMoveNotSubmitted("unobserved")
                    origin = movement.input_position
                    if origin is None or movement._foot_cell(origin) != approach.stand_position:
                        raise _InventoryMoveNotSubmitted("position_changed")
                    if strict:
                        if not source_matches(inventory._observed_stack(source_slot)):
                            raise _InventoryMoveNotSubmitted("source_changed")
                        authorize(None)

                publish(phase="opening")
                window = await containers.open(chest, blocks, face=approach.face, timeout=timeout,
                                               _before_click=before_click, _on_observed=remember_window)
        if not _target_window(window, chest):
            return "window_busy"
        if window is not owned_window:
            if strict:
                return "window_changed"
            publish(phase="opening")
        await containers.wait_for_slots(timeout=timeout, _expected_window=window)
        width = containers._observed_window_slot_count(window)
        if width is None:
            return "unobserved"
        if strict and (width not in (_SINGLE_CONTAINER_SIZE, _DOUBLE_CHEST_SIZE)
                       or containers._observed_window_block_name(window) != "minecraft:chest"):
            return "unobserved"
        publish(phase="storing")

        def before_write(request_id: int | None = None) -> None:
            nonlocal source_before, last_reply
            require_world()
            if containers.window is not window:
                raise _InventoryMoveNotSubmitted("window_changed")
            current_registry = bot.item_registry
            if (current_registry is None
                    or (current_registry is not registry and current_registry.id_for_name(item_name) != item_id)):
                raise _InventoryMoveNotSubmitted("item_registry_changed")
            if strict:
                require_same_chest()
                if opening_pending():
                    raise _InventoryMoveNotSubmitted("window_busy")
                if request_id is not None:
                    current = inventory._observed_stack(source_slot)
                    if not source_matches(current):
                        raise _InventoryMoveNotSubmitted("source_changed")
                    authorize(request_id)
                    source_before = current
                    last_reply = inventory._pending_requests[request_id].reply

        before_write()
        transfer = await _transfer_range(
            bot, direction="put", item=item_name, count=count, source_slots=inventory_slots,
            destination_slots=range(width) if chest_slots is None else chest_slots,
            match=source_matches if strict else None, timeout=timeout, before_write=before_write,
        )
        publish(phase="closing" if owned_window is not None else "storing", transfer=transfer)
        return transfer.stop_reason

    try:
        failure = None
        try:
            reason = await store()
        except _InventoryMoveNotSubmitted as stopped:
            reason = str(stopped)
        except (Exception, asyncio.CancelledError) as error:
            failure = error
            transfer = getattr(error, "transfer_progress", transfer)
            raise
        finally:
            if owned_window is not None:
                try:
                    if record is not None:
                        record(CollectionStorageResult("closing", transfer))
                finally:
                    try:
                        closed = await _close_owned_window(containers, owned_window, timeout, failure)
                    except (Exception, asyncio.CancelledError):
                        publish(phase="closing", notify=False)
                        raise
        empty_full = (multi_chest and reason == "destination_full"
                      and transfer is not None and not transfer.steps)
        if strict and (reason == "complete" or empty_full):
            try:
                require_world()
                require_same_chest()
            except _InventoryMoveNotSubmitted as stopped:
                reason = str(stopped)
            else:
                if (not close_observed or foreign_window_pending()
                        or (empty_full and (not client_close_observed or closed is not owned_window))):
                    reason = "window_changed"
    except (Exception, asyncio.CancelledError) as error:
        if record is not None:
            reason = ("cancelled" if isinstance(error, asyncio.CancelledError) else
                      "timeout" if isinstance(error, TimeoutError) else "error")
            record(CollectionStorageResult(reason, transfer))
        raise
    finally:
        if unsubscribe_chest is not None:
            unsubscribe_chest()
        if unsubscribe_close is not None:
            unsubscribe_close()
    result = CollectionStorageResult(reason, transfer)
    if record is not None:
        record(result)
    return result


async def _store_for_collection(
    bot: Bot, storage: CollectionStorage, *, source_slot: int, count: int, dimension_id: int, epoch: int,
    ticks: int, search_radius: int, timeout: float, authorize: Callable[[int | None], None],
    record: Callable[[tuple[int, int, int] | None, CollectionStorageResult], None],
) -> CollectionStorageResult:
    """Authorize the click with None and each item-stack write with its request ID."""
    if (type(source_slot) is not int or source_slot not in storage.inventory_slots
            or type(count) is not int or count <= 0):
        raise ValueError("storage requires an authorized source slot and a positive stack count")
    containers, blocks = bot._containers, bot._blocks
    if containers is None or blocks is None or bot._inventory is None:
        raise RuntimeError("player has not joined")
    registry = bot.item_registry
    item_id, item_name = _registered_storage_item(registry, storage.item)
    multiple = bool(storage.additional_positions)

    async def store(chest, time_left):
        return await _store_at_chest(
            bot, chest, count, containers=containers, blocks=blocks, registry=registry,
            item_id=item_id, item_name=item_name, epoch=epoch, inventory_slots=range(source_slot, source_slot + 1),
            chest_slots=storage.chest_slots, ticks=ticks, search_radius=search_radius, timeout=time_left,
            dimension_id=dimension_id, authorize=authorize, multi_chest=multiple,
            record=lambda attempt: record(chest if multiple else None, attempt),
        )

    if not multiple:
        return await store(storage.position, timeout)

    remaining = [storage.position, *storage.additional_positions]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_chest = last_attempt = None

    def stop(reason):
        result = (CollectionStorageResult(reason) if last_attempt is None else replace(last_attempt, stop_reason=reason))
        record(last_chest, result)
        return result

    while remaining:
        time_left = deadline - loop.time()
        if time_left <= 0:
            stop("timeout")
            raise TimeoutError("collection storage timed out before selecting another chest")
        try:
            authorize(None)
        except _InventoryMoveNotSubmitted as stopped:
            return stop(str(stopped))
        feet = containers.movement.observed_feet_position
        if feet is None:
            return stop("unobserved")
        chest = min(remaining, key=lambda position: dist(feet, tuple(axis + 0.5 for axis in position)))
        remaining.remove(chest)
        last_chest, last_attempt = chest, await store(chest, time_left)
        transfer = last_attempt.transfer
        if last_attempt.stop_reason != "destination_full" or transfer is None or transfer.steps:
            break
    return last_attempt
