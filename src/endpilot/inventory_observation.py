"""Check server-reported main inventory changes across an observation window."""

from collections import Counter

from .inventory import Inventory, _MAIN_INVENTORY_SIZE


def _main_slots(inventory: Inventory) -> dict[int, tuple[int, int]]:
    return {
        slot: (int(item.id), int(item.stack_size))
        for slot, item in inventory.slots.items() if 0 <= slot < _MAIN_INVENTORY_SIZE
    }


def _counts(slots: dict[int, tuple[int, int]]) -> Counter[int]:
    totals = Counter()
    for item_id, count in slots.values():
        if item_id:
            totals[item_id] += count
    return totals


class _InventoryObservation:
    def __init__(self, inventory: Inventory, *, require_all_slots: bool = False):
        self._inventory = inventory
        self._before = self._after = _main_slots(inventory)
        self._missing_baseline = (
            not self._before or inventory._main_inventory_has_count_gaps()
            or (require_all_slots and len(self._before) != _MAIN_INVENTORY_SIZE)
        )
        self._uncertain_ids = set()

    def observe(self, raw) -> None:
        previous = self._after
        self._after = _main_slots(self._inventory)
        if self._after.keys() - self._before.keys():
            # A later slot snapshot cannot provide the missing earlier value.
            self._missing_baseline = True
        protocol = self._inventory.packets.protocol
        binding = protocol.binding
        if raw.packet_id != binding.InventoryTransactionPacket.ID:
            return

        packet = protocol.decode(raw)
        transaction = getattr(packet.variant_transaction, "transaction", None)
        reported = Counter()
        for action in getattr(transaction, "actions", ()):
            source = action.source
            if (source.type != binding.InventorySourceType.CONTAINER_INVENTORY
                or source.container_id != binding.ContainerID.INVENTORY
                or not 0 <= int(action.slot) < _MAIN_INVENTORY_SIZE):
                continue
            old, new = action.from_item_descriptor, action.to_item_descriptor
            if int(old.id):
                reported[int(old.id)] -= int(old.stack_size)
            if int(new.id):
                reported[int(new.id)] += int(new.stack_size)
        cached = _counts(self._after)
        cached.subtract(_counts(previous))
        self._uncertain_ids.update(item for item in reported.keys() | cached.keys() if reported[item] != cached[item])

    def delta(self, item_id: int) -> int | None:
        if (self._missing_baseline or self._inventory._main_inventory_has_count_gaps()
                or item_id in self._uncertain_ids or self._before.keys() != self._after.keys()):
            return None
        return (
            sum(count for item, count in self._after.values() if item == item_id)
            - sum(count for item, count in self._before.values() if item == item_id)
        )
