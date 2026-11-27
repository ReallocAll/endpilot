"""Server-reported inventory slots needed by player item actions."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from itertools import count
from typing import Literal


@dataclass(frozen=True, slots=True)
class HeldItemStack:
    """Immutable fields from a server-described inventory stack."""

    item_id: int
    count: int
    aux_value: int
    net_id: int | None
    block_runtime_id: int
    user_data_buffer: bytes


@dataclass(frozen=True, slots=True)
class HeldItemSnapshot:
    """The next input's selected slot, its observation, and any local count prediction."""

    slot: int
    selection_source: Literal["default", "local", "server"]
    observed_item: HeldItemStack | None
    predicted_count: int | None


@dataclass(frozen=True, slots=True)
class InventorySlotChange:
    """A main inventory slot's before and after descriptions at a server update boundary."""

    slot: int
    before: HeldItemStack | None
    after: HeldItemStack | None


@dataclass(frozen=True, slots=True, eq=False)
class _InventorySlotObserver:
    slot: int | None
    callback: Callable[[InventorySlotChange], None]


def _stack_snapshot(item) -> HeldItemStack | None:
    return None if item is None else HeldItemStack(
        int(item.id), int(item.stack_size), int(item.aux_value),
        None if item.net_id_variant is None else int(item.net_id_variant),
        int(item.block_runtime_id), bytes(item.user_data_buffer),
    )


_MAIN_INVENTORY_SIZE = 36
_PLAYER_CONTAINERS = ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", "INVENTORY_CONTAINER", "HOTBAR_CONTAINER")
_ARMOR_SLOT_NAMES = ("head", "chest", "legs", "feet")
_DAMAGE_ARMOR_SLOTS = tuple(("ARMOR_CONTAINER", slot) for slot in range(len(_ARMOR_SLOT_NAMES)))
_DAMAGE_OFFHAND_SLOT = ("OFFHAND_CONTAINER", 0)


@dataclass(frozen=True, slots=True)
class _BlockTransferEndpoint:
    dimension: int | None
    layout: object
    slot: int
    local_slot: int
    half: tuple[int, int, int] | None
    possible_halves: frozenset[tuple[int, int, int]]

    def matches(self, other: "_BlockTransferEndpoint", *, allow_possible: bool = False) -> bool:
        if self.dimension != other.dimension:
            return False
        if self.layout is other.layout:
            return self.slot == other.slot
        if self.local_slot != other.local_slot:
            return False
        if self.half is not None and other.half is not None:
            return self.half == other.half
        return allow_possible and not self.possible_halves.isdisjoint(other.possible_halves)


# Tuple endpoints use None for main inventory, ARMOR_CONTAINER for armor, and the actor instance for entity slots.
_TransferEndpoint = tuple[object, int] | _BlockTransferEndpoint


def _transfer_endpoints_match(first: _TransferEndpoint, second: _TransferEndpoint, *, allow_possible=False) -> bool:
    if isinstance(first, _BlockTransferEndpoint):
        return isinstance(second, _BlockTransferEndpoint) and first.matches(second, allow_possible=allow_possible)
    return first == second


@dataclass(slots=True)
class _PendingStackRequest:
    reply: asyncio.Future
    apply: Callable
    endpoints: frozenset[_TransferEndpoint]
    range_step: bool
    on_unconfirmed: Callable[[], None] | None
    on_slot_observed: Callable[[_TransferEndpoint, object | None], None] | None
    checked_at_write: bool = False
    submitted: bool = False
    automatic_unequip: bool = False
    exact_offhand_take: bool = False


class _InventoryMoveNotSubmitted(RuntimeError):
    pass


def _response_item(binding, previous, fallback, response_slot):
    """Apply a server slot correction without inventing a new item identity."""
    amount = int(response_slot.amount)
    if amount == 0:
        return binding.SerializedNetworkItemStackDescriptor(
            id=0, stack_size=0, aux_value=0, net_id_variant=None,
            block_runtime_id=0, user_data_buffer=b"",
        )
    item = previous if int(previous.id) != 0 else fallback
    net_id = response_slot.item_stack_net_id
    return binding.SerializedNetworkItemStackDescriptor(
        id=int(item.id), stack_size=amount, aux_value=int(item.aux_value),
        net_id_variant=int(net_id.raw_id) if net_id is not None else item.net_id_variant,
        block_runtime_id=int(item.block_runtime_id),
        user_data_buffer=item.user_data_buffer,
    )


def _correct_observed_item(binding, item, response_slot):
    """Correct a changed slot only when BDS identifies its observed stack."""
    if int(response_slot.amount) > 0:
        net_id = response_slot.item_stack_net_id
        if (item is None or int(item.id) == 0 or net_id is None
                or int(item.net_id_variant or 0) != int(net_id.raw_id)):
            return None
    return _response_item(binding, item, item, response_slot)


