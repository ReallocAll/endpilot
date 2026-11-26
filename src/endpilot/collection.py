"""Collect supplied blocks and drops using their observed world and pickup evidence."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
import math
from typing import TYPE_CHECKING

from .blocks import BlockObservation, BlockSearchResult, Blocks, MiningFaceUnavailable, _ORDINARY_MINING_REACH
from .entities import Entities, EntityState, ItemPickup
from .harvest import HarvestTool, HarvestToolAcquisition, HarvestToolChest, HarvestTools
from .inventory_observation import _InventoryObservation
from .movement import BlockApproach, Movement
from .navigation import AllGoal, BlockFaceGoal, BlockInteractionGoal, BlockInteractionMatch, NavigationRules

if TYPE_CHECKING:
    from .bot import Bot


_ToolRefill = Callable[[HarvestToolChest, BlockObservation], Awaitable[HarvestToolAcquisition | None]]


@dataclass(frozen=True, slots=True)
class BlockCollection:
    """Evidence received during one collection attempt.

    Drops are new item actors first reported inside the target block. Their
    source may be indistinguishable on this stream. The collector and inventory
    delta refer to a sole candidate. An unknown delta is None. Ordered refills
    record each chest and its available acquisition evidence in tool_refill_attempts.
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
    _pickup_confirmed: bool = field(default=False, kw_only=True, repr=False)

    @property
    def pickup_confirmed(self) -> bool:
        """Whether the bot's pickup and matching main-inventory gain were confirmed."""
        return self.status == "collected" or self._pickup_confirmed


