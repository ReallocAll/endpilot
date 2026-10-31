"""Deterministic safety contracts for bounded planning at an observed-world frontier."""
import asyncio
import math
from collections import Counter

import pytest

from endpilot.navigation import GoalNear, NavigationError, plan_frontier


class Flat:
    def __init__(self, known=None, blocked=(), unknown=()):
        self.known = None if known is None else set(known)
        self.blocked = set(blocked)
        self.unknown = set(unknown)
        self.calls = Counter()

    def can_stand(self, x, y, z):
        self.calls[x, y, z] += 1
        if y != 100:
            return False
        if (x, z) in self.unknown or (self.known is not None and (x, z) not in self.known):
            return None
        return (x, z) not in self.blocked


def rectangle(xmin, xmax, zmin, zmax):
    return {(x, z) for x in range(xmin, xmax + 1) for z in range(zmin, zmax + 1)}


def check_route(world, plan, start):
    assert plan.path and plan.path[0] == start
    assert all(world.can_stand(*node) is True for node in plan.path)
    total = 0.0
    for a, b in zip(plan.path, plan.path[1:]):
        dx, dz = b[0] - a[0], b[2] - a[2]
        assert a[1] == b[1] and max(abs(dx), abs(dz)) == 1
        if dx and dz:
            assert world.can_stand(a[0] + dx, a[1], a[2]) is True
            assert world.can_stand(a[0], a[1], a[2] + dz) is True
        total += math.hypot(dx, dz)
    assert math.isclose(plan.cost, total, abs_tol=1e-9)


@pytest.mark.asyncio
async def test_known_route_complete_with_original_goal_radius():
    world = Flat()
    start = (0, 100, 0)
    for goal in (GoalNear(6, 100, 4), GoalNear(6, 100, 4, radius=1), GoalNear(0, 100, 0)):
        plan = await plan_frontier(world, start, goal)
        assert plan.status == "complete" and plan.frontier is None and not plan.progressed
        check_route(world, plan, start)
        assert math.hypot(plan.path[-1][0] - goal.x, plan.path[-1][2] - goal.z) <= goal.radius


@pytest.mark.asyncio
async def test_safe_frontier_boundary_path_cost_and_progress():
    world = Flat(known=rectangle(0, 5, -1, 1))
    plan = await plan_frontier(world, (0, 100, 0), GoalNear(9, 100, 0))
    assert plan.status == "frontier" and plan.frontier == plan.path[-1]
    assert plan.frontier == (5, 100, 0) and plan.progressed
    check_route(world, plan, (0, 100, 0))
    assert world.can_stand(6, 100, 0) is None
    assert plan.expanded <= 18 and plan.stand_queries > 0


@pytest.mark.asyncio
async def test_initial_cell_can_be_frontier_without_progress():
    plan = await plan_frontier(Flat(known={(0, 0)}), (0, 100, 0), GoalNear(8, 100, 8))
    assert plan.status == "frontier" and plan.path == ((0, 100, 0),)
    assert plan.frontier == (0, 100, 0) and not plan.progressed and plan.cost == 0


@pytest.mark.asyncio
async def test_ties_are_stable_and_rank_goal_then_cost_then_coordinates():
    world = Flat(known=rectangle(-2, 2, -2, 2))
    plans = [await plan_frontier(world, (0, 100, 0), GoalNear(10, 100, 10)) for _ in range(5)]
    assert all(p.status == "frontier" for p in plans)
    assert len({(p.path, p.frontier, p.cost, p.expanded) for p in plans}) == 1
    assert plans[0].frontier == (2, 100, 2)
    line = Flat(known={(0, 0), (1, 0), (0, 1)})
    plans = [await plan_frontier(line, (0, 100, 0), GoalNear(2, 100, 2)) for _ in range(3)]
    assert len({p.frontier for p in plans}) == 1


@pytest.mark.asyncio
async def test_wall_detour_and_no_known_wall_crossing():
    known = rectangle(-4, 4, -4, 4)
    wall = {(0, z) for z in range(-3, 4)}
    world = Flat(known=known, blocked=wall)
    plan = await plan_frontier(world, (-2, 100, 0), GoalNear(7, 100, 0))
    assert plan.status == "frontier"
    check_route(world, plan, (-2, 100, 0))
    assert not any((x, z) in wall for x, _, z in plan.path)
    assert plan.frontier[0] >= 0


@pytest.mark.asyncio
async def test_sealed_known_cells_are_unreachable_not_frontier():
    wall = rectangle(-1, 1, -1, 1) - {(0, 0)}
    plan = await plan_frontier(Flat(blocked=wall), (0, 100, 0), GoalNear(9, 100, 9))
    assert plan.status == "unreachable" and not plan.path and plan.frontier is None


@pytest.mark.asyncio
async def test_diagonal_cannot_cut_known_or_unknown_corners():
    blocked = Flat(blocked={(1, 0), (0, 1)})
    plan = await plan_frontier(blocked, (0, 100, 0), GoalNear(1, 100, 1))
    assert plan.status == "complete" and plan.cost > math.sqrt(2)
    check_route(blocked, plan, (0, 100, 0))
    unknown = Flat(known={(0, 0), (1, 1)})
    plan = await plan_frontier(unknown, (0, 100, 0), GoalNear(1, 100, 1))
    assert plan.status == "frontier" and plan.path == ((0, 100, 0),)


