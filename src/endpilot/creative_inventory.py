"""Server-issued creative catalog and the local player's creative transactions."""

import asyncio
from dataclasses import dataclass, replace
from typing import Literal

from .inventory import _correct_observed_item, _response_item
from .item_user_data import read_item_user_data


_INVENTORY = "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"
_PLAYER_CONTAINERS = (_INVENTORY, "HOTBAR_CONTAINER", "INVENTORY_CONTAINER")


@dataclass(frozen=True, slots=True)
class CreativeItemDescriptor:
    item_id: int
    name: str | None
    count: int
    aux_value: int
    block_runtime_id: int
    user_data_buffer: bytes

    @property
    def nbt(self) -> dict:
        """Decode a fresh copy of the NBT carried in the server's item data."""
        return read_item_user_data(self.user_data_buffer)


@dataclass(frozen=True, slots=True)
class CreativeEntry:
    net_id: int
    group_index: int | None
    item: CreativeItemDescriptor


@dataclass(frozen=True, slots=True)
class CreativeGroup:
    category: str
    name: str
    icon: CreativeItemDescriptor


@dataclass(frozen=True, slots=True)
class CreativeCatalog:
    groups: tuple[CreativeGroup, ...]
    entries: tuple[CreativeEntry, ...]


@dataclass(frozen=True, slots=True)
class CreativeRequestProgress:
    slot: int
    action: Literal["take", "destroy"]
    request_id: int
    submitted: bool | None
    verdict: object | None = None


@dataclass(frozen=True, slots=True)
class CreativeClearSlot:
    slot: int
    status: Literal["pending", "unknown", "already_empty", "cleared", "rejected", "unconfirmed"]
    verdict: object | None = None


@dataclass(frozen=True, slots=True)
class CreativeClearResult:
    slots: tuple[CreativeClearSlot, ...]

    @property
    def complete(self) -> bool:
        return all(slot.status in ("already_empty", "cleared") for slot in self.slots)


@dataclass(frozen=True, slots=True)
class CreativeOperationProgress:
    """Confirmed verdicts and submission state when a composite action raises."""

    requests: tuple[CreativeRequestProgress, ...]
    clear_result: CreativeClearResult | None = None


class _Progress:
    def __init__(self):
        self.requests: list[CreativeRequestProgress] = []
        self.interrupted = False

    def attach(self, error, clear_result=None):
        self.interrupted = True
        error.creative_progress = CreativeOperationProgress(tuple(self.requests), clear_result)


def creative_packet_ids(binding):
    return (
        binding.CreativeContentPacket.ID, binding.SetPlayerGameTypePacket.ID,
        binding.UpdatePlayerGameTypePacket.ID, binding.SetDefaultGameTypePacket.ID,
    )


def _stack_key(item_id, aux_value, block_runtime_id, user_data_buffer):
    return int(item_id), int(aux_value), int(block_runtime_id) & 0xFFFFFFFF, bytes(user_data_buffer)


