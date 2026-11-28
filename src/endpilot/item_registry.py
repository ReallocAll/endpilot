"""Session item names and versioned vanilla stack capacities."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ._stack_limits_2225 import STACK_LIMITS as _STACK_LIMITS_2225


def vanilla_item_stack_limits(protocol: int = 2225) -> Mapping[str, int]:
    """Return a read-only snapshot of vanilla capacities by exact namespaced item name."""
    if type(protocol) is not int or protocol != 2225:
        raise ValueError(f"unsupported vanilla item stack limit protocol {protocol!r}")
    return MappingProxyType(_STACK_LIMITS_2225.copy())


@dataclass(frozen=True, slots=True)
class ItemRegistrySnapshot:
    """An independent snapshot of a server's item registry."""

    by_id: Mapping[int, str]

    def __post_init__(self):
        object.__setattr__(self, "by_id", MappingProxyType(dict(self.by_id)))

    def name_for_id(self, item_id: int) -> str | None:
        """Return the exact name advertised for an item ID."""
        return self.by_id.get(item_id)

    def id_for_name(self, name: str) -> int | None:
        """Return the first ID with this exact name in registry order."""
        return next((item_id for item_id, registered in self.by_id.items() if registered == name), None)
