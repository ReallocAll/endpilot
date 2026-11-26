"""Container interaction and authoritative window-open observation."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import cast
from weakref import ref

from bedrock_protocol import nbt

from .block_data import _catalog
from .blocks import FACE_OFFSETS, Blocks
from .compression import PacketConnection
from .entities import Entities, EntityState
from .inventory import (
    HeldItemStack, _BlockTransferEndpoint, _InventoryMoveNotSubmitted, _PLAYER_CONTAINERS, _TransferEndpoint,
    _correct_observed_item, _response_item, _stack_snapshot,
)


@dataclass(frozen=True, slots=True)
class ContainerWindow:
    container_id: int
    container_type: str
    position: tuple[int, int, int] | None  # Absent in UpdateTradePacket.
    entity_unique_id: int


@dataclass(frozen=True, slots=True)
class ContainerSlotChange:
    """One slot change applied to the currently observed window."""

    window: ContainerWindow
    slot: int
    before: HeldItemStack | None
    after: HeldItemStack | None


@dataclass(frozen=True, slots=True, eq=False)
class _ContainerSlotObserver:
    slot: int | None
    callback: Callable[[ContainerSlotChange], None]


@dataclass(frozen=True, slots=True)
class FurnaceProgress:
    """Tick counts reported for the active furnace-family window."""

    window: ContainerWindow
    cook_ticks: int | None = None
    burn_remaining_ticks: int | None = None
    burn_total_ticks: int | None = None


@dataclass(frozen=True, slots=True)
class BrewingProgress:
    """Reported brew ticks and fuel units for the open brewing stand."""

    window: ContainerWindow
    brew_ticks: int | None = None
    fuel_amount: int | None = None
    fuel_total: int | None = None


@dataclass(frozen=True, slots=True)
class _InventoryOpen:
    caller_owned: bool
    completion: asyncio.Future[ContainerWindow | None]


@dataclass(frozen=True, slots=True)
class _EntityContainer:
    entities: Entities
    runtime_id: int
    entity_id: int
    instance: object

    def is_visible(self) -> bool:
        return self.entities._actor_instance(self.runtime_id) is self.instance

    def require_visible(self) -> None:
        if not self.is_visible():
            raise RuntimeError("the chest minecart is no longer observed")


def _observed_entity_container(entities: Entities, actor: EntityState | None) -> _EntityContainer | None:
    if actor is None or actor.entity_id in (0, -1):
        return None
    instance = entities._actor_instance(actor.runtime_id)
    if instance is None:
        return None
    return _EntityContainer(entities, actor.runtime_id, actor.entity_id, instance)


_FURNACE_SLOTS = {
    "FURNACE": ("FURNACE_INGREDIENT_CONTAINER", "FURNACE_FUEL_CONTAINER", "FURNACE_RESULT_CONTAINER"),
    "SMOKER": ("SMOKER_INGREDIENT_CONTAINER", "FURNACE_FUEL_CONTAINER", "FURNACE_RESULT_CONTAINER"),
    "BLAST_FURNACE": ("BLAST_FURNACE_INGREDIENT_CONTAINER", "FURNACE_FUEL_CONTAINER", "FURNACE_RESULT_CONTAINER"),
}
_FURNACE_FIELDS = {0: "cook_ticks", 1: "burn_remaining_ticks", 2: "burn_total_ticks"}
_BREWING_SLOTS = (
    "BREWING_STAND_INPUT_CONTAINER",
    "BREWING_STAND_RESULT_CONTAINER", "BREWING_STAND_RESULT_CONTAINER", "BREWING_STAND_RESULT_CONTAINER",
    "BREWING_STAND_FUEL_CONTAINER",
)
_BREWING_FIELDS = {0: "brew_ticks", 1: "fuel_amount", 2: "fuel_total"}
_WINDOW_SLOT_NAMES = {**_FURNACE_SLOTS, "BREWING_STAND": _BREWING_SLOTS}
_ANVIL_SLOTS = {1: "ANVIL_INPUT_CONTAINER", 2: "ANVIL_MATERIAL_CONTAINER"}
_ANVIL_WINDOW_SLOTS = {0: 1, 1: 2}
_ANVIL_UI_SLOTS = {1: 1, 2: 2}
_CHEST_FACINGS = {3162850062: "north", 741882976: "south", 1429214429: "west", 2001328343: "east"}


def _same_stack(before, current):
    if before is current:
        return True
    if before is None or current is None:
        return False
    return (before.id, before.aux_value, before.net_id_variant,
            before.block_runtime_id, before.user_data_buffer) == (
        current.id, current.aux_value, current.net_id_variant,
        current.block_runtime_id, current.user_data_buffer,
    )


def _same_slot(before, current):
    return before is current or (_same_stack(before, current) and before.stack_size == current.stack_size)


class Containers:
    def __init__(self, packets, movement, inventory, *, entities: Entities | None = None,
                 blocks: Blocks | None = None):
        self.packets = packets
        self.movement = movement
        self.inventory = inventory
        self._entities = entities
        self._blocks = blocks
        self.window: ContainerWindow | None = None
        self._dimension = None if blocks is None else blocks.dimension_id
        self._layout_token = object()
        self._window_block_name: str | None = None
        self._window_slot_count: int | None = None
        self._slots: dict[int, object] | None = None
        self._slot_observers: list[_ContainerSlotObserver] = []
        self._unconfirmed_transfer_slots: set[int] = set()
        self._unconfirmed_block_slots: set[_BlockTransferEndpoint] = set()
        self._anvil_origins: dict[int, object] = {}
        self._slots_updated = asyncio.Event()
        self._progress: FurnaceProgress | BrewingProgress | None = None
        self._progress_waiters: set[asyncio.Future[FurnaceProgress | BrewingProgress]] = set()
        self._furnace_progress_observers: dict[object, Callable[[FurnaceProgress], None]] = {}
        self._inventory_open: _InventoryOpen | None = None
        self._block_open_requests = 0
        self._owned_block_opening = False
        self._entity_opening = False
        self._entity_container: _EntityContainer | None = None
        self._unsubscribe = packets.subscribe_raw(self._on_packet)
        subscribe_reader_end = getattr(packets, "subscribe_reader_end", None)
        self._unsubscribe_reader_end = (
            None if subscribe_reader_end is None else subscribe_reader_end(self._on_reader_end)
        )

    def _decode_open(self, raw) -> ContainerWindow:
        packet = self.packets.protocol.decode(raw)
        return ContainerWindow(
            int(packet.container_id),
            packet.type.name if hasattr(packet.type, "name") else str(int(packet.type)),
            (int(packet.pos.x), int(packet.pos.y), int(packet.pos.z)),
            int(packet.entity_unique_id),
        )

    def _set_window(self, window: ContainerWindow | None) -> None:
        self.window = window
        self._slot_observers.clear()
        self._furnace_progress_observers.clear()
        self._layout_token = object()
        self._window_block_name = None
        self._window_slot_count = None
        self._entity_container = None
        self._slots = None
        self._unconfirmed_transfer_slots.clear()
        self._anvil_origins.clear()
        self._slots_updated.set()
        self._fail_progress_waiters(RuntimeError, "{kind} window closed or changed before a progress update")
        if window is not None and window.container_type in _FURNACE_SLOTS:
            self._progress = FurnaceProgress(window)
        elif window is not None and window.container_type == "BREWING_STAND":
            self._progress = BrewingProgress(window)
        else:
            self._progress = None

    def _observed_window_block_name(self, window: ContainerWindow) -> str | None:
        if (self.window is not window or window.container_type != "CONTAINER"
                or window.entity_unique_id != -1 or window.position is None or self._blocks is None):
            return None
        if self._window_block_name is None:
            # The first known primary layer describes this server-opened window.
            block = self._blocks.observed_block(window.position)
            state = None if block is None else self._blocks._vanilla_state(block.runtime_id)
            if state is not None:
                self._window_block_name = state.name
        return self._window_block_name

    def _container_slot_name(self, window: ContainerWindow, slot: int) -> str:
        if window.container_type == "ANVIL":
            if slot not in _ANVIL_SLOTS:
                raise ValueError("anvil slots are 1=input and 2=material")
            return _ANVIL_SLOTS[slot]
        slots = _WINDOW_SLOT_NAMES.get(window.container_type)
        if slots is not None:
            if not 0 <= slot < len(slots):
                if window.container_type == "BREWING_STAND":
                    raise ValueError("brewing stand slots are 0=input, 1–3=bottles, 4=fuel")
                raise ValueError("furnace slots are 0=ingredient, 1=fuel, 2=result")
            return slots[slot]
        if self._observed_window_block_name(window) == "minecraft:barrel":
            return "BARREL_CONTAINER"
        return "LEVEL_ENTITY_CONTAINER"

    def _observed_window_slot_count(self, window: ContainerWindow) -> int | None:
        return self._window_slot_count if self.window is window else None

    def _fail_progress_waiters(
        self, error_type: type[Exception], message: str, cause: BaseException | None = None,
    ) -> None:
        if not self._progress_waiters:
            return
        kind = "brewing stand" if isinstance(self._progress, BrewingProgress) else "furnace"
        for waiter in self._progress_waiters:
            if not waiter.done():
                error = error_type(message.format(kind=kind))
                error.__cause__ = cause
                waiter.set_exception(error)
                waiter.exception()
        self._progress_waiters.clear()

    def _on_reader_end(self, cause: BaseException | None) -> None:
        self._fail_progress_waiters(ConnectionError, "connection ended before a {kind} progress update", cause)
        self.close()

    def _anvil_wire_slots(self, inventory_id):
        window = self.window
        if window is not None and window.container_type == "ANVIL":
            if inventory_id == window.container_id:
                return _ANVIL_WINDOW_SLOTS
            if inventory_id == self.packets.protocol.binding.ContainerID.PLAYER_ONLY_UI:
                return _ANVIL_UI_SLOTS
        return None

    def subscribe_slot_changes(
        self, callback: Callable[[ContainerSlotChange], None], *, slot: int | None = None,
    ) -> Callable[[], None]:
        """Subscribe to observed slot changes until this window closes or changes."""
        if self.window is None:
            raise RuntimeError("no container is open")
        if slot is not None and (type(slot) is not int or slot < 0):
            raise ValueError("container slot must be a nonnegative integer")
        return PacketConnection._subscribe(self._slot_observers, _ContainerSlotObserver(slot, callback))

    def _record_slot_before(self, index: int, before) -> None:
        if before is not None and index not in before and any(
            observer.slot is None or observer.slot == index for observer in self._slot_observers
        ):
            before[index] = self._slots.get(index)

    def _notify_slot_changes(self, window, before, *, complete=False) -> None:
        if before is None or self.window is not window or not self._slot_observers or (not complete and not before):
            return
        observers = tuple(self._slot_observers)
        if complete:
            selected = {observer.slot for observer in observers}
            slots = sorted(before.keys() | self._slots.keys()) if None in selected else sorted(selected)
        else:
            slots = sorted(before)
        changes = []
        for index in slots:
            previous, current = before.get(index), self._slots.get(index)
            if _same_slot(previous, current):
                continue
            changes.append(ContainerSlotChange(window, index, _stack_snapshot(previous), _stack_snapshot(current)))
        for change in changes:
            for observer in observers:
                if (observer.slot is not None and observer.slot != change.slot) or observer not in self._slot_observers:
                    continue
                try:
                    observer.callback(change)
                except (Exception, asyncio.CancelledError) as error:
                    asyncio.get_running_loop().call_exception_handler({
                        "message": "container slot observer failed", "exception": error, "callback": observer.callback,
                    })

    def _set_anvil_slot(self, index, item, before, *, corrected=False):
        previous = self._slots.get(index)
        self._record_slot_before(index, before)
        # Quantities and server-assigned net IDs can change through corrections
        # from the same stack. A new observation must still match its identity.
        if not (previous is not None and previous.id and item is not None and item.id
                and (corrected or _same_stack(previous, item))):
            self._anvil_origins[index] = object()
        if item is None:
            self._slots.pop(index, None)
        else:
            self._slots[index] = item
        self._slots_updated.set()

    def _observe_window_slot(self, inventory_id, index, item, before):
        if self.window is None or self._slots is None:
            return
        anvil = self._anvil_wire_slots(int(inventory_id))
        if anvil is not None:
            logical = anvil.get(int(index))
            if logical is not None:
                self._set_anvil_slot(logical, item, before)
        elif int(inventory_id) == self.window.container_id:
            self._record_slot_before(int(index), before)
            self._slots[int(index)] = item
            self._unconfirmed_transfer_slots.discard(int(index))
            self._observe_transfer_slot(int(index), item)
            self._slots_updated.set()

    def _chest_facing(self, position):
        if self._blocks is None:
            return None
        observed = self._blocks.observed_block(position)
        if observed is None or observed.runtime_id is None:
            return None
        state = self._blocks._vanilla_state(observed.runtime_id)
        if state is None or state.name != "minecraft:chest":
            return None
        if self._blocks.hashed_runtime_ids:
            return _CHEST_FACINGS.get(observed.runtime_id)
        _, by_hash = _catalog()
        return next((facing for network_hash, facing in _CHEST_FACINGS.items()
                     if by_hash.get(network_hash) == observed.runtime_id), None)

    def _chest_pair_observation(self, position):
        root = self._blocks._block_entity(position)
        if (root is None or "id" not in root or not isinstance(root["id"], nbt.StringTag)
                or root["id"].value != "Chest" or not all(
                    key in root and isinstance(root[key], nbt.IntTag) for key in ("x", "y", "z", "pairx", "pairz")
                ) or "pairlead" not in root or not isinstance(root["pairlead"], nbt.ByteTag)):
            return None
        if tuple(root[axis].value for axis in "xyz") != position or root["pairlead"].value not in (0, 1):
            return None
        return (root["pairx"].value, position[1], root["pairz"].value), int(root["pairlead"].value)

    def _verified_chest_halves(self, position, facing):
        # The north-facing X-axis sample proved that the lead half occupies slots 0–26.
        if facing != "north" or (pair := self._chest_pair_observation(position)) is None:
            return None
        other, lead = pair
        if (abs(position[0] - other[0]) != 1 or position[2] != other[2] or self._chest_facing(other) != facing
                or self._chest_pair_observation(other) != (position, 1 - lead)):
            return None
        return (position, other) if lead else (other, position)

    def _block_transfer_endpoint(self, index: int) -> _BlockTransferEndpoint | None:
        window = self.window
        # An entity window's position does not identify a physical block slot.
        if window is None or window.position is None or window.entity_unique_id not in (-1, 0):
            return None
        position = window.position
        local_slot = index
        half = position
        possible = {position}
        if (self._window_slot_count == 54 and 0 <= index < 54 and window.container_type == "CONTAINER"
                and self._observed_window_block_name(window) in (None, "minecraft:chest")):
            half_index, local_slot = divmod(index, 27)
            facing = self._chest_facing(position)
            halves = self._verified_chest_halves(position, facing)
            if halves is not None:
                half = halves[half_index]
                possible = {half}
            else:
                half = None
                x, y, z = position
                if facing not in ("west", "east"):
                    possible.update(((x - 1, y, z), (x + 1, y, z)))
                if facing not in ("north", "south"):
                    possible.update(((x, y, z - 1), (x, y, z + 1)))
        return _BlockTransferEndpoint(self._dimension, self._layout_token, index, local_slot, half, frozenset(possible))

    def _observe_transfer_slot(self, index: int, item: object) -> None:
        entity = self._entity_container
        if entity is not None:
            if self.inventory is not None and entity.is_visible():
                self.inventory._observe_transfer_slot((entity.instance, index), item)
            return
        endpoint = self._block_transfer_endpoint(index)
        if endpoint is not None:
            if self._unconfirmed_block_slots:
                unresolved = set()
                for old in self._unconfirmed_block_slots:
                    if old.matches(endpoint):
                        continue
                    if (old.half is None and endpoint.half is not None and old.dimension == endpoint.dimension
                            and old.local_slot == endpoint.local_slot and endpoint.half in old.possible_halves):
                        # An exact observation covers only this one possible half.
                        possible = old.possible_halves - {endpoint.half}
                        if not possible:
                            continue
                        old = replace(old, possible_halves=possible)
                    unresolved.add(old)
                self._unconfirmed_block_slots = unresolved
            if self.inventory is not None:
                self.inventory._observe_transfer_slot(endpoint, item)

    def _on_packet(self, raw) -> None:
        b = self.packets.protocol.binding
        if raw.packet_id == b.DisconnectPacket.ID:
            self._on_reader_end(None)
        elif raw.packet_id == b.ChangeDimensionPacket.ID:
            self._dimension = int(self.packets.protocol.decode(raw).dimension_id)
            self._set_window(None)
        elif raw.packet_id == b.ContainerOpenPacket.ID:
            window = self._decode_open(raw)
            self._set_window(window)
            self._observed_window_block_name(window)
            if self._entities is not None and window.container_type == "CONTAINER":
                actor = self._entities._get_by_unique_id(window.entity_unique_id)
                self._entity_container = _observed_entity_container(self._entities, actor)
        elif raw.packet_id == b.UpdateTradePacket.ID:
            packet = self.packets.protocol.decode(raw)
            window = ContainerWindow(
                int(packet.container_id), "TRADE", None,
                int(packet.entity_unique_id),
            )
            # Do not replace a matching ContainerOpen window's actual position
            # or initial slots when its server quote arrives or refreshes.
            current = self.window
            if (current is None or current.container_id != window.container_id
                    or current.container_type != "TRADE"
                    or current.entity_unique_id != window.entity_unique_id):
                self._set_window(window)
        elif raw.packet_id == b.InventoryContentPacket.ID:
            packet = self.packets.protocol.decode(raw)
            window = self.window
            anvil = self._anvil_wire_slots(int(packet.inventory_id))
            if anvil is not None:
                if self._slots is None:
                    self._slots = {}
                before = {} if self._slot_observers else None
                for wire_index, logical in anvil.items():
                    item = packet.slots[wire_index] if wire_index < len(packet.slots) else None
                    self._set_anvil_slot(logical, item, before)
                self._notify_slot_changes(window, before)
            elif window is not None and int(packet.inventory_id) == window.container_id:
                before = (self._slots or {}) if self._slot_observers else None
                count = len(packet.slots)
                if self._window_slot_count != count:
                    self._layout_token = object()
                    self._window_slot_count = count
                self._slots = dict(enumerate(packet.slots))
                self._unconfirmed_transfer_slots.difference_update(self._slots)
                for index, item in self._slots.items():
                    self._observe_transfer_slot(index, item)
                self._slots_updated.set()
                self._notify_slot_changes(window, before, complete=True)
        elif self._progress is not None and raw.packet_id == b.ContainerSetDataPacket.ID:
            packet = self.packets.protocol.decode(raw)
            progress = self._progress
            fields = _BREWING_FIELDS if isinstance(progress, BrewingProgress) else _FURNACE_FIELDS
            field = fields.get(int(packet.id))
            if field is not None and int(packet.container_id) == progress.window.container_id:
                updated = replace(progress, **{field: int(packet.value)})
                self._progress = updated
                # Resolve here, before another packet can advance the current snapshot.
                for waiter in self._progress_waiters:
                    if not waiter.done():
                        waiter.set_result(replace(updated))
                self._progress_waiters.clear()
                if isinstance(updated, FurnaceProgress) and self._furnace_progress_observers:
                    self._notify_furnace_progress(updated)
        elif raw.packet_id == b.InventorySlotPacket.ID:
            packet = self.packets.protocol.decode(raw)
            window = self.window
            before = {} if self._slot_observers else None
            self._observe_window_slot(packet.inventory_id, packet.slot, packet.item, before)
            self._notify_slot_changes(window, before)
        elif raw.packet_id == b.InventoryTransactionPacket.ID:
            packet = self.packets.protocol.decode(raw)
            window = self.window
            before = {} if self._slot_observers else None
            transaction = getattr(packet.variant_transaction, "transaction", None)
            for action in getattr(transaction, "actions", ()):
                source = action.source
                if source.type.name == "CONTAINER_INVENTORY" and source.container_id is not None:
                    self._observe_window_slot(source.container_id, action.slot, action.to_item_descriptor, before)
            self._notify_slot_changes(window, before)
        elif raw.packet_id == b.ContainerClosePacket.ID:
            packet = self.packets.protocol.decode(raw)
            if self.window is not None and int(packet.container_id) == self.window.container_id:
                self._set_window(None)
                if packet.server_initiated_close:
                    # A server-originated close is acknowledged with the same
                    # window ID, window type and flag.
                    asyncio.create_task(self.packets.send(b.ContainerClosePacket(
                        container_id=packet.container_id,
                        container_type=packet.container_type,
                        server_initiated_close=True,
                    )))

    @property
    def slots(self) -> dict[int, object] | None:
        """Last server-supplied slots for the active window, or None before its content."""
        return None if self._slots is None else dict(self._slots)

    @property
    def furnace_progress(self) -> FurnaceProgress | None:
        return self._progress if isinstance(self._progress, FurnaceProgress) else None

    @property
    def brewing_progress(self) -> BrewingProgress | None:
        return self._progress if isinstance(self._progress, BrewingProgress) else None

    def subscribe_furnace_progress(self, callback: Callable[[FurnaceProgress], None]) -> Callable[[], None]:
        """Observe each supported report until the current furnace window closes or changes."""
        if self.furnace_progress is None:
            raise RuntimeError("no furnace window is open")
        token = object()
        self._furnace_progress_observers[token] = callback
        owner_ref = ref(self)

        def unsubscribe() -> None:
            owner = owner_ref()
            if owner is not None:
                owner._furnace_progress_observers.pop(token, None)

        return unsubscribe

    def _notify_furnace_progress(self, progress: FurnaceProgress) -> None:
        observers = self._furnace_progress_observers
        for token, callback in tuple(observers.items()):
            if token not in observers:
                continue
            try:
                callback(progress)
            except (Exception, asyncio.CancelledError) as error:
                asyncio.get_running_loop().call_exception_handler({
                    "message": "furnace progress observer failed", "exception": error, "callback": callback,
                })

    async def wait_for_furnace_progress(self, *, timeout: float = 15) -> FurnaceProgress:
        if self.furnace_progress is None:
            raise RuntimeError("no furnace window is open")
        return cast(FurnaceProgress, await self._wait_for_progress(timeout))

    async def wait_for_brewing_progress(self, *, timeout: float = 15) -> BrewingProgress:
        if self.brewing_progress is None:
            raise RuntimeError("no brewing stand window is open")
        return cast(BrewingProgress, await self._wait_for_progress(timeout))

    async def _wait_for_progress(self, timeout: float) -> FurnaceProgress | BrewingProgress:
        waiter = asyncio.get_running_loop().create_future()
        self._progress_waiters.add(waiter)
        try:
            async with asyncio.timeout(timeout):
                return await waiter
        finally:
            self._progress_waiters.discard(waiter)

    async def wait_for_slots(
        self, *, timeout: float = 15, _expected_window: ContainerWindow | None = None,
    ) -> dict[int, object]:
        """Wait for an initial InventoryContent for this window, not a client prediction."""
        window = self.window if _expected_window is None else _expected_window
        if window is None:
            raise RuntimeError("no container window is open")
        async with asyncio.timeout(timeout):
            while self.window is window:
                if self._slots is not None:
                    return dict(self._slots)
                self._slots_updated.clear()
                await self._slots_updated.wait()
        if _expected_window is not None:
            if self._unsubscribe is None:
                raise ConnectionError("connection ended before the chest contents arrived")
            raise _InventoryMoveNotSubmitted("window_changed")
        raise RuntimeError("container window closed or changed before its contents arrived")

    async def click_block(
        self, position: tuple[int, int, int], blocks, *, face: int,
        click_pos: tuple[float, float, float] | None = None,
        _before_send: Callable[[], None] | None = None,
    ) -> None:
        """Submit a single use-on transaction after position synchronization."""
        b = self.packets.protocol.binding
        x, y, z = self.movement.input_position
        observed = blocks.observed_block(position)
        runtime_id = 0 if observed is None or observed.runtime_id is None else observed.runtime_id
        px, py, pz = position
        if click_pos is None:
            dx, dy, dz = FACE_OFFSETS.get(face, (0, 0, 0))
            click_pos = (0.5 + dx * 0.5, 0.5 + dy * 0.5, 0.5 + dz * 0.5)
        cx, cy, cz = click_pos
        slot = self.inventory.selected_slot
        transaction = b.ItemUseInventoryTransaction(
            transaction=b.InventoryTransaction(actions=[]),
            action_type=b.ItemUseInventoryTransaction.ActionType.PLACE,
            trigger_type=b.ItemUseInventoryTransaction.TriggerType.PLAYER_INPUT,
            pos=b.BlockPos(x=px, y=py, z=pz),
            face=face, slot=slot, hand=b.HandSlot.MAINHAND,
            item=self.inventory.item_or_empty(slot),
            from_pos=b.Vec3(x=x, y=y, z=z),
            click_pos=b.Vec3(x=cx, y=cy, z=cz),
            target_block_id=runtime_id,
            client_predicted_result=b.ItemUseInventoryTransaction.PredictedResult.SUCCESS,
            client_cooldown_state=b.ItemUseInventoryTransaction.ClientCooldownState.OFF,
        )
        packet = b.InventoryTransactionPacket(
            legacy_request_id=b.ItemStackLegacyRequestId(raw_id=0),
            legacy_set_item_slots=None, variant_transaction=transaction,
        )
        if _before_send is None:
            await self.packets.send(packet)
        else:
            await self.packets.send(packet, _before_send=_before_send)

    async def open(
        self, position: tuple[int, int, int], blocks, *, face: int = 1, timeout: float = 15,
        _before_click: Callable[[], None] | None = None,
        _on_observed: Callable[[ContainerWindow], None] | None = None,
    ) -> ContainerWindow:
        """Return only after BDS sends ContainerOpenPacket for the clicked block."""
        owns_window = _on_observed is not None
        # The server's open packet has no request ID, so an owned open needs an exclusive click.
        if self._owned_block_opening or (owns_window and self._block_open_requests):
            raise _InventoryMoveNotSubmitted("window_busy")
        self._block_open_requests += 1
        self._owned_block_opening = owns_window
        try:
            return await self._open_block(position, blocks, face, timeout, _before_click, _on_observed)
        finally:
            self._block_open_requests -= 1
            self._owned_block_opening = False

    async def _open_block(self, position, blocks, face, timeout, _before_click, _on_observed):
        b = self.packets.protocol.binding
        await self.movement.wait_for_position_sync(timeout)
        submitted = False
        opened = None

        def before_click():
            nonlocal submitted
            if _before_click is not None:
                _before_click()
            submitted = True

        def matches(raw):
            nonlocal opened
            if _on_observed is not None:
                if raw.packet_id != b.ContainerOpenPacket.ID:
                    return True
                if not submitted:
                    return False
                # The subscriber has installed this instance before the waiter runs.
                window = self.window
                if (window is not None and window.position == position and window.container_type == "CONTAINER"
                        and window.entity_unique_id == -1):
                    opened = window
                    _on_observed(window)
                return True
            packet = self.packets.protocol.decode(raw)
            return (int(packet.pos.x), int(packet.pos.y), int(packet.pos.z)) == position

        # Subscribe before submission: a local BDS can open the window in the same batch.
        packet_ids = b.ContainerOpenPacket.ID if _on_observed is None else (
            b.ContainerOpenPacket.ID, b.DisconnectPacket.ID, b.ChangeDimensionPacket.ID,
        )
        pending = asyncio.create_task(self.packets.wait_for_packet(
            packet_ids, timeout=timeout, match=matches,
        ))
        try:
            await asyncio.sleep(0)
            # Use the origin of the latest input, including any correction
            # received while registering the window waiter.
            await self.click_block(position, blocks, face=face, _before_send=(
                before_click if _on_observed is not None or _before_click is not None else None
            ))
            if _on_observed is None:
                return self._decode_open(await pending)
            raw = await asyncio.shield(pending)
            if raw.packet_id == b.DisconnectPacket.ID:
                raise ConnectionError("server disconnected while opening the chest")
            if opened is None:
                raise _InventoryMoveNotSubmitted("window_changed")
            return opened
        except asyncio.CancelledError as cancellation:
            if _on_observed is not None and submitted:
                try:
                    # Settle an in-flight owned click before the operation cleans up.
                    await asyncio.shield(pending)
                except (Exception, asyncio.CancelledError) as error:
                    cancellation.add_note(f"chest open unconfirmed after cancellation: {error!r}")
            raise
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            elif _on_observed is not None and not pending.cancelled():
                pending.exception()

    async def open_entity(self, runtime_id: int, entities: Entities, *, timeout: float = 15) -> ContainerWindow:
        """Open an observed chest minecart and return its active server window."""
        if self._unsubscribe is None:
            raise ConnectionError("container session is closed")
        if self.window is not None or self._entity_opening:
            raise RuntimeError("a container window is open or opening")
        actor = entities.get(runtime_id)
        target = _observed_entity_container(entities, actor)
        if actor is None or actor.actor_type != "minecraft:chest_minecart" or target is None:
            raise ValueError("an observed chest minecart with a known identity is required")
        b = self.packets.protocol.binding
        submitted = False
        opened = None

        def before_send():
            nonlocal submitted
            target.require_visible()
            if self.window is not None:
                raise RuntimeError("a container window opened before the interaction")
            submitted = True

        def matches(raw):
            nonlocal opened
            if raw.packet_id == b.DisconnectPacket.ID:
                raise ConnectionError("server disconnected while opening the chest minecart")
            if raw.packet_id == b.ChangeDimensionPacket.ID:
                raise RuntimeError("dimension changed while opening the chest minecart")
            target.require_visible()
            if raw.packet_id != b.ContainerOpenPacket.ID or not submitted:
                return False
            window = self.window
            if window is None or window.container_type != "CONTAINER" or window.entity_unique_id != target.entity_id:
                raise RuntimeError("the server opened a different container")
            opened = window
            self._entity_container = target
            return True

        self._entity_opening = True
        tasks = []
        try:
            async with asyncio.timeout(timeout):
                pending = asyncio.create_task(self.packets.wait_for_packet(
                    (b.ContainerOpenPacket.ID, b.ChangeDimensionPacket.ID, b.DisconnectPacket.ID,
                     b.RemoveActorPacket.ID, b.AddActorPacket.ID),
                    timeout=None, match=matches,
                ))
                tasks.append(pending)
                await asyncio.sleep(0)
                interacting = asyncio.create_task(entities.interact(
                    self.movement, self.inventory, runtime_id, _before_send=before_send,
                ))
                tasks.append(interacting)
                done, _ = await asyncio.wait((pending, interacting), return_when=asyncio.FIRST_COMPLETED)
                if pending in done:
                    await pending
                await interacting
                await pending
                if opened is None or self.window is not opened:
                    raise RuntimeError("the chest minecart window closed or changed during open")
                target.require_visible()
                return opened
        finally:
            try:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                self._entity_opening = False

    async def open_inventory_screen(
        self, *, timeout: float = 15, _on_observed: Callable[[ContainerWindow], None] | None = None,
    ) -> ContainerWindow:
        """Open the player inventory UI and await BDS's ContainerOpen response."""
        caller_owned = _on_observed is None
        active = self._inventory_open
        if active is not None:
            if caller_owned or not active.caller_owned:
                raise RuntimeError("player inventory open already in progress")
            async with asyncio.timeout(timeout):
                borrowed = await asyncio.shield(active.completion)
            if borrowed is None or self.window is not borrowed:
                raise RuntimeError("pending player inventory window did not open")
            return borrowed

        request = _InventoryOpen(caller_owned, asyncio.get_running_loop().create_future())
        self._inventory_open = request
        b = self.packets.protocol.binding
        submitted = False
        observed = None
        confirmed = None

        def matches(raw):
            nonlocal observed
            if raw.packet_id == b.DisconnectPacket.ID:
                return True
            if not submitted:
                return caller_owned
            # The packet subscriber has installed this exact window before the waiter runs.
            window = self.window
            if window is None or window.container_type != "INVENTORY" or window != self._decode_open(raw):
                return caller_owned
            observed = window
            if _on_observed is not None:
                _on_observed(window)
            return True

        pending = asyncio.create_task(self.packets.wait_for_packet(
            (b.ContainerOpenPacket.ID, b.DisconnectPacket.ID), timeout=timeout, match=matches,
        ))
        try:
            await asyncio.sleep(0)
            submitted = True
            await self.packets.send(b.InteractPacket(
                action=b.InteractPacket.Action.OPEN_INVENTORY,
                target_id=self.inventory.player_id,
                pos=None,
            ))
            reply = pending if caller_owned else asyncio.shield(pending)
            raw = await reply
            if raw.packet_id == b.DisconnectPacket.ID:
                raise ConnectionError("server disconnected while opening player inventory")
            opened = self._decode_open(raw)
            confirmed = observed
            return opened if caller_owned or confirmed is None else confirmed
        except asyncio.CancelledError as cancellation:
            if _on_observed is not None and submitted:
                try:
                    # Settle an in-flight owned open within its original wait timeout.
                    await asyncio.shield(pending)
                except (Exception, asyncio.CancelledError) as error:
                    cancellation.add_note(f"player inventory open unconfirmed after cancellation: {error!r}")
            raise
        finally:
            try:
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            finally:
                self._inventory_open = None
                request.completion.set_result(confirmed)

    async def wait_for_close(self, *, timeout: float = 15):
        """Await the active window's server close packet, without requesting a close.

        Register the waiter before the server action. Returns a typed
        ContainerClosePacket, including its server_initiated_close flag.
        """
        window = self.window
        if window is None:
            raise RuntimeError("no container window is open")
        b = self.packets.protocol.binding

        def matches(raw):
            return int(self.packets.protocol.decode(raw).container_id) == window.container_id

        raw = await self.packets.wait_for_packet(
            b.ContainerClosePacket.ID, timeout=timeout, match=matches,
        )
        return self.packets.protocol.decode(raw)

    async def close_window(
        self, *, timeout: float = 15, _expected_window: ContainerWindow | None = None,
    ) -> ContainerWindow | None:
        """Request close; return after the same window's server close packet."""
        window = self.window
        if window is None or (_expected_window is not None and window is not _expected_window):
            return None
        b = self.packets.protocol.binding

        pending = asyncio.create_task(self.wait_for_close(timeout=timeout))
        try:
            # Register the observer before sending; BDS may reply immediately.
            await asyncio.sleep(0)
            if _expected_window is not None and self.window is not _expected_window:
                return None
            container_type = (
                int(window.container_type) if window.container_type.lstrip("-").isdigit()
                else b.ContainerType[window.container_type]
            )
            await self.packets.send(b.ContainerClosePacket(
                container_id=window.container_id,
                container_type=container_type,
                server_initiated_close=False,
            ))
            await pending
            return window
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def take_from_container(
        self, source_slot: int, destination_slot: int, amount: int = 1, *, timeout: float = 15
    ):
        """Take an observed container item; return the matching server verdict."""
        return await self._transfer(
            source_slot, destination_slot, amount, into_container=False, timeout=timeout
        )

    async def put_into_container(
        self, source_slot: int, destination_slot: int, amount: int = 1, *, timeout: float = 15
    ):
        """Place an item into the open container; return the matching server verdict."""
        return await self._transfer(
            source_slot, destination_slot, amount, into_container=True, timeout=timeout
        )

    async def apply_anvil(
        self, destination_slot: int, new_name: str | None, *,
        material_count: int | None = None, timeout: float,
    ):
        """Commit the staged survival anvil inputs and return the BDS verdict."""
        window, slots = self.window, self._slots
        if window is None or window.container_type != "ANVIL" or slots is None:
            raise RuntimeError("open an observed anvil first")
        if type(destination_slot) is not int or not 0 <= destination_slot < 36:
            raise ValueError("destination must be a player inventory slot")
        if new_name is not None and (not isinstance(new_name, str) or not new_name):
            raise ValueError("new_name must be a nonempty string or None")
        if material_count is not None and (type(material_count) is not int or not 1 <= material_count <= 255):
            raise ValueError("material_count must be an integer between 1 and 255")
        player_slot = self.inventory.slots.get(destination_slot)
        if player_slot is None:
            raise RuntimeError("anvil destination slot is unobserved")
        if int(player_slot.id):
            raise ValueError("the anvil result requires an empty inventory slot")
        inputs = {i: slots.get(i) for i in _ANVIL_SLOTS}
        primary, material = inputs[1], inputs[2]
        if (primary is None or not primary.id or not primary.net_id_variant or not primary.stack_size):
            raise ValueError("stage an observed item in the anvil input")
        if material is not None and material.id:
            if not material.net_id_variant or not material.stack_size:
                raise ValueError("the anvil material stack is unobserved")
            if material_count is not None and material_count > material.stack_size:
                raise ValueError("material_count exceeds the observed anvil material stack")
        elif material_count is not None:
            raise ValueError("material_count requires an observed anvil material")
        elif new_name is None:
            raise ValueError("provide a new name or stage an anvil material")

        b = self.packets.protocol.binding
        cereal = b.ItemStackRequestCereal
        request_id = self.inventory._allocate_request_id()

        def slot_info(name, index, net_id):
            return cereal.SlotInfoData(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName[name], dynamic_id=None),
                slot=index, net_id_variant=int(net_id),
            )

        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=[
                cereal.CraftRecipeOptionalActionData(recipe_net_id=0, filtered_string_index=0),
                *(cereal.ConsumeActionData(
                    amount=material_count if index == 2 and material_count is not None else int(item.stack_size),
                    source=slot_info(_ANVIL_SLOTS[index], index, item.net_id_variant),
                ) for index, item in inputs.items() if item is not None and item.id),
                cereal.TakeActionData(
                    amount=int(primary.stack_size), source=slot_info("CREATED_OUTPUT_CONTAINER", 50, request_id),
                    destination=slot_info(_PLAYER_CONTAINERS[0], destination_slot, 0),
                ),
            ],
            strings_to_filter=[] if new_name is None else [new_name],
            strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )

        origins = {}

        def before_send():
            if self.window is not window or self._slots is None:
                raise RuntimeError("the anvil window changed before submission")
            if any(not _same_slot(item, self._slots.get(index)) for index, item in inputs.items()):
                raise RuntimeError("anvil inputs changed before submission")
            current = self.inventory.slots.get(destination_slot)
            if current is None:
                raise RuntimeError("anvil destination slot is unobserved")
            if not _same_slot(player_slot, current):
                raise RuntimeError("the result slot changed before submission")
            origins.update(self._anvil_origins)

        def apply(response):
            before = {} if self.window is window and self._slot_observers else None
            for container in response.containers or ():
                label = container.full_container_name.name.name
                for slot in container.slots:
                    index = int(slot.slot)
                    if _ANVIL_SLOTS.get(index) == label and self.window is window and self._slots is not None:
                        if self._anvil_origins.get(index) is not origins.get(index):
                            continue
                        current = self._slots.get(index)
                        if not slot.amount or (current is not None and current.id):
                            self._set_anvil_slot(index, _response_item(b, current, current, slot), before, corrected=True)
                    elif label in _PLAYER_CONTAINERS and index == destination_slot:
                        current = self.inventory.slots.get(index)
                        if not _same_slot(player_slot, current):
                            continue
                        if response.result.name == "SUCCESS" and slot.amount:
                            # Ordinary anvil operations preserve the item type.
                            # The verdict gives name and damage, but no item NBT.
                            output = b.SerializedNetworkItemStackDescriptor(
                                id=int(primary.id), stack_size=int(slot.amount), aux_value=int(primary.aux_value),
                                net_id_variant=None, block_runtime_id=int(primary.block_runtime_id),
                                user_data_buffer=b"",
                            )
                            self.inventory._set_slot(index, _response_item(b, output, output, slot))
                        elif not slot.amount:
                            self.inventory._set_slot(index, _response_item(b, current, current, slot))
            self._notify_slot_changes(window, before)

        return await self.inventory._request_item_stack(request, apply, timeout=timeout, before_send=before_send)

    async def _transfer(
        self, source_slot: int, destination_slot: int, amount: int,
        *, into_container: bool, timeout: float, _queued: bool = False,
        block_container: str | None = None,
        _expected_slots: tuple[object, object] | None = None,
        _expected_slot_count: int | None = None,
        _on_unconfirmed: Callable[[], None] | None = None,
        _range_window_current: Callable[[], bool] | None = None,
        _before_range_write: Callable[[int], None] | None = None,
    ):
        window = self.window
        if window is None or self._slots is None:
            raise RuntimeError("container window or its server-reported slots are unavailable")
        layout = self._layout_token
        observed_slot_count = self._observed_window_slot_count(window)
        if _expected_slot_count is not None and observed_slot_count != _expected_slot_count:
            raise RuntimeError("container slot count changed before transfer")
        entity_container = self._entity_container
        if entity_container is not None:
            entity_container.require_visible()
        elif window.container_type == "CONTAINER" and window.entity_unique_id not in (-1, 0):
            raise RuntimeError("cannot transfer through an unbound entity container")
        if not 0 <= source_slot <= 255 or not 0 <= destination_slot <= 255:
            raise ValueError("item-stack request slots must fit in a byte")
        if not 1 <= amount <= 255:
            raise ValueError("item-stack request amount must be between 1 and 255")
        if block_container is not None and (
            block_container not in ("LEVEL_ENTITY_CONTAINER", "BARREL_CONTAINER")
            or window.container_type != "CONTAINER" or window.position is None
        ):
            raise RuntimeError("no matching observed block container window")

        b = self.packets.protocol.binding
        cereal = b.ItemStackRequestCereal
        player_name = _PLAYER_CONTAINERS[0]
        chest_slot, player_slot = (destination_slot, source_slot) if into_container else (source_slot, destination_slot)
        container_name = block_container or self._container_slot_name(window, chest_slot)
        slot_names = _WINDOW_SLOT_NAMES.get(window.container_type)
        brewing = window.container_type == "BREWING_STAND"
        player_endpoint = (None, player_slot)
        block_endpoint = self._block_transfer_endpoint(chest_slot)
        chest_endpoint = block_endpoint if entity_container is None else (entity_container.instance, chest_slot)
        endpoints = {player_endpoint}
        if chest_endpoint is not None:
            # Use the physical slot across re-opens, independent of window ID.
            endpoints.add(chest_endpoint)
        endpoints = frozenset(endpoints)
        pending = self.inventory._requests_for_endpoints(endpoints)

        def slots_unconfirmed():
            return (chest_slot in self._unconfirmed_transfer_slots
                    or player_slot in self.inventory._unconfirmed_transfer_slots
                    or (block_endpoint is not None and any(
                        old.matches(block_endpoint, allow_possible=True) for old in self._unconfirmed_block_slots
                    )))

        if _expected_slots is not None and (pending or slots_unconfirmed()):
            return None
        blockers = tuple(request.reply for request in pending if request.range_step)

        if into_container:
            source_name, destination_name = player_name, container_name
            action_type = cereal.PlaceActionData
        else:
            source_name, destination_name = container_name, player_name
            action_type = cereal.TakeActionData

        def slot_info(name, slot, item):
            return cereal.SlotInfoData(
                full_container_name=b.FullContainerName(
                    name=b.ContainerEnumName[name], dynamic_id=None,
                ),
                slot=slot,
                net_id_variant=int(item.net_id_variant or 0),
            )

        request_id = self.inventory._allocate_request_id()
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=[],
            strings_to_filter=[],
            strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )
        source = original_chest = original_player = None

        def prepare_request():
            nonlocal source, original_chest, original_player
            original_chest = self._slots.get(chest_slot)
            original_player = self.inventory.slots.get(player_slot)
            if into_container:
                source = self.inventory.item_or_empty(source_slot)
                destination = original_chest or self.inventory.item_or_empty(-1)
            else:
                source = original_chest
                destination = self.inventory.item_or_empty(destination_slot)
            if _expected_slots is not None and (
                not _same_slot(source, _expected_slots[0]) or not _same_slot(destination, _expected_slots[1])
            ):
                raise RuntimeError("container transfer slots changed before submission")
            if source is None or int(source.id) == 0:
                raise ValueError("source slot has no observed item")
            request.actions = [action_type(
                amount=amount,
                source=slot_info(source_name, source_slot, source),
                destination=slot_info(destination_name, destination_slot, destination),
            )]

        if not blockers:
            prepare_request()

        anvil_origins = {}
        observed_quantities: dict[_TransferEndpoint, int] = {}
        entity_changes: dict[_TransferEndpoint, object] = {}
        entity_range = _range_window_current is not None and entity_container is not None
        brewing_slot_changed = False

        def observe_pending_slot(endpoint, current):
            nonlocal brewing_slot_changed
            original = original_player if endpoint == player_endpoint else original_chest
            if entity_range:
                if endpoint != player_endpoint and not window_is_current():
                    return
                # A single observation can be an echo. Two different changes
                # to the endpoint, including A -> B -> A, cannot be one echo.
                if endpoint in entity_changes:
                    if not _same_slot(entity_changes[endpoint], current):
                        _on_unconfirmed()
                        entity_changes[endpoint] = current
                elif not _same_slot(original, current):
                    entity_changes[endpoint] = current
                return
            if brewing and endpoint == chest_endpoint and not _same_slot(original_chest, current):
                # Keep the evidence even if another full observation restores the old value.
                brewing_slot_changed = True
            if _on_unconfirmed is None or not _same_stack(original, current) or _same_slot(original, current):
                return
            amount = int(current.stack_size)
            # Two different changes cannot both be echoes of one receipt.
            if observed_quantities.setdefault(endpoint, amount) != amount:
                _on_unconfirmed()

        def check_receipt_echo(endpoint, original, slot):
            observed = observed_quantities.get(endpoint)
            if _on_unconfirmed is None or observed is None:
                return
            net_id = slot.item_stack_net_id
            if observed != int(slot.amount) or (
                net_id is not None and int(net_id.raw_id) != int(original.net_id_variant or 0)
            ):
                _on_unconfirmed()

        def before_anvil_transfer():
            if self.window is not window or self._slots is None:
                raise RuntimeError("the anvil window changed before submission")
            if (not _same_slot(original_chest, self._slots.get(chest_slot))
                    or not _same_slot(original_player, self.inventory.slots.get(player_slot))):
                raise RuntimeError("anvil transfer slots changed before submission")
            anvil_origins.update(self._anvil_origins)

        def window_is_current():
            return (self.window is window and self._layout_token is layout
                    and (entity_container is None or
                         (self._entity_container is entity_container and entity_container.is_visible())))

        def apply(response):
            # A reply can correct other slots as well as the action's endpoints.
            # The response carries amounts/net IDs, not item types: unknown
            # nonempty slots stay unknown until an inventory packet identifies them.
            before = {} if self._slot_observers and window_is_current() else None
            empty = self.inventory.item_or_empty(-1)
            player_slots = self.inventory.slots
            success = response.result.name == "SUCCESS"
            chest_receipts = player_receipts = 0

            def correct(current, original, is_endpoint, slot):
                stale_endpoint = is_endpoint and not _same_slot(original, current)
                if stale_endpoint and (not slot.amount or (brewing and not _same_stack(original, current))):
                    # The reply cannot identify a newer stack by amount alone.
                    return current
                if is_endpoint and not stale_endpoint and success:
                    return _response_item(b, current or empty, source, slot)
                # Outside those request endpoints, a nonempty correction must
                # identify the same observed stack to retain its type and metadata.
                corrected = None
                if not slot.amount or (current is not None and current.net_id_variant):
                    corrected = _correct_observed_item(b, current, slot)
                return current if stale_endpoint and corrected is None else corrected

            def preserve_entity_endpoint(endpoint, current, original, slot):
                if not entity_range or (endpoint not in entity_changes and _same_slot(original, current)):
                    return False
                expected = _response_item(b, original or empty, source, slot)
                if not _same_slot(expected, current):
                    _on_unconfirmed()
                return True

            for container in response.containers or ():
                name = container.full_container_name.name.name
                for slot in container.slots:
                    index = int(slot.slot)
                    if slot_names is not None:
                        matches_window = 0 <= index < len(slot_names) and name == slot_names[index]
                    elif window.container_type == "ANVIL":
                        matches_window = _ANVIL_SLOTS.get(index) == name
                    else:
                        matches_window = name == container_name
                    preserve_window_slot = preserve_player_slot = False
                    if matches_window and index == chest_slot:
                        chest_receipts += 1
                        check_receipt_echo(chest_endpoint, original_chest, slot)
                        if entity_range:
                            observed = (self._slots.get(index) if self._slots is not None and window_is_current()
                                        else entity_changes.get(chest_endpoint, original_chest))
                            preserve_window_slot = preserve_entity_endpoint(chest_endpoint, observed, original_chest, slot)
                    if name in _PLAYER_CONTAINERS and index == player_slot:
                        player_receipts += 1
                        check_receipt_echo(player_endpoint, original_player, slot)
                        preserve_player_slot = preserve_entity_endpoint(
                            player_endpoint, player_slots.get(index), original_player, slot,
                        )
                    if matches_window and self._slots is not None and window_is_current():
                        if preserve_window_slot:
                            continue
                        current = self._slots.get(index)
                        if brewing and index == chest_slot and (brewing_slot_changed or not _same_slot(original_chest, current)):
                            # Brewing can consume or replace a stack while the request is in flight.
                            continue
                        if window.container_type == "ANVIL":
                            if self._anvil_origins.get(index) is not anvil_origins.get(index):
                                continue
                            self._set_anvil_slot(index, correct(current, current, index == chest_slot, slot),
                                                 before, corrected=True)
                            continue
                        updated = correct(current, original_chest, index == chest_slot, slot)
                        if updated is current:
                            continue
                        self._record_slot_before(index, before)
                        if updated is None:
                            self._slots.pop(index, None)
                        else:
                            self._slots[index] = updated
                        self._slots_updated.set()
                    elif name in _PLAYER_CONTAINERS:
                        if preserve_player_slot:
                            continue
                        current = player_slots.get(index)
                        updated = correct(current, original_player, index == player_slot, slot)
                        if updated is not current:
                            self.inventory._set_slot(index, updated)
            if success:
                if chest_receipts != 1:
                    if block_endpoint is not None:
                        self._unconfirmed_block_slots.add(block_endpoint)
                    elif window_is_current():
                        self._unconfirmed_transfer_slots.add(chest_slot)
                if player_receipts != 1:
                    self.inventory._unconfirmed_transfer_slots.add(player_slot)
            self._notify_slot_changes(window, before)

        def check_range_window():
            if not _range_window_current() or self._block_transfer_endpoint(chest_slot) != block_endpoint:
                raise _InventoryMoveNotSubmitted("window_changed")

        def expected_endpoints():
            expected_chest, expected_player = _expected_slots[::-1] if into_container else _expected_slots
            chest = None if self._slots is None else self._slots.get(chest_slot)
            player = self.inventory.slots.get(player_slot)
            if slots_unconfirmed():
                reason = "container transfer slots need a fresh server observation"
            elif (not _same_slot(expected_chest, chest)
                    or not _same_slot(expected_player, player)
                    or not _same_slot(expected_player, self.inventory.item_or_empty(player_slot))):
                reason = "container transfer slots changed before submission"
            else:
                return chest, player
            if _before_range_write is not None:
                raise _InventoryMoveNotSubmitted("unobserved")
            raise RuntimeError(reason)

        def before_range_write():
            nonlocal original_chest, original_player
            check_range_window()
            if _before_range_write is not None:
                _before_range_write(request_id)
            original_chest, original_player = expected_endpoints()

        def before_send():
            nonlocal original_chest, original_player
            if _range_window_current is not None:
                check_range_window()
            else:
                if window.container_type != "ANVIL" and self.window is not window:
                    raise RuntimeError("container window closed or changed before the transfer was sent")
                if self.window is window and (self._layout_token is not layout
                                              or self._block_transfer_endpoint(chest_slot) != block_endpoint):
                    raise RuntimeError("container transfer endpoint changed before submission")
            if entity_container is not None:
                entity_container.require_visible()
            if blockers:
                if self.window is not window or self._slots is None:
                    raise RuntimeError("container window closed or changed before the transfer was sent")
                if slots_unconfirmed():
                    raise RuntimeError("container transfer slots need a fresh server observation")
                prepare_request()
            if window.container_type == "ANVIL":
                before_anvil_transfer()
            if _expected_slots is not None and _range_window_current is None:
                original_chest, original_player = expected_endpoints()

        return await self.inventory._request_item_stack(
            request, apply, timeout=timeout, queued=_queued, before_send=before_send,
            _endpoints=endpoints, _range_step=_expected_slots is not None,
            _wait_for=blockers, _on_unconfirmed=_on_unconfirmed,
            _on_slot_observed=observe_pending_slot if brewing or _on_unconfirmed is not None else None,
            _at_write=before_range_write if _range_window_current is not None else None,
        )

    def close(self) -> None:
        if self._unsubscribe is None:
            return
        self._unsubscribe()
        self._unsubscribe = None
        if self._unsubscribe_reader_end is not None:
            self._unsubscribe_reader_end()
            self._unsubscribe_reader_end = None
        self._set_window(None)
        self._unconfirmed_block_slots.clear()
