"""Collect supplied blocks and store the matching confirmed pickups."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from math import isfinite
from typing import TYPE_CHECKING, Literal

from .collection import BlockCollectionBatch, _confirmed_item_count, _position
from .collection_storage import _registered_storage_item, _store_at_chest, _target_window
from .range_transfer import RangeTransferResult, _check_range, _DOUBLE_CHEST_SIZE, _PLAYER_SIZE

if TYPE_CHECKING:
    from .bot import Bot
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
    item_id, item_name = _registered_storage_item(registry, item)

    epoch = blocks._mining_epoch
    progress = CollectAndStoreResult("collecting")

    def publish(*, notify=True, **changes) -> CollectAndStoreResult:
        nonlocal progress
        progress = replace(progress, **changes)
        if notify and on_progress is not None:
            on_progress(progress)
        return progress

    try:
        publish()
        current = containers.window
        if current is not None and not _target_window(current, chest):
            return publish(phase="finished", stop_reason="window_busy")

        collection = await bot.collect_targets(positions, ticks=ticks, search_radius=search_radius, timeout=timeout)
        picked_up = _matching_pickups(collection, item_id, item_name)
        last = collection.results[-1] if collection.results else None
        collection_stop = last.status if last is not None and last.status != "collected" else None
        progress = replace(progress, collection=collection, picked_up_count=picked_up)
        if not picked_up or collection_stop == "dimension_changed":
            return publish(phase="finished", stop_reason=collection_stop or "no_matching_pickups")

        storage = await _store_at_chest(
            bot, chest, picked_up, containers=containers, blocks=blocks, registry=registry,
            item_id=item_id, item_name=item_name, epoch=epoch, inventory_slots=inventory_slots,
            chest_slots=chest_slots, ticks=ticks, search_radius=search_radius, timeout=timeout,
            update=publish,
        )
        reason = storage.stop_reason
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
