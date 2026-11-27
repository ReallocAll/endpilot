"""Versioned vanilla block properties independent of a live session."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from importlib.resources import files
import json
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class VanillaBlockToolProperties:
    """Unmodified speed relative to bare hands and ordinary drop eligibility."""

    speed_multiplier: float
    qualifies_for_drops: bool | None


@dataclass(frozen=True, slots=True)
class VanillaBlock:
    """A namespaced vanilla block and its raw palette hardness."""

    name: str
    hardness: float
    _tools: Mapping[str, VanillaBlockToolProperties] = field(repr=False, hash=False)

    def tool_properties(self, item_name: str) -> VanillaBlockToolProperties | None:
        """Look up a measured unenchanted tool by its namespaced item name.

        Return None for items outside the measured tool set and blocks without
        a mining rule. Unknown drop eligibility is represented in the result.
        """
        return self._tools.get(item_name)


@cache
def _blocks_2225() -> Mapping[str, VanillaBlock]:
    data = json.loads(files("endpilot").joinpath("data", "harvest-2225.json").read_bytes())
    metadata = data["block_catalog"]
    tools = metadata["tools"]
    unknown_qualification = set(metadata["unknown_qualification"])
    profiles = {}
    blocks = {}
    for (last, name), hardness in zip(data["block_names"], metadata["hardness"], strict=True):
        # Every state of this name shares a rule ID.
        _, rule_id = data["palette"][last]
        known = name not in unknown_qualification
        profile = rule_id, known
        if profile not in profiles:
            rule = data["rules"][rule_id]
            properties = {}
            if rule is not None:
                required, speeds = rule
                required = None if required is None else frozenset(required)
                properties = {
                    tool: VanillaBlockToolProperties(
                        float(speeds.get(tool, 1.0)),
                        (required is None or tool in required) if known else None,
                    )
                    for tool in tools
                }
            profiles[profile] = MappingProxyType(properties)
        blocks[name] = VanillaBlock(name, float(hardness), profiles[profile])
    return MappingProxyType(blocks)


def vanilla_blocks(protocol: int = 2225) -> Mapping[str, VanillaBlock]:
    """Return the read-only vanilla catalog by namespaced block name."""
    if type(protocol) is not int or protocol != 2225:
        raise ValueError(f"unsupported vanilla block protocol {protocol!r}")
    return _blocks_2225()
