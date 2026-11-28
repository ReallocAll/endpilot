"""Server-advertised entity types and versioned vanilla base dimensions."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import NamedTuple

from bedrock_protocol import nbt

from ._entity_dimensions_2225 import ENTITY_DIMENSIONS as _DIMENSIONS_2225


class VanillaEntityDimensions(NamedTuple):
    """Measured default width and height of a vanilla entity type, in blocks."""

    width: float
    height: float


_VANILLA_DIMENSIONS_2225 = MappingProxyType({
    name: VanillaEntityDimensions(width, height) for name, (width, height) in _DIMENSIONS_2225.items()
})


def vanilla_entity_dimensions(protocol: int = 2225) -> Mapping[str, VanillaEntityDimensions]:
    """Return measured vanilla base dimensions by exact namespaced entity name."""
    if type(protocol) is not int or protocol != 2225:
        raise ValueError(f"unsupported vanilla entity dimensions protocol {protocol!r}")
    return _VANILLA_DIMENSIONS_2225


@dataclass(frozen=True, slots=True)
class EntityRegistryEntry:
    """One advertised entity type. Its type ID identifies the type across instances."""

    name: str
    type_id: int
    bid: str | None = None
    has_spawn_egg: bool | None = None
    summonable: bool | None = None


@dataclass(frozen=True, slots=True)
class EntityRegistrySnapshot:
    """An independent snapshot of the server's entity types in announcement order."""

    entries: tuple[EntityRegistryEntry, ...]

    def __post_init__(self):
        object.__setattr__(self, "entries", tuple(self.entries))

    def entry_for_name(self, name: str) -> EntityRegistryEntry | None:
        """Return the first entry with this exact name in announcement order."""
        return next((entry for entry in self.entries if entry.name == name), None)

    def entry_for_type_id(self, type_id: int) -> EntityRegistryEntry | None:
        """Return the first entry with this type ID in announcement order."""
        return next((entry for entry in self.entries if entry.type_id == type_id), None)


def _read_entity_registry(root: nbt.CompoundTag) -> EntityRegistrySnapshot | None:
    if "idlist" not in root:
        return None
    rows = root["idlist"]
    if not isinstance(rows, nbt.ListTag):
        return None
    if rows.type != nbt.Type.COMPOUND and (len(rows) or rows.type != nbt.Type.END):
        return None

    entries = []
    for index in range(len(rows)):
        row = rows[index]
        if not isinstance(row, nbt.CompoundTag):
            return None
        fields = dict(row.items())
        name, type_id = fields.get("id"), fields.get("rid")
        bid, spawn_egg, summonable = fields.get("bid"), fields.get("hasspawnegg"), fields.get("summonable")
        if not isinstance(name, nbt.StringTag) or not isinstance(type_id, nbt.IntTag):
            return None
        if bid is not None and not isinstance(bid, nbt.StringTag):
            return None
        if spawn_egg is not None and not isinstance(spawn_egg, nbt.ByteTag):
            return None
        if summonable is not None and not isinstance(summonable, nbt.ByteTag):
            return None
        entries.append(EntityRegistryEntry(
            name.value, type_id.value, None if bid is None else bid.value,
            None if spawn_egg is None else bool(spawn_egg.value),
            None if summonable is None else bool(summonable.value),
        ))
    return EntityRegistrySnapshot(tuple(entries))
