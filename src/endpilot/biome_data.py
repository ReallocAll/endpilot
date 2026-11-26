"""Versioned vanilla biome definitions, independent of any live session."""

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
