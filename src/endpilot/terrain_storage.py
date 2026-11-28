"""Optional storage of received block layers, independent of the live world cache."""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from inspect import isawaitable
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import uuid4

if TYPE_CHECKING:
    import sqlite3

    from .subchunks import SubChunk

_Column = tuple[int, int]
_Section = tuple[int, int, int]
_Cell = tuple[int, int]  # layer and packed XZY within the section
_EMPTY = MappingProxyType({})
_FORMAT = b"EPT\x01"
_SECTION_KEY_BYTES = 32
_CELL_KEY_BYTES = 8


@dataclass(frozen=True, slots=True)
class TerrainBlockObservation:
    """One described block layer, with the provenance of its runtime ID."""

    position: tuple[int, int, int]
    layer: int
    runtime_id: int | None
    is_air: bool | None
    name: str | None
    properties: Mapping[str, int | str]
    session_id: str
    source: Literal["current", "stored"]


def _state_bytes(runtime_id, is_air, name, properties) -> bytes:
    return json.dumps((runtime_id, is_air, name, properties), separators=(",", ":"), ensure_ascii=False).encode()


_AIR = _state_bytes(None, True, "minecraft:air", {})


def _blob(data: bytes) -> bytes:
    return struct.pack("<I", len(data)) + data


@dataclass(frozen=True, slots=True)
class _Layer:
    bits: int
    words: bytes
    palette: tuple[bytes, ...]

    def __getitem__(self, index: int) -> bytes:
        if not self.bits:
            return self.palette[0]
        per_word = 32 // self.bits
        (word,) = struct.unpack_from("<I", self.words, index // per_word * 4)
        return self.palette[(word >> (index % per_word * self.bits)) & ((1 << self.bits) - 1)]

    @property
    def packed_bytes(self) -> int:
        return 9 + len(self.words) + sum(4 + len(value) for value in self.palette)

    def to_bytes(self) -> bytes:
        return b"".join((bytes((self.bits,)), _blob(self.words), struct.pack("<I", len(self.palette)),
                         *(_blob(value) for value in self.palette)))


@dataclass(frozen=True, slots=True)
class _Base:
    session: bytes
    all_air: bool  # BDS explicitly sent SUCCESS_ALL_AIR without a runtime ID.
    layers: tuple[_Layer, ...]

    @property
    def packed_bytes(self) -> int:
        return 19 + sum(layer.packed_bytes for layer in self.layers)

    def to_bytes(self) -> bytes:
        return b"".join((self.session, struct.pack("<BH", self.all_air, len(self.layers)),
                         *(layer.to_bytes() for layer in self.layers)))

    def cell(self, key: _Cell) -> tuple[bytes, bytes] | None:
        layer, index = key
        if layer < len(self.layers):
            value = self.layers[layer][index]
        else:
            value = _AIR if layer == 1 or (layer == 0 and self.all_air) else None
        return (self.session, value) if value is not None else None


@dataclass(frozen=True, slots=True, init=False)
class TerrainSectionData:
    """Immutable, packed section data for a terrain storage provider.

    A complete base, when present, replaces all earlier data for the section.
    Sparse changes describe their own layer and cell. Use ``merged_with`` to
    combine ordered writes and ``to_bytes`` / ``from_bytes`` for persistence.
    """

    _base: _Base | None
    _changes: Mapping[_Cell, tuple[bytes, bytes]]

    def __init__(self):
        object.__setattr__(self, "_base", None)
        object.__setattr__(self, "_changes", _EMPTY)

    @classmethod
    def _create(cls, base: _Base | None, changes: dict[_Cell, tuple[bytes, bytes]]) -> TerrainSectionData:
        result = object.__new__(cls)
        object.__setattr__(result, "_base", base)
        object.__setattr__(result, "_changes", MappingProxyType(changes))
        return result

    @property
    def complete(self) -> bool:
        """Whether a complete section was received."""
        return self._base is not None

    def _cell(self, key: _Cell) -> tuple[bytes, bytes] | None:
        value = self._changes.get(key)
        if value is None and self._base is not None:
            value = self._base.cell(key)
        return value

    def merged_with(self, newer: TerrainSectionData) -> TerrainSectionData:
        """Return the effect of applying ``newer`` after this section."""
        if newer.complete:
            return newer
        return self._create(self._base, dict(self._changes) | newer._changes)

    def to_bytes(self) -> bytes:
        """Encode the section for a storage provider."""
        pieces = [_FORMAT, struct.pack("<BI", self.complete, len(self._changes))]
        if self._base is not None:
            pieces.append(self._base.to_bytes())
        for (layer, index), (session, value) in self._changes.items():
            pieces.extend((struct.pack("<II", layer, index), session, _blob(value)))
        return b"".join(pieces)

    @classmethod
    def from_bytes(cls, data: bytes) -> TerrainSectionData:
        """Decode a section saved with ``to_bytes``."""
        if not data.startswith(_FORMAT):
            raise ValueError("unrecognized terrain section format")
        offset = len(_FORMAT)

        def take(size):
            nonlocal offset
            end = offset + size
            if end > len(data):
                raise ValueError("truncated terrain section")
            result = data[offset:end]
            offset = end
            return result

        def integer(width):
            return int.from_bytes(take(width), "little")

        def blob():
            return take(integer(4))

        full, change_count = integer(1), integer(4)
        if full not in (0, 1):
            raise ValueError("invalid terrain section base flag")
        base = None
        if full:
            session, all_air = take(16), integer(1)
            if all_air not in (0, 1):
                raise ValueError("invalid all-air terrain flag")
            layers = []
            for _ in range(integer(2)):
                bits, words, count = integer(1), blob(), integer(4)
                if bits > 32 or not 0 < count <= 4096:
                    raise ValueError("invalid terrain layer palette")
                per_word = 32 // bits if bits else 1
                if len(words) != (((4096 + per_word - 1) // per_word * 4) if bits else 0):
                    raise ValueError("invalid terrain layer word count")
                layers.append(_Layer(bits, words, tuple(blob() for _ in range(count))))
            base = _Base(session, bool(all_air), tuple(layers))
        changes = {}
        for _ in range(change_count):
            layer, index = integer(4), integer(4)
            if index >= 4096:
                raise ValueError("invalid terrain cell index")
            changes[layer, index] = (take(16), blob())
        if offset != len(data):
            raise ValueError("trailing terrain section bytes")
        return cls._create(base, changes)


def _cell_key(x: int, y: int, z: int, layer: int) -> _Cell:
    return layer, ((x & 15) << 8) | ((z & 15) << 4) | (y & 15)


def _validate_column(column: _Column) -> None:
    if not isinstance(column, tuple) or len(column) != 2 or any(type(axis) is not int for axis in column):
        raise ValueError("column must be a pair of integer section coordinates")


@dataclass(frozen=True, slots=True)
class TerrainColumnSnapshot:
    """Independent view of the described block layers in one column."""

    column: _Column
    world_key: str
    protocol: int
    dimension_id: int
    current_sections: tuple[int, ...]
    stored_sections: tuple[int, ...]
    _current: Mapping[int, TerrainSectionData] = field(repr=False)
    _stored: Mapping[int, TerrainSectionData] = field(repr=False)

    @classmethod
    def _create(cls, column, world_key, protocol, dimension, current, stored):
        if not current and not stored:
            return None
        current_sections = tuple(sorted(y for y, section in current.items() if section.complete))
        stored_sections = tuple(sorted(y for y, section in stored.items()
                                       if section.complete and y not in current_sections))
        return cls(column, world_key, protocol, dimension, current_sections, stored_sections,
                   MappingProxyType(dict(current)), MappingProxyType(dict(stored)))

    def block(self, position: tuple[int, int, int], *, layer: int = 0) -> TerrainBlockObservation | None:
        """Read an absolute block position, or return None for an undescribed layer."""
        if (not isinstance(position, tuple) or len(position) != 3
                or any(type(axis) is not int for axis in position) or type(layer) is not int or layer < 0):
            raise ValueError("position and layer must be integer block coordinates and a nonnegative layer")
        x, y, z = position
        if (x >> 4, z >> 4) != self.column:
            return None
        current = self._current.get(y >> 4)
        key = _cell_key(x, y, z, layer)
        value = None if current is None else current._cell(key)
        source = "current"
        if value is None and (current is None or not current.complete):
            stored = self._stored.get(y >> 4)
            value = None if stored is None else stored._cell(key)
            source = "stored"
        if value is None:
            return None
        session, packed = value
        runtime_id, is_air, name, properties = json.loads(packed)
        return TerrainBlockObservation(position, layer, runtime_id, is_air, name,
                                       MappingProxyType(properties), session.hex(), source)


class TerrainStore(Protocol):
    """Handle for one world, protocol and dimension opened by TerrainStorage."""

    async def load_column(self, column: _Column) -> Mapping[int, TerrainSectionData]: ...

    async def commit(
        self, session_id: str, through: int, sections: Mapping[_Section, TerrainSectionData], *, finished: bool = False,
    ) -> None: ...

    async def aclose(self) -> None: ...


class TerrainStorageError(RuntimeError):
    """The first failure of a terrain recording generation."""

    def __init__(self, session_id: str, sequence: int, message: str):
        super().__init__(f"terrain session {session_id} at sequence {sequence}: {message}")
        self.session_id = session_id
        self.sequence = sequence


class TerrainStorage:
    """Optional terrain storage configuration, reusable across joins."""

    def __init__(
        self, world_key: str, open_store: Callable[[str, int, int], TerrainStore | Awaitable[TerrainStore]], *,
        max_pending_bytes: int = 16 << 20,
    ):
        if not isinstance(world_key, str) or not world_key:
            raise ValueError("world_key must be a nonempty string")
        if not callable(open_store):
            raise TypeError("open_store must be callable")
        if type(max_pending_bytes) is not int or max_pending_bytes <= 0:
            raise ValueError("max_pending_bytes must be a positive integer")
        self._world_key = world_key
        self._open_store = open_store
        self._max_pending_bytes = max_pending_bytes
        self._writer: _TerrainWriter | None = None
        self._error: TerrainStorageError | None = None

    @property
    def world_key(self) -> str:
        return self._world_key

    @property
    def max_pending_bytes(self) -> int:
        return self._max_pending_bytes

    @property
    def error(self) -> TerrainStorageError | None:
        """The current or most recent recording generation's first storage failure."""
        writer = self._writer
        return self._error if writer is None else writer.error

    @classmethod
    def sqlite(cls, path: str | Path, *, world_key: str) -> TerrainStorage:
        """Use a SQLite database at a filesystem path."""
        path = Path(path).absolute()
        return cls(world_key, lambda world, protocol, dimension: _SQLiteStore(path, world, protocol, dimension))

    def _bind(self, protocol: int) -> _TerrainWriter:
        if self._writer is not None:
            raise RuntimeError("terrain storage already has an active writer")
        self._error = None
        writer = self._writer = _TerrainWriter(self, protocol)
        return writer

    async def _open(self, protocol: int, dimension: int) -> TerrainStore:
        result = self._open_store(self.world_key, protocol, dimension)
        return await result if isawaitable(result) else result

    @asynccontextmanager
    async def _reader(self, protocol: int, dimension: int):
        store = await self._open(protocol, dimension)
        try:
            yield store
        finally:
            await store.aclose()

    async def _load(self, column: _Column, protocol: int, dimension: int) -> dict[int, TerrainSectionData]:
        async with self._reader(protocol, dimension) as store:
            return dict(await store.load_column(column))

    async def load_column(self, column: _Column, *, protocol: int, dimension: int) -> TerrainColumnSnapshot | None:
        """Read committed observations from an independent storage handle."""
        _validate_column(column)
        if type(protocol) is not int or type(dimension) is not int:
            raise ValueError("protocol and dimension must be integers")
        stored = await self._load(column, protocol, dimension)
        return TerrainColumnSnapshot._create(column, self.world_key, protocol, dimension, _EMPTY, stored)

    async def flush(self) -> None:
        """Wait for the writer's accepted prefix and report any recording failure."""
        writer = self._writer
        if writer is not None:
            await writer.flush()
        elif self._error is not None:
            raise self._error


class _TerrainEncoder:
    def __init__(self, session: bytes, resolve):
        self.session = session
        self._state = lru_cache(4096)(resolve)

    def base(self, chunk: SubChunk | None) -> _Base:
        if chunk is None:
            return _Base(self.session, True, ())
        return _Base(self.session, False, tuple(
            _Layer(layer.bits, layer.words, tuple(self._state(runtime_id) for runtime_id in layer.palette))
            for layer in chunk.storages
        ))

    def cell(self, runtime_id: int) -> tuple[bytes, bytes]:
        return self.session, self._state(runtime_id)


@dataclass(slots=True)
class _PendingSection:
    base: _Base | None = None
    changes: dict[_Cell, tuple[bytes, bytes]] = field(default_factory=dict)
    size: int = _SECTION_KEY_BYTES + len(_FORMAT) + 5


class _TerrainWriter:
    def __init__(self, storage: TerrainStorage, protocol: int):
        self.storage = storage
        self.protocol = protocol
        self.session = uuid4().bytes
        self.error: TerrainStorageError | None = None
        self._pending: dict[int, dict[_Section, _PendingSection]] = {}
        self._through: dict[int, int] = {}
        self._pending_bytes = self._in_flight_bytes = 0
        self._observed = self._accepted = self._durable = 0
        self._wake = asyncio.Event()
        self._progress = asyncio.Event()
        self._finishing: bool | None = None
        self._terminal = False
        self._task = asyncio.create_task(self._run(), name="endpilot terrain storage")

    @property
    def accepting(self) -> bool:
        return self._finishing is None and self.error is None

    def _fail(self, sequence: int, message: str, cause: BaseException | None = None) -> None:
        if self.error is None:
            error = TerrainStorageError(self.session.hex(), sequence, message)
            error.__cause__ = cause
            self.error = error
        elif cause is not None:
            self.error.add_note(f"{message}: {cause!r}")

    def _accept(self, new_size: int, old_size: int) -> bool:
        self._observed += 1
        delta = new_size - old_size
        if self._pending_bytes + self._in_flight_bytes + delta > self.storage.max_pending_bytes:
            self._fail(self._observed, "pending byte budget exceeded")
            self._progress.set()
            return False
        self._pending_bytes += delta
        self._accepted = self._observed
        self._wake.set()
        return True

    def full(self, dimension: int, key: _Section, encoder: _TerrainEncoder, chunk: SubChunk | None) -> None:
        try:
            base = encoder.base(chunk)
        except Exception as error:
            self._fail_encoding(error)
            return
        pending = self._pending.get(dimension, _EMPTY).get(key)
        section = _PendingSection(base)
        section.size += base.packed_bytes
        if self._accept(section.size, 0 if pending is None else pending.size):
            self._pending.setdefault(dimension, {})[key] = section
            self._through[dimension] = self._accepted

    def sparse(
        self, dimension: int, position: tuple[int, int, int], layer: int, encoder: _TerrainEncoder, runtime_id: int,
    ) -> None:
        try:
            value = encoder.cell(runtime_id)
        except Exception as error:
            self._fail_encoding(error)
            return
        x, y, z = position
        key, cell = (x >> 4, y >> 4, z >> 4), _cell_key(x, y, z, layer)
        pending = self._pending.get(dimension, _EMPTY).get(key)
        section = _PendingSection() if pending is None else pending
        before = section.changes.get(cell)
        delta = _CELL_KEY_BYTES + 20 + len(value[1]) if before is None else len(value[1]) - len(before[1])
        if self._accept(section.size + delta, 0 if pending is None else section.size):
            section.size += delta
            section.changes[cell] = value
            if pending is None:
                self._pending.setdefault(dimension, {})[key] = section
            self._through[dimension] = self._accepted

    def _fail_encoding(self, error: Exception) -> None:
        self._observed += 1
        self._fail(self._observed, "could not capture terrain observation", error)
        self._progress.set()

    async def _wait_through(self, through: int) -> None:
        while self._durable < through:
            if self._terminal or through > self._accepted:
                assert self.error is not None
                raise self.error
            self._progress.clear()
            await self._progress.wait()

    async def flush(self) -> None:
        await self._wait_through(self._accepted)
        if self.error is not None:
            raise self.error

    async def wait_for_query(self) -> None:
        await self._wait_through(self._observed)

    def stop(self, *, finished: bool) -> None:
        if self._finishing is None:
            self._finishing = finished
            self._wake.set()

    async def aclose(self) -> None:
        await _wait_operation(self._task)
        if self.error is not None:
            raise self.error

    async def _run(self) -> None:
        stores: dict[int, TerrainStore] = {}
        committed: dict[int, int] = {}
        session_id = self.session.hex()
        pending = {}
        sections = {}
        try:
            while self._finishing is None or self._pending:
                if not self._pending:
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                pending, self._pending = self._pending, {}
                through, self._through = self._through, {}
                last = self._accepted
                self._in_flight_bytes, self._pending_bytes = self._pending_bytes, 0
                for dimension, sections in pending.items():
                    if dimension not in stores:
                        stores[dimension] = await self.storage._open(self.protocol, dimension)
                    await stores[dimension].commit(session_id, through[dimension], MappingProxyType({
                        key: TerrainSectionData._create(section.base, section.changes)
                        for key, section in sections.items()
                    }))
                    committed[dimension] = through[dimension]
                sections = {}
                pending.clear()
                self._in_flight_bytes = 0
                self._durable = last
                self._progress.set()
            if self._finishing and self.error is None:
                for dimension, store in stores.items():
                    await store.commit(session_id, committed[dimension], _EMPTY, finished=True)
        except (Exception, asyncio.CancelledError) as error:
            self._fail(self._durable + 1, "terrain provider write failed", error)
        finally:
            self._terminal = True
            sections.clear()
            pending.clear()
            self._pending.clear()
            self._through.clear()
            self._pending_bytes = self._in_flight_bytes = 0
            self._progress.set()
            for store in stores.values():
                try:
                    await store.aclose()
                except (Exception, asyncio.CancelledError) as error:
                    self._fail(self._durable + 1, "terrain provider close failed", error)
            self.storage._error = self.error
            self.storage._writer = None


async def _wait_operation(task):
    # Keep ownership until the operation is safe to close, even if its caller is cancelled.
    cancellation = None
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError as error:
            cancellation = error
    if cancellation is not None:
        if not task.cancelled() and task.exception() is not None:
            cancellation.add_note(f"terrain operation failed: {task.exception()!r}")
        raise cancellation
    return task.result()


class _SQLiteStore:
    def __init__(self, path: Path, world: str, protocol: int, dimension: int):
        self._path = path
        self._scope = (world, protocol, dimension)
        self._connection: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._connection is None:
            import sqlite3

            connection = sqlite3.connect(self._path, check_same_thread=False)
            try:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS terrain_sections (
                        world TEXT, protocol INTEGER, dimension INTEGER, x INTEGER, y INTEGER, z INTEGER,
                        data BLOB NOT NULL, PRIMARY KEY (world, protocol, dimension, x, z, y)
                    );
                    CREATE TABLE IF NOT EXISTS terrain_sessions (
                        world TEXT, protocol INTEGER, dimension INTEGER, session TEXT,
                        through INTEGER NOT NULL, finished INTEGER NOT NULL,
                        PRIMARY KEY (world, protocol, dimension, session)
                    );
                """)
            except BaseException:
                connection.close()
                raise
            self._connection = connection
        return self._connection

    async def _call(self, operation, *args):
        return await _wait_operation(asyncio.create_task(asyncio.to_thread(operation, *args)))

    async def load_column(self, column):
        return await self._call(self._load_column, column)

    def _load_column(self, column):
        rows = self._connect().execute(
            "SELECT y, data FROM terrain_sections WHERE world=? AND protocol=? AND dimension=? AND x=? AND z=?",
            (*self._scope, *column),
        )
        return {y: TerrainSectionData.from_bytes(data) for y, data in rows}

    async def commit(self, session_id, through, sections, *, finished=False):
        await self._call(self._commit, session_id, through, sections, finished)

    def _commit(self, session_id, through, sections, finished):
        connection = self._connect()
        scope = (*self._scope, session_id)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT through, finished FROM terrain_sessions WHERE world=? AND protocol=? AND dimension=? AND session=?",
                scope,
            ).fetchone()
            if previous is not None and (through < previous[0] or previous[1]):
                return
            if previous is None or through > previous[0]:
                for key, section in sections.items():
                    coordinates = (*self._scope, *key)
                    if not section.complete:
                        row = connection.execute("""
                            SELECT data FROM terrain_sections
                            WHERE world=? AND protocol=? AND dimension=? AND x=? AND y=? AND z=?
                            """, coordinates).fetchone()
                        if row is not None:
                            section = TerrainSectionData.from_bytes(row[0]).merged_with(section)
                    connection.execute("""
                        INSERT INTO terrain_sections VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(world, protocol, dimension, x, z, y) DO UPDATE SET data=excluded.data
                        """, (*coordinates, section.to_bytes()))
            connection.execute("""
                INSERT INTO terrain_sessions VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(world, protocol, dimension, session) DO UPDATE
                    SET through=excluded.through, finished=excluded.finished
                """, (*scope, through, finished))

    async def aclose(self):
        if self._connection is not None:
            connection, self._connection = self._connection, None
            await self._call(connection.close)
