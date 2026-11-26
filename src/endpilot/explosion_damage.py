"""Geometry for estimating unmitigated Bedrock explosion damage."""

from collections.abc import Callable
from fractions import Fraction
from itertools import product
from math import ceil, copysign, floor, inf, isfinite, prod, sqrt
from struct import Struct
from typing import Literal

from .block_raycast import Point, Position, _UNIT_BOX, _intersection


_Box = tuple[float, float, float, float, float, float]
_CollisionObservation = tuple[tuple[_Box, ...], _Box | None]
_Contact = Literal["interior", "boundary", "origin"]
_COLLISION_LIMITS = (-1, -1, -1, 2, 2, 2)
_MAX_AXIS_SAMPLES = 8
_MAX_CANDIDATE_CELLS = 4096
_FLOAT32 = Struct("<f")


def _float32(value: float) -> float:
    try:
        return _FLOAT32.unpack(_FLOAT32.pack(value))[0]
    except OverflowError:
        return copysign(inf, value)


def _game_distance(a: Point, b: Point) -> float:
    x, y, z = (_float32(end - start) for start, end in zip(a, b, strict=True))
    x, y, z = (_float32(value * value) for value in (x, y, z))
    return _float32(sqrt(_float32(_float32(x + y) + z)))


def _axis_samples(low: float, half: float, vertical: bool) -> tuple[float, ...] | None:
    width = _float32(2 * half)
    step = _float32(1 / _float32(_float32(2 * width) + 1))
    if step <= 1 / _MAX_AXIS_SAMPLES:
        return None
    offset = (_float32(0.1) if vertical
              else _float32((1 - _float32(floor(_float32(1 / step)) * step)) / 2))
    samples = []
    t = 0.0
    for _ in range(_MAX_AXIS_SAMPLES):
        samples.append(_float32(_float32(low + _float32(width * t)) + offset))
        t = _float32(t + step)
        if t > 1:
            return tuple(samples)
    return None


def _segment_contact(start, end, direction, reverse, box) -> _Contact | None:
    low, high = box[:3], box[3:]
    if any(max(a, b) < lo or min(a, b) > hi
           for a, b, lo, hi in zip(start, end, low, high, strict=True)):
        return None

    # The ray helper returns the first forward surface, including an exit
    # for starts inside. Use it from each outside endpoint to bound contact.
    enter, leave = 0, 1
    if not all(lo <= a <= hi for a, lo, hi in zip(start, low, high, strict=True)):
        hit = _intersection(start, direction, (0, 0, 0), box, Fraction)
        if hit is None or hit[0] > 1:
            return None
        enter = hit[0]
    if not all(lo <= b <= hi for b, lo, hi in zip(end, low, high, strict=True)):
        hit = _intersection(end, reverse, (0, 0, 0), box, Fraction)
        if hit is None or hit[0] > 1:
            return None
        leave = 1 - hit[0]

    if leave == 0:
        return "origin"
    if enter == leave or any(a == b and a in (lo, hi)
                             for a, b, lo, hi in zip(start, end, low, high, strict=True)):
        return "boundary"
    return "interior"


def _within_collision_limits(box: _Box) -> bool:
    return len(box) == 6 and all(
        _COLLISION_LIMITS[i] <= box[i] < box[i + 3] <= _COLLISION_LIMITS[i + 3]
        for i in range(3)
    )


def _world_box(position: Position, box: _Box) -> tuple[Fraction, ...]:
    return tuple(Fraction(edge) + cell for edge, cell in zip(box, position * 2, strict=True))


