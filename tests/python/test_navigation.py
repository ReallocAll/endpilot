"""Deterministic A* and navigation intent regressions (real BDS in Testbed)."""
import asyncio
import math
from dataclasses import replace

import pytest

from endpilot import BotState, BotStatus, PositionEvent, TickEvent, Vec3
from endpilot.navigation import GoalNear, NavigationError, Navigator, plan_flat


class KnownFlat:
    def __init__(self, *, obstacles=(), known=None):
        self.obstacles = set(obstacles)
        self.known = known
        self.bot = None

    def can_stand(self, x, feet_y, z):
        if feet_y != 100:
            return False
        if self.known is not None and (x, z) not in self.known:
            return None
        return (x, z) not in self.obstacles

    def observe(self, bot, event):
        pass


class FakeBot:
    def __init__(self):
        self.status = BotStatus(
            state=BotState.ready, session_id=1, alive=True,
            position=Vec3(0.5, 101.62001, 0.5),
        )

    def get_status(self):
        return self.status


@pytest.mark.asyncio
async def test_open_diagonal_and_detour():
    world = KnownFlat()
    p = await plan_flat(world, (0, 100, 0), GoalNear(8, 100, 8))
    assert p.status == "complete"
    assert math.isclose(p.cost, 8 * math.sqrt(2))
    assert p.expanded < 20 and p.peak_open >= 1
    assert p.stand_queries > 0 and p.max_batch_ms >= 0
    wall = KnownFlat(obstacles={(x, 3) for x in range(-2, 3)})
    q = await plan_flat(wall, (0, 100, 0), GoalNear(0, 100, 8))
    assert q.status == "complete" and q.cost > 8
    assert not any((x, z) in wall.obstacles for x, _, z in q.path)
    for a, b in zip(q.path, q.path[1:]):
        dx, dz = b[0] - a[0], b[2] - a[2]
        if dx and dz:
            assert wall.can_stand(a[0] + dx, 100, a[2]) is True
            assert wall.can_stand(a[0], 100, a[2] + dz) is True


@pytest.mark.asyncio
async def test_corner_cannot_cut_and_unknown_is_not_air():
    blocked = KnownFlat(obstacles={(1, 0), (0, 1)})
    q = await plan_flat(blocked, (0, 100, 0), GoalNear(1, 100, 1))
    assert q.status == "complete" and q.cost > math.sqrt(2)
    sealed = KnownFlat(obstacles={(x, z) for x in (-1, 0, 1) for z in (-1, 0, 1)
                                  if (x, z) != (0, 0)})
    q = await plan_flat(sealed, (0, 100, 0), GoalNear(8, 100, 8))
    assert q.status == "unreachable"
    unknown = KnownFlat(known={(x, z) for x in range(3) for z in range(3)})
    q = await plan_flat(unknown, (0, 100, 0), GoalNear(8, 100, 8))
    assert q.status == "need_world" and not q.path


@pytest.mark.asyncio
async def test_budget_and_cancellation_yield_to_loop():
    world = KnownFlat(obstacles={(x, 1) for x in range(-40, 41)})
    q = await plan_flat(world, (0, 100, 0), GoalNear(0, 100, 30),
                        max_expansions=12, batch_size=4)
    assert q.status == "budget_exhausted" and q.expanded == 12
    yielded = 0

    def cancel():
        nonlocal yielded
        yielded += 1
        return yielded >= 17

    with pytest.raises(asyncio.CancelledError):
        await plan_flat(world, (0, 100, 0), GoalNear(0, 100, 30),
                        cancelled=cancel, batch_size=4)


