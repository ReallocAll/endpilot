"""Versioned vanilla biome definitions and server-advertised session values."""

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
import json
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class VanillaBiome:
    """A value from the bundled vanilla biome export."""

    id: int
    name: str
    temperature: float
    downfall: float
    rain: bool
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BiomeDefinition:
    """Climate and tags advertised for a named biome in this session."""

    name: str
    temperature: float
    downfall: float
    rain: bool
    tags: tuple[str, ...]


def _read_biome_definitions(packet) -> Mapping[str, BiomeDefinition] | None:
    strings = packet.string_list.strings
    definitions = {}
    for name_index, data in packet.biome_data.items():
        if not 0 <= name_index < len(strings):
            return None
        name = strings[name_index]
        if not name or name in definitions:
            return None
        tag_indices = () if data.tags is None else data.tags.tags
        if any(not 0 <= index < len(strings) for index in tag_indices):
            return None
        definitions[name] = BiomeDefinition(
            name, data.temperature, data.downfall, data.rain, tuple(strings[index] for index in tag_indices),
        )
    return MappingProxyType(definitions)


@cache
def _biomes_2225() -> Mapping[int, VanillaBiome]:
    data = json.loads(files("endpilot").joinpath("data", "biomes-2225.json").read_bytes())
    return MappingProxyType({
        biome_id: VanillaBiome(biome_id, name, float(temperature), float(downfall), rain, tuple(tags))
        for biome_id, name, temperature, downfall, rain, tags in data["biomes"]
    })


def vanilla_biomes(protocol: int = 2225) -> Mapping[int, VanillaBiome]:
    """Return the read-only vanilla catalog by numeric biome ID for this protocol."""
    if type(protocol) is not int or protocol != 2225:
        raise ValueError(f"unsupported vanilla biome protocol {protocol!r}")
    return _biomes_2225()
