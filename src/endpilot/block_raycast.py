"""Read-only ray traversal through observed primary block outlines."""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Context, Decimal
from fractions import Fraction
from functools import cache
from itertools import product
from math import copysign, cos, floor, inf, isfinite, nextafter, radians, remainder, sin, sqrt
from typing import Literal


Position = tuple[int, int, int]
Point = tuple[float, float, float]
_Scalar = float | Fraction
_Vector = tuple[_Scalar, _Scalar, _Scalar]
OutlineObservation = tuple[tuple[float, ...] | None, bool]
MiningFaceStatus = Literal["hit", "excluded", "unknown", "no_sample"]
# Faces on the low and high bounds of each axis, in Bedrock's numbering.
_FACES = ((4, 5), (0, 1), (2, 3))
_NEIGHBORS = ((0, 0, 0),) + tuple(offset for offset in product((-1, 0, 1), repeat=3) if any(offset))
_FACE_SAMPLES = ((2, 2), (1, 1), (1, 3), (3, 1), (3, 3))
_UNIT_BOX = (0, 0, 0, 1, 1, 1)


@dataclass(frozen=True, slots=True)
class BlockRaycastResult:
    """One primary block outline hit, a clear ray, or an unresolved cell."""

    status: Literal["hit", "miss", "unknown"]
    dimension_id: int
    position: Position | None = None
    face: int | None = None
    click_pos: Point | None = None
    distance: float | None = None


@dataclass(frozen=True, slots=True)
class MiningFaceResult:
    """One sampled mining face or the reason a face could not be selected."""

    status: MiningFaceStatus
    dimension_id: int
    position: Position
    face: int | None = None
    click_pos: Point | None = None
    distance: float | None = None


def _sin_cos(degrees: float) -> tuple[float, float]:
    turn = remainder(degrees, 360.0)
    if turn == 0:
        return 0., 1.
    if abs(turn) == 180:
        return 0., -1.
    if abs(turn) == 90:
        return copysign(1., turn), 0.
    angle = radians(turn)
    sine, cosine = sin(angle), cos(angle)
    # Match exact diagonal components without collapsing nearby angles.
    if abs(turn) in (45., 135.):
        diagonal = sqrt(0.5)
        return copysign(diagonal, sine), copysign(diagonal, cosine)
    return sine, cosine


def _direction(rotation: tuple[float, float]) -> Point:
    pitch_sin, pitch_cos = _sin_cos(rotation[0])
    yaw_sin, yaw_cos = _sin_cos(rotation[1])
    return -yaw_sin * pitch_cos, -pitch_sin, yaw_cos * pitch_cos


def _intersection(
    origin: _Vector, direction: _Vector, position: Position, box: tuple[float, ...],
    coordinate: Callable[[float], _Scalar],
):
    enter, leave = -inf, inf
    enter_face = leave_face = None
    for axis, (eye, slope, cell) in enumerate(zip(origin, direction, position, strict=True)):
        low, high = coordinate(box[axis]) + cell, coordinate(box[axis + 3]) + cell
        if slope == 0:
            if not low <= eye <= high:
                return None
            continue
        near, far = (low - eye) / slope, (high - eye) / slope
        near_face, far_face = _FACES[axis]
        if slope < 0:
            near, far = far, near
            near_face, far_face = far_face, near_face
        if near > enter:
            enter, enter_face = near, near_face
        if far < leave:
            leave, leave_face = far, far_face
        if enter > leave or leave < 0:
            return None
    # When the eye is inside a box, its first forward surface is the exit.
    return (enter, enter_face) if enter >= 0 else (leave, leave_face)


def trace_blocks(
    eye: Point | None, rotation: tuple[float, float], *, dimension_id: int,
    max_distance: float, outline_at: Callable[[Position], OutlineObservation],
) -> BlockRaycastResult:
    _validate_distance(max_distance)
    if eye is None or not all(map(isfinite, (*eye, *rotation))):
        return BlockRaycastResult("unknown", dimension_id)
    return _trace_blocks(eye, _direction(rotation), dimension_id, max_distance, outline_at)


def trace_blocks_towards(
    eye: Point | None, point: Point, *, dimension_id: int,
    max_distance: float, outline_at: Callable[[Position], OutlineObservation],
) -> BlockRaycastResult:
    """Trace from the current eye in the direction of a finite world point."""
    _validate_distance(max_distance)
    if len(point) != 3 or not all(map(isfinite, point)):
        raise ValueError("point must contain three finite coordinates")
    if eye is None or not all(map(isfinite, eye)):
        return BlockRaycastResult("unknown", dimension_id)
    x, y, z = map(Fraction, eye)
    origin = (x, y, z)
    direction = (Fraction(point[0]) - x, Fraction(point[1]) - y, Fraction(point[2]) - z)
    length_squared = sum(component * component for component in direction)
    if length_squared == 0:
        return BlockRaycastResult("unknown", dimension_id)
    return _trace_blocks(origin, direction, dimension_id, max_distance, outline_at,
                         direction_length_squared=length_squared)


