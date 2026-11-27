"""Immutable goals and bounded planning over observed standing positions."""

from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from heapq import heappop, heappush
from itertools import count
import math
from typing import Literal

from .block_raycast import BlockRaycastResult, _UNIT_BOX, _block_interaction_probe, _trace_blocks, _validate_distance
from .blocks import FACE_OFFSETS


Cell = tuple[int, int, int]
Point = tuple[float, float, float]
_STANDING_HEAD_HEIGHT = 1.52
_HORIZONTAL_STEPS = ((0, 1), (1, 0), (0, -1), (-1, 0))
_HORIZONTAL_FACES = {offset: face for face, offset in FACE_OFFSETS.items() if offset[1] == 0}
_STONE_DROP_HEIGHTS = (2, 3)
_LADDER_APPROACH = 2
_LADDER_STRIDE = _LADDER_APPROACH + 1
_LADDER_DECK_LENGTH = 5
_LADDER_DESCENT_HEIGHTS = range(4, 7)
_LOWER_SLAB_STRIDE = 4
_WATER_STRIDE = 6
_WATER_DECK_LENGTH = 5


def _cell(position) -> Cell:
    values = tuple(position)
    if len(values) != 3 or any(not isinstance(value, int) for value in values):
        raise ValueError("expected three integer block coordinates")
    return values


def _xz_cell(position) -> tuple[int, int]:
    values = tuple(position)
    if len(values) != 2 or any(not isinstance(value, int) for value in values):
        raise ValueError("expected two integer block coordinates")
    return values


def _radius(value) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("radius must be finite and nonnegative")
    return value


@dataclass(frozen=True, slots=True)
class FootGoal:
    """Stand at the center of a specified feet block."""

    position: Cell

    def __post_init__(self):
        object.__setattr__(self, "position", _cell(self.position))


@dataclass(frozen=True, slots=True)
class XZGoal:
    """Stand in the specified feet column at any height."""

    position: tuple[int, int]

    def __post_init__(self):
        object.__setattr__(self, "position", _xz_cell(self.position))


@dataclass(frozen=True, slots=True)
class NearXZGoal:
    """Stand within a horizontal radius of the specified feet column."""

    position: tuple[int, int]
    radius: float

    def __post_init__(self):
        object.__setattr__(self, "position", _xz_cell(self.position))
        object.__setattr__(self, "radius", _radius(self.radius))


@dataclass(frozen=True, slots=True)
class YGoal:
    """Stand at the specified feet block height in any column."""

    y: int

    def __post_init__(self):
        if not isinstance(self.y, int):
            raise ValueError("expected an integer feet block height")


@dataclass(frozen=True, slots=True)
class NearGoal:
    """Stand within a three-dimensional radius of a world point."""

    position: Point
    radius: float

    def __post_init__(self):
        position = tuple(map(float, self.position))
        if len(position) != 3 or not all(map(math.isfinite, position)):
            raise ValueError("expected three finite world coordinates")
        object.__setattr__(self, "position", position)
        object.__setattr__(self, "radius", _radius(self.radius))


@dataclass(frozen=True, slots=True)
class BlockFaceGoal:
    """Stand beside a horizontal face of an observed, nonair block."""

    position: Cell

    def __post_init__(self):
        object.__setattr__(self, "position", _cell(self.position))


@dataclass(frozen=True, slots=True)
class BlockInteractionGoal:
    """Stand where a supported face sample first hits the requested block in range."""

    position: Cell
    max_distance: float = 5.0

    def __post_init__(self):
        object.__setattr__(self, "position", _cell(self.position))
        distance = float(self.max_distance)
        _validate_distance(distance)
        object.__setattr__(self, "max_distance", distance)


@dataclass(frozen=True, slots=True)
class BlockPlacementGoal:
    """Stand where a clear ray hits a full cube's face adjoining the air destination."""

    destination: Cell
    max_distance: float = 5.0

    def __post_init__(self):
        object.__setattr__(self, "destination", _cell(self.destination))
        distance = float(self.max_distance)
        _validate_distance(distance)
        object.__setattr__(self, "max_distance", distance)


