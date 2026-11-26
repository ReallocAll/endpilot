"""Derive vanilla painting geometry from observed AddPainting snapshots."""

from dataclasses import dataclass
from struct import Struct

from ._painting_motifs_2225 import MOTIF_SIZES as _MOTIF_SIZES
from .entities import PaintingSnapshot


_DIRECTIONS = {0: (2, 1), 1: (0, -1), 2: (2, -1), 3: (0, 1)}
_FLOAT32 = Struct("<f")


@dataclass(frozen=True, slots=True)
class PaintingGeometry:
    """Block dimensions and paired wall and hanging cells in world coordinates.

    Both tuples are ordered by increasing Y, then increasing X or Z along
    the wall. Each hanging cell is one block outward from its paired wall cell.
    """

    width: int
    height: int
    wall_cells: tuple[tuple[int, int, int], ...]
    hanging_cells: tuple[tuple[int, int, int], ...]


def _recover_grid_coordinate(observed: float, offset: float) -> int | None:
    # At 2**24 even adjacent integer inputs can collapse under float32.
    if not abs(observed) < 2**24:
        return None
    nearest = round(observed - offset)
    # Check both neighbors for ties when a half-cell or wall offset was rounded.
    candidates = [
        cell for cell in range(nearest - 1, nearest + 2)
        if _FLOAT32.unpack(_FLOAT32.pack(cell + offset))[0] == observed
    ]
    return candidates[0] if len(candidates) == 1 else None


def painting_geometry(painting: PaintingSnapshot, *, protocol: int = 2225) -> PaintingGeometry | None:
    """Return the protocol 2225 vanilla footprint of an observed painting.

    Return ``None`` for unsupported protocols, unknown motifs or directions,
    or coordinates outside the supported grid precision or with no unique
    grid interpretation. The footprint describes the snapshot at spawn time.
    It uses vanilla motif sizes and says nothing about the current wall blocks.
    """
    if protocol != 2225:
        return None
    direction = _DIRECTIONS.get(painting.dir)
    size = _MOTIF_SIZES.get(painting.motif)
    if direction is None or size is None:
        return None

    width, height = size
    normal_axis, outward = direction
    width_axis = 2 - normal_axis
    position = painting.position
    boundary = _recover_grid_coordinate(position[normal_axis], outward / 32)
    first_column = _recover_grid_coordinate(position[width_axis], width / 2)
    bottom = _recover_grid_coordinate(position[1], height / 2)
    if boundary is None or first_column is None or bottom is None:
        return None

    wall = boundary - (1 if outward > 0 else 0)
    hanging = wall + outward
    rows = range(bottom, bottom + height)
    columns = range(first_column, first_column + width)
    if normal_axis == 0:
        wall_cells = tuple((wall, y, z) for y in rows for z in columns)
        hanging_cells = tuple((hanging, y, z) for y in rows for z in columns)
    else:
        wall_cells = tuple((x, y, wall) for y in rows for x in columns)
        hanging_cells = tuple((x, y, hanging) for y in rows for x in columns)
    return PaintingGeometry(width, height, wall_cells, hanging_cells)