def _face_samples(low, high, axis, plane):
    side, other = (index for index in range(3) if index != axis)
    points = []
    for u, v in _FACE_SAMPLES:
        point = list(low)
        point[axis] = plane
        point[side] += (high[side] - low[side]) * u / 4
        point[other] += (high[other] - low[other]) * v / 4
        points.append(tuple(point))
    return tuple(points)


def _covers_projection(eye, low, high, blocker):
    for axis, block_low in enumerate(blocker):
        start = eye[axis]
        block_high = block_low + 1
        if start < block_low < low[axis]:
            plane = block_low
        elif high[axis] < block_high < start:
            plane = block_high
        else:
            continue
        scales = tuple((plane - start) / (edge - start) for edge in (low[axis], high[axis]))
        # Project the complete envelope onto one near face. Strictly interior
        # bounds establish that all these rays enter the same block before it.
        if all(blocker[side] < eye[side] + scale * (edge - eye[side]) < blocker[side] + 1
               for side in range(3) if side != axis
               for scale in scales for edge in (low[side], high[side])):
            return True
    return False


def _outline_is_screened(eye, low, high, dimension_id, max_distance, outline_at):
    if all(lo <= start <= hi for lo, hi, start in zip(low, high, eye, strict=True)):
        return False

    def screening_outline(position):
        outline, _ = outline_at(position)
        # An unknown primary voxel still stops the search. Ignore neighboring
        # overhangs, and look past known shapes that cannot screen the envelope.
        if outline is None:
            return None, False
        if outline == _UNIT_BOX and _covers_projection(eye, low, high, position):
            return outline, False
        return (), False

    direction = tuple((lo + hi) / 2 - start for lo, hi, start in zip(low, high, eye, strict=True))
    # Trace only to the entrance of the envelope, or the range limit when nearer.
    front = _intersection(eye, direction, (0, 0, 0), (*low, *high), Fraction)
    assert front is not None
    direction = tuple(value * front[0] for value in direction)
    length_squared = sum(value * value for value in direction)
    hit = _trace_blocks(eye, direction, dimension_id, max_distance, screening_outline,
                        direction_length_squared=length_squared,
                        stop_at_point=length_squared <= Fraction(max_distance) ** 2)
    return hit.status == "hit"


def _block_interaction_probe(
    position: Position, *, dimension_id: int, max_distance: float,
    outline_at: Callable[[Position], OutlineObservation], required_face: int | None = None,
) -> Callable[[Point], BlockRaycastResult]:
    """Prepare a target's face samples for one synchronous goal evaluation."""
    probe = _target_face_probe(position, dimension_id=dimension_id, max_distance=max_distance,
                               outline_at=outline_at, prove_known_screen=False, required_face=required_face)
    miss = BlockRaycastResult("miss", dimension_id)
    unknown = BlockRaycastResult("unknown", dimension_id)

    def trace(eye):
        status, hit = probe(eye)
        if hit is not None:
            return hit
        return unknown if status == "unknown" else miss

    return trace


def trace_mining_face(
    eye: Point | None, position: Position, *, dimension_id: int, max_distance: float,
    outline_at: Callable[[Position], OutlineObservation],
    check_sample: Callable[[BlockRaycastResult], Literal["hit", "miss", "unknown"]] | None = None,
) -> MiningFaceResult:
    status, hit = _target_face_probe(position, dimension_id=dimension_id, max_distance=max_distance,
                                     outline_at=outline_at, prove_known_screen=True, check_sample=check_sample)(eye)
    if hit is None:
        return MiningFaceResult(status, dimension_id, position)
    return MiningFaceResult("hit", dimension_id, position, hit.face, hit.click_pos, hit.distance)


def _target_face_probe(
    position: Position, *, dimension_id: int, max_distance: float,
    outline_at: Callable[[Position], OutlineObservation], prove_known_screen: bool,
    check_sample: Callable[[BlockRaycastResult], Literal["hit", "miss", "unknown"]] | None = None,
    required_face: int | None = None,
) -> Callable[[Point | None], tuple[MiningFaceStatus, BlockRaycastResult | None]]:
    outline, overhang = outline_at(position)
    if outline == ():
        return lambda _eye: ("excluded", None)
    if outline is None:
        # The ray model admits unresolved overhang into adjacent voxels.
        bounds = (-1, -1, -1, 2, 2, 2) if overhang else _UNIT_BOX
    else:
        bounds = outline
    low = tuple(cell + Fraction(bound) for cell, bound in zip(position, bounds[:3], strict=True))
    high = tuple(cell + Fraction(bound) for cell, bound in zip(position, bounds[3:], strict=True))
    samples = (() if outline is None else tuple(
        (_face_samples(low, high, axis, low[axis]), _face_samples(low, high, axis, high[axis]))
        for axis in range(3)
    ))
    reach_squared = Fraction(max_distance) ** 2

    @cache
    def probe(eye):
        if eye is None or not all(map(isfinite, eye)):
            return "unknown", None
        origin = tuple(map(Fraction, eye))
        gap_squared = sum(max(0, lo - value, value - hi) ** 2
                          for value, lo, hi in zip(origin, low, high, strict=True))
        if gap_squared > reach_squared:
            return "excluded", None
        if outline is None:
            status = ("excluded" if _outline_is_screened(origin, low, high, dimension_id, max_distance, outline_at)
                      else "unknown")
            return status, None

        unresolved = False
        for axis, (lower, upper) in enumerate(samples):
            if origin[axis] < low[axis]:
                face, points = _FACES[axis][0], lower
            elif origin[axis] > high[axis]:
                face, points = _FACES[axis][1], upper
            else:
                continue
            if required_face is not None and face != required_face:
                continue
            for point in points:
                # A point inside a facing face is the first possible target surface.
                direction = tuple(value - start for value, start in zip(point, origin, strict=True))
                length_squared = sum(value * value for value in direction)
                if length_squared > reach_squared:
                    continue
                hit = _trace_blocks(origin, direction, dimension_id, max_distance, outline_at,
                                    direction_length_squared=length_squared, stop_at_point=True)
                if (hit.status == "hit" and hit.position == position
                        and (required_face is None or hit.face == required_face)):
                    status = "hit" if check_sample is None else check_sample(hit)
                    if status == "hit":
                        return "hit", hit
                    unresolved |= status == "unknown"
                else:
                    unresolved |= hit.status == "unknown"
        if prove_known_screen and _outline_is_screened(origin, low, high, dimension_id, max_distance, outline_at):
            return "excluded", None
        return ("unknown" if unresolved else "no_sample"), None

    return probe


