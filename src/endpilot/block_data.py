"""Static vanilla block states shared by mining, navigation and block queries."""

import hashlib
import json
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

from bedrock_protocol import nbt


# StartGame on vanilla BDS 1.26.66.1 (53696234), whose state order matches the packaged palette.
_VANILLA_REGISTRY_CHECKSUM = 0x449B4D8291A8C9A2
_VANILLA_BLOCK_PROPERTIES_SHA256 = "2e888799d080b9a4048b99c90601ea5e0e03b19959639dda940777f5f6afa31a"
_LADDER_FACINGS = {0x547A1F1A: 2, 0x50D01713: 3, 0x4D260F0C: 4, 0x497C0705: 5}
# Base-state hashes and SDK faces observed for both vanilla pistons in BDS 1.26.66.1.
_PISTON_FACINGS = {
    0x713BF7A9: 0, 0x74E5FFB0: 1, 0x69E7E79B: 3, 0x6D91EFA2: 2, 0x6293D78D: 5, 0x663DDF94: 4,
    0x8CDA3C06: 0, 0x893033FF: 1, 0x942E4C14: 3, 0x9084440D: 2, 0x9B825C22: 5, 0x97D8541B: 4,
}
_STATE_TAGS = {"byte": nbt.ByteTag, "int": nbt.IntTag, "string": nbt.StringTag}


def matches_vanilla_palette(binding, registry_checksum: int, block_properties) -> bool:
    """Verify the sequential registry and exact built-in block declarations for 2225."""
    if registry_checksum != _VANILLA_REGISTRY_CHECKSUM:
        return False
    declaration = hashlib.sha256()
    for entry in sorted(block_properties, key=lambda item: item.name):
        declaration.update(binding.encode(entry))
    return declaration.hexdigest() == _VANILLA_BLOCK_PROPERTIES_SHA256


@dataclass(frozen=True, slots=True)
class MiningRule:
    required: frozenset[str] | None
    speeds: dict[str, float]


@dataclass(frozen=True, slots=True)
class BedBlockState:
    name: str
    facing: str
    is_head: bool
    occupied: bool


@dataclass(frozen=True, slots=True)
class VanillaBlockState:
    mining_rule: MiningRule | None
    is_air: bool
    unit_cube: bool
    unsafe_support: bool
    name: str
    bed: BedBlockState | None = None
    outline: tuple[float, ...] | None = None
    unresolved_overhang: bool = False
    ladder_facing: int | None = None
    piston_face: int | None = None


@cache
def _catalog() -> tuple[tuple[VanillaBlockState | None, ...], dict[int, int]]:
    resource = files("endpilot").joinpath("data", "harvest-2225.json")
    data = json.loads(resource.read_text(encoding="utf-8"))
    resource = files("endpilot").joinpath("data", "outline-2225.json")
    outlines = json.loads(resource.read_text(encoding="utf-8"))
    boxes = tuple(None if box is None else tuple(box) for box in outlines["boxes"])
    unresolved_overhang_id = outlines["unresolved_overhang_id"]
    rules = [
        None if rule is None else MiningRule(
            None if rule[0] is None else frozenset(rule[0]), rule[1]
        )
        for rule in data["rules"]
    ]
    terrain = data["terrain"]
    cubes = set(terrain["unit_cubes"])
    unsafe = set(terrain["unsafe_support"])
    beds = {row[0]: BedBlockState(*row[1:]) for row in data["bed_states"]}
    names = []
    for last, name in data["block_names"]:
        names.extend([name] * (last + 1 - len(names)))
    palette = tuple(
        None if index == terrain["unknown"] else VanillaBlockState(
            rules[rule_id], index == terrain["air"], index in cubes, index in unsafe,
            names[index], beds.get(index), boxes[shape_id], shape_id == unresolved_overhang_id,
            _LADDER_FACINGS.get(hash_id), _PISTON_FACINGS.get(hash_id),
        )
        for index, ((hash_id, rule_id), shape_id) in
        enumerate(zip(data["palette"], outlines["shape_ids"], strict=True))
    )
    by_hash = {hash_id: index for index, (hash_id, _) in enumerate(data["palette"])}
    return palette, by_hash


def vanilla_block_state(runtime_id: int, *, hashed: bool) -> VanillaBlockState | None:
    """Look up an ID in the protocol 2225 vanilla block palette."""
    palette, by_hash = _catalog()
    index = by_hash.get(runtime_id, -1) if hashed else runtime_id
    return palette[index] if 0 <= index < len(palette) else None


@cache
def _state_lookup():
    resource = files("endpilot").joinpath("data", "harvest-2225.json")
    data = json.loads(resource.read_text(encoding="utf-8"))
    lookup = data["state_lookup"]
    by_name = {}
    first = 0
    for (last, name), layout_id in zip(data["block_names"], lookup["layout_ids"], strict=True):
        if name != "minecraft:unknown":
            by_name[name] = first, lookup["layouts"][layout_id]
        first = last + 1
    return lookup["version"], by_name, tuple(hash_id for hash_id, _ in data["palette"])


def _vanilla_state_properties(name: str, runtime_id: int, *, hashed: bool) -> dict[str, int | str]:
    """Decode the property values of an already resolved vanilla state."""
    _, by_name, _ = _state_lookup()
    first, layout = by_name[name]
    index = _catalog()[1][runtime_id] if hashed else runtime_id
    offset = index - first
    properties = {}
    for key, _, values in reversed(layout):
        offset, ordinal = divmod(offset, len(values))
        properties[key] = values[ordinal]
    return properties


def lookup_vanilla_block_state(state: nbt.CompoundTag) -> tuple[int, int] | None:
    """Resolve an exact 2225 block-state tag to (palette index, network hash)."""
    if type(state) is not nbt.CompoundTag or set(state.keys()) != {"name", "states", "version"}:
        return None
    name, properties, version = state["name"], state["states"], state["version"]
    if type(name) is not nbt.StringTag or type(properties) is not nbt.CompoundTag or type(version) is not nbt.IntTag:
        return None
    expected_version, by_name, hashes = _state_lookup()
    block = by_name.get(name.value)
    if version.value != expected_version or block is None:
        return None
    first, layout = block
    if len(properties) != len(layout):
        return None
    offset = 0
    for key, kind, values in layout:
        if key not in properties:
            return None
        prop = properties[key]
        if type(prop) is not _STATE_TAGS[kind]:
            return None
        try:
            ordinal = values.index(prop.value)
        except ValueError:
            return None
        offset = offset * len(values) + ordinal
    index = first + offset
    return index, hashes[index]
