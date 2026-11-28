"""Collect supplied blocks and drops using their observed world and pickup evidence."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
import math
from typing import TYPE_CHECKING

from ._stack_limits_2225 import STACK_LIMITS
from .blocks import BlockObservation, BlockSearchResult, Blocks, MiningFaceUnavailable, _ORDINARY_MINING_REACH
from .collection_storage import (
    CollectionStorage, CollectionStorageResult, _registered_storage_items, _store_for_collection,
)
from .entities import Entities, EntityState, ItemPickup
from .harvest import HarvestTool, HarvestToolAcquisition, HarvestToolChest, HarvestTools
from .inventory import _InventoryMoveNotSubmitted, _MAIN_INVENTORY_SIZE, _stack_snapshot
from .inventory_observation import _InventoryObservation
from .movement import BlockApproach, Movement, _PLAYER_HALF_WIDTH
from .navigation import AllGoal, BlockFaceGoal, BlockInteractionGoal, BlockInteractionMatch, NavigationRules

if TYPE_CHECKING:
    from .bot import Bot


_ToolRefill = Callable[[HarvestToolChest, BlockObservation], Awaitable[HarvestToolAcquisition | None]]


@dataclass(frozen=True, slots=True)
class BlockCollection:
    """Evidence received during one collection attempt.

    Drops are new item actors first reported inside the target block. Their
    source may be indistinguishable on this stream. In counted collection,
    the collector and inventory delta normally refer to the unique requested
    candidate. After timeout, they may describe a confirmed pickup of another
    item instead. pickup_drop identifies the confirmed actor. An unknown delta
    is None. Ordered refills record each chest and its available
    acquisition evidence in tool_refill_attempts. An opted-in storage trip
    records its last transfer and stopping reason in storage_attempt. A
    multi-chest trip also records each attempted chest in storage_attempts.
    """

    position: tuple[int, int, int]
    status: str
    block_before: BlockObservation | None
    block_after: BlockObservation | None = None
    drops: tuple[EntityState, ...] = ()
    collector_id: int | None = None
    inventory_delta: int | None = None
    timed_out: bool = False
    approach: BlockApproach | None = None
    tool: HarvestTool | None = None
    tool_acquisition: HarvestToolAcquisition | None = field(default=None, kw_only=True)
    tool_refill_attempts: tuple[tuple[HarvestToolChest, HarvestToolAcquisition | None], ...] = field(
        default=(), kw_only=True,
    )
    storage_attempt: CollectionStorageResult | None = field(default=None, kw_only=True)
    storage_attempts: tuple[tuple[tuple[int, int, int], CollectionStorageResult], ...] = field(
        default=(), kw_only=True,
    )
    pickup_drop: EntityState | None = field(default=None, kw_only=True)
    _pickup_confirmed: bool = field(default=False, kw_only=True, repr=False)

    @property
    def pickup_confirmed(self) -> bool:
        """Whether the bot's pickup and matching main-inventory gain were confirmed."""
        return self.status == "collected" or self._pickup_confirmed


_StoreForDrop = Callable[
    ["_CollectionObservation", float, Callable[[BlockCollection], None] | None], Awaitable[None],
]


@dataclass(frozen=True, slots=True)
class BlockCollectionBatch:
    """Ordered admitted positions and evidence collected before the batch ended.

    Results are the attempted prefix. An exception's collection_progress can
    also carry evidence from the interrupted target in active.
    """

    dimension_id: int
    positions: tuple[tuple[int, int, int], ...]
    results: tuple[BlockCollection, ...]
    active: BlockCollection | None = None


class CollectionTargetQueue:
    """Append coordinates to one active collect_targets call on its event loop."""

    def __init__(self) -> None:
        self._used = False
        self._admit: Callable[[Iterable[tuple[int, int, int]]], tuple[tuple[int, int, int], ...]] | None = None

    def extend(self, positions: Iterable[tuple[int, int, int]]) -> tuple[tuple[int, int, int], ...]:
        """Validate positions and return the new targets admitted in input order."""
        admit = self._admit
        if admit is None:
            raise RuntimeError("collection target queue is not open")
        return admit(positions)

    def _require_unused(self) -> None:
        if self._used:
            raise RuntimeError("collection target queue has already been used")

    def _bind(self) -> None:
        self._require_unused()
        self._used = True

    def _seal(self) -> None:
        self._admit = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CountedBlockCollection(BlockCollectionBatch):
    """Admitted candidates and confirmed main-inventory gains for one item ID.

    search_complete records the admission-time completeness of a vein search.
    Radius candidates are ordered by admission round, then by search order.
    """

    item_id: int
    requested_count: int
    collected_count: int
    search_complete: bool | None = None


@dataclass(frozen=True, slots=True)
class DropCollection:
    """Outcome for one selected item actor, with its matching server pickup if observed."""

    target: EntityState
    status: str
    pickup: ItemPickup | None = None


_Target = tuple[int, int, int] | EntityState
_Collection = BlockCollection | DropCollection


@dataclass(frozen=True, slots=True)
class MixedCollectionBatch:
    """Ordered targets and the attempted prefix of a mixed collection."""

    dimension_id: int
    targets: tuple[_Target, ...]
    results: tuple[_Collection, ...]
    active: _Collection | None = None


class _BatchProgress:
    def __init__(self, dimension_id, positions):
        self.dimension_id = dimension_id
        self.positions = positions
        self.results: list[BlockCollection] = []
        self.active: BlockCollection | None = None

    def snapshot(self) -> BlockCollectionBatch:
        return BlockCollectionBatch(self.dimension_id, self.positions, tuple(self.results), self.active)


class _CountedProgress(_BatchProgress):
    def __init__(self, dimension_id: int, item_id: int, requested_count: int):
        super().__init__(dimension_id, ())
        self.item_id = item_id
        self.requested_count = requested_count
        self.collected_count = 0
        self.search_complete: bool | None = None

    def snapshot(self) -> CountedBlockCollection:
        active_count = 0 if self.active is None else _confirmed_item_count(self.active, self.item_id)
        return CountedBlockCollection(
            self.dimension_id, self.positions, tuple(self.results), self.active,
            item_id=self.item_id, requested_count=self.requested_count,
            collected_count=self.collected_count + active_count,
            search_complete=self.search_complete,
        )


class _MixedProgress:
    def __init__(self, dimension_id, targets):
        self.dimension_id = dimension_id
        self.targets = targets
        self.results: list[_Collection] = []
        self.active: _Collection | None = None

    def snapshot(self) -> MixedCollectionBatch:
        return MixedCollectionBatch(self.dimension_id, self.targets, tuple(self.results), self.active)


def _position(values) -> tuple[int, int, int]:
    position = tuple(values)
    if len(position) != 3 or any(not isinstance(value, int) for value in position):
        raise ValueError("expected three integer block coordinates")
    return position


def _snapshot_tool_chests(
    tool_chest: HarvestToolChest | None, tool_chests: Sequence[HarvestToolChest] | None,
) -> tuple[HarvestToolChest, ...] | None:
    if tool_chest is not None and not isinstance(tool_chest, HarvestToolChest):
        raise TypeError("tool_chest must be a HarvestToolChest")
    if tool_chests is None:
        return None
    if tool_chest is not None:
        raise ValueError("tool_chest and tool_chests are mutually exclusive")
    if not isinstance(tool_chests, Sequence) or isinstance(tool_chests, (str, bytes, bytearray)):
        raise TypeError("tool_chests must be a sequence of HarvestToolChest")
    snapshot = tuple(tool_chests)
    if any(not isinstance(chest, HarvestToolChest) for chest in snapshot):
        raise TypeError("tool_chests must contain only HarvestToolChest entries")
    return snapshot