def _validate_distance(max_distance: float) -> None:
    if not isfinite(max_distance) or max_distance < 0:
        raise ValueError("max_distance must be finite and nonnegative")


def _reaching_distance(squared: Fraction) -> float:
    # Estimate the root with spare precision, then round up using the exact square.
    context = Context(prec=40, traps=[])
    ratio = context.divide(Decimal(squared.numerator), Decimal(squared.denominator))
    estimate = float(context.sqrt(ratio))
    return nextafter(estimate, inf) if Fraction(estimate) ** 2 < squared else estimate


def _trace_blocks(
    eye: _Vector, direction: _Vector, dimension_id: int, max_distance: float,
    outline_at: Callable[[Position], OutlineObservation], *, direction_length_squared: Fraction | None = None,
    stop_at_point: bool = False,
) -> BlockRaycastResult:
    coordinate = float if direction_length_squared is None else Fraction
    # Compare against the exact world range without rounding the direction or plane parameter.
    parameter_limit_squared = (None if direction_length_squared is None
                               else 1 if stop_at_point else Fraction(max_distance) ** 2 / direction_length_squared)

    def in_range(parameter: _Scalar) -> bool:
        if parameter_limit_squared is None:
            return parameter <= max_distance
        return parameter * parameter <= parameter_limit_squared

    cells = []
    steps = []
    for start, slope in zip(eye, direction, strict=True):
        cell = floor(start)
        step = (slope > 0) - (slope < 0)
        if step < 0 and start == cell:
            cell -= 1
        cells.append(cell)
        steps.append(step)

    def boundary_parameter(axis: int) -> float:
        if steps[axis] == 0:
            return inf
        # Use the same eye-relative formula as outline intersection at a face.
        boundary = cells[axis] + (steps[axis] > 0)
        return (boundary - eye[axis]) / direction[axis]

    crossings = [boundary_parameter(axis) for axis in range(3)]

    observe = cache(outline_at)
    entry = 0.0
    while True:
        position = (cells[0], cells[1], cells[2])
        crossing = min(crossings)
        crossing_in_range = in_range(crossing)
        nearest = None
        for offset in _NEIGHBORS:
            dx, dy, dz = offset
            owner = (cells[0] + dx, cells[1] + dy, cells[2] + dz)
            outline, unresolved_overhang = observe(owner)
            if outline is None and (owner == position or unresolved_overhang):
                return BlockRaycastResult("unknown", dimension_id, owner)
            # A neighboring box matters only if its interior extends into this voxel.
            if not outline or not all(outline[i] + offset[i] < 1 and outline[i + 3] + offset[i] > 0
                                      for i in range(3)):
                continue
            intersection = _intersection(eye, direction, owner, outline, coordinate)
            if intersection is not None:
                parameter, face = intersection
                if not entry <= parameter <= crossing or (not crossing_in_range and not in_range(parameter)):
                    continue
                if nearest is None or parameter < nearest[0]:
                    nearest = (parameter, face, owner, outline)
        if nearest is not None:
            parameter, face, owner, outline = nearest
            click = tuple(float(max(outline[i], min(outline[i + 3], eye[i] + direction[i] * parameter - owner[i])))
                          for i in range(3))
            distance = (float(parameter) if direction_length_squared is None
                        else _reaching_distance(parameter * parameter * direction_length_squared))
            return BlockRaycastResult("hit", dimension_id, owner, face, click, distance)
        if not crossing_in_range:
            return BlockRaycastResult("miss", dimension_id)
        entry = crossing
        # Move across coincident planes together at a voxel edge or corner.
        for axis, time in enumerate(crossings):
            if time == crossing:
                cells[axis] += steps[axis]
                crossings[axis] = boundary_parameter(axis)