@pytest.mark.asyncio
async def test_navigator_single_writer_waypoints_stop_and_session():
    bot = FakeBot()
    world = KnownFlat()
    world.bot = bot
    nav = Navigator(bot, world)
    task = asyncio.create_task(nav.goto(GoalNear(4, 100, 0)))
    for index in range(400):
        await asyncio.sleep(0)
        frame = nav.on_event(TickEvent(index, 0))
        if task.done():
            break
        if frame is not None:
            assert frame.position.y == 101.62001
    await task
    assert nav.last_plan.status == "complete"
    assert nav.controller.predicted_position is not None
    assert abs(nav.controller.predicted_position.x - 4.5) <= .1
    assert nav.on_event(TickEvent(1000, 0)) is None

    task = asyncio.create_task(nav.goto(GoalNear(10, 100, 0)))
    for index in range(12):
        await asyncio.sleep(0)
        nav.on_event(TickEvent(index, 0))
    nav.stop()
    with pytest.raises(NavigationError, match="stopped"):
        await task
    assert nav.controller.state == "coasting"
    for i in range(50):
        frame = nav.on_event(TickEvent(i + 100, 0))
        if frame is not None:
            assert frame.move.x == frame.move.y == 0
    assert nav.controller.state == "cancelled"

    task = asyncio.create_task(nav.goto(GoalNear(8, 100, 0)))
    await asyncio.sleep(0)
    assert nav.controller.state == "moving"
    bot.status = replace(bot.status, session_id=2)
    assert nav.on_event(TickEvent(1001, 0)) is None
    with pytest.raises(NavigationError, match="session"):
        await task
    assert nav.on_event(TickEvent(1002, 0)) is None


@pytest.mark.asyncio
async def test_correction_and_task_cancellation_release_intent():
    bot = FakeBot()
    world = KnownFlat()
    world.bot = bot
    nav = Navigator(bot, world)
    task = asyncio.create_task(nav.goto(GoalNear(8, 100, 0)))
    await asyncio.sleep(0)
    nav.on_event(TickEvent(1, 0))
    nav.on_event(PositionEvent(Vec3(0.5, 101.62001, 0.5), correction=True))
    with pytest.raises(NavigationError, match="correction"):
        await task
    assert nav.controller.state == "failed"
    task = asyncio.create_task(nav.goto(GoalNear(8, 100, 0)))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not nav._path


@pytest.mark.asyncio
async def test_route_block_update_invalidates_before_first_frame():
    from types import SimpleNamespace
    from endpilot import PacketDecodedEvent

    bot = FakeBot()
    world = KnownFlat()
    world.bot = bot
    nav = Navigator(bot, world)
    task = asyncio.create_task(nav.goto(GoalNear(0, 100, 8)))
    await asyncio.sleep(0)
    assert nav.controller.state == "moving"
    world.obstacles.add((0, 4))
    packet = SimpleNamespace(header=SimpleNamespace(packet_id=21))
    assert nav.on_event(PacketDecodedEvent(packet)) is None
    assert nav.on_event(TickEvent(1, 0)) is None
    with pytest.raises(NavigationError, match="path invalidated"):
        await task
    assert not nav._path
    assert nav.controller.state == "coasting"


@pytest.mark.asyncio
async def test_new_goal_replaces_old_waiter_without_two_writers():
    bot = FakeBot()
    world = KnownFlat()
    world.bot = bot
    nav = Navigator(bot, world)
    old = asyncio.create_task(nav.goto(GoalNear(8, 100, 0)))
    await asyncio.sleep(0)
    nav.on_event(TickEvent(1, 0))
    new = asyncio.create_task(nav.goto(GoalNear(0, 100, 8)))
    await asyncio.sleep(0)
    with pytest.raises(NavigationError, match="stopped"):
        await old
    assert nav.last_plan.status == "complete"
    assert nav._path and nav.controller.state == "moving"
    new.cancel()
    with pytest.raises(asyncio.CancelledError):
        await new
    assert not nav._path

@pytest.mark.asyncio
async def test_goal_near_radius_uses_euclidean_acceptance():
    world = KnownFlat()
    plan = await plan_flat(world, (0, 100, 0), GoalNear(5, 100, 0, radius=1))
    assert plan.status == "complete"
    assert plan.path[-1] == (4, 100, 0)
    assert math.isclose(plan.cost, 4.0)
    plan = await plan_flat(world, (0, 100, 0), GoalNear(1, 100, 1, radius=1))
    assert plan.status == "complete"
    assert math.hypot(plan.path[-1][0] - 1, plan.path[-1][2] - 1) <= 1
    assert math.isclose(plan.cost, 1.0)