def _exposure(center, axes, collision_at: Callable[[Position], _CollisionObservation]) -> float | None:
    # A possible shape can reach beyond its owner cell. Include owners of
    # those shapes and of cells touched only at a voxel boundary.
    first = tuple(ceil(min(origin, axis[0]) - high)
                  for origin, axis, high in zip(center, axes, _COLLISION_LIMITS[3:], strict=True))
    last = tuple(floor(max(origin, axis[-1]) - low)
                 for origin, axis, low in zip(center, axes, _COLLISION_LIMITS[:3], strict=True))
    counts = tuple(b - a + 1 for a, b in zip(first, last, strict=True))
    if prod(counts) > _MAX_CANDIDATE_CELLS:
        return None

    blockers = []
    uncertain = []
    for position in product(*(range(a, b + 1) for a, b in zip(first, last, strict=True))):
        boxes, unknown_bounds = collision_at(position)
        if unknown_bounds is not None:
            if not _within_collision_limits(unknown_bounds):
                return None
            uncertain.append(_world_box(position, unknown_bounds))
        for box in boxes:
            if not _within_collision_limits(box):
                return None
            in_cell = tuple(max(0, v) for v in box[:3]) + tuple(min(1, v) for v in box[3:])
            if in_cell != box:
                # Native traversal for a hit outside the shape's owner cell is unresolved.
                uncertain.append(_world_box(position, box))
            if all(in_cell[i] < in_cell[i + 3] for i in range(3)):
                blockers.append((_world_box(position, in_cell), box == _UNIT_BOX))
    if not blockers and not uncertain:
        return 1.0

    start = tuple(map(Fraction, center))
    visible = 0
    for sample in product(*axes):
        end = tuple(map(Fraction, sample))
        direction = tuple(b - a for a, b in zip(start, end, strict=True))
        reverse = tuple(-slope for slope in direction)
        if not any(direction):
            return None
        ambiguous = False
        for box, unit_cube in blockers:
            contact = _segment_contact(start, end, direction, reverse, box)
            if contact == "interior":
                break
            if contact == "origin" and unit_cube and start[1] == box[4] and direction[1] > 0:
                # A ray leaving the interior of an upper cube face matches the
                # floor contact in the native range windows.
                if box[0] < start[0] < box[3] and box[2] < start[2] < box[5]:
                    continue
            ambiguous |= contact is not None
        else:
            if ambiguous or any(_segment_contact(start, end, direction, reverse, box) is not None
                                for box in uncertain):
                return None
            visible += 1
    return _float32(visible / prod(map(len, axes)))


def _estimate_game_base_damage(
    feet: Point, aabb_center: Point, aabb_half_extents: Point, center: Point, radius: float,
    collision_at: Callable[[Position], _CollisionObservation],
) -> float | None:
    """Estimate damage before difficulty and equipment for a confirmed standing player.

    All coordinates use the same dimension. The caller supplies the game feet
    and AABB half-extents, along with a read-only terrain query for the explosion
    clip. The query returns known, block-local collision boxes and an optional
    envelope for unresolved collision (including unseen cells and uncertain
    clip eligibility). Both use (min_x, min_y, min_z, max_x, max_y, max_z) in
    the supported local range [-1, 2]. Observed air is ((), None).

    Return None for unsupported inputs, work beyond the estimator's limits,
    or terrain uncertainty that affects a ray without a definite blocker.
    """
    if (not isfinite(radius) or radius <= 0
            or any(len(point) != 3 or not all(map(isfinite, point))
                   for point in (feet, aabb_center, aabb_half_extents, center))
            or any(half <= 0 for half in aabb_half_extents)):
        return None
    radius = _float32(radius)
    reach = _float32(2 * radius)
    feet = tuple(map(_float32, feet))
    center = tuple(map(_float32, center))
    low = tuple(_float32(c - half) for c, half in zip(aabb_center, aabb_half_extents, strict=True))
    high = tuple(_float32(c + half) for c, half in zip(aabb_center, aabb_half_extents, strict=True))
    if (not 0 < reach < inf or not all(map(isfinite, (*feet, *center, *low, *high)))
            or any(lo >= hi for lo, hi in zip(low, high, strict=True))):
        return None

    distance = _game_distance(feet, center)
    if not isfinite(distance):
        return None
    if distance > reach:
        return 0.0
    if distance == reach:
        return 1.0

    exposure = 1.0
    if not all(lo <= value <= hi for lo, value, hi in zip(low, center, high, strict=True)):
        axes = tuple(_axis_samples(lo, half, vertical=axis == 1)
                     for axis, (lo, half) in enumerate(zip(low, aabb_half_extents, strict=True)))
        if any(axis is None for axis in axes):
            return None
        exposure = _exposure(center, axes, collision_at)
        if exposure is None:
            return None

    impact = _float32(exposure * _float32(1 - _float32(distance / reach)))
    curve = _float32(_float32(impact * impact) + impact)
    damage = _float32(_float32(_float32(7 * curve) * radius) + 1)
    return damage if isfinite(damage) else None
