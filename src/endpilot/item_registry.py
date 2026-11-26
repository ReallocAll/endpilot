"""Item names and network identifiers announced for a player session."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


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