@dataclass(frozen=True, slots=True)
class EntityNearGoal:
    """Stand near a player or ordinary villager with observed feet, by session unique ID."""

    entity_id: int
    radius: float

    def __post_init__(self):
        if not isinstance(self.entity_id, int) or self.entity_id == 0:
            raise ValueError("expected a nonzero actor unique ID")
        object.__setattr__(self, "radius", _radius(self.radius))


@dataclass(frozen=True, slots=True)
class EntityVisibilityGoal:
    """Stand within range of a feet-relative aim point with a clear block ray."""

    entity_id: int
    aim_offset: Point = field(kw_only=True)
    max_distance: float = field(kw_only=True)

    def __post_init__(self):
        if not isinstance(self.entity_id, int) or self.entity_id == 0:
            raise ValueError("expected a nonzero actor unique ID")
        offset = tuple(map(float, self.aim_offset))
        if len(offset) != 3 or not all(map(math.isfinite, offset)):
            raise ValueError("aim_offset must contain three finite world-axis coordinates")
        distance = float(self.max_distance)
        _validate_distance(distance)
        object.__setattr__(self, "aim_offset", offset)
        object.__setattr__(self, "max_distance", distance)


@dataclass(frozen=True, slots=True)
class AnyGoal:
    """Stand at a position satisfying at least one of the supplied goals."""

    goals: "tuple[NavigationGoal, ...]"

    def __post_init__(self):
        object.__setattr__(self, "goals", _goal_group(self.goals))


@dataclass(frozen=True, slots=True)
class AllGoal:
    """Stand at one position satisfying every supplied goal."""

    goals: "tuple[NavigationGoal, ...]"

    def __post_init__(self):
        object.__setattr__(self, "goals", _goal_group(self.goals))


type NavigationGoal = (
    FootGoal | XZGoal | NearXZGoal | YGoal | NearGoal | BlockFaceGoal | BlockInteractionGoal |
    BlockPlacementGoal | EntityNearGoal | EntityVisibilityGoal | AnyGoal | AllGoal
)


def _goal_group(values):
    values = tuple(values)
    if not values:
        raise ValueError("a composite goal needs at least one child")
    for value in values:
        _require_goal(value)
    return values


def _require_goal(goal) -> None:
    if not isinstance(goal, NavigationGoal.__value__):
        raise TypeError("expected a navigation goal")


