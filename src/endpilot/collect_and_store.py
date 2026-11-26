"""Collect supplied blocks and store the matching confirmed pickups."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from math import isfinite
from typing import TYPE_CHECKING, Literal

from .collection import BlockCollectionBatch, _confirmed_item_count, _position
from .inventory import _InventoryMoveNotSubmitted
from .range_transfer import (
    RangeTransferResult, _check_range, _DOUBLE_CHEST_SIZE, _PLAYER_SIZE, _TRANSFER_BLOCKS, _transfer_range,
)

if TYPE_CHECKING:
    from .bot import Bot
    from .containers import ContainerWindow
    from .movement import BlockApproach


@dataclass(frozen=True, slots=True)
class CollectAndStoreResult:
    """Task phase, matching pickup count and observed action evidence."""

    phase: Literal["collecting", "approaching", "opening", "storing", "closing", "finished"]
    picked_up_count: int = 0
    collection: BlockCollectionBatch | None = None
    transfer: RangeTransferResult | None = None
    stop_reason: str | None = None
    approach: BlockApproach | None = None

    @property
    def stored_count(self) -> int:
        """Total confirmed by matching changes at the source and destination."""
        return 0 if self.transfer is None else self.transfer.confirmed_count


def _matching_pickups(batch: BlockCollectionBatch, item_id: int, item_name: str) -> int:
    attempts = batch.results if batch.active is None else (*batch.results, batch.active)
    return sum(_confirmed_item_count(attempt, item_id, item_name=item_name) for attempt in attempts)


async def collect_and_store(
    bot: Bot, positions: Iterable[tuple[int, int, int]], *, item: int | str,
    chest: tuple[int, int, int], chest_slots: range | None = None,
    inventory_slots: range = range(_PLAYER_SIZE),
    ticks: int = 160, search_radius: int = 8, timeout: float = 15,
    on_progress: Callable[[CollectAndStoreResult], None] | None = None,
) -> CollectAndStoreResult:
    """Collect the ordered targets, then store the confirmed quantity of one item.

    A matching window already open is borrowed. Otherwise, approach the chest
    and use the face selected on arrival. The approach uses the supplied ticks,
    search_radius and timeout budgets. Omit chest_slots to use the full observed
    window. Each action keeps its own timeout budget.

    The collection prefix is eligible for storage even if a later target fails.
    When storage finishes, an unsuccessful collection status still explains
    the partial task. A storage stop takes precedence if both stages stop short.
    A dimension change preserves confirmed pickups and ends the task before storage.

    The synchronous on_progress callback receives immutable snapshots at phase
    transitions and on normal return. To stop, cancel and await the encompassing
    asyncio task. Once collection starts, an error or cancellation carries
    collect_and_store_progress, including the interrupted action's known
    collection_progress or transfer_progress. Transfer steps with confirmed=None
    may still receive a verdict later.
    """
    chest = _position(chest)
    _check_range(inventory_slots, _PLAYER_SIZE, "inventory_slots")
    if chest_slots is not None:
        _check_range(chest_slots, _DOUBLE_CHEST_SIZE, "chest_slots")
    if not isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be finite and nonnegative")
    if on_progress is not None and not callable(on_progress):
        raise TypeError("on_progress must be callable")

    containers, blocks = bot._containers, bot._blocks
    if containers is None or blocks is None:
        raise RuntimeError("player has not joined")
    registry = bot.item_registry
    if registry is None:
        raise RuntimeError("the session item registry is unavailable")
    item_name = registry.name_for_id(item) if type(item) is int else item
    item_id = registry.id_for_name(item_name) if isinstance(item_name, str) else None
    if not item_id or (type(item) is int and item_id != item):
        raise ValueError("item must be an exact registered name or its transferable network ID")

    epoch = blocks._mining_epoch
    progress = CollectAndStoreResult("collecting")
    owned_window: ContainerWindow | None = None

    def publish(**changes) -> CollectAndStoreResult:
        nonlocal progress
        progress = replace(progress, **changes)
        if on_progress is not None:
            on_progress(progress)
        return progress

    def require_world() -> None:
        if bot._containers is not containers or containers._unsubscribe is None:
            raise ConnectionError("collection session ended before storage completed")
        if blocks._mining_epoch != epoch:
            raise _InventoryMoveNotSubmitted("world_changed")

    def remember_window(window: ContainerWindow) -> None:
        nonlocal owned_window
        owned_window = window

    def target_window(window: ContainerWindow) -> bool:
        return (window.position == chest and window.container_type == "CONTAINER"
                and window.entity_unique_id == -1)

    async def close_owned_window(window: ContainerWindow, failure: BaseException | None) -> None:
        closing = asyncio.create_task(containers.close_window(timeout=timeout, _expected_window=window))
        cancellation = None
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
            closing.result()
        except (Exception, asyncio.CancelledError) as error:
            primary = failure if failure is not None else cancellation
            if primary is None:
                raise
            primary.add_note(f"collection chest close failed: {error!r}")
        if cancellation is not None:
            raise cancellation

    async def store(count: int) -> str:
        nonlocal progress
        require_world()
        window = containers.window
        if window is None:
            chest_block = blocks.observed_block(chest)
            chest_state = None if chest_block is None else blocks._vanilla_state(chest_block.runtime_id)
            if chest_state is None or chest_state.name not in _TRANSFER_BLOCKS:
                return "unobserved"

            def require_same_chest() -> None:
                if blocks.observed_block(chest) != chest_block:
                    raise _InventoryMoveNotSubmitted("chest_changed")

            publish(phase="approaching")
            async with asyncio.timeout(timeout):
                approach = await bot.approach_block(chest, ticks=ticks, search_radius=search_radius)
            progress = replace(progress, approach=approach)
            require_world()
            require_same_chest()
            window = containers.window
            if window is None:
                if approach.status != "reached":
                    return approach.status
                assert approach.face is not None and approach.stand_position is not None

                def before_click() -> None:
                    require_world()
                    require_same_chest()
                    if containers.window is not None or containers._inventory_open is not None or containers._entity_opening:
                        raise _InventoryMoveNotSubmitted("window_busy")
                    movement = containers.movement
                    origin = movement.input_position
                    if origin is None or movement._foot_cell(origin) != approach.stand_position:
                        raise _InventoryMoveNotSubmitted("position_changed")

                publish(phase="opening")
                window = await containers.open(chest, blocks, face=approach.face, timeout=timeout,
                                               _before_click=before_click, _on_observed=remember_window)
        if not target_window(window):
            return "window_busy"

        if progress.phase != "opening":
            publish(phase="opening")
        await containers.wait_for_slots(timeout=timeout, _expected_window=window)
        width = containers._observed_window_slot_count(window)
        if width is None:
            return "unobserved"
        publish(phase="storing")

        def before_write(_request_id: int | None = None) -> None:
            require_world()
            if containers.window is not window:
                raise _InventoryMoveNotSubmitted("window_changed")
            current_registry = bot.item_registry
            if (current_registry is None
                    or (current_registry is not registry and current_registry.id_for_name(item_name) != item_id)):
                raise _InventoryMoveNotSubmitted("item_registry_changed")

        before_write()
        transfer = await _transfer_range(
            bot, direction="put", item=item_name, count=count, source_slots=inventory_slots,
            destination_slots=range(width) if chest_slots is None else chest_slots,
            match=None, timeout=timeout, before_write=before_write,
        )
        publish(phase="closing" if owned_window is not None else "storing", transfer=transfer)
        return transfer.stop_reason

    try:
        publish()
        current = containers.window
        if current is not None and not target_window(current):
            return publish(phase="finished", stop_reason="window_busy")

        collection = await bot.collect_targets(positions, ticks=ticks, search_radius=search_radius, timeout=timeout)
        picked_up = _matching_pickups(collection, item_id, item_name)
        last = collection.results[-1] if collection.results else None
        collection_stop = last.status if last is not None and last.status != "collected" else None
        progress = replace(progress, collection=collection, picked_up_count=picked_up)
        if not picked_up or collection_stop == "dimension_changed":
            return publish(phase="finished", stop_reason=collection_stop or "no_matching_pickups")

        failure = None
        try:
            reason = await store(picked_up)
        except _InventoryMoveNotSubmitted as stopped:
            reason = str(stopped)
        except (Exception, asyncio.CancelledError) as error:
            failure = error
            raise
        finally:
            if owned_window is not None:
                try:
                    await close_owned_window(owned_window, failure)
                except (Exception, asyncio.CancelledError):
                    progress = replace(progress, phase="closing")
                    raise
        if reason == "complete" and collection_stop is not None:
            reason = collection_stop
        return publish(phase="finished", stop_reason=reason)
    except (Exception, asyncio.CancelledError) as error:
        collection = getattr(error, "collection_progress", progress.collection)
        transfer = getattr(error, "transfer_progress", progress.transfer)
        reason = ("cancelled" if isinstance(error, asyncio.CancelledError) else
                  "timeout" if isinstance(error, TimeoutError) else "error")
        error.collect_and_store_progress = replace(
            progress, collection=collection, transfer=transfer, stop_reason=reason,
            picked_up_count=0 if collection is None else _matching_pickups(collection, item_id, item_name),
        )
        raise
