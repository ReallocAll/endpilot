"""Block updates, observed sub-chunks and player-initiated placement."""

import asyncio
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from heapq import nsmallest
from math import atan2, degrees, dist, floor, hypot, isqrt, prod
from types import MappingProxyType
from typing import Literal

from bedrock_protocol import DecodeError, nbt

from .block_data import VanillaBlockState, _catalog, _vanilla_state_properties, vanilla_block_state
from .block_raycast import (
    BlockRaycastResult, MiningFaceResult, OutlineObservation, _UNIT_BOX, trace_blocks, trace_blocks_towards,
    trace_mining_face,
)
from .compression import PacketConnection
from .protocol import RawPacket
from .subchunks import SubChunk, _PalettedStorage, parse_biome_sections, parse_sub_chunk_prefix

# Network hash of minecraft:air.
HASHED_AIR_RUNTIME_ID = 0xDBF44120
# Reserved network ID for the minecraft:unknown placeholder.
HASHED_UNKNOWN_RUNTIME_ID = 0xFFFFFFFE
_SOURCE_WATER_HASH = 0x483B8B2A
FACE_OFFSETS = {0: (0, -1, 0), 1: (0, 1, 0), 2: (0, 0, -1),
                3: (0, 0, 1), 4: (-1, 0, 0), 5: (1, 0, 0)}
_ORDINARY_MINING_REACH = 4.5
# Inclusive lower and exclusive upper section indices for vanilla dimensions.
_VANILLA_SECTION_RANGES = {0: (-4, 20), 1: (0, 8), 2: (0, 16)}
# Bedrock r26_u6 palette indices and collision boxes checked against vanilla explosion exposure.
_EXPLOSION_COLLISION_SHAPES = {
    2791: (),  # Unpressed stone button, facing_direction=1.
    3317: ((0., 0., 0., 1., 1., 1.),),  # Stone.
    21904: ((0., 0.5, 0., 1., 1., 1.),),  # Top smooth stone slab.
}

def _publisher_bounds(packet) -> tuple[int, int, int, int]:
    x, z, radius = packet.position.x, packet.position.z, packet.radius
    return ((x - radius) >> 4, (x + radius) >> 4, (z - radius) >> 4, (z + radius) >> 4)


def _column_in_bounds(bounds: tuple[int, int, int, int] | None, x: int, z: int) -> bool:
    if bounds is None:
        return True
    min_x, max_x, min_z, max_z = bounds
    return min_x <= x <= max_x and min_z <= z <= max_z


@dataclass(frozen=True, slots=True)
class BlockUpdate:
    position: tuple[int, int, int]
    runtime_id: int
    layer: int


@dataclass(frozen=True, slots=True)
class BlockObservation:
    """Observed runtime ID and air status when established by the server's data."""

    position: tuple[int, int, int]
    runtime_id: int | None
    layer: int
    is_air: bool | None


@dataclass(frozen=True, slots=True)
class BlockStateObservation:
    """An independent snapshot of an observed vanilla block state."""

    position: tuple[int, int, int]
    runtime_id: int | None
    layer: int
    name: str
    properties: Mapping[str, int | str]

    def __post_init__(self):
        object.__setattr__(self, "position", tuple(self.position))
        object.__setattr__(self, "properties", MappingProxyType(dict(self.properties)))


@dataclass(frozen=True, slots=True)
class DimensionBounds:
    """The inclusive lower and exclusive upper block Y used to interpret terrain sections."""

    dimension_id: int
    min_y: int
    max_y_exclusive: int
    source: Literal["vanilla_default", "dimension_data"]

    @property
    def _sections(self) -> tuple[int, int]:
        return self.min_y // 16, self.max_y_exclusive // 16


@dataclass(frozen=True, slots=True)
class ColumnCoverage:
    """Server announcements and observed coverage for one column in the active dimension."""

    dimension_id: int
    column: tuple[int, int]
    announced: bool
    requested_section_ys: tuple[int, ...]
    received_section_ys: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ColumnCoverageEvent:
    """Current loaded coverage, or the last coverage of an unloaded column."""

    kind: Literal["loaded", "coverage", "unloaded"]
    coverage: ColumnCoverage


class ColumnCoverageInvalidated(RuntimeError):
    def __init__(self, reason: Literal["unloaded", "dimension_changed"]):
        self.reason = reason
        super().__init__(f"column coverage invalidated: {reason}")


_CoverageSelection = tuple[tuple[tuple[int, int], range], ...]
_CoverageWaiter = tuple[_CoverageSelection, asyncio.Future[tuple[ColumnCoverage, ...]]]


@dataclass(frozen=True, slots=True)
class BlockChange:
    dimension_id: int
    before: BlockObservation | None
    after: BlockObservation


@dataclass(frozen=True, slots=True)
class BlockEntityUpdate:
    """A direct block entity update with an independently mutable NBT root."""

    dimension_id: int
    position: tuple[int, int, int]
    data: nbt.CompoundTag


@dataclass(slots=True)
class _ColumnCoverage:
    announced: bool = False
    requestable_sections: range = range(0)
    pending: set[int] = field(default_factory=set)
    requested: set[int] = field(default_factory=set)
    received: set[int] = field(default_factory=set)
    biomes: tuple[_PalettedStorage, ...] = ()


@dataclass(frozen=True, slots=True)
class MiningTarget:
    """Current held block and face requested by the client."""

    position: tuple[int, int, int]
    face: int


class MiningFaceUnavailable(RuntimeError):
    """Automatic face selection could not safely submit START."""

    def __init__(self, status: Literal["excluded", "unknown", "no_sample", "target_changed"]):
        self.status = status
        super().__init__(f"automatic mining face unavailable: {status}")


@dataclass(frozen=True, slots=True)
class BlockMatch:
    """An observed block in a search snapshot, with its name when known."""

    position: tuple[int, int, int]
    name: str | None
    runtime_id: int | None
    distance_squared: int


@dataclass(frozen=True, slots=True)
class BlockSearchResult:
    """Matches from the cache and the query's completeness."""

    center: tuple[int, int, int]
    dimension_id: int
    matches: tuple[BlockMatch, ...]
    complete: bool


def _sphere_is_covered(radius: int, resolved: int) -> bool:
    # Each X/Y row contains an odd number of Z coordinates. Stop once the
    # sphere needs more cells than the cache resolved, even for large radii.
    remaining = resolved
    squared = radius * radius
    for dx in range(-radius, radius + 1):
        cross_section = squared - dx * dx
        span = isqrt(cross_section)
        for dy in range(-span, span + 1):
            remaining -= 2 * isqrt(cross_section - dy * dy) + 1
            if remaining < 0:
                return False
    return remaining == 0


def _vein_rows(
    center: tuple[int, int, int], distance: int,
    low: tuple[int, int, int], high: tuple[int, int, int], *, shift: int = 0,
) -> Iterator[tuple[int, int, range]]:
    """Yield block or section rows intersecting both the Manhattan ball and box."""
    cx, cy, cz = center
    lx, ly, lz = low
    hx, hy, hz = high
    width = 1 << shift
    nearest_z = max(lz - cz, cz - hz, 0)
    span_x = distance - max(ly - cy, cy - hy, 0) - nearest_z
    left, right = max(lx, cx - span_x), min(hx, cx + span_x)
    if left > right:
        return
    for x in range(left >> shift, (right >> shift) + 1):
        x0 = x << shift
        remaining = distance - max(max(lx, x0) - cx, cx - min(hx, x0 + width - 1), 0)
        span_y = remaining - nearest_z
        bottom, top = max(ly, cy - span_y), min(hy, cy + span_y)
        for y in range(bottom >> shift, (top >> shift) + 1):
            y0 = y << shift
            span_z = remaining - max(max(ly, y0) - cy, cy - min(hy, y0 + width - 1), 0)
            yield x, y, range(max(lz, cz - span_z) >> shift, (min(hz, cz + span_z) >> shift) + 1)


def _vein_box_mask(section: tuple[int, int, int], low: tuple[int, int, int], high: tuple[int, int, int]) -> int:
    sx, sy, sz = (axis << 4 for axis in section)
    xs = range(max(0, low[0] - sx), min(15, high[0] - sx) + 1)
    ys = range(max(0, low[1] - sy), min(15, high[1] - sy) + 1)
    lz, hz = max(0, low[2] - sz), min(15, high[2] - sz)
    if not xs or not ys or lz > hz:
        return 0
    row = ((1 << (hz - lz + 1)) - 1) << lz
    plane = sum(row << (y << 4) for y in ys)
    return sum(plane << (x << 8) for x in xs)


def _vein_bit(x: int, y: int, z: int) -> int:
    return 1 << (256 * (x & 15) + 16 * (y & 15) + (z & 15))