@dataclass(frozen=True, slots=True)
class NavigationCosts:
    """Positive costs for directed standing steps.

    A gap jump costs two walks. A stone ledge drop costs one downward step per block.
    A ladder traversal costs one upward or downward step per rung.
    A water crossing costs one walk per column of horizontal displacement.
    """

    walk: float = 1.0
    up: float = 1.0
    down: float = 1.0

    def __post_init__(self):
        for name in ("walk", "up", "down"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError("navigation costs must be positive and finite")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class NavigationRules:
    """Search bounds and budgets, with optional verified still-water crossings.

    max_expansions also sets a separate limit on entity visibility ray cells.
    Fully observed dry routes take priority. Swimming currently supports the
    calibrated orthogonal crossing of a four-column, three-block-deep pool.
    """

    search_radius: int = 8
    vertical_radius: int = 4
    max_expansions: int = 16384
    costs: NavigationCosts = NavigationCosts()
    allow_swimming: bool = False

    def __post_init__(self):
        for name in ("search_radius", "vertical_radius", "max_expansions"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if not isinstance(self.costs, NavigationCosts):
            raise TypeError("expected NavigationCosts")
        if not isinstance(self.allow_swimming, bool):
            raise TypeError("allow_swimming must be a bool")


@dataclass(frozen=True, slots=True)
class BlockFaceMatch:
    position: Cell
    face: int


@dataclass(frozen=True, slots=True)
class BlockInteractionMatch(BlockFaceMatch):
    """A verified surface point in the target block's local coordinates."""

    click_pos: Point


@dataclass(frozen=True, slots=True)
class BlockPlacementMatch(BlockInteractionMatch):
    """A click on ``position`` and ``face`` targeting the adjacent ``destination``."""

    destination: Cell


def _aim_point(matches: tuple[BlockFaceMatch, ...]) -> Point | None:
    for match in matches:
        if isinstance(match, BlockInteractionMatch):
            return tuple(cell + offset for cell, offset in zip(match.position, match.click_pos, strict=True))
    if not matches:
        return None
    match = matches[0]
    return tuple(cell + 0.5 + offset * 0.5 for cell, offset in
                 zip(match.position, FACE_OFFSETS[match.face], strict=True))


@dataclass(frozen=True, slots=True)
class NavigationPlan:
    """Read-only route snapshot. Nodes include the initial and final feet blocks."""

    goal: NavigationGoal
    rules: NavigationRules
    dimension_id: int
    start: Cell | None
    status: Literal["found", "unknown", "unreachable", "budget"]
    nodes: tuple[Cell, ...] = ()
    matches: tuple[BlockFaceMatch, ...] = ()
    cost: Fraction | None = None
    expanded: int = 0

    @property
    def endpoint(self) -> Cell | None:
        return self.nodes[-1] if self.nodes else None


@dataclass(frozen=True, slots=True)
class NavigationResult:
    """An execution outcome, submitted moving frames and final block faces."""

    status: Literal["reached", "unknown", "unreachable", "budget", "stopped"]
    frames: int
    expanded: int = 0
    stand_position: Cell | None = None
    matches: tuple[BlockFaceMatch, ...] = ()


@dataclass(frozen=True, slots=True)
class NavigationEvent:
    """An observation from one execution of the requested navigation goal."""

    kind: Literal["started", "plan", "invalidated", "transition_completed", "finished"]
    goal: NavigationGoal
    plan: NavigationPlan | None = None
    adopted: bool | None = None
    reason: Literal["position_changed", "stance_unavailable", "edge_unavailable", "goal_unmatched",
                    "dimension_changed", "cancelled", "error"] | None = None
    result: NavigationResult | None = None


class _VisibilityBudgetExhausted(Exception):
    pass


class _VisibilityScanBudget:
    def __init__(self, limit: int, expired: Callable[[], bool] | None = None):
        self._remaining = limit
        self._expired = expired

    def check_deadline(self) -> None:
        if self._expired is not None and self._expired():
            raise _VisibilityBudgetExhausted

    def visit_cell(self) -> None:
        self.check_deadline()
        if self._remaining <= 0:
            raise _VisibilityBudgetExhausted
        self._remaining -= 1


@dataclass(frozen=True, slots=True)
class _GoalState:
    goal: NavigationGoal
    point: Point | None = None
    block_exists: bool | None = None
    children: "tuple[_GoalState, ...]" = ()
    interaction_probe: Callable[[Point], BlockRaycastResult] | None = None
    visibility_probe: Callable[[Point], bool | None] | None = None

    def check(
        self, cell: Cell, *, eye: Point | None = None, visibility_standing: Callable[[], bool] | None = None,
    ) -> tuple[bool | None, tuple[BlockFaceMatch, ...]]:
        goal = self.goal
        if isinstance(goal, FootGoal):
            return cell == goal.position, ()
        if isinstance(goal, XZGoal):
            return (cell[0], cell[2]) == goal.position, ()
        if isinstance(goal, NearXZGoal):
            dx, dz = cell[0] - goal.position[0], cell[2] - goal.position[1]
            # Feet blocks have integer coordinates. An exact squared bound
            # preserves the inclusive comparison for all finite radii.
            numerator, denominator = goal.radius.as_integer_ratio()
            return dx * dx + dz * dz <= numerator**2 // denominator**2, ()
        if isinstance(goal, YGoal):
            return cell[1] == goal.y, ()
        if isinstance(goal, (NearGoal, EntityNearGoal)):
            if self.point is None:
                return None, ()
            center = cell[0] + 0.5, cell[1], cell[2] + 0.5
            return math.dist(center, self.point) <= goal.radius, ()
        if isinstance(goal, BlockFaceGoal):
            delta = tuple(a - b for a, b in zip(cell, goal.position))
            face = _HORIZONTAL_FACES.get(delta)
            if face is None or self.block_exists is False:
                return False, ()
            if self.block_exists is None:
                return None, ()
            return True, (BlockFaceMatch(goal.position, face),)
        if isinstance(goal, EntityVisibilityGoal):
            if self.visibility_probe is None or (visibility_standing is not None and not visibility_standing()):
                return None, ()
            if eye is None:
                x, y, z = cell
                head = (x + 0.5, y + _STANDING_HEAD_HEIGHT, z + 0.5)
            else:
                # The synchronized input origin is 1.62 blocks above the feet.
                x, y, z = eye
                head = (x, y - 1.62 + _STANDING_HEAD_HEIGHT, z)
            return self.visibility_probe(head), ()
        if self.interaction_probe is not None:
            x, y, z = cell
            if isinstance(goal, BlockPlacementGoal) and goal.destination in (cell, (x, y + 1, z)):
                return False, ()
            hit = self.interaction_probe(eye if eye is not None else (x + 0.5, y + 1.62, z + 0.5))
            if hit.status == "hit":
                assert hit.position is not None and hit.face is not None and hit.click_pos is not None
                if isinstance(goal, BlockPlacementGoal):
                    return True, (BlockPlacementMatch(hit.position, hit.face, hit.click_pos, goal.destination),)
                return True, (BlockInteractionMatch(hit.position, hit.face, hit.click_pos),)
            return (None if hit.status == "unknown" else False), ()

        values = tuple(child.check(cell, eye=eye, visibility_standing=visibility_standing) for child in self.children)
        if isinstance(goal, AllGoal):
            if any(value is False for value, _ in values):
                return False, ()
            fulfilled = all(value is True for value, _ in values)
        else:
            fulfilled = any(value is True for value, _ in values)
        if fulfilled:
            return True, tuple(match for value, matches in values if value is True for match in matches)
        return (None if any(value is None for value, _ in values) else False), ()


def _block_placement_probe(goal: BlockPlacementGoal, blocks) -> Callable[[Point], BlockRaycastResult]:
    dimension = blocks.dimension_id
    miss = BlockRaycastResult("miss", dimension)
    unknown = BlockRaycastResult("unknown", dimension)
    layers = tuple(blocks.observed_block(goal.destination, layer=layer) for layer in (0, 1))
    if any(block is not None and block.is_air is False for block in layers):
        return lambda _eye: miss
    destination_known = all(block is not None and block.is_air is True for block in layers)

    candidates = []
    x, y, z = goal.destination
    for face, (dx, dy, dz) in FACE_OFFSETS.items():
        support = (x - dx, y - dy, z - dz)
        supported = blocks._placement_support(support)
        if supported is False:
            continue
        confirmed = destination_known and supported is True
        outline_at = blocks._outline_at
        if not confirmed:
            # Test feasibility under the missing observations a placement would require.
            def outline_at(position, support=support, supported=supported):
                if position == goal.destination and not destination_known:
                    return (), False
                if position == support and supported is None:
                    return _UNIT_BOX, False
                return blocks._outline_at(position)
        probe = _block_interaction_probe(support, dimension_id=dimension, max_distance=goal.max_distance,
                                         outline_at=outline_at, required_face=face)
        candidates.append((confirmed, probe))

    def trace(eye):
        unresolved = False
        for confirmed, probe in candidates:
            hit = probe(eye)
            if hit.status == "hit" and confirmed:
                return hit
            unresolved |= hit.status != "miss"
        return unknown if unresolved else miss

    return trace


def _entity_visibility_probe(
    goal: EntityVisibilityGoal, point: Point, blocks, budget: _VisibilityScanBudget,
) -> Callable[[Point], bool | None]:
    dimension = blocks.dimension_id
    outline_at = blocks._outline_at
    target = tuple(map(Fraction, point))
    range_squared = Fraction(goal.max_distance) ** 2

    def visible(eye: Point) -> bool | None:
        origin = tuple(map(Fraction, eye))
        direction = tuple(end - start for end, start in zip(target, origin, strict=True))
        length_squared = sum(value * value for value in direction)
        if length_squared > range_squared:
            return False
        hit = _trace_blocks(origin, direction, dimension, goal.max_distance, outline_at,
                            direction_length_squared=length_squared, stop_at_point=True, any_known_hit=True,
                            visit_cell=budget.visit_cell)
        budget.check_deadline()
        return None if hit.status == "unknown" else hit.status == "miss"

    return visible


def _resolve_goal(
    goal: NavigationGoal, blocks, entity_feet: Callable[[int], Point | None],
    visibility_budget: _VisibilityScanBudget | None,
) -> _GoalState:
    if isinstance(goal, BlockFaceGoal):
        block = blocks.observed_block(goal.position)
        exists = None if block is None or block.is_air is None else not block.is_air
        return _GoalState(goal, block_exists=exists)
    if isinstance(goal, BlockInteractionGoal):
        probe = _block_interaction_probe(goal.position, dimension_id=blocks.dimension_id,
                                         max_distance=goal.max_distance, outline_at=blocks._outline_at)
        return _GoalState(goal, interaction_probe=probe)
    if isinstance(goal, BlockPlacementGoal):
        return _GoalState(goal, interaction_probe=_block_placement_probe(goal, blocks))
    if isinstance(goal, NearGoal):
        return _GoalState(goal, point=goal.position)
    if isinstance(goal, EntityNearGoal):
        return _GoalState(goal, point=entity_feet(goal.entity_id))
    if isinstance(goal, EntityVisibilityGoal):
        feet = entity_feet(goal.entity_id)
        if feet is not None:
            point = tuple(value + offset for value, offset in zip(feet, goal.aim_offset, strict=True))
            if all(map(math.isfinite, point)):
                assert visibility_budget is not None
                return _GoalState(goal, visibility_probe=_entity_visibility_probe(goal, point, blocks, visibility_budget))
    if isinstance(goal, (AnyGoal, AllGoal)):
        children = tuple(_resolve_goal(child, blocks, entity_feet, visibility_budget) for child in goal.goals)
        return _GoalState(goal, children=children)
    return _GoalState(goal)


def _runout(source: Cell, landing: Cell) -> Cell:
    return 2 * landing[0] - source[0], landing[1], 2 * landing[2] - source[2]


def _is_gap_jump(source: Cell, landing: Cell) -> bool:
    dx, dz = abs(landing[0] - source[0]), abs(landing[2] - source[2])
    return source[1] == landing[1] and (dx, dz) in ((2, 0), (0, 2))


def _is_stone_ledge_drop(source: Cell, landing: Cell) -> bool:
    return (source[1] - landing[1] in _STONE_DROP_HEIGHTS
            and abs(landing[0] - source[0]) + abs(landing[2] - source[2]) == 1)


def _is_lower_slab_lane(source: Cell, destination: Cell) -> bool:
    dx, dz = abs(destination[0] - source[0]), abs(destination[2] - source[2])
    return destination[1] == source[1] and (dx, dz) in ((_LOWER_SLAB_STRIDE, 0), (0, _LOWER_SLAB_STRIDE))


def _is_water_crossing(source: Cell, landing: Cell) -> bool:
    dx, dz = abs(landing[0] - source[0]), abs(landing[2] - source[2])
    return landing[1] == source[1] and (dx, dz) in ((_WATER_STRIDE, 0), (0, _WATER_STRIDE))


def _is_ladder_climb(source: Cell, landing: Cell) -> bool:
    dx, dz = landing[0] - source[0], landing[2] - source[2]
    height = landing[1] - source[1]
    return ((abs(dx), abs(dz)) in ((_LADDER_STRIDE, 0), (0, _LADDER_STRIDE)) and height == 4
            or (dx, dz, height) == (_LADDER_STRIDE, 0, 6))


@dataclass(frozen=True, slots=True)
class _LadderDescent:
    rung_x: int
    rung_z: int
    dx: int
    dz: int

    def at(self, step: int, y: int) -> Cell:
        return self.rung_x + step * self.dx, y, self.rung_z + step * self.dz

    def offsets(self, x: float, z: float) -> tuple[float, float]:
        """Measure across the rung center and forward from its exposed edge."""
        x -= self.rung_x + 0.5
        z -= self.rung_z + 0.5
        return x * self.dz - z * self.dx, 0.5 + (x * self.dx + z * self.dz)


def _ladder_descent_geometry(source: Cell, landing: Cell) -> _LadderDescent | None:
    dx, dz = source[0] - landing[0], source[2] - landing[2]
    if (source[1] - landing[1] not in _LADDER_DESCENT_HEIGHTS
            or (abs(dx), abs(dz)) not in ((_LADDER_STRIDE, 0), (0, _LADDER_STRIDE))):
        return None
    dx, dz = dx // _LADDER_STRIDE, dz // _LADDER_STRIDE
    return _LadderDescent(source[0] - dx, source[2] - dz, dx, dz)


def _block_face_cells(position: Cell):
    x, y, z = position
    for dx, dz in _HORIZONTAL_STEPS:
        yield x + dx, y, z + dz


class _StandingGraph:
    def __init__(self, blocks, start: Cell, rules: NavigationRules, *, allow_vertical: bool,
                 allow_gap: bool = False, allow_stone_ledge_drop: bool = False, allow_ladder: bool = False,
                 allow_water: bool = False, allow_lower_slab_lane: bool = False):
        self.blocks = blocks
        self.origin = start
        self.rules = rules
        self.allow_vertical = allow_vertical
        self.allow_gap = allow_gap
        self.allow_stone_ledge_drop = allow_stone_ledge_drop
        self.allow_ladder = allow_ladder
        self.allow_water = allow_water
        self.allow_lower_slab_lane = allow_lower_slab_lane

    def in_bounds(self, cell: Cell) -> bool:
        return (abs(cell[0] - self.origin[0]) <= self.rules.search_radius
                and abs(cell[1] - self.origin[1]) <= self.rules.vertical_radius
                and abs(cell[2] - self.origin[2]) <= self.rules.search_radius)

    def edge(self, source: Cell, destination: Cell, *, check_descent_source: bool = True) -> bool | None:
        if not self.in_bounds(destination):
            return False
        if _is_water_crossing(source, destination):
            x, y, z = destination
            dx, dz = (x - source[0]) // _WATER_STRIDE, (z - source[2]) // _WATER_STRIDE
            tail = _WATER_DECK_LENGTH - 1
            last = (x + dx * tail, y, z + dz * tail)
            return self.blocks._still_water_crossing(source, dx, dz, _WATER_STRIDE, _WATER_DECK_LENGTH) if (
                self.allow_water and self.in_bounds(last)
            ) else False
        if _is_lower_slab_lane(source, destination):
            if not self.allow_lower_slab_lane or not self.in_bounds(source):
                return False
            dx = (destination[0] - source[0]) // _LOWER_SLAB_STRIDE
            dz = (destination[2] - source[2]) // _LOWER_SLAB_STRIDE
            return self.blocks._lower_slab_lane(source, dx, dz, _LOWER_SLAB_STRIDE)
        if _is_gap_jump(source, destination):
            return self.blocks._gap_jump(source, destination) if self.allow_gap else False
        if source[1] == destination[1]:
            return self.blocks._flat_stance(destination, allow_vegetation=True)
        if not self.allow_vertical:
            return False
        if destination[1] > source[1]:
            if _is_ladder_climb(source, destination):
                return self._ladder_climb(source, destination) if self.allow_ladder else False
            return destination[1] - source[1] == 1 and self.blocks._ascent_step(source, destination)
        if self.allow_ladder and (ladder := _ladder_descent_geometry(source, destination)) is not None:
            return self._ladder_descent(ladder, source[1], destination[1])
        buffer = _runout(source, destination)
        if not self.in_bounds(buffer):
            return False
        if _is_stone_ledge_drop(source, destination):
            last = _runout(destination, buffer)
            return self.blocks._stone_ledge_drop(source, (destination, buffer, last)) if (
                self.allow_stone_ledge_drop and self.in_bounds(last)
            ) else False
        if source[1] - destination[1] != 1:
            return False
        required = (
            self.blocks._flat_stance(source) if check_descent_source else True,
            self.blocks._descent_landing(destination),
            self.blocks._descent_landing(buffer),
        )
        if False in required:
            return False
        return True if all(required) else None

    def _ladder_column(self, rung: Cell, dx: int, dz: int, top: int) -> bool | None:
        x, bottom, z = rung
        facing = _HORIZONTAL_FACES[-dx, 0, -dz]
        first = self.blocks._ladder_rung(rung, facing)
        if first is False:
            return False
        required = [first]
        required.extend(self.blocks._ladder_rung((x, y, z), facing) for y in range(bottom + 1, top))
        required.extend(self.blocks._ordinary_stone_support((x + dx, y, z + dz)) for y in range(bottom, top))
        if False in required:
            return False
        return True if all(required) else None

    def _ladder_climb(self, source: Cell, landing: Cell) -> bool | None:
        sx, sy, sz = source
        tx, ty, tz = landing
        dx, dz = (tx - sx) // _LADDER_STRIDE, (tz - sz) // _LADDER_STRIDE

        def at(step, y):
            return sx + step * dx, y, sz + step * dz

        if not self.in_bounds(at(_LADDER_APPROACH + _LADDER_DECK_LENGTH, ty)):
            return False
        column = self._ladder_column(at(_LADDER_APPROACH, sy), dx, dz, ty)
        if column is False:
            return False
        required = [column, self.blocks._ordinary_stone_support(at(0, sy - 1)),
                    self.blocks._ordinary_stone_support(at(1, sy - 1)),
                    self.blocks._ordinary_stone_support(at(_LADDER_APPROACH, sy - 1)),
                    self.blocks._stance_clearance(source), self.blocks._stance_clearance(at(0, sy + 1))]
        required.extend(self.blocks._stance_clearance(at(1, y)) for y in range(sy, ty + 3))
        for step in range(_LADDER_APPROACH, _LADDER_APPROACH + _LADDER_DECK_LENGTH + 1):
            if step > _LADDER_APPROACH:
                required.append(self.blocks._ordinary_stone_support(at(step, ty - 1)))
            required.extend(self.blocks._stance_clearance(at(step, y)) for y in range(ty, ty + 3))
        if False in required:
            return False
        return True if all(required) else None

    def _ladder_descent(self, ladder: _LadderDescent, top: int, bottom: int) -> bool | None:
        at = ladder.at
        if not self.in_bounds(at(-_LADDER_STRIDE, bottom)):
            return False
        column = self._ladder_column(at(0, bottom), ladder.dx, ladder.dz, top)
        if column is False:
            return False
        # The top launch overlaps the stone behind the lip. The bottom exit
        # crosses two clear columns in front of the backed ladder.
        required = [column, self.blocks._ordinary_stone_support(at(2, top - 1))]
        required.extend(self.blocks._ordinary_stone_support(at(step, bottom - 1))
                        for step in range(-_LADDER_STRIDE, 1))
        required.extend(self.blocks._stance_clearance(at(step, y))
                        for step in (0, 1, 2) for y in range(top, top + 3))
        required.extend(self.blocks._stance_clearance(at(step, y))
                        for step in (-2, -1) for y in range(bottom, top + 2))
        required.extend(self.blocks._stance_clearance(at(-_LADDER_STRIDE, y)) for y in range(bottom, bottom + 3))
        if False in required:
            return False
        return True if all(required) else None

    def plan(self, start: Cell, goal: _GoalState, *, limit: int | None,
             used_ascents: set[tuple[Cell, Cell]] | frozenset[tuple[Cell, Cell]] = frozenset(),
             expired: Callable[[], bool] | None = None,
             start_eye: Point | None = None,
             start_visibility_standing: Callable[[], bool] | None = None,
             ) -> NavigationPlan:
        expanded = 0
        args = goal.goal, self.rules, self.blocks.dimension_id, start
        if not self.in_bounds(start):
            return NavigationPlan(*args, "unreachable")
        stance = self.blocks._flat_stance(start, allow_vegetation=True)
        if stance is False:
            return NavigationPlan(*args, "unreachable")

        down = Fraction(self.rules.costs.down)
        up = Fraction(self.rules.costs.up)
        walk = Fraction(self.rules.costs.walk)
        steps = [(0, 1, walk), (1, 1, up), (-1, 1, down)]
        if self.allow_gap:
            steps.append((0, 2, 2 * walk))
        if self.allow_lower_slab_lane:
            steps.append((0, _LOWER_SLAB_STRIDE, _LOWER_SLAB_STRIDE * walk))
        if self.allow_stone_ledge_drop:
            steps.extend((-height, 1, height * down) for height in _STONE_DROP_HEIGHTS)
        if self.allow_ladder:
            steps.extend(((4, _LADDER_STRIDE, 4 * up), (6, _LADDER_STRIDE, 6 * up)))
            steps.extend((-height, _LADDER_STRIDE, height * down) for height in _LADDER_DESCENT_HEIGHTS)
        order = count()
        uncertain = stance is None
        water_steps = (*steps, (0, _WATER_STRIDE, _WATER_STRIDE * walk))
        searches = [(False, False)]
        if self.allow_water:
            searches.append((False, True))
        searches.append((True, self.allow_water))
        # Dry routes take priority over observed water routes. Both take priority
        # over a route that would depend on missing information.
        for optimistic, water in searches:
            if not optimistic and stance is None:
                continue
            if optimistic and not uncertain:
                break
            queue = [(Fraction(0), next(order), start)]
            previous = {start: None}
            distances = {start: Fraction(0)}
            while queue:
                distance, _, cell = heappop(queue)
                if distance != distances[cell]:
                    continue
                if (limit is not None and expanded >= limit) or (expired is not None and expired()):
                    return NavigationPlan(*args, "budget", expanded=expanded)
                expanded += 1
                is_start = cell == start
                try:
                    matches, faces = goal.check(cell, eye=start_eye if is_start else None,
                                                visibility_standing=start_visibility_standing if is_start else None)
                except _VisibilityBudgetExhausted:
                    return NavigationPlan(*args, "budget", expanded=expanded)
                if optimistic and matches is not False:
                    return NavigationPlan(*args, "unknown", expanded=expanded)
                uncertain |= matches is None
                if matches is True:
                    path = []
                    while cell is not None:
                        path.append(cell)
                        cell = previous[cell]
                    return NavigationPlan(*args, "found", tuple(reversed(path)), faces, distance, expanded)
                x, y, z = cell
                for dx, dz in _HORIZONTAL_STEPS:
                    for dy, stride, cost in water_steps if water else steps:
                        neighbor = (x + stride * dx, y + dy, z + stride * dz)
                        if dy == 1 and (cell, neighbor) in used_ascents:
                            continue
                        candidate = distance + cost
                        if neighbor in distances and candidate >= distances[neighbor]:
                            continue
                        edge = self.edge(cell, neighbor)
                        uncertain |= edge is None
                        if edge is False or (edge is None and not optimistic):
                            continue
                        previous[neighbor] = cell
                        distances[neighbor] = candidate
                        heappush(queue, (candidate, next(order), neighbor))
        return NavigationPlan(*args, "unreachable", expanded=expanded)
