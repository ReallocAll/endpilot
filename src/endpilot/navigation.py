"""Known-flat-terrain A* with explicit single-writer movement integration.

The application calls Navigator.on_event(event) from its existing Runtime
observer and submits any returned Input through its sole input writer.
goto() reports predicted arrival, not independent BDS authority.
"""
from __future__ import annotations

import asyncio
import heapq
import math
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Callable

from ._movement import MovementController
from .events import PacketDecodedEvent, TickEvent
from .types import BotState, Vec3

Cell = tuple[int, int, int]
_ROOT2 = math.sqrt(2.0)
_OFFSETS = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
            (1, 1, _ROOT2), (1, -1, _ROOT2), (-1, 1, _ROOT2), (-1, -1, _ROOT2))


@dataclass(frozen=True, slots=True)
class GoalNear:
    x: int
    feet_y: int
    z: int
    radius: int = 0

    def __post_init__(self) -> None:
        if any(type(v) is not int for v in (self.x, self.feet_y, self.z, self.radius)):
            raise TypeError("goal coordinates and radius must be integers")
        if not 0 <= self.radius <= 8:
            raise ValueError("radius must be between 0 and 8 blocks")


@dataclass(frozen=True, slots=True)
class PlanResult:
    status: str  # complete, need_world, unreachable, budget_exhausted
    path: tuple[Cell, ...]
    cost: float
    expanded: int
    peak_open: int
    stand_queries: int
    elapsed_ms: float
    max_batch_ms: float


class NavigationError(RuntimeError):
    """Navigation failed closed; the exception message identifies the reason."""


def _distance(a: Cell, b: GoalNear) -> float:
    dx, dz = abs(a[0] - b.x), abs(a[2] - b.z)
    # Octile distance is consistent for cardinal 1 and diagonal sqrt(2).
    return max(0.0, max(dx, dz) + (_ROOT2 - 1.0) * min(dx, dz) - b.radius * _ROOT2)


async def plan_flat(world, start: Cell, goal: GoalNear, *, max_expansions: int = 4096,
                    batch_size: int = 64, cancelled: Callable[[], bool] | None = None) -> PlanResult:
    """Incremental A* over observed flat full-cube cells only.

    A diagonal needs both orthogonal side cells to be standable, so the
    0.6-block-wide player's swept AABB cannot cut across an occupied corner.
    Unknown support, headroom and block shape never become traversable.
    """
    if len(start) != 3 or any(type(v) is not int for v in start):
        raise ValueError("start must be a three-integer cell")
    if not 1 <= max_expansions <= 100000 or not 1 <= batch_size <= 1024:
        raise ValueError("invalid search budget")
    if start[1] != goal.feet_y:
        raise NavigationError("non-flat goal")
    t0 = perf_counter()
    stand_cache: dict[Cell, bool | None] = {}
    stand_queries = 0
    unknown = False

    def stand(cell: Cell) -> bool | None:
        nonlocal stand_queries, unknown
        if cell not in stand_cache:
            stand_queries += 1
            stand_cache[cell] = world.can_stand(*cell)
        value = stand_cache[cell]
        if value is None:
            unknown = True
        return value

    expanded, peak, max_batch = 0, 0, 0.0

    def result(status: str, path: tuple[Cell, ...] = (), cost: float = math.inf) -> PlanResult:
        return PlanResult(status, path, cost, expanded, peak, stand_queries,
                          (perf_counter() - t0) * 1000, max_batch)

    if stand(start) is not True:
        return result("need_world" if unknown else "unreachable")
    if goal.radius == 0 and stand((goal.x, goal.feet_y, goal.z)) is not True:
        return result("need_world" if unknown else "unreachable")

    best: dict[Cell, float] = {start: 0.0}
    parents: dict[Cell, Cell] = {}
    queue = [(_distance(start, goal), 0, start)]
    peak = 1
    seq = 0
    while queue:
        batch_start = perf_counter()
        for _ in range(batch_size):
            if cancelled is not None and cancelled():
                raise asyncio.CancelledError
            if not queue:
                break
            f, _, node = heapq.heappop(queue)
            g = best[node]
            if f > g + _distance(node, goal) + 1e-8:
                continue
            if math.hypot(node[0] - goal.x, node[2] - goal.z) <= goal.radius:
                path = [node]
                while path[-1] != start:
                    path.append(parents[path[-1]])
                path.reverse()
                max_batch = max(max_batch, (perf_counter() - batch_start) * 1000)
                return result("complete", tuple(path), g)
            if expanded >= max_expansions:
                max_batch = max(max_batch, (perf_counter() - batch_start) * 1000)
                return result("budget_exhausted")
            expanded += 1
            x, y, z = node
            for dx, dz, step in _OFFSETS:
                neighbor = (x + dx, y, z + dz)
                if stand(neighbor) is not True:
                    continue
                if dx and dz and (stand((x + dx, y, z)) is not True
                                  or stand((x, y, z + dz)) is not True):
                    continue
                candidate = g + step
                if candidate >= best.get(neighbor, math.inf) - 1e-10:
                    continue
                best[neighbor] = candidate
                parents[neighbor] = node
                seq += 1
                heapq.heappush(queue, (candidate + _distance(neighbor, goal), seq, neighbor))
                peak = max(peak, len(queue))
        max_batch = max(max_batch, (perf_counter() - batch_start) * 1000)
        await asyncio.sleep(0)
    return result("need_world" if unknown else "unreachable")