class CreativeInventory:
    def __init__(self, packets, inventory, containers, unique_id, item_names, initial_packets, *, on_game_mode=None):
        self._packets = packets
        self._inventory = inventory
        self._containers = containers
        self._unique_id = unique_id
        self._item_names = item_names
        self._default_mode = None
        self._own_mode = None
        self._on_game_mode = on_game_mode
        self._content = None
        self.catalog: CreativeCatalog | None = None
        self._entries: dict[int, CreativeEntry] = {}
        self._binding = packets.protocol.binding
        self._packet_ids = creative_packet_ids(self._binding)
        for packet in initial_packets:
            self._apply(packet)
        self._unsubscribe = packets.subscribe_raw(self._on_packet)
        subscribe_reader_end = getattr(packets, "subscribe_reader_end", None)
        self._unsubscribe_reader_end = (
            None if subscribe_reader_end is None else subscribe_reader_end(self._reset)
        )

    @property
    def game_mode(self):
        mode = self._own_mode
        if mode == self._binding.GameType.DEFAULT:
            mode = self._default_mode
        if mode in (self._binding.GameType.DEFAULT, self._binding.GameType.UNDEFINED):
            return None
        return mode

    def _item(self, descriptor):
        item_id = int(descriptor.id)
        return CreativeItemDescriptor(
            item_id, self._item_names.get(item_id),
            int(descriptor.stack_size), int(descriptor.aux_value),
            int(descriptor.block_runtime_id), bytes(descriptor.user_data_buffer),
        )

    def _load(self, packet):
        entries = tuple(
            CreativeEntry(int(entry.creative_item_net_id), getattr(entry, "group_index", None),
                          self._item(entry.item_descriptor))
            for entry in packet.entries
        )
        self.catalog = CreativeCatalog(
            tuple(CreativeGroup(group.creative_item_category.name, group.name, self._item(group.icon))
                  for group in getattr(packet, "groups", ())),
            entries,
        )
        self._entries = {entry.net_id: entry for entry in entries}

    def _apply(self, packet):
        b = self._binding
        if isinstance(packet, b.StartGamePacket):
            self._own_mode = packet.entity_game_type
            self._default_mode = packet.settings.game_type
        elif isinstance(packet, b.SetPlayerGameTypePacket):
            self._own_mode = packet.player_game_type
        elif isinstance(packet, b.UpdatePlayerGameTypePacket):
            if int(packet.target_player) != self._unique_id:
                return
            self._own_mode = packet.player_game_type
        elif isinstance(packet, b.SetDefaultGameTypePacket):
            self._default_mode = packet.default_game_type
        elif isinstance(packet, b.CreativeContentPacket):
            self._content = packet
            self._load(packet)
            return
        if self._on_game_mode is not None:
            self._on_game_mode(self.game_mode)

    def _on_packet(self, raw):
        b = self._binding
        if raw.packet_id == b.DisconnectPacket.ID:
            self._reset()
        elif raw.packet_id == b.ItemRegistryPacket.ID and self._content is not None:
            self._load(self._content)
        elif raw.packet_id in self._packet_ids:
            self._apply(self._packets.protocol.decode(raw))

    def _reset(self, _cause=None):
        self._own_mode = self._default_mode = None
        self._content = self.catalog = None
        self._entries.clear()

    def close(self):
        self._unsubscribe()
        if self._unsubscribe_reader_end is not None:
            self._unsubscribe_reader_end()
        self._reset()

    def _window(self):
        if self._inventory._request_stream_ended:
            raise ConnectionError("item-stack request stream is closed")
        if self.game_mode != self._binding.GameType.CREATIVE:
            raise PermissionError("the server has not granted creative mode to this player")
        window = self._containers.window
        if window is None or window.container_type != "INVENTORY":
            raise RuntimeError("open the player inventory screen before a creative action")
        return window

    def _entry(self, entry: CreativeEntry):
        if self._entries.get(entry.net_id) is not entry:
            raise ValueError("entry must come from the current creative catalog")
        return entry.item

    @staticmethod
    def _check_slot(slot):
        if type(slot) is not int or not 0 <= slot < 36:
            raise ValueError("inventory slot must be between 0 and 35")

    @staticmethod
    def _check_amount(amount):
        if type(amount) is not int or not 1 <= amount <= 255:
            raise ValueError("amount must be between 1 and 255")

    def _observed(self, slot):
        item = self._inventory.slots.get(slot)
        if item is None:
            raise RuntimeError(f"inventory slot {slot} has not been observed")
        return item

    def _require_window(self, window):
        if self._window() is not window:
            raise RuntimeError("the creative action's inventory window changed")

    def _entry_for_action(self, net_id):
        self._window()
        entry = self._entries.get(net_id) if type(net_id) is int else None
        if entry is None:
            raise ValueError("ID must come from the current creative catalog")
        return entry

    async def take_id(self, net_id, slot, amount, *, timeout=15):
        entry = self._entry_for_action(net_id)
        return await self.take(entry, slot, amount, timeout=timeout, queued=True)

    async def take(self, entry, slot, amount, *, timeout, queued=False, progress=None):
        window = self._window()
        self._check_slot(slot)
        self._check_amount(amount)
        item = self._entry(entry)
        observed = self._observed(slot)
        b = self._binding
        cereal = b.ItemStackRequestCereal
        request_id = self._inventory._allocate_request_id()
        output = b.SerializedNetworkItemStackDescriptor(
            id=item.item_id, stack_size=amount, aux_value=item.aux_value, net_id_variant=None,
            block_runtime_id=item.block_runtime_id & 0xFFFFFFFF, user_data_buffer=item.user_data_buffer,
        )
        actions = [
            cereal.CraftCreativeActionData(creative_item_net_id=entry.net_id, num_crafts=1),
            cereal.PlaceActionData(
                amount=amount, source=self._slot_info("CREATED_OUTPUT_CONTAINER", 50, request_id),
                destination=self._slot_info(_INVENTORY, slot, int(observed.net_id_variant or 0)),
            ),
        ]
        return await self._request(request_id, window, slot, observed, output, actions, entry,
                                   timeout=timeout, queued=queued, progress=progress)

    async def destroy(self, slot, amount, *, timeout, queued=False, progress=None):
        window = self._window()
        self._check_slot(slot)
        self._check_amount(amount)
        observed = self._inventory.slots.get(slot)
        if observed is None:
            raise ValueError(f"inventory slot {slot} has not been observed")
        if not observed.id or amount > int(observed.stack_size):
            raise ValueError("the observed inventory stack has fewer items than requested")
        b = self._binding
        request_id = self._inventory._allocate_request_id()
        action = b.ItemStackRequestCereal.DestroyActionData(
            amount=amount, source=self._slot_info(_INVENTORY, slot, int(observed.net_id_variant or 0)),
        )
        return await self._request(request_id, window, slot, observed, observed, [action], None,
                                   timeout=timeout, queued=queued, progress=progress)

    async def clear(self, slot, *, timeout):
        self._window()
        self._check_slot(slot)
        observed = self._observed(slot)
        if observed.id:
            return await self.destroy(slot, int(observed.stack_size), timeout=timeout)
        return None

    def _clear_verdict(self, slot, reply):
        if reply.result.name != "SUCCESS":
            return CreativeClearSlot(slot, "rejected", reply)
        observed = self._inventory.slots.get(slot)
        return CreativeClearSlot(slot, "cleared" if observed is not None and not observed.id
                                 else "unconfirmed", reply)

    def _clear_result(self, results):
        observed = self._inventory.slots
        return CreativeClearResult(tuple(
            replace(outcome, status="unconfirmed")
            if outcome.status in ("cleared", "already_empty")
            and (observed.get(outcome.slot) is None or observed[outcome.slot].id)
            else outcome
            for outcome in results.values()
        ))

    async def clear_inventory(self, slots, *, timeout):
        window = self._window()
        slots = tuple(range(36) if slots is None else slots)
        for slot in slots:
            self._check_slot(slot)
        results = {slot: CreativeClearSlot(slot, "pending") for slot in slots}
        progress = _Progress()
        try:
            async with asyncio.timeout(timeout):
                for slot in results:
                    self._require_window(window)
                    observed = self._inventory.slots.get(slot)
                    if observed is None:
                        results[slot] = CreativeClearSlot(slot, "unknown")
                    elif not observed.id:
                        results[slot] = CreativeClearSlot(slot, "already_empty")
                    else:
                        reply = await self.destroy(slot, int(observed.stack_size), timeout=None, progress=progress)
                        results[slot] = self._clear_verdict(slot, reply)
        except (Exception, asyncio.CancelledError) as error:
            for request in progress.requests:
                if request.verdict is not None and results[request.slot].status == "pending":
                    results[request.slot] = self._clear_verdict(request.slot, request.verdict)
            progress.attach(error, self._clear_result(results))
            raise
        return self._clear_result(results)

    async def fill(self, slot, entry, count, *, timeout):
        window = self._window()
        self._check_slot(slot)
        self._check_amount(count)
        item = self._entry(entry)
        current = self._observed(slot)
        same = _stack_key(current.id, current.aux_value, current.block_runtime_id, current.user_data_buffer) == (
            _stack_key(item.item_id, item.aux_value, item.block_runtime_id, item.user_data_buffer)
        )
        progress = _Progress()
        try:
            async with asyncio.timeout(timeout):
                if same:
                    difference = count - int(current.stack_size)
                    if difference > 0:
                        return (await self.take(entry, slot, difference, timeout=None, progress=progress),)
                    if difference < 0:
                        return (await self.destroy(slot, -difference, timeout=None, progress=progress),)
                    return ()
                replies = ()
                if current.id:
                    reply = await self.destroy(slot, int(current.stack_size), timeout=None, progress=progress)
                    replies = (reply,)
                    if reply.result.name != "SUCCESS":
                        return replies
                    self._require_window(window)
                    if self._observed(slot).id:
                        raise RuntimeError(f"inventory slot {slot} was not confirmed empty after destruction")
                return (*replies, await self.take(entry, slot, count, timeout=None, progress=progress))
        except (Exception, asyncio.CancelledError) as error:
            progress.attach(error)
            raise

    def _slot_info(self, container, slot, net_id):
        b = self._binding
        return b.ItemStackRequestCereal.SlotInfoData(
            full_container_name=b.FullContainerName(name=b.ContainerEnumName[container], dynamic_id=None),
            slot=slot, net_id_variant=net_id,
        )

    async def _request(self, request_id, window, index, original, output, actions, entry, *,
                       timeout, queued, progress):
        b = self._binding
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id), actions=actions,
            strings_to_filter=[], strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )
        if progress is not None:
            step = len(progress.requests)
            progress.requests.append(CreativeRequestProgress(
                index, "destroy" if entry is None else "take", request_id, False,
            ))

        def update_progress(submitted, verdict=None):
            if progress is not None:
                previous = progress.requests[step]
                progress.requests[step] = replace(
                    previous, submitted=submitted, verdict=previous.verdict if verdict is None else verdict,
                )

        def before_send():
            if progress is not None and progress.interrupted:
                raise RuntimeError("creative action ended before submission")
            self._require_window(window)
            if entry is not None:
                self._entry(entry)
            if self._inventory.slots.get(index) is not original:
                raise RuntimeError("the creative action's slot changed before submission")
            update_progress(None)

        def apply(response):
            update_progress(True, response)
            for container in response.containers or ():
                if container.full_container_name.name.name not in _PLAYER_CONTAINERS:
                    continue
                for slot in container.slots:
                    if int(slot.slot) != index:
                        continue
                    current = self._inventory.slots.get(index)
                    if current is original:
                        item = output if response.result.name == "SUCCESS" else original
                        if int(slot.amount) > 0 and (item is None or not item.id):
                            self._inventory._set_slot(index, None)
                        else:
                            self._inventory._set_slot(index, _response_item(b, item, item, slot))
                    elif (current is None or not current.id or int(slot.amount) > 0):
                        corrected = _correct_observed_item(b, current, slot)
                        if corrected is not None:
                            self._inventory._set_slot(index, corrected)

        return await self._inventory._request_item_stack(
            request, apply, timeout=timeout, queued=queued, before_send=before_send,
            after_send=lambda: update_progress(True),
        )
