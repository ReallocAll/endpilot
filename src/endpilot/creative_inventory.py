"""Server-issued creative catalog and the local player's creative transactions."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from bedrock_protocol import GameType

from .compression import PacketConnection
from .inventory import _ARMOR_SLOT_NAMES, _CraftingGridEndpoint, _correct_observed_item, _response_item, _stack_snapshot
from .item_user_data import read_item_user_data


_INVENTORY = "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"
_PLAYER_CONTAINERS = (_INVENTORY, "HOTBAR_CONTAINER", "INVENTORY_CONTAINER")
_ARMOR = "ARMOR_CONTAINER"
_IRON_ARMOR_ITEMS = ("minecraft:iron_helmet", "minecraft:iron_chestplate", "minecraft:iron_leggings", "minecraft:iron_boots")


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
    action: Literal["take", "destroy", "destroy_grid"]
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
class CreativeArmorClearSlot:
    slot: Literal["head", "chest", "legs", "feet"]
    status: Literal["pending", "unknown", "already_empty", "cleared", "rejected", "unconfirmed"]
    staging_slot: int | None = None
    unequip: CreativeRequestProgress | None = None
    destroy: CreativeRequestProgress | None = None
    staged: Literal["confirmed", "unknown"] | None = None


@dataclass(frozen=True, slots=True)
class CreativeArmorClearResult(CreativeClearResult):
    armor: tuple[CreativeArmorClearSlot, ...]

    @property
    def complete(self) -> bool:
        return super(CreativeArmorClearResult, self).complete and all(
            slot.status in ("already_empty", "cleared") for slot in self.armor
        )


@dataclass(frozen=True, slots=True)
class CreativeCraftingGridClearResult(CreativeArmorClearResult):
    crafting_grid: tuple[CreativeClearSlot, ...]

    @property
    def complete(self) -> bool:
        return super(CreativeCraftingGridClearResult, self).complete and all(
            slot.status in ("already_empty", "cleared") for slot in self.crafting_grid
        )


@dataclass(frozen=True, slots=True)
class CreativeOperationProgress:
    """Confirmed verdicts and submission state when a composite action raises."""

    requests: tuple[CreativeRequestProgress, ...]
    clear_result: CreativeClearResult | None = None


class _Progress:
    def __init__(self):
        self.requests: list[CreativeRequestProgress] = []
        self.interrupted = False

    def start(self, slot, action, request_id):
        step = len(self.requests)
        self.requests.append(CreativeRequestProgress(slot, action, request_id, False))

        def update(submitted, verdict=None):
            previous = self.requests[step]
            self.requests[step] = replace(
                previous, submitted=submitted, verdict=previous.verdict if verdict is None else verdict,
            )

        return update

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


def _player_slot_corrections(verdict, slot):
    return [
        entry
        for container in verdict.containers or ()
        if (container.full_container_name.name.name in _PLAYER_CONTAINERS
            and container.full_container_name.dynamic_id is None)
        for entry in container.slots
        if int(entry.slot) == slot
    ]


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
        self._game_mode_observers: dict[object, Callable[[GameType | None], None]] = {}
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
    def game_mode(self) -> GameType | None:
        mode = self._own_mode
        if mode == self._binding.GameType.DEFAULT:
            mode = self._default_mode
        if mode in (self._binding.GameType.DEFAULT, self._binding.GameType.UNDEFINED):
            return None
        return mode

    def subscribe_game_mode(self, callback: Callable[[GameType | None], None]) -> Callable[[], None]:
        observers = self._game_mode_observers
        token = object()
        observers[token] = callback

        def unsubscribe() -> None:
            observers.pop(token, None)

        return unsubscribe

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
            previous_mode = self.game_mode
            self._apply(self._packets.protocol.decode(raw))
            mode = self.game_mode
            if mode != previous_mode:
                PacketConnection._notify(self._game_mode_observers.values(), mode)

    def _reset(self, _cause=None):
        self._own_mode = self._default_mode = None
        self._content = self.catalog = None
        self._entries.clear()
        self._game_mode_observers.clear()

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

    async def equip_armor(self, entry, slot, *, timeout):
        window = self._window()
        if type(slot) is not int or not 0 <= slot < len(_ARMOR_SLOT_NAMES):
            raise ValueError("armor slot must be between 0 and 3")
        item = self._entry(entry)
        if item.name != _IRON_ARMOR_ITEMS[slot]:
            raise ValueError("creative catalog item is not supported in the selected armor slot")
        if not self._armor_empty(slot):
            raise ValueError("the armor slot must be observed empty")

        b = self._binding
        cereal = b.ItemStackRequestCereal
        request_id = self._inventory._allocate_request_id()
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=[cereal.CraftCreativeActionData(creative_item_net_id=entry.net_id, num_crafts=1),
                     cereal.PlaceActionData(amount=1, source=self._slot_info("CREATED_OUTPUT_CONTAINER", 50, request_id),
                                            destination=self._slot_info(_ARMOR, slot, 0))],
            strings_to_filter=[], strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )
        changed = False

        def at_write():
            self._require_window(window)
            self._entry(entry)
            if not self._armor_empty(slot):
                raise RuntimeError("the creative armor slot changed before submission")
            pending = self._inventory._pending_requests[request_id]
            if any(other is not pending and other.submitted
                   for other in self._inventory._requests_for_endpoints(pending.endpoints)):
                raise RuntimeError("the creative armor slot has an item-stack request in flight")

        def uncertain():
            nonlocal changed
            changed = True

        def observe(_, observed):
            if observed is None or observed.id or observed.stack_size or observed.net_id_variant:
                uncertain()

        def apply(response):
            if (response.result.name != "SUCCESS" or changed or self._containers.window is not window
                    or not self._armor_empty(slot)):
                return
            corrections = [correction for container in response.containers or ()
                           if container.full_container_name.name.name == _ARMOR
                           and container.full_container_name.dynamic_id is None
                           for correction in container.slots if int(correction.slot) == slot]
            if len(corrections) != 1 or int(corrections[0].amount) != 1:
                return
            net_id = corrections[0].item_stack_net_id
            if net_id is None or int(net_id.raw_id) <= 0:
                return
            equipped = b.SerializedNetworkItemStackDescriptor(
                id=item.item_id, stack_size=1, aux_value=item.aux_value, net_id_variant=int(net_id.raw_id),
                block_runtime_id=item.block_runtime_id & 0xFFFFFFFF, user_data_buffer=item.user_data_buffer,
            )
            self._inventory._set_armor_slot(slot, equipped)

        return await self._inventory._request_item_stack(
            request, apply, timeout=timeout, _at_write=at_write, _on_slot_observed=observe,
            _on_unconfirmed=uncertain,
        )

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

    async def clear_inventory(self, slots, *, timeout, include_armor=False, crafting=None):
        window = self._window()
        slots = tuple(range(36) if slots is None else slots)
        for slot in slots:
            self._check_slot(slot)
        results = {slot: CreativeClearSlot(slot, "pending") for slot in slots}
        armor = [CreativeArmorClearSlot(slot, "pending") for slot in _ARMOR_SLOT_NAMES] if include_armor else []
        grid = {slot: CreativeClearSlot(slot, "pending") for slot in range(28, 32)} if crafting is not None else {}
        progress = _Progress()
        main_finished = False

        def result():
            main = self._clear_result(results)
            if not include_armor and crafting is None:
                return main
            armor_result = tuple(
                replace(outcome, status="unconfirmed")
                if outcome.status in ("cleared", "already_empty") and not self._armor_empty(index)
                else outcome
                for index, outcome in enumerate(armor)
            )
            if crafting is None:
                return CreativeArmorClearResult(main.slots, armor_result)
            return CreativeCraftingGridClearResult(main.slots, armor_result, tuple(
                replace(outcome, status="unconfirmed")
                if outcome.status in ("cleared", "already_empty")
                and not self._grid_empty(crafting._grid_slot(window, slot))
                else outcome
                for slot, outcome in grid.items()
            ))

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
                main_finished = True
                for index, outcome in enumerate(armor):
                    self._require_window(window)
                    observed = self._inventory.armor_slots.get(index)
                    if observed is None:
                        armor[index] = replace(outcome, status="unknown")
                    elif self._armor_empty(index):
                        armor[index] = replace(outcome, status="already_empty")
                    else:
                        await self._clear_armor_slot(index, _stack_snapshot(observed), window, progress, armor)
                        if armor[index].status != "cleared":
                            break
                for slot in grid:
                    self._require_window(window)
                    item = crafting._grid_slot(window, slot)
                    if self._grid_empty(item):
                        grid[slot] = CreativeClearSlot(slot, "already_empty")
                    elif (item is None or not item.id or not 1 <= item.stack_size <= 255
                          or not item.net_id_variant or item.net_id_variant < 0):
                        grid[slot] = CreativeClearSlot(slot, "unknown")
                    else:
                        step = len(progress.requests)
                        try:
                            await self._destroy_grid(crafting, window, slot, item, progress)
                        finally:
                            verdict = progress.requests[step].verdict if len(progress.requests) > step else None
                            status = "unconfirmed"
                            if verdict is not None:
                                if verdict.result.name != "SUCCESS":
                                    status = "rejected"
                                elif self._grid_empty(crafting._grid_slot(window, slot)):
                                    status = "cleared"
                            grid[slot] = CreativeClearSlot(slot, status, verdict)
        except (Exception, asyncio.CancelledError) as error:
            if not main_finished:
                for request in progress.requests:
                    if request.verdict is not None and results[request.slot].status == "pending":
                        results[request.slot] = self._clear_verdict(request.slot, request.verdict)
            progress.attach(error, result())
            raise
        return result()

    @staticmethod
    def _grid_empty(item):
        return item is not None and not item.id and not item.stack_size and not item.net_id_variant

    async def _destroy_grid(self, crafting, window, slot, item, progress):
        inventory = self._inventory
        expected = {slot: crafting._grid_state(slot)}
        b = self._binding
        request_id = inventory._allocate_request_id()
        action = b.ItemStackRequestCereal.DestroyActionData(
            amount=int(item.stack_size),
            source=self._slot_info("CRAFTING_INPUT_CONTAINER", slot, int(item.net_id_variant)),
        )
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id), actions=[action],
            strings_to_filter=[], strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )
        endpoints = frozenset((_CraftingGridEndpoint(id(window), slot, personal=True, destroy=True),))
        update_progress = progress.start(slot, "destroy_grid", request_id)

        def at_write():
            self._require_window(window)
            crafting._require_grid_state(window, expected)
            if any(pending.submitted for pending in inventory._requests_for_endpoints(endpoints)):
                raise RuntimeError("a crafting input has a pending item-stack request")
            update_progress(None)

        def apply(reply):
            update_progress(True, reply)
            crafting._apply_grid_destroy(window, slot, expected[slot], reply)

        return await inventory._request_item_stack(
            request, apply, timeout=None, _endpoints=endpoints, _at_write=at_write,
            after_send=lambda: update_progress(True),
        )

    def _armor_empty(self, index):
        item = self._inventory.armor_slots.get(index)
        return item is not None and not item.id and not item.stack_size and not item.net_id_variant

    @staticmethod
    def _armor_transfer_target(source, slot, verdict):
        # The correction identifies the resulting stack, which can get a new ID.
        matches = _player_slot_corrections(verdict, slot)
        if len(matches) != 1 or int(matches[0].amount) != 1:
            return None
        net_id = matches[0].item_stack_net_id
        if net_id is None or not int(net_id.raw_id):
            return None
        return replace(source, net_id=int(net_id.raw_id))

    def _staged_armor_matches(self, slot, expected):
        return (expected is not None and slot not in self._inventory._unconfirmed_transfer_slots
                and slot not in self._inventory._drop_key_counts
                and _stack_snapshot(self._inventory.slots.get(slot)) == expected)

    async def _clear_armor_slot(self, index, source, window, progress, results):
        take_step = len(progress.requests)
        expected = None

        def track(slot, request_id, submitted, verdict=None):
            nonlocal expected
            if len(progress.requests) == take_step:
                progress.requests.append(CreativeRequestProgress(slot, "take", request_id, submitted))
            else:
                previous = progress.requests[take_step]
                progress.requests[take_step] = replace(
                    previous, submitted=submitted, verdict=previous.verdict if verdict is None else verdict,
                )
            if verdict is not None and verdict.result.name == "SUCCESS":
                expected = self._armor_transfer_target(source, slot, verdict)

        def before_destroy(request_id):
            if not self._armor_empty(index) or not self._staged_armor_matches(slot, expected):
                raise RuntimeError("the creative armor transfer changed before destruction")
            endpoints = ((None, slot), ("ARMOR_CONTAINER", index))
            pending = self._inventory._pending_requests[request_id]
            if any(other is not pending for other in self._inventory._requests_for_endpoints(endpoints)):
                raise RuntimeError("a creative armor transfer slot has an item-stack request in flight")

        try:
            await self._inventory._unequip_armor(
                index, timeout=None, before_write=lambda: self._require_window(window), track=track,
            )
            slot = progress.requests[take_step].slot
            if self._armor_empty(index) and self._staged_armor_matches(slot, expected):
                self._require_window(window)
                observed = self._inventory.slots[slot]
                b = self._binding
                action = b.ItemStackRequestCereal.DestroyActionData(
                    amount=1, source=self._slot_info(_INVENTORY, slot, expected.net_id),
                )
                await self._request(
                    self._inventory._allocate_request_id(), window, slot, observed, observed, [action], None,
                    timeout=None, queued=False, progress=progress, at_write=before_destroy,
                )
        finally:
            steps = progress.requests[take_step:]
            if steps:
                unequip = steps[0]
                destroy = steps[1] if len(steps) > 1 else None
                results[index] = self._armor_outcome(results[index], index, expected, unequip, destroy)

    def _armor_outcome(self, outcome, index, expected, unequip, destroy):
        slot = unequip.slot
        status, staged = "unconfirmed", None
        if unequip.verdict is not None and unequip.verdict.result.name != "SUCCESS":
            status = "rejected"
        elif destroy is not None and destroy.verdict is not None and destroy.verdict.result.name == "SUCCESS":
            observed = self._inventory.slots.get(slot)
            empty = (observed is not None and not observed.id and not observed.stack_size and not observed.net_id_variant
                     and slot not in self._inventory._unconfirmed_transfer_slots
                     and slot not in self._inventory._drop_key_counts)
            if not empty:
                staged = "unknown"
            elif self._armor_empty(index):
                status = "cleared"
        elif unequip.submitted is not False:
            if destroy is not None and destroy.verdict is not None:
                status = "rejected"
            if ((destroy is None or destroy.submitted is False or destroy.verdict is not None)
                    and self._staged_armor_matches(slot, expected)):
                staged = "confirmed"
            else:
                staged = "unknown"
        return replace(outcome, status=status, staging_slot=slot, unequip=unequip, destroy=destroy, staged=staged)

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
                       timeout, queued, progress, at_write=None):
        b = self._binding
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id), actions=actions,
            strings_to_filter=[], strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )
        track = None if progress is None else progress.start(index, "destroy" if entry is None else "take", request_id)
        original_snapshot = _stack_snapshot(original) if at_write is not None else None
        changed = False

        def update_progress(submitted, verdict=None):
            if track is not None:
                track(submitted, verdict)

        def before_send():
            if progress is not None and progress.interrupted:
                raise RuntimeError("creative action ended before submission")
            self._require_window(window)
            if entry is not None:
                self._entry(entry)
            if at_write is not None:
                at_write(request_id)
            elif self._inventory.slots.get(index) is not original:
                raise RuntimeError("the creative action's slot changed before submission")
            update_progress(None)

        def observe(_, item):
            nonlocal changed
            if not changed and _stack_snapshot(item) != original_snapshot:
                changed = True

        def apply(response):
            update_progress(True, response)
            if at_write is not None:
                if changed:
                    # An observation can return to the original stack before the reply.
                    return
                corrections = _player_slot_corrections(response, index)
                current = self._inventory.slots.get(index)
                if len(corrections) == 1 and _stack_snapshot(current) == original_snapshot:
                    self._inventory._set_slot(index, _correct_observed_item(b, current, corrections[0]))
                return
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
            request, apply, timeout=timeout, queued=queued,
            before_send=before_send if at_write is None else None,
            _at_write=before_send if at_write is not None else None,
            _on_slot_observed=observe if at_write is not None else None,
            after_send=lambda: update_progress(True),
        )