def _settled_refill_miss(acquisition: HarvestToolAcquisition | None) -> bool:
    if acquisition is None or acquisition.transfer is not None:
        return False
    # A collection refill opens its own window. Harvest preserves this status
    # only after the owned window's matching close completes normally.
    return ((acquisition.status == "tool_unavailable" and acquisition.window is not None)
            or (acquisition.status == "chest_approach_unreachable" and acquisition.window is None))


def _storage_items(bot: "Bot", storage: CollectionStorage | None, timeout: float) -> dict[int, str] | None:
    """Validate the entire storage authorization and return exact names by network ID."""
    if storage is None:
        return None
    if not isinstance(storage, CollectionStorage):
        raise TypeError("storage must be a CollectionStorage")
    if timeout is None or not 0 < timeout < math.inf:
        raise ValueError("collection timeout must be finite and positive when storage is enabled")
    return _registered_storage_items(bot.item_registry, storage.item)


def _confirmed_item_count(result: BlockCollection, item_id: int, *, item_name: str | None = None) -> int:
    if not result.pickup_confirmed:
        return 0
    drop = result.pickup_drop
    if drop is None:
        if len(result.drops) != 1:
            return 0
        drop, = result.drops
    if drop.item_network_id != item_id or (item_name is not None and drop.item_id != item_name):
        return 0
    return drop.item_count or 0