class Inventory:
    def __init__(self, packets, player_id: int):
        self.packets = packets
        self.player_id = int(player_id)
        self._slots = {}
        self._drop_origins = {}
        self._drop_key_counts: dict[int, int] = {}
        self._drop_key_lock = asyncio.Lock()
        self._armor_slots = {}
        self._offhand_slot = None  # None means the server has not described this slot.
        self._damage_descriptors = {}
        self._next_request_id = -1
        self._selected_slot = 0
        self._selection_source: Literal["default", "local", "server"] = "default"
        self._selection_revision = 0
        self._selection_order = count(1)
        self._applied_selection_order = 0
        self._events = {}
        self._changed = asyncio.Event()
        self._count_observation_gaps: set[int] = set()
        self._pending_requests = {}
        self._unconfirmed_transfer_slots: set[int] = set()
        self._qa_requests = {}
        self._request_sends = set()
        self._request_stream_ended = False
        self._held_item_waiters: dict[asyncio.Future[HeldItemSnapshot], HeldItemSnapshot] = {}
        self._slot_observers: list[_InventorySlotObserver] = []
        self._verdict_before: dict[int, object | None] | None = None
        self._unsubscribe = packets.subscribe_raw(self._on_packet)
        subscribe_reader_end = getattr(packets, "subscribe_reader_end", None)
        self._unsubscribe_reader_end = (
            None if subscribe_reader_end is None else subscribe_reader_end(self._end_requests)
        )

    def _record_damage_descriptor(self, slot, item, *, received):
        if received and item is not None:
            self._damage_descriptors[slot] = item
            return
        observed = self._damage_descriptors.get(slot)
        # A verdict can corroborate an existing packet for this slot, but a
        # reconstructed descriptor cannot establish fresh item data.
        if item is None or (observed is not None and _stack_snapshot(item) != _stack_snapshot(observed)):
            self._damage_descriptors.pop(slot, None)

    def _set_armor_slot(self, slot, item, *, received=False):
        if item is None:
            self._armor_slots.pop(slot, None)
        else:
            self._armor_slots[slot] = item
        if 0 <= slot < len(_DAMAGE_ARMOR_SLOTS):
            self._record_damage_descriptor(_DAMAGE_ARMOR_SLOTS[slot], item, received=received)

    def _set_offhand_slot(self, item, *, received=False):
        self._offhand_slot = item
        self._record_damage_descriptor(_DAMAGE_OFFHAND_SLOT, item, received=received)

    def _set_slot(self, slot, item, *, drop_origin=None, received=False):
        slot = int(slot)
        if self._verdict_before is not None:
            self._record_slot_before(slot, self._verdict_before)
        self._drop_key_counts.pop(slot, None)
        if drop_origin is None:
            self._drop_origins.pop(slot, None)
        else:
            self._drop_origins[slot] = drop_origin
        if item is None:
            self._slots.pop(slot, None)
        else:
            self._slots[slot] = item
        if 0 <= slot < 9:
            self._record_damage_descriptor((None, slot), item, received=received)
        if received:
            self._count_observation_gaps.discard(slot)
        event = self._events.get(slot)
        if event is not None:
            event.set()
        self._changed.set()

    def _on_packet(self, raw):
        b = self.packets.protocol.binding
        if raw.packet_id == b.ItemStackResponsePacket.ID:
            try:
                responses = self.packets.protocol.decode(raw).responses
            except Exception as error:
                # Without decoded IDs any pending verdict may belong to this packet.
                self._fail_pending_requests(ValueError, "could not decode item-stack response", error)
                return
            for response in responses:
                request_id = int(response.client_request_id.raw_id)
                pending = self._pending_requests.get(request_id)
                if pending is None or (pending.checked_at_write and not pending.submitted):
                    continue
                try:
                    self._mark_range_overlap({
                        (None, int(slot.slot))
                        for container in response.containers or ()
                        if container.full_container_name.name.name in _PLAYER_CONTAINERS
                        for slot in container.slots
                    }, except_request=pending)
                    before = self._apply_inventory_verdict(pending, response)
                except Exception as error:
                    self._fail_request(pending.reply, error)
                else:
                    pending.reply.set_result(response)
                    # Apply all slot corrections for this verdict before exposing its change.
                    self._notify_held_item()
                    self._notify_inventory_changes(before)
                finally:
                    self._pending_requests.pop(request_id, None)
        elif raw.packet_id == b.DisconnectPacket.ID:
            self._end_requests()
        elif raw.packet_id == b.InventoryContentPacket.ID:
            packet = self.packets.protocol.decode(raw)
            inventory_id = int(packet.inventory_id)
            if inventory_id == int(b.ContainerID.ARMOR):
                previous = self._armor_slots
                self._armor_slots = dict(enumerate(packet.slots))
                for endpoint in _DAMAGE_ARMOR_SLOTS:
                    self._record_damage_descriptor(endpoint, self._armor_slots.get(endpoint[1]), received=True)
                self._observe_transfer_content("ARMOR_CONTAINER", previous, self._armor_slots)
                return
            if inventory_id == int(b.ContainerID.OFFHAND):
                self._set_offhand_slot(packet.slots[0] if packet.slots else None, received=True)
                return
            if inventory_id != int(b.ContainerID.INVENTORY):
                return
            previous = self._slots
            before = previous if self._slot_observers else None
            self._slots = dict(enumerate(packet.slots))
            for slot in range(9):
                self._record_damage_descriptor((None, slot), self._slots.get(slot), received=True)
            self._unconfirmed_transfer_slots.difference_update(self._slots)
            self._observe_transfer_content(None, previous, self._slots)
            self._drop_origins.clear()
            self._drop_key_counts.clear()
            self._count_observation_gaps.clear()
            for event in self._events.values():
                event.set()
            self._changed.set()
            self._notify_held_item()
            self._notify_inventory_changes(before, complete=True)
        elif raw.packet_id == b.InventorySlotPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.inventory_id) == int(b.ContainerID.INVENTORY):
                slot = int(packet.slot)
                before = {slot: self._slots.get(slot)} if self._observes_inventory_slot(slot) else None
                self._unconfirmed_transfer_slots.discard(slot)
                try:
                    self._set_slot(slot, packet.item, received=True)
                except Exception:
                    self._count_observation_gaps.add(slot)
                    self._changed.set()
                    raise
                self._observe_transfer_slot((None, slot), packet.item)
                if slot == self._selected_slot:
                    self._notify_held_item()
                self._notify_inventory_changes(before)
            elif int(packet.inventory_id) == int(b.ContainerID.ARMOR):
                slot = int(packet.slot)
                self._set_armor_slot(slot, packet.item, received=True)
                self._observe_transfer_slot(("ARMOR_CONTAINER", slot), packet.item)
            elif int(packet.inventory_id) == int(b.ContainerID.OFFHAND) and int(packet.slot) == 0:
                self._set_offhand_slot(packet.item, received=True)
        elif raw.packet_id == b.MobEquipmentPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if packet.runtime_id == self.player_id and int(packet.container_id) == int(b.ContainerID.INVENTORY):
                selected = int(packet.selected_slot)
                if 0 <= selected < 9:
                    item = packet.item if int(packet.slot) == selected else None
                    self._record_damage_descriptor((None, selected), item, received=True)
                self._observe_selection(selected)
        elif raw.packet_id == b.PlayerHotbarPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if packet.should_select_slot and int(packet.container_id) == int(b.ContainerID.INVENTORY):
                self._observe_selection(int(packet.selected_slot))
        elif raw.packet_id == b.InventoryTransactionPacket.ID:
            packet = self.packets.protocol.decode(raw)
            before = {} if self._slot_observers else None
            # Apply server-initiated container slot changes, including automatic pickup.
            transaction = getattr(packet.variant_transaction, "transaction", None)
            for action in getattr(transaction, "actions", ()):
                source = action.source
                if source.type.name != "CONTAINER_INVENTORY" or source.container_id is None:
                    continue
                inventory_id = int(source.container_id)
                if inventory_id == int(b.ContainerID.INVENTORY):
                    slot = int(action.slot)
                    self._record_slot_before(slot, before)
                    self._unconfirmed_transfer_slots.discard(slot)
                    self._set_slot(slot, action.to_item_descriptor, received=True)
                    self._observe_transfer_slot((None, slot), action.to_item_descriptor)
                elif inventory_id == int(b.ContainerID.ARMOR):
                    slot = int(action.slot)
                    self._set_armor_slot(slot, action.to_item_descriptor, received=True)
                    self._observe_transfer_slot(("ARMOR_CONTAINER", slot), action.to_item_descriptor)
                elif inventory_id == int(b.ContainerID.OFFHAND) and int(action.slot) == 0:
                    self._set_offhand_slot(action.to_item_descriptor, received=True)
            self._notify_held_item()
            self._notify_inventory_changes(before)

    def subscribe_inventory_changes(
        self, callback: Callable[[InventorySlotChange], None], *, slot: int | None = None,
    ) -> Callable[[], None]:
        """Subscribe to server-observed main inventory changes and return an unsubscribe function."""
        from .compression import PacketConnection

        if self._request_stream_ended:
            raise ConnectionError("inventory observation ended")
        if slot is not None and (type(slot) is not int or not 0 <= slot < _MAIN_INVENTORY_SIZE):
            raise ValueError("inventory slot must be in 0..35")

        return PacketConnection._subscribe(self._slot_observers, _InventorySlotObserver(slot, callback))

    def _observes_inventory_slot(self, slot: int) -> bool:
        return bool(self._slot_observers) and 0 <= slot < _MAIN_INVENTORY_SIZE and any(
            observer.slot is None or observer.slot == slot for observer in self._slot_observers
        )

    def _record_slot_before(self, slot: int, before) -> None:
        if before is not None and slot not in before and self._observes_inventory_slot(slot):
            before[slot] = self._slots.get(slot)

    def _apply_inventory_verdict(self, pending, response):
        before = self._verdict_before = {} if self._slot_observers else None
        try:
            pending.apply(response)
        finally:
            self._verdict_before = None
        return before

    def _notify_inventory_changes(self, before, *, complete=False) -> None:
        if before is None or not self._slot_observers or (not complete and not before):
            return
        observers = tuple(self._slot_observers)
        if complete:
            selected = {observer.slot for observer in observers}
            slots = range(_MAIN_INVENTORY_SIZE) if None in selected else sorted(selected)
        else:
            slots = sorted(before)
        changes = []
        for slot in slots:
            previous, current = before.get(slot), self._slots.get(slot)
            if previous is current:
                continue
            old, new = _stack_snapshot(previous), _stack_snapshot(current)
            if old != new:
                changes.append(InventorySlotChange(slot, old, new))
        for change in changes:
            for observer in observers:
                if (observer.slot is not None and observer.slot != change.slot) or observer not in self._slot_observers:
                    continue
                try:
                    observer.callback(change)
                except (Exception, asyncio.CancelledError) as error:
                    asyncio.get_running_loop().call_exception_handler({
                        "message": "inventory observer failed", "exception": error, "callback": observer.callback,
                    })

    @property
    def slots(self) -> dict[int, object]:
        """Latest server-described inventory slots; missing slots are unknown."""
        return dict(self._slots)

    def client_slots(self) -> dict[int, object]:
        """Inventory as presented by the player, including submitted drop keys."""
        slots = self.slots
        for slot in self._drop_key_counts:
            slots[slot] = self.item_or_empty(slot)
        return slots

    @property
    def armor_slots(self) -> dict[int, object]:
        """Latest server-described armor slots; missing slots are not observations."""
        return dict(self._armor_slots)

    @property
    def offhand_item(self):
        """Latest server-described offhand item, or None when not observed."""
        return self._offhand_slot

    @property
    def held_item(self) -> HeldItemSnapshot:
        """Return an independent snapshot of the selected main-hand slot."""
        slot = self._selected_slot
        return HeldItemSnapshot(slot, self._selection_source, self._observed_stack(slot), self._drop_key_counts.get(slot))

    def _observed_stack(self, slot: int) -> HeldItemStack | None:
        return _stack_snapshot(self._slots.get(slot))

    def _observed_damage_equipment(self) -> tuple[tuple[HeldItemStack, ...], tuple[HeldItemStack, ...]] | None:
        """Read complete armor and possible hand stacks for an equipment-dependent estimate."""
        if self._request_stream_ended:
            return None
        hotbar = (self._selected_slot,) if self._selection_source == "server" else tuple(range(9))
        if any(slot in self._unconfirmed_transfer_slots or slot in self._drop_key_counts for slot in hotbar):
            return None
        armor = tuple(_stack_snapshot(self._damage_descriptors.get(slot)) for slot in _DAMAGE_ARMOR_SLOTS)
        hands = (_stack_snapshot(self._damage_descriptors.get(_DAMAGE_OFFHAND_SLOT)),
                 *(_stack_snapshot(self._damage_descriptors.get((None, slot))) for slot in hotbar))
        for stack in (*armor, *hands):
            if stack is None or bool(stack.item_id) != (stack.count > 0):
                return None
            # A verdict can preserve a stack's type and assign an ID while
            # omitting its item data. A vanilla no-NBT descriptor has a buffer.
            if stack.item_id and len(stack.user_data_buffer) < 10:
                return None
        return armor, hands

    async def wait_for_held_item_change(self, *, timeout: float | None = 15) -> HeldItemSnapshot:
        """Return the first changed main-hand snapshot observed after starting the wait."""
        if self._request_stream_ended:
            raise ConnectionError("connection ended before a held-item change")
        waiter = asyncio.get_running_loop().create_future()
        self._held_item_waiters[waiter] = self.held_item
        try:
            async with asyncio.timeout(timeout):
                return await waiter
        finally:
            self._held_item_waiters.pop(waiter, None)

    def _notify_held_item(self) -> None:
        if not self._held_item_waiters:
            return
        current = self.held_item
        for waiter, baseline in tuple(self._held_item_waiters.items()):
            if not waiter.done() and current != baseline:
                self._held_item_waiters.pop(waiter)
                waiter.set_result(current)

    def _allocate_request_id(self) -> int:
        # One request ID space for armor and open-container operations.
        request_id = self._next_request_id
        self._next_request_id -= 2
        return request_id

    @staticmethod
    def _fail_request(reply, error):
        reply.set_exception(error)
        # The caller may have stopped waiting. Its next read can still raise this error.
        reply.exception()

    def _fail_pending_requests(self, error_type, message, cause):
        for pending in self._pending_requests.values():
            error = error_type(message)
            error.__cause__ = cause
            self._fail_request(pending.reply, error)
        self._pending_requests.clear()

    def _end_requests(self, cause=None):
        self._request_stream_ended = True
        self._slot_observers.clear()
        self._fail_pending_requests(ConnectionError, "connection ended before an item-stack verdict", cause)
        for waiter in self._held_item_waiters:
            if not waiter.done():
                error = ConnectionError("connection ended before a held-item change")
                error.__cause__ = cause
                self._fail_request(waiter, error)
        self._held_item_waiters.clear()
        self._unconfirmed_transfer_slots.clear()
        for task in tuple(self._request_sends):
            task.cancel()

    def _requests_for_endpoints(self, endpoints, *, allow_possible=True):
        return tuple(pending for pending in self._pending_requests.values() if any(
            _transfer_endpoints_match(owned, requested, allow_possible=allow_possible)
            for owned in pending.endpoints for requested in endpoints
        ))

    def _mark_range_overlap(self, endpoints, *, except_request):
        for pending in self._requests_for_endpoints(endpoints, allow_possible=False):
            if pending is not except_request and pending.on_unconfirmed is not None:
                pending.on_unconfirmed()

    def _observe_transfer_content(self, container, previous, current) -> None:
        if self._pending_requests:
            for index, item in current.items():
                self._observe_transfer_slot((container, index), item)
            # A content packet replaces the previous observation, including omitted slots.
            for index in previous.keys() - current.keys():
                self._observe_transfer_slot((container, index), None)

    def _observe_transfer_slot(self, endpoint: _TransferEndpoint, item: object | None) -> None:
        for pending in self._pending_requests.values():
            if pending.submitted and pending.on_slot_observed is not None:
                for owned in pending.endpoints:
                    if _transfer_endpoints_match(owned, endpoint):
                        pending.on_slot_observed(owned, item)

    @staticmethod
    def _request_player_endpoints(request):
        endpoints = set()
        for action in request.actions:
            for part in (getattr(action, "source", None), getattr(action, "destination", None)):
                container = getattr(part, "full_container_name", None)
                if container is None:
                    continue
                if container.name.name in _PLAYER_CONTAINERS:
                    endpoints.add((None, int(part.slot)))
                elif container.name.name == "ARMOR_CONTAINER":
                    endpoints.add(("ARMOR_CONTAINER", int(part.slot)))
                elif container.name.name == "OFFHAND_CONTAINER":
                    endpoints.add(_DAMAGE_OFFHAND_SLOT)
        return frozenset(endpoints)

    async def _request_item_stack(self, request, apply, *, timeout, queued=False, before_send=None,
                                  after_send=None, _endpoints=None, _range_step=False,
                                  _wait_for=(), _on_unconfirmed=None, _on_slot_observed=None,
                                  _at_write=None, _automatic_unequip=False, _exact_offhand_take=False):
        """Own pending replies for inventory, containers, trading, recipes, enchanting and creative actions.

        The operation owns its slot/window checks and supplies synchronous hooks.
        `before_send` may prepare or reject the request before encoding; `_at_write`
        may reject it after encoding, before transport. `after_send` reports a local
        send, not a verdict. `_on_slot_observed` runs for each matching endpoint
        when a slot is observed while its submitted request is pending.
        `_on_unconfirmed` runs for other pending requests with definite endpoint
        overlap on submission or before applying a reply reporting overlapping
        player inventory slots. These three hooks must return without raising.
        `apply` runs inline on the reader before resolving the matching reply.
        An ordinary exception fails the reply without rolling back applied changes.

        Caller timeout or cancellation withdraws only an unsubmitted deferred or
        write-checked request. Other requests may still send or apply a late
        verdict. Disconnect or reader end fails pending replies and cancels sends.
        """
        if self._request_stream_ended:
            raise ConnectionError("item-stack request stream is closed")
        request_id = int(request.client_request_id.raw_id)
        reply = asyncio.get_running_loop().create_future()
        endpoints = self._request_player_endpoints(request) if _endpoints is None else _endpoints
        pending = _PendingStackRequest(
            reply, apply, endpoints, _range_step, _on_unconfirmed, _on_slot_observed,
            checked_at_write=_at_write is not None, automatic_unequip=_automatic_unequip,
            exact_offhand_take=_exact_offhand_take,
        )
        self._pending_requests[request_id] = pending
        if queued:
            self._qa_requests[request_id] = reply

        def submit():
            if _at_write is not None:
                if reply.done():
                    raise RuntimeError("item-stack request ended before submission")
                _at_write()
            self._mark_range_overlap(endpoints, except_request=pending)
            pending.submitted = True

        async def send():
            b = self.packets.protocol.binding
            try:
                if _wait_for:
                    await asyncio.wait(_wait_for)
                if reply.done():
                    return
                if before_send is not None:
                    before_send()
                packet = b.ItemStackRequestPacket(requests=[request])
                if _at_write is None:
                    submit()
                    await self.packets.send(packet)
                else:
                    await self.packets.send(packet, _before_send=submit)
                if after_send is not None:
                    after_send()
            except Exception as error:
                if self._pending_requests.pop(request_id, None) is not None:
                    self._fail_request(reply, error)

        task = asyncio.create_task(send())
        self._request_sends.add(task)
        task.add_done_callback(self._request_sends.discard)
        if queued:
            return request_id
        try:
            async with asyncio.timeout(timeout):
                return await asyncio.shield(reply)
        except (TimeoutError, asyncio.CancelledError) as error:
            if (_wait_for or _at_write is not None) and not pending.submitted and self._pending_requests.pop(request_id, None) is pending:
                task.cancel()
                reply.cancel()
            if _range_step:
                if reply.done() and not reply.cancelled() and reply.exception() is None:
                    # The matching verdict has already finished applying in the packet reader.
                    error._item_stack_response = reply.result()
                elif pending.submitted or not reply.done():
                    # A write may have started or still be queued.
                    error._item_stack_request_id = request_id
            raise

    def _qa_response(self, request_id: int):
        reply = self._qa_requests.get(request_id)
        return reply.result() if reply is not None and reply.done() else None

    def count_item(self, item_id: int) -> int:
        """Sum the latest server-described inventory slots for a numeric item ID."""
        return sum(
            int(item.stack_size) for item in self._slots.values()
            if int(item.id) == item_id
        )

    async def wait_for_item_count(self, item_id: int, minimum: int, *, timeout: float = 15) -> int:
        """Wait for an observed inventory count; unrelated slot changes are allowed."""
        async with asyncio.timeout(timeout):
            while True:
                if self._count_observation_gaps:
                    raise RuntimeError("inventory count is not current after a failed slot update")
                count = self.count_item(item_id)
                if count >= minimum:
                    return count
                self._changed.clear()
                await self._changed.wait()

    @property
    def selected_slot(self) -> int:
        """Hotbar slot for subsequent inputs, updated by server corrections."""
        return self._selected_slot

    @property
    def selection_revision(self) -> int:
        """Advance whenever the local selection changes, including a round trip."""
        return self._selection_revision

    def _apply_selection(self, slot: int, order: int, source: Literal["local", "server"]) -> None:
        if order <= self._applied_selection_order:
            return
        self._applied_selection_order = order
        if slot != self._selected_slot:
            self._selected_slot = slot
            self._selection_revision += 1
            self._selection_source = source
        elif source == "server" or self._selection_source == "default":
            self._selection_source = source
        self._notify_held_item()

    def _observe_selection(self, slot: int) -> None:
        if 0 <= slot < 9:
            self._apply_selection(slot, next(self._selection_order), "server")

    async def _send_selection(self, packet, slot: int) -> None:
        # Reserve order before handing the packet to the transport. Only
        # successful sends and received corrections can advance the selection.
        order = next(self._selection_order)
        await self.packets.send(packet)
        self._apply_selection(slot, order, "local")

    def item_or_empty(self, slot: int):
        """Return the client's item for input, using the last known stack identity.

        A submitted drop key predicts the count until the server describes that
        slot again. Unseen slots use the client's initial empty-hand descriptor.
        """
        item = self._slots.get(slot)
        if slot in self._drop_key_counts:
            return self._stack_at_count(item, self._drop_key_counts[slot])
        return item if item is not None else self._stack_at_count(None, 0)

    def _stack_at_count(self, item, count: int):
        b = self.packets.protocol.binding
        if count == 0:
            return b.SerializedNetworkItemStackDescriptor(
                id=0, stack_size=0, aux_value=0, net_id_variant=None,
                block_runtime_id=0, user_data_buffer=b"",
            )
        return b.SerializedNetworkItemStackDescriptor(
            id=item.id, stack_size=count, aux_value=item.aux_value, net_id_variant=item.net_id_variant,
            block_runtime_id=item.block_runtime_id, user_data_buffer=item.user_data_buffer,
        )

    async def wait_for_item(self, slot: int, timeout: float = 15):
        """Return the current server-reported nonempty inventory slot."""
        event = self._events.setdefault(slot, asyncio.Event())
        async with asyncio.timeout(timeout):
            while True:
                item = self._slots.get(slot)
                if item is not None and int(item.id) != 0:
                    return item
                event.clear()
                await event.wait()

    async def select_hotbar(self, slot: int) -> None:
        """Request a selected hotbar slot; only BDS can confirm the held item."""
        if not 0 <= slot < 9:
            raise ValueError("hotbar slot must be between 0 and 8")
        if self.packets.protocol.number == 2225:
            await self.announce_equipment(slot, self.item_or_empty(slot))
            return
        b = self.packets.protocol.binding
        # BDS 1.26.52 accepted PlayerHotbar when MobEquipment alone did not switch the slot.
        await self._send_selection(b.PlayerHotbarPacket(
            selected_slot=slot,
            container_id=b.ContainerID.INVENTORY,
            should_select_slot=True,
        ), slot)

    async def announce_equipment(self, slot: int, item) -> None:
        """Carry the server-observed item descriptor for a use transaction."""
        b = self.packets.protocol.binding
        await self._send_selection(b.MobEquipmentPacket(
            runtime_id=self.player_id,
            item=item,
            slot=slot,
            selected_slot=slot,
            container_id=b.ContainerID.INVENTORY,
        ), slot)



    def _require_no_automatic_unequip(self, armor_slot: int, inventory_slot: int) -> None:
        endpoints = (("ARMOR_CONTAINER", armor_slot), (None, inventory_slot))
        if any(pending.automatic_unequip for pending in self._requests_for_endpoints(endpoints)):
            raise RuntimeError("an armor transfer slot has an automatic unequip request in flight")

    async def equip_armor(self, source_slot: int, armor_slot: int, *, timeout: float = 15):
        """Swap a player inventory item with armor slot 0=head, 1=chest, 2=legs, 3=feet.

        Return the matching ItemStackResponse; only server-provided response slots
        (or later inventory packets) update the observed inventory and armor state.
        """
        if not 0 <= source_slot <= 255:
            raise ValueError("source slot must fit in a byte")
        if not 0 <= armor_slot < 4:
            raise ValueError("armor slot must be between 0 and 3")
        source = self._slots.get(source_slot)
        if source is None or int(source.id) == 0:
            raise ValueError("source slot has no observed item")
        self._require_no_automatic_unequip(armor_slot, source_slot)

        b = self.packets.protocol.binding
        armor_observed = self._armor_slots.get(armor_slot)
        destination = armor_observed or self.item_or_empty(-1)
        cereal = b.ItemStackRequestCereal

        def slot_info(name, slot, item):
            return cereal.SlotInfoData(
                full_container_name=b.FullContainerName(
                    name=b.ContainerEnumName[name], dynamic_id=None,
                ),
                slot=slot,
                net_id_variant=int(item.net_id_variant or 0),
            )

        source_info = slot_info(
            "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", source_slot, source,
        )
        destination_info = slot_info("ARMOR_CONTAINER", armor_slot, destination)
        # Place into an empty armor slot. Swap when replacing equipped armor.
        placing = int(destination.id) == 0
        action = (
            cereal.PlaceActionData(
                amount=1, source=source_info, destination=destination_info,
            )
            if placing else cereal.SwapActionData(
                source=source_info, destination=destination_info,
            )
        )
        request_id = self._allocate_request_id()
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=[action],
            strings_to_filter=[],
            strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )
        armor_endpoint = _DAMAGE_ARMOR_SLOTS[armor_slot]
        armor_received = False

        def observe(endpoint, item):
            nonlocal armor_received
            if endpoint == armor_endpoint and item is not None:
                armor_received = True

        def apply(response):
            success = response.result.name == "SUCCESS"
            armor_corrected = False
            for container in response.containers or ():
                name = container.full_container_name.name.name
                for slot in container.slots:
                    index = int(slot.slot)
                    if name == "ARMOR_CONTAINER" and index == armor_slot:
                        armor_corrected = True
                        item = source if success else destination
                        current = self._armor_slots.get(index)
                        if current is not armor_observed:
                            updated = _correct_observed_item(b, current, slot)
                        elif not success and int(item.id) == 0 and int(slot.amount) > 0:
                            # Response slots have no item type: do not invent one.
                            updated = None
                        else:
                            updated = _response_item(b, item, item, slot)
                        self._set_armor_slot(index, updated)
                    elif name in (
                        "HOTBAR_CONTAINER", "INVENTORY_CONTAINER",
                        "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                    ) and index == source_slot:
                        item = (source if placing else destination) if success else source
                        current = self._slots.get(index)
                        updated = (_response_item(b, item, item, slot) if current is source
                                   else _correct_observed_item(b, current, slot))
                        self._set_slot(index, updated)
            if success and not armor_corrected and not armor_received:
                # An accepted equip can omit the destination from its verdict.
                self._damage_descriptors.pop(armor_endpoint, None)

        return await self._request_item_stack(request, apply, timeout=timeout, _on_slot_observed=observe)

    def _empty_armor_destination(self) -> int:
        complete = True
        for index in range(_MAIN_INVENTORY_SIZE):
            item = self._slots.get(index)
            if (item is None or index in self._unconfirmed_transfer_slots or index in self._drop_key_counts
                    or self._requests_for_endpoints(((None, index),))):
                complete = False
                continue
            if not item.id and not item.stack_size and not item.net_id_variant:
                return index
            if not item.id or not item.stack_size:
                complete = False
        if complete:
            raise RuntimeError("the main inventory is full")
        raise RuntimeError("no empty main inventory slot is confirmed")

    async def unequip_armor(
        self, armor_slot: int | Literal["head", "chest", "legs", "feet"],
        destination_slot: int | None = None, *, timeout: float = 15,
    ):
        """Take equipped armor into an observed empty inventory slot by default.

        An explicit occupied destination swaps the stacks. Return the matching
        BDS ItemStackResponse and apply its armor and inventory corrections.
        """
        if isinstance(armor_slot, str):
            if armor_slot not in _ARMOR_SLOT_NAMES:
                raise ValueError("armor slot must be head, chest, legs or feet")
            armor_slot = _ARMOR_SLOT_NAMES.index(armor_slot)
        if not 0 <= armor_slot < 4:
            raise ValueError("armor slot must be between 0 and 3")
        automatic = destination_slot is None
        if not automatic and not 0 <= destination_slot <= 255:
            raise ValueError("destination slot must fit in a byte")
        source = self._armor_slots.get(armor_slot)
        if automatic and source is None:
            raise RuntimeError("the armor slot is unobserved")
        if source is None or int(source.id) == 0:
            raise ValueError("armor slot has no observed item")
        armor_endpoint = ("ARMOR_CONTAINER", armor_slot)
        if automatic:
            if not source.net_id_variant or int(source.stack_size) != 1:
                raise RuntimeError("the armor stack has no confirmed identity or single-item amount")
            if self._requests_for_endpoints((armor_endpoint,)):
                raise RuntimeError("the armor slot has an item-stack request in flight")
            destination_slot = self._empty_armor_destination()
        else:
            self._require_no_automatic_unequip(armor_slot, destination_slot)
        destination_observed = self._slots.get(destination_slot)
        destination = destination_observed or self.item_or_empty(destination_slot)
        inventory_endpoint = (None, destination_slot)
        changed = set()
        if automatic:
            original = {
                armor_endpoint: _stack_snapshot(source),
                inventory_endpoint: _stack_snapshot(destination_observed),
            }

        b = self.packets.protocol.binding
        cereal = b.ItemStackRequestCereal

        def slot_info(name, slot, item):
            return cereal.SlotInfoData(
                full_container_name=b.FullContainerName(
                    name=b.ContainerEnumName[name], dynamic_id=None,
                ), slot=slot, net_id_variant=int(item.net_id_variant or 0),
            )

        armor_info = slot_info("ARMOR_CONTAINER", armor_slot, source)
        inventory_info = slot_info(
            "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", destination_slot, destination,
        )
        taking = automatic or int(destination.id) == 0
        action = (
            cereal.TakeActionData(
                amount=1, source=armor_info, destination=inventory_info,
            )
            if taking else cereal.SwapActionData(
                source=armor_info, destination=inventory_info,
            )
        )
        request_id = self._allocate_request_id()
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=[action], strings_to_filter=[],
            strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )

        def at_write():
            pending = self._pending_requests[request_id]
            if any(other is not pending for other in self._requests_for_endpoints(pending.endpoints)):
                raise RuntimeError("an armor transfer slot has an item-stack request in flight")
            armor = self._armor_slots.get(armor_slot)
            inventory = self._slots.get(destination_slot)
            if (destination_slot in self._unconfirmed_transfer_slots or destination_slot in self._drop_key_counts
                    or _stack_snapshot(armor) != original[armor_endpoint]
                    or _stack_snapshot(inventory) != original[inventory_endpoint]):
                raise RuntimeError("an armor transfer slot changed before submission")

        def observe(endpoint, item):
            # A slot can change and return to its original descriptor before the reply.
            if _stack_snapshot(item) != original[endpoint]:
                changed.add(endpoint)

        def apply(response):
            success = response.result.name == "SUCCESS"
            for container in response.containers or ():
                name = container.full_container_name.name.name
                for slot in container.slots:
                    index = int(slot.slot)
                    if name == "ARMOR_CONTAINER" and index == armor_slot:
                        expected = (destination if not taking else self.item_or_empty(-1)
                                    ) if success else source
                        current = self._armor_slots.get(index)
                        if automatic and (armor_endpoint in changed
                                          or _stack_snapshot(current) != original[armor_endpoint]):
                            continue
                        if not automatic and current is not source:
                            updated = _correct_observed_item(b, current, slot)
                        elif int(expected.id) == 0 and int(slot.amount) > 0:
                            # A correction carries count/net ID, not the item type.
                            updated = None
                        else:
                            updated = _response_item(b, expected, expected, slot)
                        self._set_armor_slot(index, updated)
                    elif (name in (
                        "HOTBAR_CONTAINER", "INVENTORY_CONTAINER",
                        "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                    ) and index == destination_slot):
                        expected = source if success else destination
                        current = self._slots.get(index)
                        if automatic and (inventory_endpoint in changed
                                          or _stack_snapshot(current) != original[inventory_endpoint]):
                            continue
                        if not automatic and current is not destination_observed:
                            updated = _correct_observed_item(b, current, slot)
                        elif int(expected.id) == 0 and int(slot.amount) > 0:
                            updated = None
                        else:
                            updated = _response_item(b, expected, expected, slot)
                        self._set_slot(index, updated)

        return await self._request_item_stack(
            request, apply, timeout=timeout, _automatic_unequip=automatic,
            _at_write=at_write if automatic else None, _on_slot_observed=observe if automatic else None,
        )

    async def equip_offhand(self, source_slot: int, *, containers, timeout: float = 15):
        """Move an observed inventory stack to the offhand and return the BDS verdict."""
        return await self._offhand_exchange(source_slot, containers, into_offhand=True, timeout=timeout)

    async def unequip_offhand(self, destination_slot: int, *, containers, amount: int | None = None,
                              timeout: float = 15):
        """Move the observed offhand stack to inventory and return the BDS verdict."""
        if amount is not None and (type(amount) is not int or not 1 <= amount <= 255):
            raise ValueError("amount must be between 1 and 255")
        return await self._offhand_exchange(
            destination_slot, containers, into_offhand=False, timeout=timeout, take_amount=amount,
        )

    async def _offhand_exchange(self, index: int, containers, *, into_offhand: bool, timeout: float,
                                take_amount: int | None = None):
        if type(index) is not int or not 0 <= index < 36:
            raise ValueError("inventory slot must be between 0 and 35")
        window = containers.window
        if window is None or window.container_type != "INVENTORY":
            raise RuntimeError("open the player inventory screen before changing the offhand")

        player_item = self._slots.get(index)
        offhand_item = self._offhand_slot
        for label, item in (("inventory", player_item), ("offhand", offhand_item)):
            if item is None:
                raise RuntimeError(f"the {label} slot is unobserved")
            if item.id and (not item.net_id_variant or not item.stack_size):
                raise RuntimeError(f"the {label} stack has no observed identity or amount")

        b = self.packets.protocol.binding
        cereal = b.ItemStackRequestCereal

        def slot_info(name, slot, item):
            return cereal.SlotInfoData(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName[name], dynamic_id=None),
                slot=slot, net_id_variant=int(item.net_id_variant) if item.id else 0,
            )

        player = slot_info("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", index, player_item)
        # Inventory packets describe the offhand at index 0. Stack requests use slot 1.
        offhand = slot_info("OFFHAND_CONTAINER", 1, offhand_item)
        source, destination = (player, offhand) if into_offhand else (offhand, player)
        source_item, destination_item = (player_item, offhand_item) if into_offhand else (offhand_item, player_item)
        if not source_item.id:
            raise ValueError("the source slot is empty")
        exact_take = take_amount is not None
        if destination_item.id and not exact_take:
            action = cereal.SwapActionData(source=source, destination=destination)
        else:
            amount = int(source_item.stack_size) if take_amount is None else take_amount
            if exact_take and amount > source_item.stack_size:
                raise ValueError("amount exceeds the observed offhand stack")
            if not 1 <= amount <= 255:
                raise ValueError("stack amount must be between 1 and 255 for an offhand transfer")
            transfer = cereal.PlaceActionData if into_offhand else cereal.TakeActionData
            action = transfer(amount=amount, source=source, destination=destination)

        request_id = self._allocate_request_id()
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=[action], strings_to_filter=[], strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )

        expected = (_stack_snapshot(player_item), _stack_snapshot(offhand_item)) if exact_take else None

        def before_send():
            nonlocal player_item, offhand_item
            if containers.window is not window:
                raise RuntimeError("the player inventory window changed before submission")
            current_player, current_offhand = self._slots.get(index), self._offhand_slot
            if exact_take:
                pending = self._pending_requests[request_id]
                if any(other is not pending for other in self._requests_for_endpoints(pending.endpoints)):
                    raise RuntimeError("an offhand transfer slot has an item-stack request in flight")
                if (_stack_snapshot(current_player), _stack_snapshot(current_offhand)) != expected:
                    raise RuntimeError("an offhand transfer slot changed before submission")
                player_item, offhand_item = current_player, current_offhand
            elif current_player is not player_item or current_offhand is not offhand_item:
                raise RuntimeError("an offhand transfer slot changed before submission")
            elif any(pending.submitted and pending.exact_offhand_take
                     for pending in self._requests_for_endpoints({_DAMAGE_OFFHAND_SLOT})):
                raise RuntimeError("an exact offhand take still has an item-stack request in flight")

        def apply(response):
            # The matching verdict still belongs to the player's slots after the screen closes.
            success = response.result.name == "SUCCESS"
            offhand_after = offhand_item if exact_take else player_item
            player_after = player_item if exact_take and player_item.id else offhand_item

            def corrected(original, replacement, slot):
                if not success:
                    return _correct_observed_item(b, original, slot)
                if slot.amount and not replacement.id:
                    return None
                return _response_item(b, replacement, replacement, slot)

            for container in response.containers or ():
                name = container.full_container_name.name.name
                for slot in container.slots:
                    # Preserve typed observations received before the matching verdict.
                    if name == "OFFHAND_CONTAINER" and slot.slot == 1 and self._offhand_slot is offhand_item:
                        self._set_offhand_slot(corrected(offhand_item, offhand_after, slot))
                    elif (name in (
                        "HOTBAR_CONTAINER", "INVENTORY_CONTAINER", "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                    ) and slot.slot == index and self._slots.get(index) is player_item):
                        self._set_slot(index, corrected(player_item, player_after, slot))

        return await self._request_item_stack(
            request, apply, timeout=timeout,
            before_send=None if exact_take else before_send, _at_write=before_send if exact_take else None,
            _exact_offhand_take=exact_take,
        )

    async def move_item(
        self, source_slot: int, destination_slot: int, amount: int = 1, *, timeout: float = 15, _queued: bool = False,
        _on_confirmed=None, _expected_slots: tuple[HeldItemStack, HeldItemStack] | None = None,
        _window_is_current: Callable[[], bool] | None = None, _on_unconfirmed: Callable[[], None] | None = None,
    ):
        """Place part or all of an observed stack in another player inventory slot.

        Return the matching BDS ItemStackResponse; only its corrections or later
        server inventory packets change observed counts and stack IDs.
        """
        if not 0 <= source_slot <= 255 or not 0 <= destination_slot <= 255:
            raise ValueError("inventory slots must fit in a byte")
        if not 1 <= amount <= 255:
            raise ValueError("amount must be between 1 and 255")
        endpoints = frozenset(((None, source_slot), (None, destination_slot)))
        if _expected_slots is not None and self._requests_for_endpoints(endpoints):
            return None
        source = self._slots.get(source_slot)
        if source is None or int(source.id) == 0:
            raise ValueError("source slot has no observed item")
        destination_observed = self._slots.get(destination_slot)
        destination = self.item_or_empty(destination_slot)

        b = self.packets.protocol.binding
        cereal = b.ItemStackRequestCereal

        def slot_info(slot, item):
            return cereal.SlotInfoData(
                full_container_name=b.FullContainerName(
                    name=b.ContainerEnumName.COMBINED_HOTBAR_AND_INVENTORY_CONTAINER,
                    dynamic_id=None,
                ),
                slot=slot, net_id_variant=int(item.net_id_variant or 0),
            )

        request_id = self._allocate_request_id()
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=[cereal.PlaceActionData(
                amount=amount,
                source=slot_info(source_slot, source),
                destination=slot_info(destination_slot, destination),
            )],
            strings_to_filter=[],
            strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )

        if _expected_slots is not None:
            return await self._move_range_step(
                request, source_slot, destination_slot, amount, timeout=timeout, expected=_expected_slots,
                endpoints=endpoints, window_is_current=_window_is_current, on_unconfirmed=_on_unconfirmed,
            )

        def apply(response):
            for container in response.containers or ():
                if container.full_container_name.name.name not in (
                    "HOTBAR_CONTAINER", "INVENTORY_CONTAINER",
                    "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                ):
                    continue
                for slot in container.slots:
                    index = int(slot.slot)
                    if index not in (source_slot, destination_slot):
                        continue
                    current = self._slots.get(index)
                    original = source if index == source_slot else destination_observed
                    if current is not original:
                        self._set_slot(index, _correct_observed_item(b, current, slot))
                        continue
                    if index == source_slot:
                        before = fallback = source
                    else:
                        before = destination
                        fallback = source if response.result.name == "SUCCESS" else destination
                    updated = (
                        None if int(slot.amount) > 0
                        and int(before.id) == int(fallback.id) == 0
                        else _response_item(b, before, fallback, slot)
                    )
                    self._set_slot(index, updated)
            if _on_confirmed is not None and response.result.name == "SUCCESS":
                _on_confirmed()

        return await self._request_item_stack(request, apply, timeout=timeout, queued=_queued)

    async def _move_range_step(self, request, source, destination, amount, *, timeout, expected, endpoints,
                               window_is_current, on_unconfirmed):
        """Protect the two range endpoints from unrelated observations and late verdicts."""
        b = self.packets.protocol.binding
        request_id = int(request.client_request_id.raw_id)
        originals = {}
        observed_changes = set()
        observed_counts = {}

        def at_write():
            if not window_is_current():
                raise _InventoryMoveNotSubmitted("window_changed")
            own_request = self._pending_requests[request_id]
            if any(pending is not own_request for pending in self._requests_for_endpoints(endpoints)):
                raise _InventoryMoveNotSubmitted("unconfirmed")
            slots = (source, destination)
            if (self._unconfirmed_transfer_slots.intersection(slots)
                    or tuple(self._observed_stack(index) for index in slots) != expected
                    or tuple(_stack_snapshot(self.item_or_empty(index)) for index in slots) != expected):
                raise RuntimeError("inventory transfer slots changed before submission")
            originals.update((index, self._slots[index]) for index in slots)

        def observe(endpoint, item):
            index = endpoint[1]
            before, current = _stack_snapshot(originals[index]), _stack_snapshot(item)
            if current == before:
                return
            observed_changes.add(index)
            if current is None or not before.net_id or current.net_id != before.net_id:
                return
            if replace(before, count=current.count) != current or observed_counts.setdefault(index, current.count) != current.count:
                on_unconfirmed()

        def apply(response):
            if not window_is_current() or response.result.name != "SUCCESS":
                return
            receipts = {source: [], destination: []}
            for group in response.containers or ():
                if group.full_container_name.name.name in _PLAYER_CONTAINERS:
                    for slot in group.slots:
                        if slot.slot in receipts:
                            receipts[slot.slot].append(slot)
            if any(len(items) != 1 for items in receipts.values()):
                self._unconfirmed_transfer_slots.update(receipts)
                on_unconfirmed()
                return
            moved = expected[0].count - int(receipts[source][0].amount)
            if not 0 < moved <= amount or moved != int(receipts[destination][0].amount) - expected[1].count:
                self._unconfirmed_transfer_slots.update(receipts)
                on_unconfirmed()
                return
            for index, (slot,) in receipts.items():
                original = originals[index]
                observed = observed_counts.get(index)
                if observed is not None and (observed != int(slot.amount) or (
                    slot.item_stack_net_id is not None
                    and int(slot.item_stack_net_id.raw_id) != int(original.net_id_variant or 0)
                )):
                    on_unconfirmed()
                current = self._observed_stack(index)
                corrected = _response_item(b, original, originals[source], slot)
                after = _stack_snapshot(corrected)
                if index not in observed_changes and current == _stack_snapshot(original):
                    self._set_slot(index, corrected)
                elif current is not None and after.net_id and current.net_id == after.net_id and current != after:
                    on_unconfirmed()

        return await self._request_item_stack(
            request, apply, timeout=timeout, _endpoints=endpoints, _range_step=True,
            _on_unconfirmed=on_unconfirmed, _on_slot_observed=observe, _at_write=at_write,
        )

    async def swap_items(
        self, source_slot: int, destination_slot: int, *, timeout: float = 15, _queued: bool = False,
        _on_confirmed=None,
    ):
        """Swap two inventory slots; reconcile only the matching BDS response slots.

        Both occupied and empty destinations are legal. A response without slot
        corrections is a verdict, not an independently observed item swap.
        """
        if not 0 <= source_slot <= 255 or not 0 <= destination_slot <= 255:
            raise ValueError("inventory slots must fit in a byte")

        source_observed = self._slots.get(source_slot)
        destination_observed = self._slots.get(destination_slot)
        source = self.item_or_empty(source_slot)
        destination = self.item_or_empty(destination_slot)
        b = self.packets.protocol.binding
        cereal = b.ItemStackRequestCereal

        def slot_info(index, item):
            return cereal.SlotInfoData(
                full_container_name=b.FullContainerName(
                    name=b.ContainerEnumName.COMBINED_HOTBAR_AND_INVENTORY_CONTAINER,
                    dynamic_id=None,
                ),
                slot=index, net_id_variant=int(item.net_id_variant or 0),
            )

        request_id = self._allocate_request_id()
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=[cereal.SwapActionData(
                source=slot_info(source_slot, source),
                destination=slot_info(destination_slot, destination),
            )],
            strings_to_filter=[],
            strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )

        def apply(response):
            success = response.result.name == "SUCCESS"
            for container in response.containers or ():
                if container.full_container_name.name.name not in (
                    "HOTBAR_CONTAINER", "INVENTORY_CONTAINER",
                    "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                ):
                    continue
                for slot in container.slots:
                    index = int(slot.slot)
                    if index not in (source_slot, destination_slot):
                        continue
                    if index == source_slot:
                        item = destination if success else source
                    elif index == destination_slot:
                        item = source if success else destination
                    else:
                        continue
                    # A newer InventorySlot/Content update may supersede the
                    # captured identity. A response has amounts/net IDs only.
                    original = (
                        source_observed if index == source_slot else destination_observed
                    )
                    current = self._slots.get(index)
                    if current is not original:
                        self._set_slot(index, _correct_observed_item(b, current, slot))
                        continue
                    self._set_slot(
                        index, None if int(slot.amount) > 0
                        and (item is None or int(item.id) == 0)
                        else _response_item(b, item, item, slot)
                    )
            if _on_confirmed is not None and success:
                _on_confirmed()

        return await self._request_item_stack(request, apply, timeout=timeout, queued=_queued)

    async def drop_item(self, slot: int, amount: int = 1, *, timeout: float = 15, _queued: bool = False):
        """Drop part of an observed inventory stack; return the matching BDS verdict.

        Inventory state changes only through server response slots or later
        inventory packets. A successful reply is not proof of a visible drop.
        """
        if not 0 <= slot <= 255:
            raise ValueError("slot must fit in a byte")
        if not 1 <= amount <= 255:
            raise ValueError("amount must be between 1 and 255")
        source = self._slots.get(slot)
        if source is None or int(source.id) == 0:
            raise ValueError("slot has no observed item")
        source_origin = self._drop_origins.get(slot, source)

        b = self.packets.protocol.binding
        cereal = b.ItemStackRequestCereal
        request_id = self._allocate_request_id()
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=[cereal.DropActionData(
                amount=amount,
                source=cereal.SlotInfoData(
                    full_container_name=b.FullContainerName(
                        name=b.ContainerEnumName.COMBINED_HOTBAR_AND_INVENTORY_CONTAINER,
                        dynamic_id=None,
                    ),
                    slot=slot, net_id_variant=int(source.net_id_variant or 0),
                ),
                randomly=False,
            )],
            strings_to_filter=[],
            strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )

        def apply(response):
            for container in response.containers or ():
                if container.full_container_name.name.name not in (
                    "HOTBAR_CONTAINER", "INVENTORY_CONTAINER",
                    "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                ):
                    continue
                for updated in container.slots:
                    if int(updated.slot) != slot:
                        continue
                    current = self._slots.get(slot)
                    # A prior drop can change the net ID without replacing the stack.
                    same_stack = current is not None and self._drop_origins.get(slot, current) is source_origin
                    item = (_response_item(b, current, current, updated) if same_stack
                            else _correct_observed_item(b, current, updated))
                    origin = source_origin if same_stack and int(item.id) != 0 else None
                    self._set_slot(slot, item, drop_origin=origin)

        return await self._request_item_stack(request, apply, timeout=timeout, queued=_queued)

    async def drop_selected_item(self, whole_stack: bool, containers) -> None:
        """Submit a world drop and maintain the client's drop-key prediction."""
        async with self._drop_key_lock:
            if containers.window is not None:
                raise RuntimeError("close the current window before using the drop key")
            await self._drop_hotbar_stack(whole_stack)

    async def _drop_hotbar_stack(self, whole_stack: bool) -> None:
        slot = self.selected_slot
        observed = self._slots.get(slot)
        if observed is None:
            return
        source = self.item_or_empty(slot)
        if int(source.id) == 0 or int(source.stack_size) <= 0:
            return
        b = self.packets.protocol.binding
        count = int(source.stack_size)
        amount = count if whole_stack else 1
        empty = self._stack_at_count(None, 0)
        remaining = self._stack_at_count(source, count - amount)
        await self.packets.send(b.InventoryTransactionPacket(
            legacy_request_id=b.ItemStackLegacyRequestId(raw_id=0),
            legacy_set_item_slots=None,
            variant_transaction=b.NormalTransactionData(transaction=b.InventoryTransaction(actions=[
                b.InventoryAction(
                    source=b.InventorySource(type=b.InventorySourceType.WORLD_INTERACTION, container_id=None,
                                             flags=b.InventorySource.InventorySourceFlags.NO_FLAG),
                    slot=0, from_item_descriptor=empty,
                    to_item_descriptor=self._stack_at_count(source, amount),
                ),
                b.InventoryAction(
                    source=b.InventorySource(type=b.InventorySourceType.CONTAINER_INVENTORY,
                                             container_id=b.ContainerID.INVENTORY, flags=None),
                    slot=slot, from_item_descriptor=source,
                    to_item_descriptor=remaining,
                ),
            ])),
        ))
        # Accepted key drops may never produce an inventory-slot echo. A packet
        # received during submission already supersedes the local prediction.
        if self._slots.get(slot) is observed:
            self._drop_key_counts[slot] = count - amount
            if slot == self._selected_slot:
                self._notify_held_item()

    async def use_item(self, movement, *, before_send=None) -> None:
        """Submit one use-in-air transaction; server effects are observed separately."""
        x, y, z = await movement.wait_for_position_sync()
        b = self.packets.protocol.binding
        transaction = b.ItemUseInventoryTransaction(
            transaction=b.InventoryTransaction(actions=[]),
            action_type=b.ItemUseInventoryTransaction.ActionType.USE,
            trigger_type=b.ItemUseInventoryTransaction.TriggerType.PLAYER_INPUT,
            pos=b.BlockPos(x=0, y=0, z=0),
            face=255,
            slot=self.selected_slot,
            hand=b.HandSlot.MAINHAND,
            item=self.item_or_empty(self.selected_slot),
            from_pos=b.Vec3(x=x, y=y, z=z),
            click_pos=b.Vec3(x=0.0, y=0.0, z=0.0),
            target_block_id=0,
            client_predicted_result=b.ItemUseInventoryTransaction.PredictedResult.SUCCESS,
            client_cooldown_state=b.ItemUseInventoryTransaction.ClientCooldownState.OFF,
        )
        # Send item use in air as a standalone InventoryTransactionPacket.
        packet = b.InventoryTransactionPacket(
            legacy_request_id=b.ItemStackLegacyRequestId(raw_id=0),
            legacy_set_item_slots=None,
            variant_transaction=transaction,
        )
        if before_send is None:
            await self.packets.send(packet)
        else:
            await self.packets.send(packet, _before_send=before_send)

    def close(self):
        self._end_requests()
        self._qa_requests.clear()
        self._drop_origins.clear()
        if self._unsubscribe_reader_end is not None:
            self._unsubscribe_reader_end()
            self._unsubscribe_reader_end = None
        self._unsubscribe()

    async def _wait_for_request_sends(self):
        if self._request_sends:
            await asyncio.gather(*self._request_sends, return_exceptions=True)