class Navigator:
    """Intent-only navigator: never calls Bot.set_input() or owns a connection.

    One application Runtime observer calls on_event for decoded packets, tick,
    state, position and dimension events, then composes/sends its Input exactly
    once. Do not separately subscribe WorldQuery.observe.
    """

    def __init__(self, bot, world) -> None:
        if getattr(world, "bot", bot) is not bot:
            raise ValueError("WorldQuery belongs to a different Bot")
        self.bot = bot
        self.world = world
        self.controller = MovementController()
        self.last_plan: PlanResult | None = None
        self._epoch = 0
        self._session: int | None = None
        self._dimension: int | None = None
        self._future: asyncio.Future | None = None
        self._path: tuple[Cell, ...] = ()
        self._next = 0
        self._dirty = False

    def _fail(self, reason: str) -> None:
        self._path = ()
        self.controller.stop()
        future, self._future = self._future, None
        if future is not None and not future.done():
            future.set_exception(NavigationError(reason))

    def stop(self) -> None:
        """Invalidate the active search/route and release movement intent."""
        self._epoch += 1
        self._fail("navigation stopped")

    def _route_safe(self) -> bool:
        # Re-evaluate on observed world mutation, without a second collision cache.
        path = self._path
        if not path:
            return True
        begin = max(0, self._next - 2)
        for cell in path[begin:]:
            if self.world.can_stand(*cell) is not True:
                return False
        for a, b in zip(path[begin:], path[begin + 1:]):
            dx, dz = b[0] - a[0], b[2] - a[2]
            if abs(dx) > 1 or abs(dz) > 1:
                return False
            if dx and dz and (self.world.can_stand(a[0] + dx, a[1], a[2]) is not True
                              or self.world.can_stand(a[0], a[1], a[2] + dz) is not True):
                return False
        return True

    def on_event(self, event) -> object | None:
        """Called synchronously on Runtime's loop by the sole event/input writer."""
        self.world.observe(self.bot, event)
        if isinstance(event, PacketDecodedEvent) and event.packet.header.packet_id in (
            11, 21, 61, 110, 121, 172, 174
        ):
            self._dirty = True
        if self._future is None and self.controller.state not in ("moving", "coasting"):
            return None
        status = self.bot.get_status()
        if (status.session_id != self._session or status.dimension != self._dimension
                or status.state != BotState.ready or not status.alive):
            self._fail("session, dimension or readiness changed")
            return None
        if isinstance(event, TickEvent) and self._future is not None:
            if self._dirty:
                self._dirty = False
                if not self._route_safe():
                    self._fail("path invalidated by world update")
                    return None
            if self.controller.state == "predicted_arrival":
                if self._next >= len(self._path):
                    future, self._future = self._future, None
                    self._path = ()
                    if not future.done():
                        future.set_result(None)
                    return None
                predicted = self.controller.predicted_position
                if predicted is None:
                    self._fail("missing predicted position")
                    return None
                cell = self._path[self._next]
                self._next += 1
                # After a settled waypoint PR #10 resets momentum. Initialize
                # at the previous prediction, not a potentially stale status.
                self.controller.start(replace(status, position=predicted),
                                      Vec3(cell[0] + .5, status.position.y, cell[2] + .5),
                                      max_ticks=160)
        frame = self.controller.on_event(status, event)
        if self.controller.state == "failed":
            self._fail(self.controller.failure or "movement failed")
        return frame

    async def goto(self, goal: GoalNear, *, max_expansions: int = 4096) -> None:
        """Walk toward a goal; returns predicted completion, not a BDS oracle."""
        self.stop()
        epoch = self._epoch
        status = self.bot.get_status()
        if status.state != BotState.ready or not status.alive:
            raise NavigationError("requires a live Ready session")
        # BDS position is eye height (feet + 1.62001), not feet height.
        feet_y = math.floor(status.position.y - 1.62001 + 1e-4)
        if abs(status.position.y - (feet_y + 1.62001)) > .15:
            raise NavigationError("unverified grounded eye height")
        if abs(status.position.x - (math.floor(status.position.x) + .5)) > .15 or abs(
            status.position.z - (math.floor(status.position.z) + .5)) > .15:
            raise NavigationError("start position is not at a safe block center")
        if goal.feet_y != feet_y:
            raise NavigationError("non-flat target")
        self._session, self._dimension = status.session_id, status.dimension
        start = (math.floor(status.position.x), feet_y, math.floor(status.position.z))

        def cancelled() -> bool:
            current = self.bot.get_status()
            return (epoch != self._epoch or current.session_id != self._session
                    or current.dimension != self._dimension or current.state != BotState.ready)

        try:
            self.last_plan = await plan_flat(self.world, start, goal, max_expansions=max_expansions,
                                             cancelled=cancelled)
            if cancelled():
                raise NavigationError("session or goal changed while planning")
            if self.last_plan.status != "complete":
                raise NavigationError(self.last_plan.status)
            self._path = self.last_plan.path
            self._next = 1
            # Planning yields to the loop; a block update may have changed an
            # already-expanded cell. Revalidate the entire route on first tick.
            self._dirty = True
            if len(self._path) == 1:
                return
            self._future = asyncio.get_running_loop().create_future()
            cell = self._path[self._next]
            self._next += 1
            self.controller.start(status, Vec3(cell[0] + .5, status.position.y, cell[2] + .5),
                                  max_ticks=160)
            await self._future
        finally:
            if self._epoch == epoch:
                self.stop()
