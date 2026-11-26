"""Ray geometry for a local player's query over observed entities."""

from collections.abc import Callable, Iterable
from math import floor, inf, isfinite
from typing import Literal, TYPE_CHECKING

from .block_raycast import BlockRaycastResult, Point, _direction, _validate_distance

if TYPE_CHECKING:
    from .entities import EntityState


Box = tuple[Point, Point]
_View = tuple[Point, tuple[float, float]]
_Status = Literal["hit", "miss", "unknown"]
_REPORT_MARGIN = 0.10


def _entry(eye: Point, direction: Point, box: Box) -> float | None:
    low, high = box
    entry, leave = 0.0, inf
    for axis in range(3):
        origin, slope = eye[axis], direction[axis]
        if slope == 0:
            if not low[axis] <= origin <= high[axis]:
                return None
            continue
        first, last = (low[axis] - origin) / slope, (high[axis] - origin) / slope
        if slope < 0:
            first, last = last, first
        entry, leave = max(entry, first), min(leave, last)
        if entry > leave:
            return None
    return entry


def expanded_box(box: Box) -> Box:
    low, high = box
    return tuple(value - _REPORT_MARGIN for value in low), tuple(value + _REPORT_MARGIN for value in high)


def trace_entities(
    pose: _View | None,
    candidates: Iterable[tuple["EntityState", Box]],
    unresolved: Iterable[Box],
    *,
    max_distance: float,
    trace_blocks: Callable[[float], BlockRaycastResult],
) -> tuple[_Status, "EntityState | None", float | None]:
    """Resolve known nominal boxes and potential envelopes on a single view ray."""
    _validate_distance(max_distance)
    miss = ("miss", None, None)
    unknown = ("unknown", None, None)
    if pose is None or not all(map(isfinite, (*pose[0], *pose[1]))):
        return unknown
    eye, rotation = pose
    direction = _direction(rotation)
    reach = floor(max_distance)
    first_unresolved = inf
    best = None
    best_key = (inf, 0)

    for state, box in candidates:
        nominal = _entry(eye, direction, box)
        if nominal is None or nominal > reach:
            continue
        reported = _entry(eye, direction, expanded_box(box))
        assert reported is not None
        key = reported, state.runtime_id
        if key < best_key:
            best, best_key = state, key

    for envelope in unresolved:
        entry = _entry(eye, direction, envelope)
        if entry is not None and entry <= reach:
            first_unresolved = min(first_unresolved, entry)

    limit = min(best_key[0], first_unresolved)
    if limit == inf:
        return miss
    obstacle = trace_blocks(limit)
    if obstacle.status == "unknown":
        return unknown
    if obstacle.status == "hit":
        # A coincident block and reported entity entry have no measured priority.
        assert obstacle.distance is not None
        return miss if obstacle.distance < limit else unknown
    if first_unresolved <= best_key[0]:
        return unknown
    return "hit", best, best_key[0]