@pytest.mark.asyncio
async def test_unknown_behind_wall_is_not_an_exit():
    known = rectangle(-1, 1, -1, 1)
    world = Flat(known=known, blocked=known - {(0, 0)})
    plan = await plan_frontier(world, (0, 100, 0), GoalNear(9, 100, 0))
    assert plan.status == "unreachable" and not plan.path


@pytest.mark.asyncio
async def test_blocked_exact_goal_and_radius_do_not_alias():
    world = Flat(blocked={(5, 0)})
    plan = await plan_frontier(world, (0, 100, 0), GoalNear(5, 100, 0))
    assert plan.status == "unreachable"
    plan = await plan_frontier(world, (0, 100, 0), GoalNear(5, 100, 0, radius=1))
    assert plan.status == "complete" and plan.path[-1] != (5, 100, 0)
    assert math.hypot(plan.path[-1][0] - 5, plan.path[-1][2]) <= 1


@pytest.mark.asyncio
async def test_unsafe_and_unknown_start_fail_without_a_path():
    for world, status in ((Flat(blocked={(0, 0)}), "unreachable"),
                          (Flat(known={(1, 0)}), "need_world"),
                          (Flat(known={(0, 0)}, unknown={(0, 0)}), "need_world")):
        plan = await plan_frontier(world, (0, 100, 0), GoalNear(3, 100, 0))
        assert plan.status == status and not plan.path


@pytest.mark.asyncio
async def test_budget_caps_expansions_and_no_partial_route_is_executable():
    plan = await plan_frontier(Flat(), (0, 100, 0), GoalNear(40, 100, 0),
                               max_expansions=5, batch_size=2)
    assert plan.status == "budget_exhausted" and plan.expanded == 5
    assert not plan.path and plan.frontier is None and not plan.progressed
    assert plan.stand_queries < 100
    for kwargs in ({"max_expansions": 0}, {"max_expansions": 100001},
                   {"batch_size": 0}, {"batch_size": 1025}):
        with pytest.raises(ValueError):
            await plan_frontier(Flat(), (0, 100, 0), GoalNear(3, 100, 0), **kwargs)
    with pytest.raises(NavigationError, match="non-flat"):
        await plan_frontier(Flat(), (0, 100, 0), GoalNear(3, 101, 0))


@pytest.mark.asyncio
async def test_cancellation_callback_and_cooperative_yield():
    calls = 0

    def cancel():
        nonlocal calls
        calls += 1
        return calls >= 6

    with pytest.raises(asyncio.CancelledError):
        await plan_frontier(Flat(), (0, 100, 0), GoalNear(30, 100, 0),
                            batch_size=1, cancelled=cancel)
    observed = []

    async def observer():
        await asyncio.sleep(0)
        observed.append(True)

    watch = asyncio.create_task(observer())
    plan = await plan_frontier(Flat(), (0, 100, 0), GoalNear(30, 100, 0),
                               batch_size=1, max_expansions=64)
    await watch
    assert observed and plan.status == "complete"


@pytest.mark.asyncio
async def test_revalidation_detects_stale_complete_path():
    class Flip(Flat):
        def can_stand(self, x, y, z):
            result = super().can_stand(x, y, z)
            if (x, y, z) == (0, 100, 0) and self.calls[x, y, z] >= 2:
                return False
            return result

    plan = await plan_frontier(Flip(), (0, 100, 0), GoalNear(1, 100, 0))
    assert plan.status == "need_world" and not plan.path


@pytest.mark.asyncio
async def test_revalidation_detects_stale_frontier_and_diagonal_guard():
    class FlipFrontier(Flat):
        def can_stand(self, x, y, z):
            result = super().can_stand(x, y, z)
            if (x, y, z) == (0, 100, 0) and self.calls[x, y, z] >= 2:
                return None
            return result

    plan = await plan_frontier(FlipFrontier(known={(0, 0)}), (0, 100, 0), GoalNear(3, 100, 3))
    assert plan.status == "need_world" and not plan.path

    class FlipGuard(Flat):
        def can_stand(self, x, y, z):
            result = super().can_stand(x, y, z)
            if (x, y, z) == (1, 100, 0) and self.calls[x, y, z] >= 2:
                return False
            return result

    plan = await plan_frontier(FlipGuard(), (0, 100, 0), GoalNear(1, 100, 1))
    assert plan.status == "need_world" and not plan.path


@pytest.mark.asyncio
async def test_concurrent_observed_world_mutation_or_clear_is_safe():
    for clear in (False, True):
        world = Flat(known=rectangle(-2, 12, -2, 2))

        async def mutate():
            await asyncio.sleep(0)
            if clear:
                world.known.clear()
            else:
                world.blocked.add((2, 0))

        task = asyncio.create_task(mutate())
        plan = await plan_frontier(world, (0, 100, 0), GoalNear(10, 100, 0), batch_size=1)
        await task
        if plan.status in ("frontier", "complete"):
            check_route(world, plan, (0, 100, 0))
        if clear:
            assert plan.status not in ("complete", "frontier")
        next_plan = await plan_frontier(world, (0, 100, 0), GoalNear(10, 100, 0))
        if clear:
            assert next_plan.status == "need_world"


@pytest.mark.asyncio
async def test_large_observed_graph_is_bounded():
    world = Flat(known=rectangle(-100, 100, -100, 100))
    plan = await plan_frontier(world, (0, 100, 0), GoalNear(300, 100, 0),
                               max_expansions=512, batch_size=8)
    assert plan.status == "budget_exhausted" and plan.expanded == 512
    assert plan.stand_queries < 10000 and not plan.path