class Blocks:
    def __init__(self, packets, movement, inventory, *, hashed_runtime_ids: bool = False,
                 custom_blocks: bool = False, dimension_id: int = 0,
                 player_game_type: str | None = None, player_unique_id: int | None = None):
        self.packets = packets
        self.movement = movement
        self.inventory = inventory
        self.hashed_runtime_ids = hashed_runtime_ids
        self._protocol = getattr(packets.protocol, "number", None)
        self._custom_blocks = custom_blocks
        self.dimension_id = dimension_id
        self._player_game_type = player_game_type
        self._player_unique_id = player_unique_id
        self._mining_lock = asyncio.Lock()
        self._mining_target: MiningTarget | None = None
        self._mining_completed = False
        self._mining_generation = 0
        self._mining_epoch = 0
        self._mining_closed = False
        self._published_bounds: tuple[int, int, int, int] | None = None
        self._dimension_definitions: dict[int, DimensionBounds] = {}
        self._replayed_dimension_data: deque[tuple[RawPacket, tuple[DimensionBounds, ...]]] = deque()
        self._updates: dict[tuple[int, int, int, int], int] = {}
        self._sub_chunks: dict[tuple[int, int, int], SubChunk | None] = {}
        self._block_entities: dict[tuple[int, int, int], dict[tuple[int, int, int], nbt.CompoundTag]] = {}
        self._decode_block_entity = getattr(nbt, "decode_network_compound", None)
        if self._decode_block_entity is None and packets.protocol.number == 2225:
            self._decode_block_entity = self._decode_block_entity_packet
        self._observe_block_entity = None
        self._unsubscribe = None
        self._unsubscribe_end = None
        self._request_column: Callable[[tuple[int, int], range], None] | None = None
        self._coverage: dict[tuple[int, int], _ColumnCoverage] = {}
        self._column_waiters: dict[tuple[int, int], list[_CoverageWaiter]] = {}
        self._column_observers: list[Callable[[ColumnCoverageEvent], None]] = []
        self._block_observers: dict[
            Callable[[BlockChange], None], tuple[int | None, Callable[[int, int], None] | None],
        ] = {}
        self._block_entity_observers: dict[object, Callable[[], None]] = {}
        self._last_replayed_block_entity: RawPacket | None = None
        self._applied_block_entity: tuple[RawPacket, int, tuple[int, int, int], nbt.CompoundTag] | None = None
        self._closed = False

    def start(
        self, *, observe_block_entity: Callable[
            [tuple[int, int, int], nbt.CompoundTag | None, nbt.CompoundTag | None], None,
        ] | None = None,
        replayed_dimension_packets: Iterable[RawPacket] = (),
    ) -> None:
        self._observe_block_entity = observe_block_entity
        for raw in replayed_dimension_packets:
            PacketConnection._notify((self._prime_dimension_data,), raw)
        self._unsubscribe = self.packets.subscribe_raw(self._on_packet)
        self._unsubscribe_end = self.packets.subscribe_reader_end(self._end_observation)

    def close(self) -> None:
        self._mining_closed = True
        self._invalidate_mining()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._unsubscribe_end is not None:
            self._unsubscribe_end()
            self._unsubscribe_end = None
        self._observe_block_entity = None
        self._end_observation()

    def _end_observation(self, cause: BaseException | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        self._dimension_definitions.clear()
        self._replayed_dimension_data.clear()
        self._updates.clear()
        self._sub_chunks.clear()
        self._block_entities.clear()
        self._last_replayed_block_entity = None
        self._applied_block_entity = None
        self._published_bounds = None
        self._request_column = None

        def failure():
            error = ConnectionError("world observation ended")
            error.__cause__ = cause
            return error

        self._invalidate_columns(self.dimension_id, failure, lambda column: True)
        self._column_observers.clear()
        self._block_observers.clear()
        for remove in self._block_entity_observers.values():
            remove()
        self._block_entity_observers.clear()

    def _on_packet(self, raw) -> None:
        if self._closed:
            return
        self._applied_block_entity = None
        b = self.packets.protocol.binding
        if raw.packet_id == b.ChangeDimensionPacket.ID:
            self._invalidate_mining()
            previous_dimension = self.dimension_id
            new_dimension = int(self.packets.protocol.decode(raw).dimension_id)
            self.dimension_id = new_dimension
            leave_callbacks = []
            if previous_dimension != new_dimension:
                for observer, (dimension, on_leave) in tuple(self._block_observers.items()):
                    if on_leave is not None and dimension == previous_dimension:
                        del self._block_observers[observer]
                        leave_callbacks.append(on_leave)
            self._published_bounds = None
            self._updates.clear()
            self._sub_chunks.clear()
            self._block_entities.clear()
            self._invalidate_columns(previous_dimension, lambda: ColumnCoverageInvalidated("dimension_changed"),
                                     lambda column: True)
            self._notify_observers(leave_callbacks, previous_dimension, new_dimension)
        elif raw.packet_id == b.DimensionDataPacket.ID:
            if self._replayed_dimension_data and raw is self._replayed_dimension_data[0][0]:
                _, definitions = self._replayed_dimension_data.popleft()
            else:
                definitions = self._decode_dimension_data(raw)
            for bounds in definitions:
                dimension = bounds.dimension_id
                if dimension == self.dimension_id and self._section_range(dimension) != bounds._sections:
                    for state in self._coverage.values():
                        state.biomes = ()
                self._dimension_definitions[dimension] = bounds
        elif raw.packet_id == b.LevelChunkPacket.ID:
            self._observe_column(self.packets.protocol.decode(raw))
        elif raw.packet_id == b.BlockActorDataPacket.ID:
            replaying = self._last_replayed_block_entity is not None
            if raw is self._last_replayed_block_entity:
                self._last_replayed_block_entity = None
            packet = self.packets.protocol.decode(raw)
            pos = (int(packet.pos.x), int(packet.pos.y), int(packet.pos.z))
            if self._column_is_published(pos[0] >> 4, pos[2] >> 4):
                root = nbt.CompoundTag(packet.data)
                self._set_block_entity(pos, root)
                if self._block_entity_observers and not replaying:
                    self._applied_block_entity = (raw, self.dimension_id, pos, root)
        elif raw.packet_id == b.UpdateBlockPacket.ID:
            packet = self.packets.protocol.decode(raw)
            self._apply_update(packet, int(packet.layer))
        elif raw.packet_id == b.UpdateSubChunkBlocksPacket.ID:
            packet = self.packets.protocol.decode(raw)
            for layer, changes in enumerate((packet.blocks_changed.standards, packet.blocks_changed.extras)):
                for change in changes:
                    self._apply_update(change, layer)
        elif raw.packet_id == b.SubChunkPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.dimension_type) != self.dimension_id:
                return
            center = packet.center_pos
            for entry in packet.sub_chunk_data:
                delta = entry.sub_chunk_pos_offset
                key = (int(center.x) + int(delta.x),
                       int(center.y) + int(delta.y), int(center.z) + int(delta.z))
                if not self._column_is_published(key[0], key[2]):
                    continue
                result = entry.result.name
                if result == "SUCCESS_ALL_AIR":
                    self._set_sub_chunk(key, None)
                elif result == "SUCCESS":
                    data = entry.serialized_sub_chunk
                    if data is None:
                        self._set_section_block_entities(key, {})
                        continue
                    try:
                        chunk, offset = parse_sub_chunk_prefix(data)
                    except ValueError:
                        self._set_section_block_entities(key, {})
                        continue
                    self._set_sub_chunk(key, chunk, self._read_section_block_entities(key, data, offset))
        elif raw.packet_id == b.NetworkChunkPublisherUpdatePacket.ID:
            bounds = _publisher_bounds(self.packets.protocol.decode(raw))
            if bounds != self._published_bounds:
                self._published_bounds = bounds
                for key in tuple(self._sub_chunks):
                    if not self._column_is_published(key[0], key[2]):
                        del self._sub_chunks[key]
                for pos in tuple(self._updates):
                    if not self._column_is_published(pos[0] >> 4, pos[2] >> 4):
                        del self._updates[pos]
                for section in tuple(self._block_entities):
                    if not self._column_is_published(section[0], section[2]):
                        self._set_section_block_entities(section, {})
                self._invalidate_columns(self.dimension_id, lambda: ColumnCoverageInvalidated("unloaded"),
                                         lambda column: not self._column_is_published(*column))
        elif raw.packet_id == b.DisconnectPacket.ID:
            self._mining_closed = True
            self._invalidate_mining()
            self._end_observation()
        elif raw.packet_id == b.RespawnPacket.ID:
            if self._own_ready_respawn(raw):
                self._invalidate_mining()
        elif raw.packet_id == b.SetPlayerGameTypePacket.ID:
            self._player_game_type = self.packets.protocol.decode(raw).player_game_type.name
        elif raw.packet_id == b.UpdatePlayerGameTypePacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.target_player) == self._player_unique_id:
                self._player_game_type = packet.player_game_type.name
        if (self._mining_target is not None and not self._mining_completed
                and raw.packet_id in (b.UpdateBlockPacket.ID, b.UpdateSubChunkBlocksPacket.ID, b.SubChunkPacket.ID)):
            block = self.observed_block(self._mining_target.position)
            if block is not None and block.is_air is True:
                self._mining_completed = True

    def _own_ready_respawn(self, raw) -> bool:
        b = self.packets.protocol.binding
        packet = self.packets.protocol.decode(raw)
        player_id = None if self.movement is None else self.movement.player_id
        return (packet.runtime_id in (0, player_id)
                and packet.state == b.PlayerRespawnState.READY_TO_SPAWN)

    def _invalidate_mining(self) -> None:
        self._mining_epoch += 1
        self._mining_target = None
        self._mining_completed = False

    @property
    def published_bounds(self) -> tuple[int, int, int, int] | None:
        return self._published_bounds

    def _column_is_published(self, x: int, z: int) -> bool:
        return _column_in_bounds(self._published_bounds, x, z)

    def _decode_dimension_data(self, raw: RawPacket) -> tuple[DimensionBounds, ...]:
        definitions = []
        for definition in self.packets.protocol.decode(raw).dimension_definitions.values():
            minimum = int(definition.min_y)
            height = int(definition.height_range)
            lower, upper = minimum // 16, (minimum + height) // 16
            if minimum % 16 == height % 16 == 0 and -128 <= lower < upper <= 128:
                definitions.append(DimensionBounds(int(definition.dimension_type), minimum, minimum + height,
                                                   "dimension_data"))
        return tuple(definitions)

    def _prime_dimension_data(self, raw: RawPacket) -> None:
        # The reader replays the same RawPacket objects in order. Cache decoded
        # definitions for early reads, then apply them to terrain during replay.
        definitions = ()
        try:
            definitions = self._decode_dimension_data(raw)
        finally:
            self._replayed_dimension_data.append((raw, definitions))

    def _section_range(self, dimension: int) -> tuple[int, int] | None:
        bounds = self._dimension_definitions.get(dimension)
        return _VANILLA_SECTION_RANGES.get(dimension) if bounds is None else bounds._sections

    @property
    def dimension_bounds(self) -> DimensionBounds | None:
        if self._closed:
            return None
        dimension = self.dimension_id
        for _, definitions in reversed(self._replayed_dimension_data):
            for bounds in reversed(definitions):
                if bounds.dimension_id == dimension:
                    return bounds
        bounds = self._dimension_definitions.get(dimension)
        if bounds is None and self._protocol == 2225:
            sections = _VANILLA_SECTION_RANGES.get(dimension)
            if sections is not None:
                lower, upper = sections
                return DimensionBounds(dimension, 16 * lower, 16 * upper, "vanilla_default")
        return bounds

    def _observe_column(self, packet) -> None:
        x, z = column = (int(packet.pos.x), int(packet.pos.z))
        if int(packet.dimension_id) != self.dimension_id or not self._column_is_published(x, z):
            return
        if getattr(packet, "is_client_biome_update", False):
            state = self._coverage.get(column)
            if state is not None:
                state.biomes = ()
            return
        state = self._coverage.setdefault(column, _ColumnCoverage())
        state.announced = True
        state.biomes = ()
        state.requestable_sections = range(0)
        bounds = self._section_range(self.dimension_id)
        if bounds is None or packet.cache_enabled:
            return
        lower, upper = bounds
        limit = packet.client_request_sub_chunk_limit
        if limit is not None:
            if (self._protocol == 2225 and bounds == _VANILLA_SECTION_RANGES.get(self.dimension_id)
                    and packet.sub_chunks_count == 0):
                try:
                    state.biomes = parse_biome_sections(packet.serialized_chunk, upper - lower)
                except ValueError:
                    # A failed biome decode leaves block announcements and requests intact.
                    pass
            stop = upper if limit == -1 else min(upper, lower + int(limit))
            state.requestable_sections = range(lower, stop)
            if self._request_column is not None:
                self._request_column(column, state.requestable_sections)
            return
        count = int(packet.sub_chunks_count)
        if not 0 < count <= upper - lower:
            return
        data = packet.serialized_chunk
        sections = []
        for index in range(count):
            section, size = parse_sub_chunk_prefix(data)
            y = int.from_bytes(data[2:3], signed=True) if data[0] == 9 else lower + index
            if lower <= y < upper:
                sections.append((y, section))
            data = data[size:]
        for y, section in sections:
            self._set_sub_chunk((x, y, z), section)

    @staticmethod
    def _notify_observers(observers, *args) -> None:
        for callback in tuple(observers):
            try:
                callback(*args)
            except (Exception, asyncio.CancelledError) as error:
                asyncio.get_running_loop().call_exception_handler({
                    "message": "world observer failed", "exception": error, "callback": callback,
                })

    def _apply_update(self, change, layer: int) -> None:
        position = (int(change.pos.x), int(change.pos.y), int(change.pos.z))
        if not self._column_is_published(position[0] >> 4, position[2] >> 4):
            return
        before = self.observed_block(position, layer=layer) if self._block_observers else None
        self._updates[(*position, layer)] = int(change.runtime_id)
        if layer == 0:
            self._set_block_entity(position, None)
        if self._block_observers:
            after = self.observed_block(position, layer=layer)
            if after is not None and before != after:
                self._notify_observers(self._block_observers, BlockChange(self.dimension_id, before, after))

    def _set_sub_chunk(self, key: tuple[int, int, int], chunk: SubChunk | None, block_entities=None) -> None:
        self._sub_chunks[key] = chunk
        for pos in tuple(self._updates):
            if (pos[0] >> 4, pos[1] >> 4, pos[2] >> 4) == key:
                del self._updates[pos]
        self._set_section_block_entities(key, block_entities or {})
        x, y, z = key
        column = (x, z)
        state = self._coverage.setdefault(column, _ColumnCoverage())
        if y in state.received:
            return
        kind = "coverage" if state.received else "loaded"
        state.received.add(y)
        for columns, future in self._column_waiters.get(column, ()):
            if not future.done():
                snapshots = self._coverage_if_ready(columns)
                if snapshots is not None:
                    future.set_result(snapshots)
        if self._column_observers:
            snapshot = self._snapshot(column, self.dimension_id, state)
            self._notify_observers(self._column_observers, ColumnCoverageEvent(kind, snapshot))

    def _block_entity(self, position: tuple[int, int, int]) -> nbt.CompoundTag | None:
        x, y, z = position
        section = self._block_entities.get((x >> 4, y >> 4, z >> 4))
        return None if section is None else section.get(position)

    def observed_block_entity(self, position: tuple[int, int, int]) -> nbt.CompoundTag | None:
        """Return an independent copy of the current server-observed root."""
        root = self._block_entity(position)
        return None if root is None else nbt.CompoundTag(root)

    def _set_block_entity(self, position, root):
        x, y, z = position
        key = (x >> 4, y >> 4, z >> 4)
        section = self._block_entities.get(key)
        previous = None if section is None else section.get(position)
        if root is not None:
            self._block_entities.setdefault(key, {})[position] = root
        elif section is not None:
            section.pop(position, None)
            if not section:
                del self._block_entities[key]
        if self._observe_block_entity is not None:
            self._observe_block_entity(position, previous, root)

    def _set_section_block_entities(self, key, roots):
        previous = self._block_entities.pop(key, {})
        if roots:
            self._block_entities[key] = roots
        if self._observe_block_entity is not None:
            for position in previous.keys() | roots.keys():
                self._observe_block_entity(position, previous.get(position), roots.get(position))

    def _decode_block_entity_packet(self, data: bytes, offset: int) -> tuple[nbt.CompoundTag, int]:
        binding = self.packets.protocol.binding
        decode, packet = binding.decode, binding.BlockActorDataPacket
        # Protocol 2225's BlockActorData body is three coordinates followed by the same network NBT root.
        prefix = b"\x00\x00\x00"
        # Probe a bounded window instead of copying the entire remaining suffix for every small root.
        view = memoryview(data)
        end = min(offset + 512, len(data))
        while True:
            try:
                return decode(packet, b"".join((prefix, view[offset:end]))).data, end
            except DecodeError as error:
                if error.reason == "trailing bytes" and error.path == ():
                    root_end = offset + error.offset - len(prefix)
                    if not offset < root_end < end:
                        raise
                    return decode(packet, b"".join((prefix, view[offset:root_end]))).data, root_end
                if error.reason != "truncated" or end == len(data):
                    raise
            end = min(offset + 2 * (end - offset), len(data))

    def _read_section_block_entities(self, key, data, offset):
        decode = self._decode_block_entity
        if decode is None:
            return {}
        roots = {}
        try:
            while offset < len(data):
                root, next_offset = decode(data, offset)
                if not offset < next_offset <= len(data):
                    return {}
                offset = next_offset
                if not all(axis in root and isinstance(root[axis], nbt.IntTag) for axis in "xyz"):
                    return {}
                position = tuple(root[axis].value for axis in "xyz")
                if tuple(value >> 4 for value in position) != key:
                    continue
                if position in roots:
                    return {}
                roots[position] = root
        except ValueError:
            # A partially decoded tail cannot attest to the current contents of this section.
            return {}
        return roots

    @staticmethod
    def _snapshot(column, dimension_id, state) -> ColumnCoverage:
        if state is None:
            return ColumnCoverage(dimension_id, column, False, (), ())
        return ColumnCoverage(dimension_id, column, state.announced,
                              tuple(sorted(state.requested)), tuple(sorted(state.received)))

    def _invalidate_columns(self, dimension_id, failure, invalidated) -> None:
        for column, waiters in tuple(self._column_waiters.items()):
            if invalidated(column):
                for _, future in waiters:
                    if not future.done():
                        future.set_exception(failure())
        removed = [(column, self._coverage.pop(column)) for column in tuple(self._coverage) if invalidated(column)]
        for column, state in removed:
            if state.received and self._column_observers:
                snapshot = self._snapshot(column, dimension_id, state)
                self._notify_observers(self._column_observers, ColumnCoverageEvent("unloaded", snapshot))

    def column_coverage(self, column: tuple[int, int]) -> ColumnCoverage:
        self._validate_column(column)
        return self._snapshot(column, self.dimension_id, self._coverage.get(column))

    @staticmethod
    def _validate_column(column: tuple[int, int]) -> None:
        if not isinstance(column, tuple) or len(column) != 2 or any(type(axis) is not int for axis in column):
            raise ValueError("column must be a pair of integer section coordinates")

    async def wait_for_column_coverage(
        self, column: tuple[int, int], sections: range, *, timeout: float = 15,
    ) -> ColumnCoverage:
        snapshot, = await self._wait_for_coverage(((column, sections),), timeout)
        return snapshot

    async def wait_for_terrain_coverage(
        self, columns: Mapping[tuple[int, int], range], *, timeout: float = 15,
    ) -> tuple[ColumnCoverage, ...]:
        if not isinstance(columns, Mapping):
            raise TypeError("columns must be a mapping of column coordinates to section ranges")
        selection = tuple(columns.items())
        if not selection:
            raise ValueError("columns must not be empty")
        return await self._wait_for_coverage(selection, timeout)

    def _coverage_if_ready(self, columns: _CoverageSelection) -> tuple[ColumnCoverage, ...] | None:
        for column, sections in columns:
            state = self._coverage.get(column)
            if state is None or not all(y in state.received for y in sections):
                return None
        return tuple(self._snapshot(column, self.dimension_id, self._coverage[column]) for column, _ in columns)

    async def _wait_for_coverage(self, columns: _CoverageSelection, timeout: float) -> tuple[ColumnCoverage, ...]:
        for column, sections in columns:
            self._validate_column(column)
            if not isinstance(sections, range) or sections.step != 1 or not -128 <= sections.start < sections.stop <= 128:
                raise ValueError("sections must be a nonempty ascending range within [-128, 128)")
        if self._closed:
            raise ConnectionError("world observation ended")
        if any(not self._column_is_published(*column) for column, _ in columns):
            raise ColumnCoverageInvalidated("unloaded")
        snapshots = self._coverage_if_ready(columns)
        if snapshots is not None:
            return snapshots
        future = asyncio.get_running_loop().create_future()
        entry = (columns, future)
        for column, _ in columns:
            self._column_waiters.setdefault(column, []).append(entry)
        try:
            async with asyncio.timeout(timeout):
                return await future
        finally:
            for column, _ in columns:
                waiters = self._column_waiters[column]
                waiters.remove(entry)
                if not waiters:
                    del self._column_waiters[column]

    def subscribe_column_coverage(self, callback: Callable[[ColumnCoverageEvent], None]) -> Callable[[], None]:
        if self._closed:
            raise ConnectionError("world observation ended")
        return PacketConnection._subscribe(self._column_observers, callback)

    def subscribe_block_changes(
        self, callback: Callable[[BlockChange], None], *, dimension_id: int | None = None,
        position: tuple[int, int, int] | None = None, layer: int | None = None,
        on_dimension_leave: Callable[[int, int], None] | None = None,
    ) -> Callable[[], None]:
        if self._closed:
            raise ConnectionError("world observation ended")
        if on_dimension_leave is not None and dimension_id is None:
            dimension_id = self.dimension_id

        def filtered(change):
            if ((dimension_id is None or dimension_id == change.dimension_id)
                    and (position is None or position == change.after.position)
                    and (layer is None or layer == change.after.layer)):
                callback(change)

        observers = self._block_observers
        observers[filtered] = (dimension_id, on_dimension_leave)

        def unsubscribe():
            observers.pop(filtered, None)

        return unsubscribe

    def subscribe_block_entity_updates(
        self, callback: Callable[[BlockEntityUpdate], None], *,
        position: tuple[int, int, int] | None = None, dimension_id: int | None = None,
    ) -> Callable[[], None]:
        if self._closed or self.packets._reader_end_result()[0]:
            raise ConnectionError("world observation ended")
        registrations = self._block_entity_observers
        if not registrations:
            # Retained login terrain is replayed before any newly received packets.
            actor_id = self.packets.protocol.binding.BlockActorDataPacket.ID
            self._last_replayed_block_entity = next(
                (raw for raw in reversed(self.packets._replayed) if raw.packet_id == actor_id), None,
            )
        key = object()

        def observe(raw):
            applied = self._applied_block_entity
            if applied is None or raw is not applied[0] or key not in registrations:
                return
            _, dimension, pos, root = applied
            if ((position is None or position == pos)
                    and (dimension_id is None or dimension_id == dimension)):
                self._notify_observers((callback,), BlockEntityUpdate(dimension, pos, nbt.CompoundTag(root)))

        def unsubscribe():
            remove = registrations.pop(key, None)
            if remove is not None:
                remove()

        # Join the raw dispatch snapshot so registration inside a raw callback starts with the next packet.
        registrations[key] = self.packets.subscribe_raw(observe)
        return unsubscribe

    def observed_block(self, position: tuple[int, int, int], *, layer: int = 0) -> BlockObservation | None:
        """Return the latest observation, or None until the server describes this layer."""
        x, y, z = position
        runtime_id = self._updates.get((x, y, z, layer))
        if runtime_id is None:
            key = (x >> 4, y >> 4, z >> 4)
            if key not in self._sub_chunks:
                return None
            chunk = self._sub_chunks[key]
            if chunk is not None:
                runtime_id = chunk.get_block(x & 15, y & 15, z & 15, layer)
            if runtime_id is None:
                if layer == 1:
                    # A complete section with no extra layer describes empty space there.
                    return BlockObservation(position, None, layer, True)
                if layer == 0 and chunk is None:
                    runtime_id = HASHED_AIR_RUNTIME_ID if self.hashed_runtime_ids else None
                    return BlockObservation(position, runtime_id, layer, True)
                return None
        if self.hashed_runtime_ids:
            is_air = None if runtime_id == HASHED_UNKNOWN_RUNTIME_ID else runtime_id == HASHED_AIR_RUNTIME_ID
        else:
            state = self._vanilla_state(runtime_id)
            is_air = None if state is None else state.is_air
        return BlockObservation(position, runtime_id, layer, is_air)

    def observed_block_state(
        self, position: tuple[int, int, int], *, layer: int = 0,
    ) -> BlockStateObservation | None:
        if self._protocol != 2225:
            return None
        observation = self.observed_block(position, layer=layer)
        if observation is None:
            return None
        position, runtime_id, layer = observation.position, observation.runtime_id, observation.layer
        if observation.is_air is True:
            return BlockStateObservation(position, runtime_id, layer, "minecraft:air", {})
        if runtime_id is None:
            return None
        state = self._vanilla_state(runtime_id)
        if state is None:
            return None
        properties = _vanilla_state_properties(state.name, runtime_id, hashed=self.hashed_runtime_ids)
        return BlockStateObservation(position, runtime_id, layer, state.name, properties)

    def observed_biome(self, position: tuple[int, int, int]) -> int | None:
        """Return the current column's observed biome ID at an integer block position."""
        x, y, z = position
        state = self._coverage.get((x >> 4, z >> 4))
        if state is None or not state.biomes:
            return None
        lower, _ = self._section_range(self.dimension_id)
        section = (y >> 4) - lower
        if not 0 <= section < len(state.biomes):
            return None
        return state.biomes[section].get(x & 15, y & 15, z & 15)

    def _vanilla_state(self, runtime_id: int | None) -> VanillaBlockState | None:
        if (runtime_id is None or self._protocol != 2225
                or (self._custom_blocks and not self.hashed_runtime_ids)):
            return None
        return vanilla_block_state(runtime_id, hashed=self.hashed_runtime_ids)

    def _outline_at(self, position: tuple[int, int, int]) -> OutlineObservation:
        observation = self.observed_block(position)
        if observation is None:
            return None, True
        if observation.is_air:
            return (), False
        state = self._vanilla_state(observation.runtime_id)
        return (None, True) if state is None else (state.outline, state.unresolved_overhang)

    def _explosion_collision_at(
        self, position: tuple[int, int, int],
    ) -> tuple[tuple[float, ...], ...] | None:
        """Return block-local collision boxes for exposure, or None for unresolved terrain."""
        if self._protocol != 2225:
            return None
        block = self.observed_block(position)
        if block is None or block.is_air is None:
            return None
        extra = self.observed_block(position, layer=1)
        if extra is None or extra.is_air is not True:
            return None
        if block.is_air:
            return ()
        runtime_id = block.runtime_id
        if runtime_id is None:
            return None
        index = _catalog()[1].get(runtime_id, -1) if self.hashed_runtime_ids else runtime_id
        return _EXPLOSION_COLLISION_SHAPES.get(index)

    def raycast_block(self, max_distance: float) -> BlockRaycastResult:
        return self._raycast_view(self.movement.raycast_pose, max_distance)

    def _raycast_view(
        self, pose: tuple[tuple[float, float, float] | None, tuple[float, float]] | None, max_distance: float,
    ) -> BlockRaycastResult:
        eye, rotation = (None, (0.0, 0.0)) if pose is None else pose
        return trace_blocks(eye, rotation, dimension_id=self.dimension_id,
                            max_distance=max_distance, outline_at=self._outline_at)

    def raycast_block_towards(self, point: tuple[float, float, float], max_distance: float) -> BlockRaycastResult:
        eye, _ = self.movement.raycast_pose
        return trace_blocks_towards(eye, point, dimension_id=self.dimension_id,
                                    max_distance=max_distance, outline_at=self._outline_at)

    def mining_face(self, position: tuple[int, int, int]) -> MiningFaceResult:
        return self._sample_mining_face(position)

    def _sample_mining_face(self, position, *, encoded: bool = False) -> MiningFaceResult:
        position = tuple(position)
        if len(position) != 3 or any(type(axis) is not int for axis in position):
            raise ValueError("mining position must contain three integer coordinates")
        if self._closed or self.packets._reader_end_result()[0]:
            return MiningFaceResult("unknown", self.dimension_id, position)
        eye, _ = self.movement.raycast_pose
        if not self.movement._can_send_input:
            eye = None

        def check_sample(sample):
            point = tuple(cell + local for cell, local in zip(position, sample.click_pos, strict=True))
            _, status = self._encoded_mining_aim(eye, point, position, sample.face)
            return status

        return trace_mining_face(eye, position, dimension_id=self.dimension_id,
                                 max_distance=_ORDINARY_MINING_REACH, outline_at=self._outline_at,
                                 check_sample=check_sample if encoded else None)

    def _observed_positions_in_sphere(
        self, center: tuple[int, int, int], radius: int,
    ) -> Iterator[tuple[tuple[int, int, int], int]]:
        cx, cy, cz = center
        squared = radius * radius
        for sx, sy, sz in self._sub_chunks:
            lo_x, lo_y, lo_z = 16 * sx, 16 * sy, 16 * sz
            if lo_z > cz + radius or lo_z + 15 < cz - radius:
                continue
            for x in range(max(lo_x, cx - radius), min(lo_x + 15, cx + radius) + 1):
                cross_section = squared - (x - cx) ** 2
                span = isqrt(cross_section)
                for y in range(max(lo_y, cy - span), min(lo_y + 15, cy + span) + 1):
                    remaining = cross_section - (y - cy) ** 2
                    depth = isqrt(remaining)
                    for z in range(max(lo_z, cz - depth), min(lo_z + 15, cz + depth) + 1):
                        yield (x, y, z), squared - remaining + (z - cz) ** 2
        for x, y, z, layer in self._updates:
            if layer == 0 and (x >> 4, y >> 4, z >> 4) not in self._sub_chunks:
                distance = (x - cx) ** 2 + (y - cy) ** 2 + (z - cz) ** 2
                if distance <= squared:
                    yield (x, y, z), distance

    def find_blocks(
        self, names: str | int | Iterable[str] | Iterable[int] | None = None, *, radius: int, limit: int | None,
        predicate: Callable[[BlockMatch], bool | None] | None = None,
        center: tuple[float, float, float] | None = None,
    ) -> BlockSearchResult:
        if not isinstance(radius, int) or (limit is not None and not isinstance(limit, int)):
            raise TypeError("radius and limit must be integers")
        if radius < 0 or (limit is not None and limit < 0):
            raise ValueError("radius and limit cannot be negative")
        if predicate is not None and not callable(predicate):
            raise TypeError("predicate must be callable")
        wanted_names: set[str] = set()
        wanted_ids: set[int] = set()
        if names is None:
            if predicate is None:
                raise ValueError("a block name, runtime ID, or predicate is required")
        else:
            selectors = (names,) if isinstance(names, (str, int)) else names
            for selector in selectors:
                if isinstance(selector, str):
                    if not selector:
                        raise ValueError("block names cannot be empty")
                    wanted_names.add(selector if ":" in selector else f"minecraft:{selector}")
                elif isinstance(selector, int) and not isinstance(selector, bool):
                    if self.hashed_runtime_ids and -(1 << 31) <= selector < 0:
                        selector &= 0xFFFFFFFF
                    if not 0 <= selector <= 0xFFFFFFFF:
                        raise ValueError("block runtime ID is outside the session's 32-bit range")
                    wanted_ids.add(selector)
                else:
                    raise TypeError("block selectors must be names or integer runtime IDs")
            if wanted_names and wanted_ids:
                raise TypeError("block names and runtime IDs cannot be mixed")
            if not wanted_names and not wanted_ids:
                raise ValueError("at least one block name or runtime ID is required")
        if center is None:
            position = self.movement.position
            if position is None:
                raise RuntimeError("player position has not synchronized")
        else:
            from math import isfinite

            if len(center) != 3 or not all(isinstance(axis, int) or isfinite(axis) for axis in center):
                raise ValueError("center must contain three finite coordinates")
            position = center
        x, y, z = position
        origin = (floor(x), floor(y), floor(z))
        resolved = 0
        predicates_resolved = True

        def matches() -> Iterator[BlockMatch]:
            nonlocal resolved, predicates_resolved
            for cell, distance in self._observed_positions_in_sphere(origin, radius):
                observation = self.observed_block(cell)
                if observation is None:
                    continue
                runtime_id = observation.runtime_id
                if wanted_ids:
                    if runtime_id is None:
                        continue
                    if runtime_id not in wanted_ids:
                        resolved += 1
                        continue
                if observation.is_air:
                    name = "minecraft:air"
                else:
                    state = self._vanilla_state(runtime_id)
                    name = None if state is None else state.name
                if wanted_names and name is None:
                    continue
                resolved += 1
                if (wanted_names and name not in wanted_names) or (limit == 0 and predicate is None):
                    continue
                match = BlockMatch(cell, name, runtime_id, distance)
                if predicate is not None:
                    decision = predicate(match)
                    if decision is None:
                        predicates_resolved = False
                        continue
                    if decision is False:
                        continue
                    if decision is not True:
                        raise TypeError("predicate must return True, False, or None")
                if limit != 0:
                    yield match

        def rank(match: BlockMatch) -> tuple[int, int, int, int]:
            return match.distance_squared, *match.position

        ordered = sorted(matches(), key=rank) if not limit else nsmallest(limit, matches(), key=rank)
        complete = predicates_resolved and _sphere_is_covered(radius, resolved)
        return BlockSearchResult(origin, self.dimension_id, tuple(ordered), complete)

    def _wide_vein_neighbors(self, center: tuple[int, int, int], distance: int):
        """Share unread section masks across searches from the same seed."""
        cx, cy, cz = center
        sparse: dict[tuple[int, int, int], int] = {}
        for x, y, z, layer in self._updates:
            if layer == 0 and abs(x - cx) + abs(y - cy) + abs(z - cz) <= distance:
                key = (x >> 4, y >> 4, z >> 4)
                if key not in self._sub_chunks:
                    sparse[key] = sparse.get(key, 0) | _vein_bit(x, y, z)
        active = set(sparse)
        active.update(
            section for section in self._sub_chunks
            if sum(max((axis << 4) - coord, coord - (axis << 4) - 15, 0)
                   for axis, coord in zip(section, center)) <= distance
        )
        pending: dict[tuple[int, int, int], int] = {}
        seed_section = tuple(axis >> 4 for axis in center)
        seed_bit = _vein_bit(*center)

        def cells(low, high, check_coverage) -> Iterator[tuple[int, int, int] | None]:
            if check_coverage and any(
                (x, y, z) not in self._sub_chunks and (x, y, z) not in sparse
                for x, y, row in _vein_rows(center, distance, low, high, shift=4) for z in row
            ):
                check_coverage = False
                yield None
            key_volume = prod((upper >> 4) - (lower >> 4) + 1 for lower, upper in zip(low, high))
            if key_volume <= len(active):
                sections = (
                    (x, y, z) for x, y, row in _vein_rows(center, distance, low, high, shift=4) for z in row
                    if (x, y, z) in active
                )
            else:
                sections = iter(tuple(active))
            for section in sections:
                box = _vein_box_mask(section, low, high)
                if box == 0:
                    continue
                unread = pending.get(section)
                sx, sy, sz = origin = tuple(axis << 4 for axis in section)
                if unread is None:
                    end = sx + 15, sy + 15, sz + 15
                    unread = sum(
                        ((1 << (row.stop - row.start)) - 1) * _vein_bit(x, y, row.start)
                        for x, y, row in _vein_rows(center, distance, origin, end)
                    )
                    if section == seed_section:
                        unread &= ~seed_bit
                updates = sparse.get(section)
                if not check_coverage and updates is not None:
                    unread &= updates
                within = unread & box
                unread ^= within
                if unread:
                    pending[section] = unread
                else:
                    active.remove(section)
                    pending.pop(section, None)
                if updates is not None:
                    described = within & updates
                    if check_coverage and described != within:
                        check_coverage = False
                        yield None
                    within = described
                # Clear each observed position as it enters the shared search.
                while within:
                    bit = within & -within
                    index = bit.bit_length() - 1
                    yield (sx + (index >> 8), sy + (index >> 4 & 15), sz + (index & 15))
                    within ^= bit

        return cells

    def find_block_vein(
        self, position: tuple[int, int, int], *, max_blocks: int, max_distance: int, flood_radius: int,
    ) -> BlockSearchResult:
        if not isinstance(position, (tuple, list)) or len(position) != 3 or any(type(axis) is not int for axis in position):
            raise ValueError("expected three integer block coordinates")
        if any(type(value) is not int for value in (max_blocks, max_distance, flood_radius)):
            raise TypeError("max_blocks, max_distance and flood_radius must be integers")
        if min(max_blocks, max_distance, flood_radius) < 0:
            raise ValueError("max_blocks, max_distance and flood_radius cannot be negative")
        position = tuple(position)
        dimension = self.dimension_id
        sections = self._section_range(dimension)
        if sections is not None and not sections[0] <= (position[1] >> 4) < sections[1]:
            return BlockSearchResult(position, dimension, (), True)
        seed = self.observed_block(position)
        if seed is not None and seed.is_air is True:
            return BlockSearchResult(position, dimension, (), True)
        state = None if seed is None else self._vanilla_state(seed.runtime_id)
        if state is None or max_blocks == 0:
            return BlockSearchResult(position, dimension, (), False)

        cx, cy, cz = position
        min_y, max_y = ((cy - max_distance, cy + max_distance) if sections is None
                        else (16 * sections[0], 16 * sections[1] - 1))
        name = state.name
        matches = [BlockMatch(position, name, seed.runtime_id, 0)]
        if max_distance == 0 or flood_radius == 0:
            return BlockSearchResult(position, dimension, tuple(matches), True)
        seen = {position}
        complete = True
        width = 2 * min(flood_radius, max_distance) + 1
        volume = width * width * min(width, max_y - min_y + 1)
        wide_cells = self._wide_vein_neighbors(position, max_distance) if volume > 16**3 else None

        # Appending to the list adds the next breadth-first candidates.
        for current in matches:
            x, y, z = current.position
            low = (max(cx - max_distance, x - flood_radius),
                   max(cy - max_distance, y - flood_radius, min_y),
                   max(cz - max_distance, z - flood_radius))
            high = (min(cx + max_distance, x + flood_radius),
                    min(cy + max_distance, y + flood_radius, max_y),
                    min(cz + max_distance, z + flood_radius))
            if wide_cells is not None:
                cells = wide_cells(low, high, complete)
            else:
                cells = ((px, py, pz) for px, py, row in _vein_rows(position, max_distance, low, high) for pz in row)

            def neighbors() -> Iterator[BlockMatch]:
                nonlocal complete
                for cell in cells:
                    observation = None if cell is None else self.observed_block(cell)
                    if observation is None:
                        complete = False
                        continue
                    if observation.is_air is True:
                        continue
                    known = self._vanilla_state(observation.runtime_id)
                    if known is None:
                        complete = False
                        continue
                    if known.name == name and cell not in seen:
                        squared = (cell[0] - cx)**2 + (cell[1] - cy)**2 + (cell[2] - cz)**2
                        yield BlockMatch(cell, name, observation.runtime_id, squared)

            available = max_blocks - len(matches)
            candidates: Iterable[BlockMatch] = neighbors()
            if wide_cells is not None and available:
                candidates = nsmallest(available + 1, candidates, key=lambda match: match.position)
            for candidate in candidates:
                if len(matches) == max_blocks:
                    return BlockSearchResult(position, dimension, tuple(matches), False)
                matches.append(candidate)
                seen.add(candidate.position)
            if flood_radius >= max_distance:
                break  # The seed already covered every eligible coordinate.
            if not complete and len(matches) == max_blocks:
                break
        return BlockSearchResult(position, dimension, tuple(matches), complete)

    def _stance_air(self, observation: BlockObservation | None) -> bool | None:
        if observation is None or observation.is_air is None:
            return None
        if observation.is_air:
            return True
        return False if self._vanilla_state(observation.runtime_id) is not None else None

    def _water_contact(self, eye: tuple[float, float, float]) -> bool | None:
        """Find observed water in the center column of the standing player span."""
        x, y, z = eye
        unknown = False
        for block_y in range(floor(y - 1.62 + 0.01), floor(y) + 1):
            for layer in (0, 1):
                block = self.observed_block((floor(x), block_y, floor(z)), layer=layer)
                if block is not None and block.is_air is True:
                    continue
                state = None if block is None else self._vanilla_state(block.runtime_id)
                if state is None:
                    unknown = True
                elif state.name in ("minecraft:water", "minecraft:flowing_water"):
                    return True
        return None if unknown else False

    def _stance_clearance(self, position: tuple[int, int, int], *, allow_vegetation: bool = False) -> bool | None:
        block = self.observed_block(position)
        if allow_vegetation and block is not None and block.is_air is False:
            state = self._vanilla_state(block.runtime_id)
            body = None if state is None else state.name in (
                "minecraft:dandelion", "minecraft:fern", "minecraft:short_grass", "minecraft:wildflowers",
            )
        else:
            body = self._stance_air(block)
        extra = self._stance_air(self.observed_block(position, layer=1))
        if body is False or extra is False:
            return False
        return True if body is True and extra is True else None

    def _flight_collision_boxes(
        self, state: VanillaBlockState, runtime_id: int,
    ) -> tuple[tuple[float, ...], ...] | None:
        name = state.name
        if state.unit_cube:
            return ((0, 0, 0, 1, 1, 1),)
        if name.endswith("_slab") and state.outline is not None:
            return (state.outline,)
        if name == "minecraft:border_block":
            return ((0, 0, 0, 1, 1.5, 1),)
        if name.endswith("_wall") and state.outline is not None:
            # Bedrock wall collision shares the exported X/Z outline bounds.
            min_x, _, min_z, max_x, _, max_z = state.outline
            return ((min_x, 0, min_z, max_x, 1.5, max_z),)
        if not (name == "minecraft:pitcher_crop" or name.endswith(("_fence", "fence_gate"))):
            return None

        properties = _vanilla_state_properties(name, runtime_id, hashed=self.hashed_runtime_ids)

        if name == "minecraft:pitcher_crop":
            if properties["upper_block_bit"]:
                return ()
            edge, height = (0.3125, 0.1875) if properties["growth"] == 0 else (0.1875, 0.3125)
            return ((edge, -0.0625, edge, 1 - edge, height, 1 - edge),)
        if name.endswith("_fence"):
            # The collision is the union of the two arms, which can leave corner gaps.
            west, east, north, south = (
                properties[f"minecraft:connection_{direction}"]
                for direction in ("west", "east", "north", "south")
            )
            return (
                (0 if west else 0.375, 0, 0.375, 1 if east else 0.625, 1.5, 0.625),
                (0.375, 0, 0 if north else 0.375, 0.625, 1.5, 1 if south else 0.625),
            )
        if properties["open_bit"]:
            return ()
        if properties["minecraft:cardinal_direction"] in ("north", "south"):
            return ((0, 0, 0.375, 1, 1.5, 0.625),)
        return ((0.375, 0, 0, 0.625, 1.5, 1),)

    def _flight_clearance(
        self, start: tuple[float, float, float], end: tuple[float, float, float],
    ) -> bool | None:
        """Check a swept 0.6 by 1.8 standing body against observed terrain."""
        low = (-0.3, 0.0, -0.3)
        high = (0.3, 1.8, 0.3)
        delta = tuple(b - a for a, b in zip(start, end))
        # Ignore surface contact and float32 round-off at the feet.
        epsilon = 0.0001

        def interval(axis, minimum, maximum):
            a, d = start[axis], delta[axis]
            enter = minimum - high[axis] + epsilon
            leave = maximum - low[axis] - epsilon
            if d == 0:
                return (0.0, 1.0) if enter < a < leave else (1.0, 0.0)
            return sorted(((enter - a) / d, (leave - a) / d))

        def intersects(x, y, z, box):
            x0, x1 = interval(0, x + box[0], x + box[3])
            y0, y1 = interval(1, y + box[1], y + box[4])
            z0, z1 = interval(2, z + box[2], z + box[5])
            return max(0.0, x0, y0, z0) < min(1.0, x1, y1, z1)

        # The 2225 collision export reaches 0.5 above a block and 1/16 below it,
        # with no static shapes reaching outside X/Z. Use this to find candidate cells.
        axes = []
        for axis, (overhang_min, overhang_max) in enumerate(((0, 1), (-0.0625, 1.5), (0, 1))):
            intervals = []
            a, b = start[axis], end[axis]
            first = floor(min(a, b) + low[axis] + 1 - overhang_max)
            last = floor(max(a, b) + high[axis] - overhang_min)
            for cell in range(first, last + 1):
                t0, t1 = interval(axis, cell + overhang_min, cell + overhang_max)
                if max(0.0, t0) < min(1.0, t1):
                    intervals.append((cell, t0, t1))
            axes.append(intervals)

        unit_box = (0, 0, 0, 1, 1, 1)
        known = True
        for x, x0, x1 in axes[0]:
            for y, y0, y1 in axes[1]:
                for z, z0, z1 in axes[2]:
                    if max(0.0, x0, y0, z0) >= min(1.0, x1, y1, z1):
                        continue
                    for layer in (0, 1):
                        block = self.observed_block((x, y, z), layer=layer)
                        if block is not None and block.is_air is True:
                            continue
                        runtime_id = None if block is None else block.runtime_id
                        if runtime_id is None:
                            known = False
                            continue
                        state = self._vanilla_state(runtime_id)
                        if state is None or state.piston_face is not None or state.name in (
                            "minecraft:moving_block", "minecraft:piston_arm_collision",
                            "minecraft:sticky_piston_arm_collision",
                        ) or state.name.endswith("_shulker_box"):
                            known = False
                            continue
                        boxes = self._flight_collision_boxes(state, runtime_id)
                        if boxes is None:
                            # Unresolved effects and in-cell shapes (such as fluids or doors).
                            if intersects(x, y, z, unit_box):
                                known = False
                        elif any(intersects(x, y, z, box) for box in boxes):
                            return False
        return True if known else None

    def _has_full_collision(self, position: tuple[int, int, int]) -> bool:
        block = self.observed_block(position)
        state = None if block is None else self._vanilla_state(block.runtime_id)
        return state is not None and state.unit_cube

    def _stance_support(self, position: tuple[int, int, int]) -> bool | None:
        floor = self.observed_block(position)
        if floor is not None and floor.is_air is True:
            return False
        support = None if floor is None else self._vanilla_state(floor.runtime_id)
        if support is None:
            return None
        if support.unit_cube:
            return not support.unsafe_support
        # A dry upper slab has the same standing plane as a full cube.
        if support.name.endswith("_slab") and support.outline == (0, 0.5, 0, 1, 1, 1):
            return self._stance_air(self.observed_block(position, layer=1))
        return False

    def _placement_support(self, position: tuple[int, int, int]) -> bool | None:
        main = self.observed_block(position)
        extra = self.observed_block(position, layer=1)
        if (main is not None and main.is_air is True) or (extra is not None and extra.is_air is False):
            return False
        state = None if main is None else self._vanilla_state(main.runtime_id)
        if state is not None and (not state.unit_cube or state.outline not in (None, _UNIT_BOX)):
            return False
        if state is None or state.outline is None or extra is None or extra.is_air is None:
            return None
        return True

    def _flat_stance(self, position: tuple[int, int, int], *, allow_vegetation: bool = False) -> bool | None:
        """Classify a standing column, with optional vegetation in the primary feet layer."""
        x, y, z = position
        support = self._stance_support((x, y - 1, z))
        if support is False:
            return False

        known = support is True
        for cell, vegetation in ((position, allow_vegetation), ((x, y + 1, z), False)):
            clear = self._stance_clearance(cell, allow_vegetation=vegetation)
            if clear is False:
                return False
            known = known and clear is True
        return True if known else None

    def _descent_landing(self, position: tuple[int, int, int]) -> bool | None:
        """Check the standing column and the head space while crossing above it."""
        x, y, z = position
        standing = self._flat_stance(position)
        overhead = self._stance_clearance((x, y + 2, z))
        if standing is False or overhead is False:
            return False
        return True if standing is True and overhead is True else None

    def _dry_grass_path_stance(self, position: tuple[int, int, int]) -> bool | None:
        x, y, z = position
        floor = (x, y - 1, z)
        block = self.observed_block(floor)
        state = None if block is None else self._vanilla_state(block.runtime_id)
        if (state is None or state.name != "minecraft:grass_path"
                or state.outline != (0, 0, 0, 1, 15 / 16, 1)):
            return False
        clearance = (self._stance_air(self.observed_block(floor, layer=1)),
                     self._stance_clearance(position), self._stance_clearance((x, y + 1, z)))
        if False in clearance:
            return False
        return True if all(clearance) else None

    def _ascent_low_roof(self, ceiling: tuple[int, int, int]) -> bool | None:
        block = self.observed_block(ceiling)
        roof = None if block is None else self._vanilla_state(block.runtime_id)
        if (roof is not None and roof.unit_cube and roof.outline == _UNIT_BOX
                and not roof.unsafe_support and not roof.unresolved_overhang
                and (roof.name.endswith("_leaves") or roof.plain_masonry)):
            return self._stance_air(self.observed_block(ceiling, layer=1))
        return False

    def _ascent_step(
        self, source: tuple[int, int, int], landing: tuple[int, int, int],
    ) -> bool | None:
        sx, sy, sz = source
        lx, ly, lz = landing
        ceiling = (lx, ly + 2, lz)
        overhead = self._stance_clearance(ceiling)
        if overhead is False:
            overhead = self._ascent_low_roof(ceiling)
        # A 1.25-block rise and 1.8-block player can enter the fourth block
        # above the lower floor while still overlapping the launch column.
        clearance = (
            self._flat_stance(source), self._flat_stance(landing),
            self._stance_clearance((sx, sy + 2, sz)),
            self._stance_clearance((sx, sy + 3, sz)),
            overhead,
        )
        if False in clearance:
            return False
        return True if all(clearance) else None

    def _ordinary_stone_support(self, position: tuple[int, int, int]) -> bool | None:
        floor = self.observed_block(position)
        state = None if floor is None else self._vanilla_state(floor.runtime_id)
        if floor is not None and floor.is_air is True:
            return False
        extra = self._stance_air(self.observed_block(position, layer=1))
        if extra is False or (state is not None and
                              (state.name != "minecraft:stone" or not state.unit_cube or state.unsafe_support)):
            return False
        return True if state is not None and extra is True else None

    def _lower_smooth_stone_slab(self, position: tuple[int, int, int]) -> bool | None:
        floor = self.observed_block(position)
        extra = self._stance_air(self.observed_block(position, layer=1))
        if extra is False or (floor is not None and floor.is_air is True):
            return False
        state = None if floor is None else self._vanilla_state(floor.runtime_id)
        if state is None:
            return None
        if state.name != "minecraft:smooth_stone_slab" or state.outline != (0, 0, 0, 1, 0.5, 1):
            return False
        properties = _vanilla_state_properties(state.name, floor.runtime_id, hashed=self.hashed_runtime_ids)
        return extra if properties.get("minecraft:vertical_half") == "bottom" else False

    def _lower_slab_lane(self, source: tuple[int, int, int], dx: int, dz: int, stride: int) -> bool | None:
        """Check the dry, straight strip between two integer-height stone stances."""
        sx, y, sz = source
        known = True
        for column in (*range(1, stride), 0, stride):
            x, z = sx + column * dx, sz + column * dz
            floor = (x, y - 1, z)
            support = (self._ordinary_stone_support(floor) if column in (0, stride)
                       else self._lower_smooth_stone_slab(floor))
            if support is False:
                return False
            feet = self._stance_clearance((x, y, z))
            head = self._stance_clearance((x, y + 1, z))
            if feet is False or head is False:
                return False
            known = known and support is True and feet is True and head is True
        return True if known else None

    def _still_water(self, position: tuple[int, int, int]) -> bool | None:
        block = self.observed_block(position)
        extra = self._stance_air(self.observed_block(position, layer=1))
        if extra is False or (block is not None and block.is_air is not None
                              and block.runtime_id != _SOURCE_WATER_HASH):
            return False
        return True if block is not None and block.runtime_id == _SOURCE_WATER_HASH and extra is True else None

    def _still_water_crossing(self, source, dx, dz, stride, deck_length) -> bool | None:
        """Check the calibrated pool and the complete dry runout."""
        if self._protocol != 2225 or not self.hashed_runtime_ids:
            return False
        x, y, z = source

        def at(column, level, lateral=0):
            return x + column * dx - lateral * dz, level, z + column * dz + lateral * dx

        first = self._still_water(at(2, y - 1))
        if first is False:
            return False
        required = [first]
        for column in range(2, stride):
            for lane in range(-3, 4):
                required.append(self._ordinary_stone_support(at(column, y - 4, lane)))
                required.extend(self._still_water(at(column, level, lane)) for level in range(y - 3, y))
                required.extend(self._stance_clearance(at(column, level, lane)) for level in range(y, y + 3))
            for side in (-4, 4):
                required.extend(self._ordinary_stone_support(at(column, level, side)) for level in range(y - 3, y))
        for column in (0, 1, *range(stride, stride + deck_length)):
            for lane in (-1, 0, 1):
                required.append(self._ordinary_stone_support(at(column, y - 1, lane)))
                required.extend(self._stance_clearance(at(column, level, lane)) for level in range(y, y + 3))
        # The two vertical faces contain the whole source-water volume.
        for column in (1, stride):
            for lane in range(-3, 4):
                required.extend(self._ordinary_stone_support(at(column, level, lane)) for level in range(y - 3, y))
        if False in required:
            return False
        return True if all(required) else None

    def _ladder_rung(self, position: tuple[int, int, int], facing: int) -> bool | None:
        rung = self.observed_block(position)
        if rung is not None and rung.is_air is True:
            return False
        state = None if rung is None else self._vanilla_state(rung.runtime_id)
        if state is not None and state.ladder_facing != facing:
            return False
        clear = self._stance_air(self.observed_block(position, layer=1))
        if clear is False:
            return False
        return True if state is not None and clear is True else None

    def _gap_jump(
        self, source: tuple[int, int, int], landing: tuple[int, int, int],
    ) -> bool | None:
        sx, y, sz = source
        lx, _, lz = landing
        mx, mz = (sx + lx) // 2, (sz + lz) // 2
        required = [self._stance_clearance((mx, y - 1, mz)), self._ordinary_stone_support((sx, y - 1, sz)),
                    self._ordinary_stone_support((lx, y - 1, lz))]
        # The peak and the standing player's height reach into the fourth block.
        for x, z in ((sx, sz), (mx, mz), (lx, lz)):
            required.extend(self._stance_clearance((x, level, z)) for level in range(y, y + 4))
        if False in required:
            return False
        return True if all(required) else None

    def _stone_ledge_drop(
        self, source: tuple[int, int, int], runout: tuple[tuple[int, int, int], ...],
    ) -> bool | None:
        sx, sy, sz = source
        required = [self._ordinary_stone_support((sx, sy - 1, sz)),
                    self._stance_clearance(source), self._stance_clearance((sx, sy + 1, sz))]
        for x, y, z in runout:
            required.append(self._ordinary_stone_support((x, y - 1, z)))
            required.extend(self._stance_clearance((x, level, z)) for level in range(y, sy + 2))
        if False in required:
            return False
        return True if all(required) else None

    def _requestable_column(self, position: tuple[int, int, int]) -> _ColumnCoverage | None:
        x, y, z = position
        state = self._coverage.get((x >> 4, z >> 4))
        return state if state is not None and y >> 4 in state.requestable_sections else None

    async def request_sub_chunk(self, position: tuple[int, int, int]) -> None:
        """Request the 16³ section containing position; submission is not a response."""
        b = self.packets.protocol.binding
        x, y, z = position
        request = b.SubChunkRequestPacket(
            dimension_type=self.dimension_id,
            center_pos=b.SubChunkPos(x=x >> 4, y=0, z=z >> 4),
            sub_chunk_pos_offsets=[b.SubChunkPacket.SubChunkPosOffset(x=0, y=y >> 4, z=0)],
        )
        await self._send_sub_chunk_request(request)

    async def _send_sub_chunk_request(self, request) -> None:
        if self._closed or int(request.dimension_type) != self.dimension_id:
            raise ConnectionError("world observation ended or changed dimension")
        x, z = int(request.center_pos.x), int(request.center_pos.z)
        column = (x, z)
        state = self._coverage.setdefault(column, _ColumnCoverage()) if self._column_is_published(x, z) else None
        await self.packets.send(request)
        if state is not None and self._coverage.get(column) is state:
            state.requested.update(int(request.center_pos.y) + int(offset.y) for offset in request.sub_chunk_pos_offsets)

    async def wait_for_update(
        self, position: tuple[int, int, int], *, layer: int = 0, timeout: float = 15
    ) -> BlockUpdate:
        """Wait for a *new* UpdateBlock packet, not a snapshot of the world."""
        binding = self.packets.protocol.binding

        def matches(raw):
            packet = self.packets.protocol.decode(raw)
            pos = (int(packet.pos.x), int(packet.pos.y), int(packet.pos.z))
            return pos == position and int(packet.layer) == layer

        raw = await self.packets.wait_for_packet(
            binding.UpdateBlockPacket.ID, timeout=timeout, match=matches
        )
        packet = self.packets.protocol.decode(raw)
        return BlockUpdate(position, int(packet.runtime_id), int(packet.layer))

    async def place_against(
        self, against: BlockUpdate | BlockObservation, *, face: int = 1,
        slot: int = 0, click_pos: tuple[float, float, float] | None = None,
    ) -> int:
        """Submit a block-face interaction; return the input tick, not acceptance.

        The support may come from an UpdateBlock or an observed sub-chunk.
        Observe the resulting block and/or BDS state to establish the effect.
        """
        b = self.packets.protocol.binding
        item = await self.inventory.wait_for_item(slot)
        x, y, z = await self.movement.wait_for_position_sync()
        await self.inventory.announce_equipment(slot, item)
        px, py, pz = against.position
        if click_pos is None:
            dx, dy, dz = FACE_OFFSETS.get(face, (0, 0, 0))
            click_pos = (0.5 + dx * 0.5, 0.5 + dy * 0.5, 0.5 + dz * 0.5)
        transaction = b.ItemUseInventoryTransaction(
            transaction=b.InventoryTransaction(actions=[]),
            action_type=b.ItemUseInventoryTransaction.ActionType.PLACE,
            trigger_type=b.ItemUseInventoryTransaction.TriggerType.PLAYER_INPUT,
            pos=b.BlockPos(x=px, y=py, z=pz),
            face=face,
            slot=slot,
            hand=b.HandSlot.MAINHAND,
            item=item,
            from_pos=b.Vec3(x=x, y=y, z=z),
            click_pos=b.Vec3(x=click_pos[0], y=click_pos[1], z=click_pos[2]),
            target_block_id=against.runtime_id or 0,
            client_predicted_result=b.ItemUseInventoryTransaction.PredictedResult.SUCCESS,
            client_cooldown_state=b.ItemUseInventoryTransaction.ClientCooldownState.OFF,
        )
        packed = b.PackedItemUseLegacyInventoryTransaction(
            id=b.ItemStackLegacyRequestId(raw_id=0),
            slots=None,
            transaction=transaction,
        )
        return await self.movement.send_item_use(packed)

    @property
    def mining_target(self) -> MiningTarget | None:
        return None if self._mining_completed else self._mining_target

    def _check_mining_epoch(self, epoch: int) -> None:
        if self._mining_closed:
            raise ConnectionError("mining session is closed")
        if self._mining_epoch != epoch:
            raise RuntimeError("world changed before submitting mining input")

    def _held_mining_generation(self, position: tuple[int, int, int]) -> int | None:
        target = self.mining_target
        if target is not None and target.position == position:
            return self._mining_generation
        return None

    def _observed_mining_input(self, target: MiningTarget, *, allow_edge_hit: bool = False):
        if target.face not in FACE_OFFSETS:
            raise ValueError("mining face must be between 0 and 5")
        block = self.observed_block(target.position)
        if block is None or block.is_air is not False or block.runtime_id is None:
            raise ValueError("mining requires an observed non-air block")
        offset = FACE_OFFSETS[target.face]
        click = tuple(0.5 + value * 0.5 for value in offset)
        eye = self.movement.input_position
        if dist(eye, tuple(base + coordinate for base, coordinate in zip(target.position, click))) > _ORDINARY_MINING_REACH:
            if not allow_edge_hit or not self._has_mining_view_hit(target):
                raise ValueError("mining target is outside ordinary reach")
        return block, eye, click

    def _has_mining_view_hit(self, target: MiningTarget) -> bool:
        pose = self.movement._stationary_mining_pose()
        if pose is None:
            return False
        eye, (pitch, yaw) = pose
        aim = self.packets.protocol.binding.Vec2(x=pitch, y=yaw)
        hit = self._encoded_mining_ray(eye, aim)
        return hit.status == "hit" and hit.position == target.position and hit.face == target.face

    def _encoded_mining_ray(self, eye, aim) -> BlockRaycastResult:
        # Use the same native float32 position and rotation as PlayerAuthInput.
        wire_eye = self.packets.protocol.binding.Vec3(x=eye[0], y=eye[1], z=eye[2])
        return self._raycast_view(((wire_eye.x, wire_eye.y, wire_eye.z), (aim.x, aim.y)), _ORDINARY_MINING_REACH)

    def _encoded_mining_aim(self, eye, point, position, face):
        b = self.packets.protocol.binding
        dx, dy, dz = (value - start for value, start in zip(point, eye, strict=True))
        aim = b.Vec2(x=degrees(-atan2(dy, hypot(dx, dz))), y=degrees(atan2(-dx, dz)))
        hit = self._encoded_mining_ray(eye, aim)
        if hit.status == "unknown":
            status = "unknown"
        elif hit.position == position and hit.face == face:
            status = "hit"
        else:
            status = "miss"
        return (float(aim.x), float(aim.y)), status

    def _aim_mining_face(self, position, face, click_pos, *, encoded: bool = False) -> None:
        pose = self.movement._stationary_mining_pose()
        if pose is None:
            raise MiningFaceUnavailable("unknown")
        eye, _ = pose
        point = tuple(cell + local for cell, local in zip(position, click_pos, strict=True))
        if dist(eye, point) > _ORDINARY_MINING_REACH:
            raise MiningFaceUnavailable("no_sample")
        hit = self.raycast_block_towards(point, _ORDINARY_MINING_REACH)
        if hit.status == "unknown":
            raise MiningFaceUnavailable("unknown")
        if (hit.position != position or hit.face != face or hit.click_pos is None
                or dist(hit.click_pos, click_pos) > 1e-6):
            raise MiningFaceUnavailable("no_sample")
        if not encoded:
            self.movement._aim_at(point)
            return
        rotation, status = self._encoded_mining_aim(eye, point, position, face)
        if status != "hit":
            raise MiningFaceUnavailable("unknown" if status == "unknown" else "no_sample")
        self.movement.set_rotation(*rotation)

    def _release_mining_actions(self, target: MiningTarget | None = None) -> list:
        if self._mining_target is None:
            return []
        actions = []
        if target is None:
            target = self.mining_target
        if target is not None:
            actions.append(("ABORT_DESTROY_BLOCK", target.position, 0))
        actions.append(("STOP_DESTROY_BLOCK", (0, 0, 0), 0))
        return actions

    async def _submit_mining(
        self, actions, target: MiningTarget | None, epoch: int, *,
        validate: Callable[[], None] | None = None, build_item_use: Callable[[], object] | None = None,
        continuing_generation: int | None = None,
    ) -> int:
        def commit():
            self._check_mining_epoch(epoch)
            if continuing_generation is not None and (
                target is None or self._held_mining_generation(target.position) != continuing_generation
            ):
                raise RuntimeError("mining target changed before continuing")
            if validate is not None:
                validate()
            if continuing_generation is None:
                self._mining_generation += 1
            self._mining_target = target
            self._mining_completed = False

        return await self.movement.send_block_actions(
            actions, before_send=commit, build_item_use=build_item_use,
        )

    async def start_mining(self, position: tuple[int, int, int], *, face: int = 1) -> int:
        return await self._hold_mining(MiningTarget(position, face), continuing=False)

    async def continue_mining(self, position: tuple[int, int, int], *, face: int = 1) -> int:
        return await self._hold_mining(MiningTarget(position, face), continuing=True)

    async def _hold_mining(self, target: MiningTarget, *, continuing: bool) -> int:
        epoch = self._mining_epoch
        generation = self._held_mining_generation(target.position) if continuing else None
        async with self._mining_lock:
            self._check_mining_epoch(epoch)
            if continuing and generation is None:
                generation = self._held_mining_generation(target.position)
            if generation is not None:
                actions = [("CONTINUE_DESTROY_BLOCK", target.position, target.face)]
            else:
                actions = self._release_mining_actions()
                actions.append(("START_DESTROY_BLOCK", target.position, target.face))
            return await self._submit_mining(
                actions, target, epoch, validate=lambda: self._observed_mining_input(target, allow_edge_hit=True),
                continuing_generation=generation,
            )

    async def stop_mining(self) -> int | None:
        epoch = self._mining_epoch
        async with self._mining_lock:
            self._check_mining_epoch(epoch)
            actions = self._release_mining_actions()
            if not actions:
                return None
            return await self._submit_mining(actions, None, epoch)

    async def finish_mining(self, position: tuple[int, int, int], *, face: int = 1) -> int:
        """Submit a creative break using the server-reported game type."""
        b = self.packets.protocol.binding
        target = MiningTarget(position, face)
        epoch = self._mining_epoch
        async with self._mining_lock:
            self._check_mining_epoch(epoch)

            def build_item_use():
                self._check_mining_epoch(epoch)
                if self._player_game_type != "CREATIVE":
                    raise RuntimeError("instant break requires server-reported creative mode")
                block, eye, click = self._observed_mining_input(target)
                slot = self.inventory.selected_slot
                transaction = b.ItemUseInventoryTransaction(
                    transaction=b.InventoryTransaction(actions=[]),
                    action_type=b.ItemUseInventoryTransaction.ActionType.DESTROY,
                    trigger_type=b.ItemUseInventoryTransaction.TriggerType.PLAYER_INPUT,
                    pos=b.BlockPos(x=position[0], y=position[1], z=position[2]),
                    face=face, slot=slot, hand=b.HandSlot.MAINHAND,
                    item=self.inventory.item_or_empty(slot),
                    from_pos=b.Vec3(x=eye[0], y=eye[1], z=eye[2]),
                    click_pos=b.Vec3(x=click[0], y=click[1], z=click[2]),
                    target_block_id=block.runtime_id,
                    client_predicted_result=b.ItemUseInventoryTransaction.PredictedResult.SUCCESS,
                    client_cooldown_state=b.ItemUseInventoryTransaction.ClientCooldownState.OFF,
                )
                return b.PackedItemUseLegacyInventoryTransaction(
                    id=b.ItemStackLegacyRequestId(raw_id=0), slots=None, transaction=transaction,
                )

            previous = self.mining_target
            actions = []
            if previous is None or previous.position != position:
                actions = self._release_mining_actions()
                actions.append(("START_DESTROY_BLOCK", position, face))
            actions.extend((("CONTINUE_DESTROY_BLOCK", position, face),
                            ("PREDICT_DESTROY_BLOCK", position, face)))
            return await self._submit_mining(actions, None, epoch, build_item_use=build_item_use)

    async def mine_block(
        self, position: tuple[int, int, int], *, face: int | None = 1, timeout: float = 15,
        before_start: Callable[[], None] | None = None,
        _prepare_start: Callable[[], None] | None = None,
    ) -> BlockUpdate:
        """Hold a server-authoritative break until a block update is observed.

        On hashed-runtime-ID worlds, require an air update. Palette-ID worlds
        return the first matching update; callers must interpret its runtime ID.
        One deadline covers acquiring ownership, starting, and observing the break.
        A timed-out or cancelled break that already started is aborted.
        """
        epoch = self._mining_epoch
        prepare_start = _prepare_start
        async with asyncio.timeout(timeout) as budget:
            if face is None:
                match = self._sample_mining_face(position, encoded=True)
                if match.status != "hit":
                    raise MiningFaceUnavailable(match.status)
                position = match.position
                face = match.face
                before = self.observed_block(position)

                def prepare_automatic_start():
                    self._check_mining_epoch(epoch)
                    deadline = budget.when()
                    if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError("mining timed out before submitting START")
                    current = self.observed_block(position)
                    if current is None or (self.hashed_runtime_ids and current.runtime_id == HASHED_UNKNOWN_RUNTIME_ID):
                        raise MiningFaceUnavailable("unknown")
                    if current != before:
                        raise MiningFaceUnavailable("target_changed")
                    if not self.movement._can_send_input:
                        raise MiningFaceUnavailable("unknown")
                    if _prepare_start is not None:
                        _prepare_start()
                    self._aim_mining_face(position, face, match.click_pos, encoded=True)

                prepare_start = prepare_automatic_start
            await self._mining_lock.acquire()
        try:
            self._check_mining_epoch(epoch)
            return await self._mine_observed(position, face, budget.when(), before_start, epoch, prepare_start)
        finally:
            self._mining_lock.release()

    async def _mine_observed(self, position, face, deadline, before_start, epoch, prepare_start) -> BlockUpdate:
        b = self.packets.protocol.binding
        target = MiningTarget(position, face)
        loop = asyncio.get_running_loop()
        started = False

        def start():
            nonlocal started
            if deadline is not None and loop.time() >= deadline:
                raise TimeoutError("mining timed out before submitting START")
            if before_start is not None:
                before_start()
            started = True

        def matches(raw):
            if raw.packet_id == b.RespawnPacket.ID:
                return self._own_ready_respawn(raw)
            if raw.packet_id != b.UpdateBlockPacket.ID:
                return True
            if not started:
                return False
            packet = self.packets.protocol.decode(raw)
            return (
                (int(packet.pos.x), int(packet.pos.y), int(packet.pos.z)) == position
                and int(packet.layer) == 0
                and (not self.hashed_runtime_ids
                     or int(packet.runtime_id) == HASHED_AIR_RUNTIME_ID)
            )

        waiter = asyncio.create_task(self.packets.wait_for_packet(
            (b.UpdateBlockPacket.ID, b.ChangeDimensionPacket.ID, b.DisconnectPacket.ID, b.RespawnPacket.ID),
            timeout=None, match=matches,
        ))
        try:
            async with asyncio.timeout_at(deadline):
                # The observer must be active before START enters the wire.
                await asyncio.sleep(0)
                actions = self._release_mining_actions()
                actions.append(("START_DESTROY_BLOCK", position, face))
                await self._submit_mining(actions, target, epoch, validate=start, build_item_use=prepare_start)
                raw = await waiter
            if raw.packet_id == b.DisconnectPacket.ID:
                raise ConnectionError("server disconnected while mining")
            self._check_mining_epoch(epoch)
            if raw.packet_id != b.UpdateBlockPacket.ID:
                raise RuntimeError("world changed while mining")
        except BaseException as error:
            waiter.cancel()
            if started and self._mining_target is target:
                try:
                    await self._submit_mining(self._release_mining_actions(), None, epoch)
                except Exception as cleanup_error:
                    # A lost transport may also reject ABORT. Keep the cause.
                    error.add_note(f"mining abort failed: {cleanup_error!r}")
            await asyncio.gather(waiter, return_exceptions=True)
            raise
        # Seeing air ends the observation, not the server's held break input.
        await self._submit_mining(self._release_mining_actions(target), None, epoch)
        packet = self.packets.protocol.decode(raw)
        return BlockUpdate(position, int(packet.runtime_id), int(packet.layer))