class _BlockCollector:
    def __init__(self, blocks: Blocks, movement: Movement, entities: Entities, harvest: HarvestTools):
        self.blocks = blocks
        self.movement = movement
        self.entities = entities
        self.harvest = harvest
        self._lock = asyncio.Lock()
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def _check_connection(self) -> None:
        if self.movement._server_disconnected:
            raise ConnectionError("server disconnected while collecting a block")
        if self._closed:
            raise ConnectionError("block collection session is closed")

    async def collect(
        self, position: tuple[int, int, int], *, ticks: int, search_radius: int, timeout: float,
    ) -> BlockCollection:
        self._check_connection()
        return await self._run(self._collect(position, ticks=ticks, search_radius=search_radius, timeout=timeout))

    async def collect_targets(
        self, bot: "Bot", positions: Iterable[tuple[int, int, int]], *, count: int | None = None,
        ticks: int = 160, search_radius: int = 8, timeout: float = 15, tool_chest: HarvestToolChest | None = None,
        tool_chests: Sequence[HarvestToolChest] | None = None, storage: CollectionStorage | None = None,
        requested_drop_id: int | None = None, additions: CollectionTargetQueue | None = None,
    ) -> BlockCollectionBatch:
        if additions is not None:
            if not isinstance(additions, CollectionTargetQueue):
                raise TypeError("additions must be a CollectionTargetQueue")
            additions._require_unused()
        if count is not None and (not isinstance(count, int) or count < 0):
            raise ValueError("collection count must be a nonnegative integer")
        tool_chests = _snapshot_tool_chests(tool_chest, tool_chests)
        storage_items = _storage_items(bot, storage, timeout)
        positions = tuple(dict.fromkeys(map(_position, positions)))[:count]
        progress = _BatchProgress(self.blocks.dimension_id, positions)
        seal = None
        try:
            self._check_connection()
            if additions is not None:
                additions._bind()
                seal = additions._seal
            if not positions:
                return progress.snapshot()

            epoch = self.blocks._mining_epoch
            before = tuple(self.blocks.observed_block(position) for position in positions)
            targets = zip(positions, before, strict=True)
            if additions is not None:
                pending = list(targets)
                admitted = set(positions)
                owner = asyncio.current_task()
                loop = asyncio.get_running_loop()
                if owner is None:
                    raise RuntimeError("a collection target queue requires an asyncio task")
                initial_cancellations = owner.cancelling()
                if initial_cancellations:
                    # Deliver pending cancellation before admitting work. Handled cancellations can leave a count.
                    await asyncio.sleep(0)
                    initial_cancellations = owner.cancelling()
                    self._check_connection()
                    self.blocks._check_mining_epoch(epoch)

                def require_active():
                    if asyncio.get_running_loop() is not loop:
                        raise RuntimeError("collection target queue belongs to another event loop")
                    if owner.cancelling() > initial_cancellations or owner.done():
                        additions._seal()
                        raise RuntimeError("collection target queue is not open")
                    try:
                        self._check_connection()
                        self.blocks._check_mining_epoch(epoch)
                    except (ConnectionError, RuntimeError) as error:
                        additions._seal()
                        raise RuntimeError("collection target queue is no longer active") from error

                def admit(extra):
                    require_active()
                    supplied = tuple(dict.fromkeys(map(_position, extra)))
                    require_active()
                    remaining = None if count is None else count - len(admitted)
                    accepted = tuple(position for position in supplied if position not in admitted)[:remaining]
                    snapshots = [(position, self.blocks.observed_block(position)) for position in accepted]
                    pending.extend(snapshots)
                    admitted.update(accepted)
                    progress.positions += accepted
                    return accepted

                additions._admit = admit
                targets = pending

            async def collect_batch():
                try:
                    await self._collect_batch(
                        bot, progress, targets, epoch, ticks=ticks, search_radius=search_radius, timeout=timeout,
                        tool_chest=tool_chest, tool_chests=tool_chests, storage=storage, storage_items=storage_items,
                        requested_drop_id=requested_drop_id,
                    )
                finally:
                    if seal is not None:
                        seal()

            await self._run(collect_batch(), before_cleanup=seal)
        except (Exception, asyncio.CancelledError) as error:
            error.collection_progress = progress.snapshot()
            raise
        finally:
            if seal is not None:
                seal()
        return progress.snapshot()

    async def collect_by_count(
        self, bot: "Bot", names: str | int | Iterable[str] | Iterable[int], *, item_id: int, count: int,
        radius: int = 16, refresh_candidates: bool = False, ticks: int = 160, search_radius: int = 8, timeout: float = 15,
        tool_chest: HarvestToolChest | None = None, tool_chests: Sequence[HarvestToolChest] | None = None,
        storage: CollectionStorage | None = None,
    ) -> CountedBlockCollection:
        center = None

        def find_candidates() -> BlockSearchResult:
            nonlocal center, names
            if refresh_candidates and names is not None and not isinstance(names, (str, int, tuple)):
                names = tuple(names)
            search = self.blocks.find_blocks(names, radius=radius, limit=None, center=center)
            center = search.center
            return search

        return await self._collect_counted(
            bot, find_candidates, refresh_candidates=refresh_candidates,
            item_id=item_id, count=count, ticks=ticks, search_radius=search_radius, timeout=timeout,
            tool_chest=tool_chest, tool_chests=tool_chests, storage=storage,
        )

    async def collect_vein_by_count(
        self, bot: "Bot", seed: tuple[int, int, int], *, item_id: int, count: int,
        max_blocks: int, max_distance: int, flood_radius: int, ticks: int, search_radius: int, timeout: float,
        tool_chest: HarvestToolChest | None = None, tool_chests: Sequence[HarvestToolChest] | None = None,
        storage: CollectionStorage | None = None,
    ) -> CountedBlockCollection:
        return await self._collect_counted(
            bot, partial(self.blocks.find_block_vein, seed, max_blocks=max_blocks, max_distance=max_distance,
                         flood_radius=flood_radius),
            item_id=item_id, count=count, ticks=ticks, search_radius=search_radius, timeout=timeout,
            tool_chest=tool_chest, tool_chests=tool_chests, storage=storage, report_completeness=True,
        )

    async def _collect_counted(
        self, bot: "Bot", find_candidates: Callable[[], BlockSearchResult], *, item_id: int, count: int,
        ticks: int, search_radius: int, timeout: float, tool_chest: HarvestToolChest | None,
        tool_chests: Sequence[HarvestToolChest] | None, storage: CollectionStorage | None,
        report_completeness: bool = False, refresh_candidates: bool = False,
    ) -> CountedBlockCollection:
        if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id == 0:
            raise ValueError("collection item ID must be a nonzero integer")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise ValueError("collection count must be a positive integer")
        tool_chests = _snapshot_tool_chests(tool_chest, tool_chests)
        storage_items = _storage_items(bot, storage, timeout)
        progress = _CountedProgress(self.blocks.dimension_id, item_id, count)
        seen: set[tuple[int, int, int]] = set()

        def admit(search: BlockSearchResult) -> list[BlockObservation]:
            observations = []
            for match in search.matches:
                if match.position in seen:
                    continue
                before = self.blocks.observed_block(match.position)
                if before is not None and before.is_air is False:
                    seen.add(match.position)
                    observations.append(before)
            progress.positions += tuple(before.position for before in observations)
            return observations

        try:
            self._check_connection()
            epoch = self.blocks._mining_epoch
            search = find_candidates()
            if report_completeness:
                progress.search_complete = search.complete
            observations = admit(search)
            if observations:
                def targets() -> Iterator[tuple[tuple[int, int, int], BlockObservation]]:
                    batch = observations
                    while batch:
                        for before in batch:
                            yield before.position, before
                        if not refresh_candidates:
                            return
                        # The batch loop requests another target only after normal exhaustion.
                        self._check_connection()
                        self.blocks._check_mining_epoch(epoch)
                        batch = admit(find_candidates())

                await self._run(self._collect_batch(
                    bot, progress, targets(), epoch, ticks=ticks, search_radius=search_radius,
                    timeout=timeout, tool_chest=tool_chest, tool_chests=tool_chests,
                    storage=storage, storage_items=storage_items, requested_drop_id=item_id,
                    refresh_candidates=refresh_candidates,
                ))
        except (Exception, asyncio.CancelledError) as error:
            error.collection_progress = progress.snapshot()
            raise
        return progress.snapshot()

    async def collect_mixed_targets(
        self, targets: Iterable[_Target], *, count: int | None = None, ticks: int = 160,
        search_radius: int = 8, timeout: float = 15,
    ) -> MixedCollectionBatch:
        if count is not None and (not isinstance(count, int) or count < 0):
            raise ValueError("collection count must be a nonnegative integer")
        distinct = {}
        for target in targets:
            if isinstance(target, EntityState):
                key = (target.runtime_id, target.entity_id)
            else:
                key = target = _position(target)
            distinct.setdefault(key, target)
        targets = tuple(distinct.values())[:count]
        progress = _MixedProgress(self.blocks.dimension_id, targets)
        try:
            self._check_connection()
            if not targets:
                return progress.snapshot()
            epoch = self.blocks._mining_epoch
            selected = {}
            before = {}
            for target in targets:
                if isinstance(target, EntityState):
                    instance = self.entities._actor_instance(target.runtime_id)
                    if (target.actor_type == "minecraft:item" and target.entity_id and instance is not None
                            and self.entities._bound_item(target, instance) is not None):
                        selected[target.runtime_id] = (target, instance)
                else:
                    before[target] = self.blocks.observed_block(target)
            return await self._run(self._collect_mixed_batch(
                progress, before, selected, epoch, ticks=ticks, search_radius=search_radius, timeout=timeout,
            ))
        except (Exception, asyncio.CancelledError) as error:
            error.collection_progress = progress.snapshot()
            raise

    async def _run[T](self, operation: Coroutine[None, None, T], *,
                      before_cleanup: Callable[[], None] | None = None) -> T:
        # A kick can leave the packet reader running while callers wait for the lock.
        async def watch_disconnect():
            self._check_connection()
            packets = self.blocks.packets
            await packets.wait_for_packet(packets.protocol.binding.DisconnectPacket.ID, timeout=None)
            raise ConnectionError("server disconnected while collecting a block")

        watcher = asyncio.create_task(watch_disconnect())
        action = asyncio.create_task(operation)
        try:
            done, _ = await asyncio.wait((watcher, action), return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                await watcher
            self._check_connection()
            return await action
        finally:
            if before_cleanup is not None:
                before_cleanup()
            for task in (action, watcher):
                if not task.done():
                    task.cancel()
            await asyncio.gather(action, watcher, return_exceptions=True)

    async def _collect(
        self, position: tuple[int, int, int], *, ticks: int, search_radius: int, timeout: float,
    ) -> BlockCollection:
        async with self._lock:
            self._check_connection()
            return await self._collect_one(
                position, self.blocks.observed_block(position), self.blocks.dimension_id,
                ticks=ticks, search_radius=search_radius, timeout=timeout,
            )

    async def _collect_batch(
        self, bot: "Bot", progress: _BatchProgress,
        targets: Iterable[tuple[tuple[int, int, int], BlockObservation | None]], epoch: int, *,
        ticks: int, search_radius: int, timeout: float, tool_chest: HarvestToolChest | None,
        tool_chests: tuple[HarvestToolChest, ...] | None, storage: CollectionStorage | None,
        storage_items: dict[int, str] | None, requested_drop_id: int | None, refresh_candidates: bool = False,
    ) -> None:
        counted = progress if isinstance(progress, _CountedProgress) else None
        ordered_refill = tool_chests is not None
        chests = tool_chests if tool_chests is not None else (tool_chest,) if tool_chest is not None else ()
        refill = None if not chests else partial(
            self._refill_tool, bot, dimension=progress.dimension_id, epoch=epoch,
            ticks=ticks, search_radius=search_radius, timeout=timeout, ordered=ordered_refill,
            requested_drop_id=requested_drop_id, refresh_candidates=refresh_candidates,
        )
        store = None if storage is None else partial(
            self._store_for_drop, bot, storage, storage_items,
            dimension=progress.dimension_id, ticks=ticks, search_radius=search_radius,
        )
        preflight = None if storage is None or not storage.before_dig_when_full else storage
        async with self._lock:
            settle_before_approach = False
            for position, before in targets:
                self._check_connection()
                self.blocks._check_mining_epoch(epoch)
                result = await self._collect_one(
                    position, before, progress.dimension_id, ticks=ticks, search_radius=search_radius,
                    timeout=timeout, epoch=epoch, progress=progress, refill=refill, refill_chests=chests,
                    ordered_refill=ordered_refill, store=store, settle_before_approach=settle_before_approach,
                    requested_drop_id=requested_drop_id, refresh_candidates=refresh_candidates,
                    preflight=preflight, bot=bot, storage_items=storage_items,
                )
                progress.results.append(result)
                progress.active = None
                if result.approach is not None or result.status != "target_changed":
                    settle_before_approach = (result.status == "collected" and result.storage_attempt is not None
                                              and result.storage_attempt.stop_reason == "complete")
                if counted is None:
                    if result.status != "collected":
                        break
                    continue
                received = _confirmed_item_count(result, counted.item_id)
                counted.collected_count += received
                if result.status == "target_changed":
                    acquisition = result.tool_acquisition
                    refill_attempted = result.tool_refill_attempts or (refresh_candidates and acquisition is not None)
                    if refill_attempted and not (
                        _settled_refill_miss(acquisition) or (acquisition is not None and acquisition.status == "equipped")
                    ):
                        break
                    self.blocks._check_mining_epoch(epoch)
                    continue
                if (result.status != "collected" or result.timed_out or received == 0
                        or counted.collected_count >= counted.requested_count):
                    break

    async def _collect_mixed_batch(
        self, progress: _MixedProgress, before, selected, epoch: int, *, ticks: int, search_radius: int, timeout: float,
    ) -> MixedCollectionBatch:
        async with self._lock:
            self._check_connection()
            self.blocks._check_mining_epoch(epoch)
            drops = _SelectedDrops(self.blocks, self.entities, selected, epoch, self.movement.player_id)
            unsubscribe = self.blocks.packets.subscribe_raw(drops.observe) if selected else None
            routed_pickup = False

            def mark_routed():
                nonlocal routed_pickup
                routed_pickup = True

            try:
                for target in progress.targets:
                    self._check_connection()
                    self.blocks._check_mining_epoch(epoch)
                    if isinstance(target, EntityState):
                        result = await self._collect_selected_drop(
                            target, drops, progress, ticks=ticks, search_radius=search_radius, timeout=timeout,
                            on_route=mark_routed,
                        )
                        success = result.status == "picked_up"
                    else:
                        result = await self._collect_one(
                            target, before[target], progress.dimension_id, ticks=ticks, search_radius=search_radius,
                            timeout=timeout, epoch=epoch, progress=progress, settle_before_approach=routed_pickup,
                        )
                        routed_pickup = False
                        success = result.status == "collected"
                    progress.results.append(result)
                    progress.active = None
                    if not success:
                        break
                return progress.snapshot()
            finally:
                if unsubscribe is not None:
                    unsubscribe()

    async def _collect_selected_drop(
        self, target: EntityState, drops: "_SelectedDrops", progress: _MixedProgress, *, ticks: int,
        search_radius: int, timeout: float, on_route: Callable[[], None],
    ) -> DropCollection:
        result = drops.result(target)
        if result is not None:
            return result
        _, instance = drops.selected[target.runtime_id]
        progress.active = DropCollection(target, "pickup_unconfirmed")

        def resolve(runtime_id):
            if runtime_id == target.runtime_id and self.blocks._mining_epoch == drops.epoch:
                return self.entities._bound_item(target, instance)
            return None

        deadline = asyncio.timeout(timeout)
        follow = asyncio.create_task(self.movement._approach_drop(
            self.blocks, self.entities, target, resolve, ticks=ticks, search_radius=search_radius,
            deadline=deadline.when(), on_route=on_route,
        ))
        follow.add_done_callback(lambda _: drops.changed.set())
        try:
            async with deadline:
                while True:
                    self._check_connection()
                    self.blocks._check_mining_epoch(drops.epoch)
                    if follow.done():
                        await follow
                    result = drops.result(target)
                    if result is not None:
                        return result
                    drops.changed.clear()
                    await drops.changed.wait()
        except TimeoutError:
            if not deadline.expired():
                raise
            return drops.result(target) or DropCollection(target, "pickup_unconfirmed")
        finally:
            progress.active = drops.result(target) or DropCollection(target, "pickup_unconfirmed")
            if not follow.done():
                follow.cancel()
            await asyncio.gather(follow, return_exceptions=True)

    async def _approach(self, position, *, ticks, search_radius, timeout, settle_epoch: int | None = None):
        if not (0 <= ticks < math.inf and 0 <= search_radius < math.inf):
            raise ValueError("ticks and search radii must be finite and nonnegative")
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        visible = BlockInteractionGoal(position, max_distance=_ORDINARY_MINING_REACH)
        adjacent = AllGoal((BlockFaceGoal(position), visible))
        rules = NavigationRules(search_radius=int(search_radius))
        route = await self.movement._navigate_goal(
            self.blocks, visible, rules, ticks, deadline=deadline, preferred_goal=adjacent, settle_epoch=settle_epoch,
        )
        status = {"unknown": "unobserved", "budget": "budget_exhausted"}.get(route.status, route.status)
        match = next((face for face in route.matches if isinstance(face, BlockInteractionMatch)), None)
        return BlockApproach(status, route.frames, route.stand_position, match.face if match else None), match

    async def _refill_tool(
        self, bot: "Bot", chest: HarvestToolChest, target: BlockObservation, *, dimension: int, epoch: int,
        ticks: int, search_radius: int, timeout: float, ordered: bool, requested_drop_id: int | None = None,
        refresh_candidates: bool = False,
    ) -> HarvestToolAcquisition | None:
        chest_block = self.harvest._refill_chest(target, chest.position, requested_drop_id=requested_drop_id)
        if chest_block is None:
            return None
        if (busy := self.harvest._window_busy()) is not None:
            return HarvestToolAcquisition(busy)
        goal = BlockInteractionGoal(chest.position, max_distance=_ORDINARY_MINING_REACH)
        rules = NavigationRules(search_radius=int(search_radius))
        async with asyncio.timeout(timeout):
            route = await self.movement._navigate_goal(self.blocks, goal, rules, ticks)
        self._check_connection()
        if self.blocks.dimension_id != dimension:
            return None
        self.blocks._check_mining_epoch(epoch)
        status = {"unknown": "unobserved", "budget": "budget_exhausted"}.get(route.status, route.status)
        if refresh_candidates and route.status not in ("reached", "unreachable"):
            return HarvestToolAcquisition(f"chest_approach_{status}")
        if self.blocks.observed_block(target.position) != target:
            return None
        if (busy := self.harvest._window_busy()) is not None:
            return HarvestToolAcquisition(busy)
        if route.status != "reached":
            if (ordered and route.status == "unreachable"
                    and self.harvest._refill_chest(
                        target, chest.position, requested_drop_id=requested_drop_id,
                    ) != chest_block):
                return None
            return HarvestToolAcquisition(f"chest_approach_{status}")
        match, = route.matches
        assert isinstance(match, BlockInteractionMatch) and match.position == chest.position
        unsubscribe = None
        window_replaced = False

        def track_owned_window(owned):
            nonlocal unsubscribe
            packets = self.blocks.packets
            close_id = packets.protocol.binding.ContainerClosePacket.ID

            def observe_window(raw):
                nonlocal window_replaced
                window = self.harvest._containers.window
                if window is not None and window is not owned:
                    window_replaced = True
                elif window is None and raw.packet_id == close_id:
                    if int(packets.protocol.decode(raw).container_id) == owned.container_id:
                        stop_observing()

            # The close packet matches by ID. A replacement can reuse that ID
            # and disappear before the acquisition returns.
            stop_observing = packets.subscribe_raw(observe_window)
            unsubscribe = stop_observing

        try:
            acquisition = await self.harvest._equip_from_chest(
                bot, target.position, chest=chest.position, chest_slots=chest.chest_slots,
                inventory_slots=chest.inventory_slots, face=match.face, click_pos=match.click_pos, timeout=timeout,
                expected_target=target, expected_chest=chest_block, expected_epoch=epoch,
                on_owned_window=track_owned_window if ordered else None,
                requested_drop_id=requested_drop_id,
            )
        finally:
            if unsubscribe is not None:
                unsubscribe()
        if window_replaced and acquisition.status in ("equipped", "tool_unavailable"):
            return replace(acquisition, status="window_changed")
        return acquisition

    def _pickup_is_blocked(self, slots, drop: EntityState, *, submitting: int | None = None) -> bool:
        if not self.harvest._inventory_is_observed(slots, submitting=submitting):
            return False
        main = (slots[index] for index in range(_MAIN_INVENTORY_SIZE))
        matching = []
        for stack in main:
            if not stack.id:
                return False
            if stack.id == drop.item_network_id:
                matching.append(stack)
        if not matching:
            return True
        limit = STACK_LIMITS.get(drop.item_id) if self.blocks.packets.protocol.number == 2225 else None
        return limit is not None and all(stack.stack_size == limit for stack in matching)

    def _preflight_inventory_is_observed(self, slots, *, submitting: int | None = None) -> bool:
        return (not self.blocks.inventory._main_inventory_has_count_gaps()
                and self.harvest._inventory_is_observed(slots, submitting=submitting))

    async def _store_before_dig(
        self, bot: "Bot", storage: CollectionStorage, items: dict[int, str], *, epoch: int, dimension: int, ticks: int,
        search_radius: int, timeout: float, check_target, record,
    ) -> bool:
        inventory = self.blocks.inventory
        slots = inventory.slots
        if not self._preflight_inventory_is_observed(slots):
            record(None, CollectionStorageResult("unobserved"), new_visit=False)
            return False
        if any(not slots[index].id for index in range(_MAIN_INVENTORY_SIZE)):
            return True

        expected = {index: _stack_snapshot(slots[index]) for index in storage.inventory_slots}
        sources = [index for index, stack in expected.items() if stack.item_id in items]
        if not sources:
            record(None, CollectionStorageResult("source_exhausted"), new_visit=False)
            return False

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        cleared = set()

        for source_slot in sources:
            time_left = deadline - loop.time()
            if time_left <= 0:
                record(None, CollectionStorageResult("timeout"), new_visit=False)
                raise TimeoutError("preflight storage timed out")
            before = expected[source_slot]
            started = False
            last_chest = last_attempt = None

            def authorize(request_id):
                nonlocal started
                if (status := check_target()) is not None:
                    raise _InventoryMoveNotSubmitted(status)
                current = inventory.slots
                if not self._preflight_inventory_is_observed(current, submitting=request_id):
                    raise _InventoryMoveNotSubmitted("unobserved")
                for index, stack in expected.items():
                    if index in cleared:
                        if current[index].id:
                            raise _InventoryMoveNotSubmitted("source_changed")
                    elif (not started or index != source_slot) and _stack_snapshot(current[index]) != stack:
                        raise _InventoryMoveNotSubmitted("source_changed")
                if request_id is not None:
                    started = True

            def remember(chest, attempt):
                nonlocal last_chest, last_attempt
                record(chest, attempt, new_visit=chest is not None and chest != last_chest)
                last_chest = chest
                last_attempt = attempt

            try:
                authorize(None)
                attempt = await _store_for_collection(
                    bot, storage, items=items, item_id=before.item_id,
                    source_slot=source_slot, count=before.count, dimension_id=dimension, epoch=epoch,
                    ticks=ticks, search_radius=search_radius, timeout=time_left, authorize=authorize, record=remember,
                    whole_source=True,
                )
                if attempt.stop_reason != "complete":
                    return False
                if attempt.transfer is None or attempt.transfer.confirmed_count != before.count:
                    remember(last_chest, replace(attempt, stop_reason="unconfirmed"))
                    return False
                cleared.add(source_slot)
                authorize(None)
            except _InventoryMoveNotSubmitted as stopped:
                if last_attempt is None:
                    remember(None, CollectionStorageResult(str(stopped)))
                else:
                    remember(last_chest, replace(last_attempt, stop_reason=str(stopped)))
                return False
        return True

    async def _store_for_drop(
        self, bot: "Bot", storage: CollectionStorage, items: dict[int, str], observation: "_CollectionObservation",
        timeout: float, record: Callable[[BlockCollection], None] | None, *,
        dimension: int, ticks: int, search_radius: int,
    ) -> None:
        candidates = observation.pickup_candidates()
        if len(candidates) != 1:
            return
        drop, = candidates
        if observation.live_drop(drop) is None:
            return
        inventory = self.blocks.inventory
        slots = inventory.slots
        if not self._pickup_is_blocked(slots, drop):
            return

        last_chest = None

        def remember(chest: tuple[int, int, int] | None, attempt: CollectionStorageResult) -> None:
            nonlocal last_chest
            last_chest = chest
            observation.storage_attempt = attempt
            if chest is not None:
                observation.storage_attempts[chest] = attempt
            result = observation.result()
            observation.storage_pickup_confirmed |= result.pickup_confirmed
            if record is not None:
                record(result)

        source_slot = next((slot for slot in storage.inventory_slots if slots[slot].id in items), None)
        if source_slot is None:
            remember(None, CollectionStorageResult("source_exhausted"))
            return
        source = _stack_snapshot(slots[source_slot])
        observation.storage_item_id = source.item_id
        # The original drop is still present, so its pickup cannot precede
        # this settled snapshot. Earlier unrelated inventory changes can.
        observation.inventory_feedback = _InventoryObservation(inventory, require_all_slots=True)

        def authorize(request_id: int | None) -> None:
            self._check_connection()
            if self.blocks.dimension_id != dimension or self.blocks._mining_epoch != observation.epoch:
                raise _InventoryMoveNotSubmitted("world_changed")
            if observation.live_drop(drop) is None:
                raise _InventoryMoveNotSubmitted("drop_changed")
            if not observation.storage_write_seen:
                current_slots = inventory.slots
                if _stack_snapshot(current_slots.get(source_slot)) != source:
                    raise _InventoryMoveNotSubmitted("source_changed")
                if not self._pickup_is_blocked(current_slots, drop, submitting=request_id):
                    raise _InventoryMoveNotSubmitted("inventory_available")
            # After the first write the storage helper checks its selected
            # slot against the last matching source receipt.
            if request_id is not None:
                observation.storage_write_seen = True

        try:
            attempt = await _store_for_collection(
                bot, storage, items=items, item_id=source.item_id, source_slot=source_slot, count=source.count,
                dimension_id=dimension, epoch=observation.epoch, ticks=ticks, search_radius=search_radius,
                timeout=timeout, authorize=authorize, record=remember,
            )
        except (Exception, asyncio.CancelledError) as error:
            if observation.storage_attempt is None:
                reason = ("cancelled" if isinstance(error, asyncio.CancelledError) else
                          "timeout" if isinstance(error, TimeoutError) else "error")
                remember(None, CollectionStorageResult(reason, getattr(error, "transfer_progress", None)))
            raise
        if attempt.stop_reason == "complete":
            transfer = attempt.transfer
            if transfer is None or transfer.stop_reason != "complete" or transfer.confirmed_count != source.count:
                attempt = replace(attempt, stop_reason="unconfirmed")
        if observation.storage_attempt != attempt:
            remember(last_chest, attempt)

    async def _collect_one(
        self, position: tuple[int, int, int], before: BlockObservation | None, dimension: int, *,
        ticks: int, search_radius: int, timeout: float, epoch: int | None = None,
        progress: _BatchProgress | _MixedProgress | None = None, refill: _ToolRefill | None = None,
        refill_chests: tuple[HarvestToolChest, ...] = (), ordered_refill: bool = False,
        store: _StoreForDrop | None = None, settle_before_approach: bool = False, requested_drop_id: int | None = None,
        refresh_candidates: bool = False, preflight: CollectionStorage | None = None, bot: "Bot | None" = None,
        storage_items: dict[int, str] | None = None,
    ) -> BlockCollection:
        if before is None or before.is_air is None:
            return BlockCollection(position, "target_unobserved", before)
        if before.is_air:
            return BlockCollection(position, "target_air", before)
        if progress is not None and self.blocks.observed_block(position) != before:
            return BlockCollection(position, "target_changed", before)
        counted = progress if isinstance(progress, _CountedProgress) else None
        epoch = self.blocks._mining_epoch if epoch is None else epoch
        evidence = BlockCollection(position, "interrupted", before)

        def remember(result: BlockCollection) -> None:
            nonlocal evidence
            evidence = result
            if progress is not None:
                progress.active = result

        def check_target(*, check_observation: bool = True) -> str | None:
            self._check_connection()
            if self.blocks.dimension_id != dimension:
                return "dimension_changed"
            self.blocks._check_mining_epoch(epoch)
            if check_observation and self.blocks.observed_block(position) != before:
                return "target_changed"
            return None

        def remember_refill(chest: HarvestToolChest, acquisition: HarvestToolAcquisition | None) -> None:
            attempts = evidence.tool_refill_attempts
            if ordered_refill:
                attempts += ((chest, acquisition),)
            remember(replace(evidence, tool_acquisition=acquisition, tool_refill_attempts=attempts))

        remember(evidence)
        if preflight is not None:
            if (status := check_target()) is not None:
                return replace(evidence, status=status)

            def remember_storage(chest, attempt, *, new_visit):
                attempts = evidence.storage_attempts
                if chest is None and attempts:
                    chest, previous = attempts[-1]
                    attempt = replace(attempt, transfer=previous.transfer)
                if chest is not None:
                    entry = (chest, attempt)
                    attempts = (*attempts, entry) if new_visit else (*attempts[:-1], entry)
                remember(replace(evidence, storage_attempt=attempt, storage_attempts=attempts))

            succeeded = await self._store_before_dig(
                bot, preflight, storage_items, epoch=epoch, dimension=dimension, ticks=ticks, search_radius=search_radius,
                timeout=timeout, check_target=check_target, record=remember_storage,
            )
            if not succeeded:
                return replace(evidence, status="storage_unconfirmed")
        preflight_last, preflight_attempts = evidence.storage_attempt, evidence.storage_attempts

        while True:
            approach, match = await self._approach(
                position, ticks=ticks, search_radius=search_radius, timeout=timeout,
                settle_epoch=epoch if settle_before_approach else None,
            )
            settle_before_approach = False
            remember(replace(evidence, approach=approach))
            # A changed target can itself make a route unreachable or unobserved.
            status = check_target(check_observation=(
                approach.status == "reached" or (not refresh_candidates and evidence.tool_acquisition is not None)
                or (counted is not None and approach.status in ("unreachable", "unobserved"))
            ))
            if status is not None or approach.status != "reached":
                return replace(evidence, status=status or approach.status)
            try:
                tool = await self.harvest.equip(
                    position, timeout=timeout, requested_drop_id=requested_drop_id,
                )
            except TimeoutError:
                self._check_connection()
                return replace(evidence, status="tool_timeout", timed_out=True)
            remember(replace(evidence, tool=tool))
            if (status := check_target()) is not None:
                return replace(evidence, status=status)
            if tool is not None:
                break
            if refill is None:
                return replace(evidence, status="tool_unavailable")
            acquisition = None
            for chest in refill_chests:
                try:
                    acquisition = await refill(chest, before)
                except (Exception, asyncio.CancelledError) as error:
                    remember_refill(chest, getattr(error, "harvest_progress", None))
                    if not isinstance(error, TimeoutError):
                        raise
                    self._check_connection()
                    return replace(evidence, status="tool_timeout", timed_out=True)
                remember_refill(chest, acquisition)
                status = check_target()
                if status == "dimension_changed":
                    return replace(evidence, status=status)
                settled_miss = ordered_refill and _settled_refill_miss(acquisition)
                equipped = acquisition is not None and acquisition.status == "equipped"
                if ordered_refill and (settled_miss or equipped) and self.harvest._window_busy() is not None:
                    return replace(evidence, status="tool_refill_failed")
                if status is not None:
                    return replace(evidence, status=status)
                if not settled_miss:
                    break
            if acquisition is None or acquisition.status == "tool_unavailable":
                return replace(evidence, status="tool_unavailable")
            if acquisition.status != "equipped":
                return replace(evidence, status="tool_refill_failed")
            refill = None

        assert match is not None and match.position == position

        def prepare_start() -> None:
            self.blocks._check_mining_epoch(epoch)
            if self.blocks.observed_block(position) != before:
                raise _BeforeStartRejected("target_changed")
            if preflight is not None:
                slots = self.blocks.inventory.slots
                if (not self._preflight_inventory_is_observed(slots)
                        or all(slots[index].id for index in range(_MAIN_INVENTORY_SIZE))
                        or (preflight_last is not None and self.harvest._window_busy() is not None)):
                    raise _BeforeStartRejected("storage_unconfirmed")
            if requested_drop_id is not None and not self.harvest._has_equipped_drop_tool(before, tool, requested_drop_id):
                raise _BeforeStartRejected("tool_unavailable")
            try:
                self.blocks._aim_mining_face(position, match.face, match.click_pos)
            except MiningFaceUnavailable as error:
                raise _BeforeStartRejected("unobserved" if error.status == "unknown" else "unreachable") from None

        def with_equipment(result: BlockCollection) -> BlockCollection:
            attempts = result.storage_attempts
            if preflight_attempts and not attempts and result.storage_attempt is not None:
                attempts = ((preflight.position, result.storage_attempt),)
            return replace(result, approach=approach, tool=tool, tool_acquisition=evidence.tool_acquisition,
                           tool_refill_attempts=evidence.tool_refill_attempts,
                           storage_attempt=result.storage_attempt or preflight_last,
                           storage_attempts=(*preflight_attempts, *attempts))

        def record(result: BlockCollection) -> None:
            remember(with_equipment(result))

        result = await _observe_mined_block(
            self.blocks, self.entities, position, face=match.face, timeout=timeout, follow_ticks=120,
            epoch=epoch, prepare_start=prepare_start, record=record if progress is not None else None, store=store,
            search_radius=search_radius, requested_drop_id=counted.item_id if counted is not None else None,
        )
        result = with_equipment(result)
        remember(result)
        self._check_connection()
        return result


class _BeforeStartRejected(Exception):
    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


class _SelectedDrops:
    def __init__(self, blocks, entities, selected, epoch, player_id):
        self.blocks = blocks
        self.entities = entities
        self.selected = selected
        self.runtime_by_entity_id = {target.entity_id: runtime_id for runtime_id, (target, _) in selected.items()}
        self.epoch = epoch
        self.player_id = player_id
        self.changed = asyncio.Event()
        self.pickups: dict[int, ItemPickup] = {}
        self.replaced: set[int] = set()

    def observe(self, raw) -> None:
        protocol = self.blocks.packets.protocol
        b = protocol.binding
        if self.blocks._mining_epoch != self.epoch or raw.packet_id == b.DisconnectPacket.ID:
            self.changed.set()
            return
        if raw.packet_id == b.TakeItemActorPacket.ID:
            observed = self.entities._observed_pickup(raw)
            if observed is None:
                return
            pickup, instance = observed
            selected = self.selected.get(pickup.item_runtime_id)
            if selected is not None:
                target, expected_instance = selected
                if pickup.item is not None and pickup.item_entity_id == target.entity_id and instance is expected_instance:
                    self.pickups.setdefault(pickup.item_runtime_id, pickup)
                self.changed.set()
        elif raw.packet_id in (b.AddItemActorPacket.ID, b.AddActorPacket.ID, b.AddPlayerPacket.ID, b.AddPaintingPacket.ID):
            runtime_id = int(protocol.decode(raw).runtime_id)
            current = self.entities.get(runtime_id)
            previous_runtime = None if current is None else self.runtime_by_entity_id.get(current.entity_id)
            for observed_runtime in (runtime_id, previous_runtime):
                selected = self.selected.get(observed_runtime)
                if selected is not None and self.entities._bound_item(*selected) is None:
                    self.replaced.add(observed_runtime)
                    self.changed.set()
        elif raw.packet_id == b.RemoveActorPacket.ID:
            self.changed.set()

    def result(self, target: EntityState) -> DropCollection | None:
        runtime_id = target.runtime_id
        selected = self.selected.get(runtime_id)
        if selected is None or selected[0].entity_id != target.entity_id:
            return DropCollection(target, "target_unobserved")
        pickup = self.pickups.get(runtime_id)
        if pickup is not None:
            status = "picked_up" if pickup.collector_runtime_id == self.player_id else "picked_up_by_other"
            return DropCollection(target, status, pickup)
        if runtime_id in self.replaced:
            return DropCollection(target, "target_changed")
        if self.blocks._mining_epoch != self.epoch:
            return DropCollection(target, "pickup_unconfirmed")
        if self.entities._bound_item(*selected) is None:
            replacement = self.entities.get(runtime_id) or self.entities._get_by_unique_id(target.entity_id)
            return DropCollection(target, "target_changed" if replacement is not None else "pickup_unconfirmed")
        return None


def _drop_identity(drop: EntityState) -> tuple[int | None, int | None]:
    return (drop.entity_id, None) if drop.entity_id else (None, drop.runtime_id)


class _CollectionObservation:
    def __init__(
        self, blocks: Blocks, entities: Entities, before: BlockObservation, epoch: int, *, bind_drops: bool = False,
        requested_drop_id: int | None = None,
    ):
        self.blocks = blocks
        self.entities = entities
        self.inventory = blocks.inventory
        self.before = before
        self.epoch = epoch
        self.end_packet: int | None = None
        self.after = None
        self.started = False
        self.inventory_feedback: _InventoryObservation | None = None
        self.known_drops = {}
        self.seen_drops = {_drop_identity(drop) for drop in entities.drops()}
        self.candidates = {}
        self.requested_drop_id = requested_drop_id
        self.instances = {} if bind_drops or requested_drop_id is not None else None
        self.pickups = {}
        self.storage_item_id: int | None = None
        self.storage_attempt: CollectionStorageResult | None = None
        self.storage_attempts: dict[tuple[int, int, int], CollectionStorageResult] = {}
        self.storage_write_seen = False
        self.storage_pickup_confirmed = False
        self.changed = asyncio.Event()

    def start(self):
        if self.blocks.observed_block(self.before.position) != self.before:
            raise _BeforeStartRejected("target_changed")
        self.inventory_feedback = _InventoryObservation(self.inventory)
        self.known_drops = {drop.runtime_id: drop for drop in self.entities.drops()}
        self.seen_drops.update(_drop_identity(drop) for drop in self.known_drops.values())
        self.started = True

    def observe(self, raw) -> bool:
        protocol = self.blocks.packets.protocol
        b = protocol.binding
        packet_id = raw.packet_id
        # The session's block, entity and inventory callbacks run before this one.
        if (packet_id in (b.DisconnectPacket.ID, b.ChangeDimensionPacket.ID)
                or self.blocks._mining_epoch != self.epoch):
            self.end_packet = packet_id
            return True
        if packet_id == b.RespawnPacket.ID:
            return False
        if packet_id == b.AddItemActorPacket.ID:
            packet = protocol.decode(raw)
            runtime_id = int(packet.runtime_id)
            drop = self.entities.get(runtime_id)
            if drop is None:
                return False
            self.known_drops[runtime_id] = drop
            identity = _drop_identity(drop)
            if self.started and identity not in self.seen_drops and all(
                corner <= value < corner + 1
                for corner, value in zip(self.before.position, drop.position)
            ) and (self.requested_drop_id is not None or (drop.item_network_id != 0 and drop.item_count > 0)):
                self.candidates[identity] = drop
                if self.instances is not None:
                    self.instances[identity] = self.entities._actor_instance(runtime_id)
            self.seen_drops.add(identity)
        elif not self.started:
            return False
        elif packet_id in (b.UpdateBlockPacket.ID, b.UpdateSubChunkBlocksPacket.ID):
            if self.after is None:
                current = self.blocks.observed_block(self.before.position)
                if current is not None and current.is_air is True:
                    self.after = current
        elif packet_id == b.TakeItemActorPacket.ID:
            self._observe_pickup(raw)
        elif packet_id != b.RemoveActorPacket.ID and self.inventory_feedback is not None:
            self.inventory_feedback.observe(raw)
        if self.storage_item_id is not None and self.pickups and not self.storage_pickup_confirmed:
            self.storage_pickup_confirmed = self.result().pickup_confirmed
        self.changed.set()
        return False

    def _observe_pickup(self, raw) -> None:
        if self.instances is None:
            packet = self.blocks.packets.protocol.decode(raw)
            player_id = int(packet.actor_id)
            drop = self.known_drops.get(int(packet.item_id))
            identity = _drop_identity(drop) if drop is not None else None
        else:
            observed = self.entities._observed_pickup(raw)
            if observed is None:
                return
            pickup, instance = observed
            player_id, drop = pickup.collector_runtime_id, pickup.item
            identity = _drop_identity(drop) if drop is not None else None
            candidate = self.candidates.get(identity)
            if candidate is not None and (
                instance is None or self.instances[identity] is not instance
                or pickup.item_runtime_id != candidate.runtime_id
                or not candidate.entity_id or pickup.item_entity_id != candidate.entity_id
                or drop.item_network_id != candidate.item_network_id or drop.item_count != candidate.item_count
            ):
                identity, drop = None, None
        collectors, _ = self.pickups.setdefault(identity, (set(), drop))
        collectors.add(player_id)

    def pickup_candidates(self) -> tuple[EntityState, ...]:
        requested = self.requested_drop_id
        if requested is None:
            return tuple(self.candidates.values())
        candidates = tuple(drop for drop in self.candidates.values() if drop.item_network_id in (requested, None, 0))
        # Without an observed requested item, there is no candidate to pursue.
        return candidates if any(drop.item_network_id == requested for drop in candidates) else ()

    def live_drop(self, drop: EntityState) -> EntityState | None:
        identity = _drop_identity(drop)
        candidates = self.pickup_candidates()
        if (self.blocks._mining_epoch != self.epoch or not drop.entity_id
                or len(candidates) != 1 or candidates[0] is not drop or identity in self.pickups):
            return None
        if self.instances is None:
            current = self.entities._get_by_unique_id(drop.entity_id)
        else:
            instance = self.instances[identity]
            current = None if instance is None else self.entities._bound_item(drop, instance)
        if (current is not None and current.actor_type == "minecraft:item"
            and current.item_network_id == drop.item_network_id
            and (self.instances is None or current.item_count == drop.item_count)):
            return current
        return None

    def drop_anchor(self, drop: EntityState) -> tuple[float, float, float] | None:
        current = self.live_drop(drop)
        return None if current is None else self.entities._item_navigation_anchor(current)

    def nearby_drop(self, drop: EntityState) -> EntityState | None:
        current = self.live_drop(drop)
        anchor = None if current is None else self.entities._item_navigation_anchor(current)
        movement = self.blocks.movement
        feet = movement._drop_pickup_stance(anchor)
        return current if feet is not None and movement._drop_pickup_clearance(self.blocks, feet, anchor) is True else None

    def _pickup_delta(self, item_id: int) -> int | None:
        delta = self.inventory_feedback.delta(item_id)
        if delta is None or not self.storage_write_seen or item_id != self.storage_item_id:
            return delta
        transfer = None if self.storage_attempt is None else self.storage_attempt.transfer
        if transfer is None or any(step.confirmed is None for step in transfer.steps):
            return None
        return delta + transfer.confirmed_count

    def result(self, *, timed_out: bool = False, dimension_changed: bool = False) -> BlockCollection:
        drops = tuple(self.candidates.values())
        collector_id = None
        delta = None
        pickup_drop = None
        status = "break_unconfirmed"
        if self.after is not None:
            status = "drop_unobserved"
            candidates = self.pickup_candidates()
            other_item_at_timeout = (
                timed_out and self.requested_drop_id is not None and not candidates
                and len(drops) == 1 and bool(drops[0].item_network_id)
            )
            if other_item_at_timeout:
                candidates = drops
            if len(candidates) > 1:
                status = "ambiguous_drop"
            elif candidates and (self.requested_drop_id is None or (candidates[0].item_count or 0) > 0):
                drop, = candidates
                identity = _drop_identity(drop)
                collectors, _ = self.pickups.get(identity, (set(), None))
                delta = self._pickup_delta(drop.item_network_id)
                if len(collectors) > 1:
                    status = "ambiguous_drop"
                elif not collectors:
                    status = "pickup_unconfirmed"
                else:
                    collector_id, = collectors
                    if collector_id != self.inventory.player_id:
                        status = "taken_by_other"
                    elif any(
                        other != identity and collector_id in players
                        and (source is None or source.item_network_id == drop.item_network_id
                             or (self.requested_drop_id is not None and not source.item_network_id))
                        for other, (players, source) in self.pickups.items()
                    ):
                        status = "ambiguous_drop"
                    elif self.storage_pickup_confirmed or delta == drop.item_count:
                        status = "collected"
                        delta = drop.item_count
                        if self.requested_drop_id is not None:
                            pickup_drop = drop
                    else:
                        status = "inventory_unconfirmed"
            if other_item_at_timeout and status != "collected":
                status, collector_id, delta = "drop_unobserved", None, None
        pickup_confirmed = status == "collected"
        attempt = self.storage_attempt
        storage_attempts = tuple(self.storage_attempts.items())
        if attempt is not None and attempt.stop_reason != "complete":
            status = "storage_incomplete" if pickup_confirmed else "pickup_unconfirmed"
            if timed_out and attempt.stop_reason == "cancelled":
                attempt = replace(attempt, stop_reason="timeout")
                if storage_attempts:
                    storage_attempts = (*storage_attempts[:-1], (storage_attempts[-1][0], attempt))
        return BlockCollection(
            position=self.before.position,
            status="dimension_changed" if dimension_changed else status,
            block_before=self.before,
            block_after=self.after,
            drops=drops,
            collector_id=collector_id,
            inventory_delta=delta,
            timed_out=timed_out,
            storage_attempt=attempt,
            storage_attempts=storage_attempts,
            pickup_drop=pickup_drop,
            _pickup_confirmed=pickup_confirmed and (dimension_changed or status != "collected"),
        )


async def _observe_mined_block(
    blocks: Blocks, entities: Entities, position: tuple[int, int, int], *,
    face: int, timeout: float, follow_ticks: int = 0, epoch: int | None = None,
    prepare_start: Callable[[], None] | None = None,
    record: Callable[[BlockCollection], None] | None = None,
    store: _StoreForDrop | None = None,
    search_radius: int = 8, requested_drop_id: int | None = None,
) -> BlockCollection:
    before = blocks.observed_block(position)
    if before is None or before.is_air is None:
        return BlockCollection(position, "target_unobserved", before)
    if before.is_air:
        return BlockCollection(position, "target_air", before)

    packets = blocks.packets
    b = packets.protocol.binding
    epoch = blocks._mining_epoch if epoch is None else epoch
    observation = _CollectionObservation(
        blocks, entities, before, epoch, bind_drops=store is not None, requested_drop_id=requested_drop_id,
    )
    deadline = asyncio.timeout(timeout)
    watched_packets = (
        b.UpdateBlockPacket.ID, b.UpdateSubChunkBlocksPacket.ID, b.AddItemActorPacket.ID,
        b.TakeItemActorPacket.ID, b.InventoryContentPacket.ID, b.InventorySlotPacket.ID,
        b.InventoryTransactionPacket.ID, b.DisconnectPacket.ID, b.ChangeDimensionPacket.ID,
        b.RespawnPacket.ID,
    )
    if store is not None:
        watched_packets += (b.ItemStackResponsePacket.ID, b.RemoveActorPacket.ID)

    async def watch():
        await packets.wait_for_packet(watched_packets, timeout=None, match=observation.observe)

    async def mine_and_wait():
        blocks._check_mining_epoch(epoch)
        try:
            await blocks.mine_block(
                position, face=face, timeout=timeout, before_start=observation.start, _prepare_start=prepare_start,
            )
        except _BeforeStartRejected as error:
            return BlockCollection(position, error.status, before)
        followed = False
        remaining_follow_ticks = follow_ticks
        while True:
            result = observation.result()
            if result.status in ("collected", "ambiguous_drop", "taken_by_other"):
                return result
            if store is not None and observation.storage_attempt is None and result.status == "pickup_unconfirmed":
                drop, = observation.pickup_candidates()
                remaining = deadline.when() - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return observation.result(timed_out=True)
                await store(observation, remaining, record)
                attempt = observation.storage_attempt
                if attempt is not None:
                    result = observation.result()
                    # A matching pickup removes the actor before its inventory update can arrive.
                    if (attempt.stop_reason != "complete" or result.pickup_confirmed
                            or (result.status != "inventory_unconfirmed" and observation.live_drop(drop) is None)):
                        return result
                    if result.status == "pickup_unconfirmed" and follow_ticks:
                        movement = blocks.movement
                        start_tick = movement._tick
                        route = await movement._navigate_drop(
                            blocks, drop.entity_id, lambda: observation.drop_anchor(drop),
                            NavigationRules(search_radius=int(search_radius)), follow_ticks, deadline=deadline.when(),
                        )
                        result = observation.result()
                        reached = route.status == "reached"
                        if result.status != "inventory_unconfirmed" and (
                            result.status != "pickup_unconfirmed"
                            or (not reached and movement._drop_pickup_stance(observation.drop_anchor(drop)) is None)
                        ):
                            timed_out = asyncio.get_running_loop().time() >= deadline.when()
                            return replace(result, timed_out=timed_out)
                        remaining_follow_ticks = max(0, follow_ticks - (movement._tick - start_tick)) if reached else 0
                        followed = False
            if result.status == "pickup_unconfirmed" and (not followed or observation.storage_attempt is not None):
                drop, = observation.pickup_candidates()
                current = observation.live_drop(drop)
                if current is None and observation.storage_attempt is not None:
                    return result
                resolve = observation.live_drop if observation.storage_attempt is None else observation.nearby_drop
                if current is not None and remaining_follow_ticks and not followed and resolve(drop) is not None:
                    followed = True
                    await blocks.movement.follow_entity(
                        entities, current.runtime_id, ticks=remaining_follow_ticks,
                        stop_distance=0 if observation.storage_attempt is None else _PLAYER_HALF_WIDTH,
                        _resolve=lambda _: resolve(drop),
                        _clearance=None if observation.storage_attempt is None else blocks._flight_clearance,
                    )
                    continue
            observation.changed.clear()
            await observation.changed.wait()

    # Start observing before mining registers its own, narrower block waiter.
    watcher = asyncio.create_task(watch())
    action = asyncio.create_task(mine_and_wait())
    timed_out = dimension_changed = False
    try:
        async with deadline:
            done, _ = await asyncio.wait((watcher, action), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            await watcher
        if observation.end_packet == b.DisconnectPacket.ID:
            raise ConnectionError("server disconnected while collecting a block")
        if observation.end_packet == b.ChangeDimensionPacket.ID:
            dimension_changed = True
        elif observation.end_packet is not None:
            if observation.after is None:
                blocks._check_mining_epoch(epoch)
        else:
            return await action
        if observation.storage_item_id is None:
            return observation.result(dimension_changed=dimension_changed)
    except TimeoutError:
        if not deadline.expired():
            raise
        timed_out = True
        if observation.storage_item_id is None:
            return observation.result(timed_out=True)
    finally:
        if not action.done():
            action.cancel()
        if observation.storage_item_id is not None:
            # A storage detour can publish a partial result while closing its
            # owned window. Keep observing pickups until that cleanup settles.
            await asyncio.gather(action, return_exceptions=True)
        if not watcher.done():
            watcher.cancel()
        await asyncio.gather(action, watcher, return_exceptions=True)
        if record is not None and observation.started:
            record(observation.result(
                timed_out=timed_out and observation.storage_item_id is not None,
                dimension_changed=dimension_changed and observation.storage_item_id is not None,
            ))
    return observation.result(timed_out=timed_out, dimension_changed=dimension_changed)