@dataclass(frozen=True, slots=True)
class BlockCollectionBatch:
    """Ordered target snapshot and evidence collected before the batch ended.

    Results are the attempted prefix. An exception's collection_progress can
    also carry evidence from the interrupted target in active.
    """

    dimension_id: int
    positions: tuple[tuple[int, int, int], ...]
    results: tuple[BlockCollection, ...]
    active: BlockCollection | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CountedBlockCollection(BlockCollectionBatch):
    """Candidate snapshot and confirmed main-inventory gains for one item ID.

    search_complete records the admission-time completeness of a vein search.
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


def _confirmed_item_count(result: BlockCollection, item_id: int, *, item_name: str | None = None) -> int:
    if not result.pickup_confirmed:
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
        tool_chests: Sequence[HarvestToolChest] | None = None,
    ) -> BlockCollectionBatch:
        if count is not None and (not isinstance(count, int) or count < 0):
            raise ValueError("collection count must be a nonnegative integer")
        tool_chests = _snapshot_tool_chests(tool_chest, tool_chests)
        positions = tuple(dict.fromkeys(map(_position, positions)))[:count]
        progress = _BatchProgress(self.blocks.dimension_id, positions)
        try:
            self._check_connection()
            if not positions:
                return progress.snapshot()
            epoch = self.blocks._mining_epoch
            before = tuple(self.blocks.observed_block(position) for position in positions)
            await self._run(self._collect_batch(
                bot, progress, before, epoch, ticks=ticks, search_radius=search_radius, timeout=timeout,
                tool_chest=tool_chest, tool_chests=tool_chests,
            ))
        except (Exception, asyncio.CancelledError) as error:
            error.collection_progress = progress.snapshot()
            raise
        return progress.snapshot()

    async def collect_by_count(
        self, bot: "Bot", names: str | int | Iterable[str] | Iterable[int], *, item_id: int, count: int,
        radius: int = 16, ticks: int = 160, search_radius: int = 8, timeout: float = 15,
        tool_chest: HarvestToolChest | None = None, tool_chests: Sequence[HarvestToolChest] | None = None,
    ) -> CountedBlockCollection:
        return await self._collect_counted(
            bot, partial(self.blocks.find_blocks, names, radius=radius, limit=None),
            item_id=item_id, count=count, ticks=ticks, search_radius=search_radius, timeout=timeout,
            tool_chest=tool_chest, tool_chests=tool_chests,
        )

    async def collect_vein_by_count(
        self, bot: "Bot", seed: tuple[int, int, int], *, item_id: int, count: int,
        max_blocks: int, max_distance: int, flood_radius: int, ticks: int, search_radius: int, timeout: float,
        tool_chest: HarvestToolChest | None = None, tool_chests: Sequence[HarvestToolChest] | None = None,
    ) -> CountedBlockCollection:
        return await self._collect_counted(
            bot, partial(self.blocks.find_block_vein, seed, max_blocks=max_blocks, max_distance=max_distance,
                         flood_radius=flood_radius),
            item_id=item_id, count=count, ticks=ticks, search_radius=search_radius, timeout=timeout,
            tool_chest=tool_chest, tool_chests=tool_chests, report_completeness=True,
        )

    async def _collect_counted(
        self, bot: "Bot", find_candidates: Callable[[], BlockSearchResult], *, item_id: int, count: int,
        ticks: int, search_radius: int, timeout: float, tool_chest: HarvestToolChest | None,
        tool_chests: Sequence[HarvestToolChest] | None,
        report_completeness: bool = False,
    ) -> CountedBlockCollection:
        if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id == 0:
            raise ValueError("collection item ID must be a nonzero integer")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise ValueError("collection count must be a positive integer")
        tool_chests = _snapshot_tool_chests(tool_chest, tool_chests)
        progress = _CountedProgress(self.blocks.dimension_id, item_id, count)
        try:
            self._check_connection()
            epoch = self.blocks._mining_epoch
            search = find_candidates()
            if report_completeness:
                progress.search_complete = search.complete
            observations = tuple(
                before for match in search.matches if (before := self.blocks.observed_block(match.position)) is not None
                and before.is_air is False
            )
            progress.positions = tuple(before.position for before in observations)
            if observations:
                await self._run(self._collect_batch(
                    bot, progress, observations, epoch, ticks=ticks, search_radius=search_radius,
                    timeout=timeout, tool_chest=tool_chest, tool_chests=tool_chests,
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

    async def _run[T](self, operation: Coroutine[None, None, T]) -> T:
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
        self, bot: "Bot", progress: _BatchProgress, observations: tuple[BlockObservation | None, ...], epoch: int, *,
        ticks: int, search_radius: int, timeout: float, tool_chest: HarvestToolChest | None,
        tool_chests: tuple[HarvestToolChest, ...] | None,
    ) -> None:
        counted = progress if isinstance(progress, _CountedProgress) else None
        ordered_refill = tool_chests is not None
        chests = tool_chests if tool_chests is not None else (tool_chest,) if tool_chest is not None else ()
        refill = None if not chests else partial(
            self._refill_tool, bot, dimension=progress.dimension_id, epoch=epoch,
            ticks=ticks, search_radius=search_radius, timeout=timeout, ordered=ordered_refill,
        )
        async with self._lock:
            for position, before in zip(progress.positions, observations, strict=True):
                self._check_connection()
                self.blocks._check_mining_epoch(epoch)
                result = await self._collect_one(
                    position, before, progress.dimension_id, ticks=ticks, search_radius=search_radius,
                    timeout=timeout, epoch=epoch, progress=progress, refill=refill, refill_chests=chests,
                    ordered_refill=ordered_refill,
                )
                progress.results.append(result)
                progress.active = None
                if counted is None:
                    if result.status != "collected":
                        break
                    continue
                received = _confirmed_item_count(result, counted.item_id)
                counted.collected_count += received
                if result.status == "target_changed":
                    acquisition = result.tool_acquisition
                    if result.tool_refill_attempts and not (
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
            try:
                for target in progress.targets:
                    self._check_connection()
                    self.blocks._check_mining_epoch(epoch)
                    if isinstance(target, EntityState):
                        result = await self._collect_selected_drop(target, drops, progress, ticks=ticks, timeout=timeout)
                        success = result.status == "picked_up"
                    else:
                        result = await self._collect_one(
                            target, before[target], progress.dimension_id, ticks=ticks, search_radius=search_radius,
                            timeout=timeout, epoch=epoch, progress=progress,
                        )
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
        self, target: EntityState, drops: "_SelectedDrops", progress: _MixedProgress, *, ticks: int, timeout: float,
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
        follow = asyncio.create_task(self.movement.follow_entity(
            self.entities, target.runtime_id, ticks=ticks, stop_distance=0.0, _resolve=resolve,
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

    async def _approach(self, position, *, ticks, search_radius, timeout):
        if not (0 <= ticks < math.inf and 0 <= search_radius < math.inf):
            raise ValueError("ticks and search radii must be finite and nonnegative")
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        visible = BlockInteractionGoal(position, max_distance=_ORDINARY_MINING_REACH)
        adjacent = AllGoal((BlockFaceGoal(position), visible))
        rules = NavigationRules(search_radius=int(search_radius))
        route = await self.movement._navigate_goal(
            self.blocks, visible, rules, ticks, deadline=deadline, preferred_goal=adjacent,
        )
        status = {"unknown": "unobserved", "budget": "budget_exhausted"}.get(route.status, route.status)
        match = next((face for face in route.matches if isinstance(face, BlockInteractionMatch)), None)
        return BlockApproach(status, route.frames, route.stand_position, match.face if match else None), match

    async def _refill_tool(
        self, bot: "Bot", chest: HarvestToolChest, target: BlockObservation, *, dimension: int, epoch: int,
        ticks: int, search_radius: int, timeout: float, ordered: bool,
    ) -> HarvestToolAcquisition | None:
        chest_block = self.harvest._refill_chest(target, chest.position)
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
        if self.blocks.observed_block(target.position) != target:
            return None
        if (busy := self.harvest._window_busy()) is not None:
            return HarvestToolAcquisition(busy)
        if route.status != "reached":
            if (ordered and route.status == "unreachable"
                    and self.harvest._refill_chest(target, chest.position) != chest_block):
                return None
            status = {"unknown": "unobserved", "budget": "budget_exhausted"}.get(route.status, route.status)
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
            )
        finally:
            if unsubscribe is not None:
                unsubscribe()
        if window_replaced and acquisition.status in ("equipped", "tool_unavailable"):
            return replace(acquisition, status="window_changed")
        return acquisition

    async def _collect_one(
        self, position: tuple[int, int, int], before: BlockObservation | None, dimension: int, *,
        ticks: int, search_radius: int, timeout: float, epoch: int | None = None,
        progress: _BatchProgress | _MixedProgress | None = None, refill: _ToolRefill | None = None,
        refill_chests: tuple[HarvestToolChest, ...] = (), ordered_refill: bool = False,
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
        while True:
            approach, match = await self._approach(position, ticks=ticks, search_radius=search_radius, timeout=timeout)
            remember(replace(evidence, approach=approach))
            # A changed target can itself make a route unreachable or unobserved.
            status = check_target(check_observation=(
                approach.status == "reached" or evidence.tool_acquisition is not None
                or (counted is not None and approach.status in ("unreachable", "unobserved"))
            ))
            if status is not None or approach.status != "reached":
                return replace(evidence, status=status or approach.status)
            try:
                tool = await self.harvest.equip(position, timeout=timeout)
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
            try:
                self.blocks._aim_mining_face(position, match.face, match.click_pos)
            except MiningFaceUnavailable as error:
                raise _BeforeStartRejected("unobserved" if error.status == "unknown" else "unreachable") from None

        def with_equipment(result: BlockCollection) -> BlockCollection:
            return replace(result, approach=approach, tool=tool, tool_acquisition=evidence.tool_acquisition,
                           tool_refill_attempts=evidence.tool_refill_attempts)

        def record(result: BlockCollection) -> None:
            remember(with_equipment(result))

        result = await _observe_mined_block(
            self.blocks, self.entities, position, face=match.face, timeout=timeout, follow_ticks=120,
            epoch=epoch, prepare_start=prepare_start, record=record if progress is not None else None,
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
    def __init__(self, blocks: Blocks, entities: Entities, before: BlockObservation, epoch: int):
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
        self.pickups = {}
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
            ) and drop.item_network_id != 0 and drop.item_count > 0:
                self.candidates[identity] = drop
            self.seen_drops.add(identity)
        elif not self.started:
            return False
        elif packet_id in (b.UpdateBlockPacket.ID, b.UpdateSubChunkBlocksPacket.ID):
            if self.after is None:
                current = self.blocks.observed_block(self.before.position)
                if current is not None and current.is_air is True:
                    self.after = current
        elif packet_id == b.TakeItemActorPacket.ID:
            packet = protocol.decode(raw)
            runtime_id = int(packet.item_id)
            drop = self.known_drops.get(runtime_id)
            identity = _drop_identity(drop) if drop is not None else None
            collectors, _ = self.pickups.setdefault(identity, (set(), drop))
            collectors.add(int(packet.actor_id))
        elif self.inventory_feedback is not None:
            self.inventory_feedback.observe(raw)
        self.changed.set()
        return False

    def live_drop(self, drop: EntityState) -> EntityState | None:
        identity = _drop_identity(drop)
        if (self.blocks._mining_epoch != self.epoch or not drop.entity_id
                or len(self.candidates) != 1 or identity in self.pickups):
            return None
        current = self.entities._get_by_unique_id(drop.entity_id)
        if (current is not None and current.actor_type == "minecraft:item"
            and current.item_network_id == drop.item_network_id):
            return current
        return None

    def result(self, *, timed_out: bool = False, dimension_changed: bool = False) -> BlockCollection:
        drops = tuple(self.candidates.values())
        collector_id = None
        delta = None
        status = "break_unconfirmed"
        if self.after is not None:
            status = "drop_unobserved"
            if len(drops) > 1:
                status = "ambiguous_drop"
            elif drops:
                drop, = drops
                identity = _drop_identity(drop)
                collectors, _ = self.pickups.get(identity, (set(), None))
                delta = self.inventory_feedback.delta(drop.item_network_id)
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
                        and (source is None or source.item_network_id == drop.item_network_id)
                        for other, (players, source) in self.pickups.items()
                    ):
                        status = "ambiguous_drop"
                    elif delta == drop.item_count:
                        status = "collected"
                    else:
                        status = "inventory_unconfirmed"
        return BlockCollection(
            position=self.before.position,
            status="dimension_changed" if dimension_changed else status,
            block_before=self.before,
            block_after=self.after,
            drops=drops,
            collector_id=collector_id,
            inventory_delta=delta,
            timed_out=timed_out,
            _pickup_confirmed=dimension_changed and status == "collected",
        )


async def _observe_mined_block(
    blocks: Blocks, entities: Entities, position: tuple[int, int, int], *,
    face: int, timeout: float, follow_ticks: int = 0, epoch: int | None = None,
    prepare_start: Callable[[], None] | None = None,
    record: Callable[[BlockCollection], None] | None = None,
) -> BlockCollection:
    before = blocks.observed_block(position)
    if before is None or before.is_air is None:
        return BlockCollection(position, "target_unobserved", before)
    if before.is_air:
        return BlockCollection(position, "target_air", before)

    packets = blocks.packets
    b = packets.protocol.binding
    epoch = blocks._mining_epoch if epoch is None else epoch
    observation = _CollectionObservation(blocks, entities, before, epoch)

    async def watch():
        await packets.wait_for_packet((
            b.UpdateBlockPacket.ID, b.UpdateSubChunkBlocksPacket.ID, b.AddItemActorPacket.ID,
            b.TakeItemActorPacket.ID, b.InventoryContentPacket.ID, b.InventorySlotPacket.ID,
            b.InventoryTransactionPacket.ID, b.DisconnectPacket.ID, b.ChangeDimensionPacket.ID,
            b.RespawnPacket.ID,
        ), timeout=None, match=observation.observe)

    async def mine_and_wait():
        blocks._check_mining_epoch(epoch)
        try:
            await blocks.mine_block(
                position, face=face, timeout=timeout, before_start=observation.start, _prepare_start=prepare_start,
            )
        except _BeforeStartRejected as error:
            return BlockCollection(position, error.status, before)
        followed = False
        while True:
            result = observation.result()
            if result.status in ("collected", "ambiguous_drop", "taken_by_other"):
                return result
            if follow_ticks and not followed and result.status == "pickup_unconfirmed":
                drop, = result.drops
                current = observation.live_drop(drop)
                if current is not None:
                    followed = True
                    await blocks.movement.follow_entity(
                        entities, current.runtime_id, ticks=follow_ticks, stop_distance=0,
                        _resolve=lambda _: observation.live_drop(drop),
                    )
                    continue
            observation.changed.clear()
            await observation.changed.wait()

    # Start observing before mining registers its own, narrower block waiter.
    watcher = asyncio.create_task(watch())
    action = asyncio.create_task(mine_and_wait())
    try:
        async with asyncio.timeout(timeout) as deadline:
            done, _ = await asyncio.wait((watcher, action), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            await watcher
        if observation.end_packet == b.DisconnectPacket.ID:
            raise ConnectionError("server disconnected while collecting a block")
        if observation.end_packet == b.ChangeDimensionPacket.ID:
            return observation.result(dimension_changed=True)
        if observation.end_packet is not None:
            if observation.after is None:
                blocks._check_mining_epoch(epoch)
            return observation.result()
        return await action
    except TimeoutError:
        if not deadline.expired():
            raise
        return observation.result(timed_out=True)
    finally:
        for task in (action, watcher):
            if not task.done():
                task.cancel()
        await asyncio.gather(action, watcher, return_exceptions=True)
        if record is not None and observation.started:
            record(observation.result())
