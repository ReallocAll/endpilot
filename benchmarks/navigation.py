"""Reproducible synthetic flat A* benchmarks; NOT a real-BDS movement test.

Run from an EndPilot checkout with installed SDK:
    python benchmarks/navigation.py

Workloads use an in-memory can_stand oracle (no subchunk decode, packets,
network, block collision shapes, or BDS). Do not compare these search times
against real-world navigation times.
"""
from __future__ import annotations

import asyncio
import json
import platform

from endpilot.navigation import GoalNear, plan_flat, plan_frontier


class SyntheticFlat:
    def __init__(self, known: set[tuple[int, int]], blocked: set[tuple[int, int]]):
        self.known = known
        self.blocked = blocked

    def can_stand(self, x: int, y: int, z: int) -> bool | None:
        if y != 100:
            return False
        if (x, z) not in self.known:
            return None
        return (x, z) not in self.blocked


async def measure(name: str, world: SyntheticFlat, goal: GoalNear) -> dict:
    result = await plan_flat(world, (0, 100, 0), goal, max_expansions=4096)
    return {
        "map": name, "status": result.status,
        "expanded": result.expanded, "peak_open": result.peak_open,
        "stand_queries": result.stand_queries, "path_nodes": len(result.path),
        "path_cost": result.cost if result.path else None,
        "search_ms": round(result.elapsed_ms, 3),
        "max_batch_ms": round(result.max_batch_ms, 3),
    }



async def measure_frontier(name: str, world: SyntheticFlat, goal: GoalNear,
                           budget: int = 4096) -> dict:
    result = await plan_frontier(world, (0, 100, 0), goal, max_expansions=budget)
    return {
        "map": name, "status": result.status,
        "budget": budget, "search_truncated": result.search_truncated,
        "progressed": result.progressed,
        "expanded": result.expanded, "stand_queries": result.stand_queries,
        "path_nodes": len(result.path), "frontier": result.frontier,
        "path_cost": result.cost if result.path else None,
        "search_ms": round(result.elapsed_ms, 3),
        "max_batch_ms": round(result.max_batch_ms, 3),
    }


async def main() -> None:
    rows = []
    for distance in (128, 512, 2048):
        known = {(x, z) for x in range(-2, distance + 3) for z in range(-2, 3)}
        rows.append(await measure(f"open_{distance}", SyntheticFlat(known, set()),
                                  GoalNear(distance, 100, 0)))

    dense_known = {(x, z) for x in range(-30, 31) for z in range(-30, 31)}
    dense_blocked = {(x, z) for x in range(-20, 21) for z in range(-20, 21)
                     if (x * 7 + z * 13) % 11 < 3 and (x, z) not in ((0, 0), (15, 15))}
    rows.append(await measure("dense_61", SyntheticFlat(dense_known, dense_blocked),
                              GoalNear(15, 100, 15)))

    corridor_known = {(x, z) for x in range(-3, 4) for z in range(-1, 503)}
    corridor_blocked = {(x, z) for x in (-1, 1) for z in range(-1, 501)}
    rows.append(await measure("corridor_500", SyntheticFlat(corridor_known, corridor_blocked),
                              GoalNear(0, 100, 500)))

    enclosed_known = {(x, z) for x in range(-100, 101) for z in range(-100, 101)}
    enclosed_blocked = {(x, z) for x in range(-1, 2) for z in range(-1, 2)
                        if (x, z) != (0, 0)}
    rows.append(await measure("unreachable", SyntheticFlat(enclosed_known, enclosed_blocked),
                              GoalNear(50, 100, 50)))


    # Five bounded frontier workloads; these are synthetic standability
    # oracles, not real SubChunk observations or BDS movement benchmarks.
    frontier_rows = []
    frontier_rows.append(await measure_frontier(
        "small_local", SyntheticFlat({(x, z) for x in range(5) for z in range(-1, 2)}, set()),
        GoalNear(10, 100, 0)))
    frontier_rows.append(await measure_frontier(
        "multiple_frontiers", SyntheticFlat({(x, z) for x in range(-8, 9) for z in range(-8, 9)}, set()),
        GoalNear(20, 100, 10)))
    frontier_rows.append(await measure_frontier(
        "sealed", SyntheticFlat({(x, z) for x in range(-4, 5) for z in range(-4, 5)},
                                {(x, z) for x in range(-1, 2) for z in range(-1, 2)
                                 if (x, z) != (0, 0)}),
        GoalNear(20, 100, 0)))
    frontier_rows.append(await measure_frontier(
        "long_corridor", SyntheticFlat({(x, z) for x in range(-1, 2) for z in range(260)},
                                        {(x, z) for x in (-1, 1) for z in range(260)}),
        GoalNear(0, 100, 300)))
    frontier_rows.append(await measure_frontier(
        "sparse_unknown_boundary",
        SyntheticFlat({(x, z) for x in range(-12, 13) for z in range(-12, 13)
                       if (x * 7 + z * 11) % 17 not in (0, 1) or (x, z) == (0, 0)}, set()),
        GoalNear(40, 100, 40)))
    # Budget-boundary runs: finite known partial frontiers versus no
    # frontier within an equally strict expansion cap.
    budget_rows = [
        await measure_frontier(
            "bounded_1000",
            SyntheticFlat({(x, z) for x in range(40) for z in range(25)}, set()),
            GoalNear(300, 100, 0), budget=128),
        await measure_frontier(
            "bounded_gt_4096",
            SyntheticFlat({(x, z) for x in range(130) for z in range(60)}, set()),
            GoalNear(300, 100, 0)),
        await measure_frontier(
            "interior_without_frontier",
            SyntheticFlat({(x, z) for x in range(-160, 161)
                           for z in range(-160, 161)}, set()),
            GoalNear(300, 100, 0), budget=128),
    ]
    assert [(r["status"], r["search_truncated"]) for r in budget_rows] == [
        ("frontier", True), ("frontier", True), ("budget_exhausted", True)
    ]

    assert [r["status"] for r in frontier_rows] == [
        "frontier", "frontier", "unreachable", "frontier", "frontier"
    ]

    assert [r["status"] for r in rows] == ["complete"] * 5 + ["unreachable"]
    print(json.dumps({"python": platform.python_version(),
                      "environment": "synthetic in-memory known-flat oracle",
                      "results": rows, "frontier_results": frontier_rows,
                      "budget_results": budget_rows}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
