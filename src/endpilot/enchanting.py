"""The observed enchanting table window and its server-issued choices."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from bedrock_protocol import GameType

from .inventory import _correct_observed_item, _response_item


ENCHANTING_SLOTS = {14: "ENCHANTING_INPUT_CONTAINER", 15: "ENCHANTING_MATERIAL_CONTAINER"}
_PLAYER_SLOTS = ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", "HOTBAR_CONTAINER", "INVENTORY_CONTAINER")
_PLAYER_UI = 124


@dataclass(frozen=True, slots=True)
class Enchantment:
    enchant_type: int
    level: int


@dataclass(frozen=True, slots=True)
class EnchantOption:
    cost: int
    net_id: int
    name: str
    slot: int
    enchants: tuple[tuple[Enchantment, ...], tuple[Enchantment, ...], tuple[Enchantment, ...]]


@dataclass(frozen=True, slots=True)
class _TableStack:
    amount: int
    net_id: int | None
    item: object | None
    origin: object = field(default_factory=object)

    @property
    def identity(self):
        if not self.amount:
            return None
        item = self.item
        return (self.net_id, None if item is None else
                (int(item.id), int(item.aux_value), int(item.block_runtime_id), item.user_data_buffer))


class Enchanting:
    def __init__(self, packets, containers, inventory, item_names, *,
                 game_mode: Callable[[], GameType | None] | None = None):
        self.packets = packets
        self.containers = containers
        self.inventory = inventory
        self._item_names = item_names
        self._game_mode = game_mode
        self._window = None
        self._options: tuple[EnchantOption, ...] | None = None
        self._options_input: object | None = None
        self._first_choices: tuple[EnchantOption, ...] | None = None
        self._slots: dict[int, _TableStack] = {}
        self._changed = asyncio.Event()
        self._closed = False
        self._unsubscribe = packets.subscribe_raw(self._on_packet)
        subscribe_end = getattr(packets, "subscribe_reader_end", None)
        self._unsubscribe_end = None if subscribe_end is None else subscribe_end(lambda _: self.close())

    def _sync_window(self):
        current = self.containers.window
        window = (current if not self._closed and current is not None
                  and current.container_type == "ENCHANTMENT" else None)
        if window is not self._window:
            self._window = window
            self._slots.clear()
            self._set_options(None)
        return window

    def _require_window(self):
        if self._closed:
            raise ConnectionError("enchanting session is closed")
        window = self._sync_window()
        if window is None:
            raise RuntimeError("no enchanting table is open")
        return window

    @property
    def options(self) -> tuple[EnchantOption, ...] | None:
        self._sync_window()
        return self._options

    @property
    def slots(self) -> dict[int, object]:
        """UI item types and stacks. A normal enchant reply can omit item metadata."""
        self._sync_window()
        return {index: stack.item for index, stack in self._slots.items() if stack.item is not None}

    async def wait_for_options(self, *, timeout: float) -> tuple[EnchantOption, ...]:
        """Wait for an observed list in this window, including an explicit empty list."""
        window = self._require_window()
        async with asyncio.timeout(timeout):
            while self._require_window() is window:
                if self._options is not None:
                    return self._options
                self._changed.clear()
                await self._changed.wait()
        raise RuntimeError("enchanting table window changed")

    async def wait_for_choices(self, *, timeout: float) -> tuple[EnchantOption, ...]:
        """Wait for an observed input's first usable choices in the active table.

        The returned tuple preserves button positions. Observe the window and
        serialize input changes through their server replies before using it.
        """
        window = self._require_window()
        if (options := self._current_choices()) is not None:
            return options
        async with asyncio.timeout(timeout):
            while self._require_window() is window:
                if (options := self._current_choices(first=True)) is not None:
                    return options
                self._changed.clear()
                await self._changed.wait()
        raise RuntimeError("enchanting table window changed")

    def _current_choices(self, *, first=False) -> tuple[EnchantOption, ...] | None:
        stack = self._slots.get(14)
        if stack is not None and stack.amount and stack.origin is self._options_input:
            return self._first_choices if first else self._options
        return None

    def _set_options(self, options):
        self._options = options
        stack = self._slots.get(14)
        if (stack is not None and stack.amount and options
                and any(option.cost > 0 and option.net_id > 0 for option in options)):
            if stack.origin is not self._options_input:
                # One batch can deliver several quotes before waiters resume.
                self._first_choices = options
            self._options_input = stack.origin
        else:
            self._options_input = None
            self._first_choices = None
        self._changed.set()

    def _record_slot(self, index, item):
        if index not in ENCHANTING_SLOTS:
            return
        previous = self._slots.get(index)
        if item is None or not int(item.id):
            stack = _TableStack(0, None, None)
        else:
            net_id = item.net_id_variant
            stack = _TableStack(int(item.stack_size), None if net_id is None else int(net_id), item)
        if stack.amount and previous is not None and previous.identity == stack.identity:
            stack = replace(stack, origin=previous.origin)
        elif index == 14 and (not stack.amount or (previous is not None and previous.amount)):
            self._set_options(None)
        self._slots[index] = stack

    def _on_packet(self, raw):
        b = self.packets.protocol.binding
        if raw.packet_id == b.DisconnectPacket.ID:
            self.close()
            return
        if self._sync_window() is None:
            return
        if raw.packet_id == b.PlayerEnchantOptionsPacket.ID:
            packet = self.packets.protocol.decode(raw)
            self._set_options(tuple(EnchantOption(
                cost=int(option.cost), net_id=int(option.enchant_net_id), name=str(option.enchant_name),
                slot=int(option.enchants.slot),
                enchants=tuple(tuple(Enchantment(int(entry.enchant_type), int(entry.level)) for entry in group)
                               for group in option.enchants.item_enchants),
            ) for option in packet.options))
        elif raw.packet_id == b.InventorySlotPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.inventory_id) == _PLAYER_UI:
                self._record_slot(int(packet.slot), packet.item)
        elif raw.packet_id == b.InventoryContentPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.inventory_id) == _PLAYER_UI:
                for index in ENCHANTING_SLOTS:
                    self._record_slot(index, packet.slots[index] if index < len(packet.slots) else None)
        elif raw.packet_id == b.InventoryTransactionPacket.ID:
            packet = self.packets.protocol.decode(raw)
            transaction = getattr(packet.variant_transaction, "transaction", None)
            for action in getattr(transaction, "actions", ()):
                source = action.source
                if (source.type.name == "CONTAINER_INVENTORY" and source.container_id is not None
                        and int(source.container_id) == _PLAYER_UI):
                    self._record_slot(int(action.slot), action.to_item_descriptor)

    def _slot_info(self, label, index, net_id):
        b = self.packets.protocol.binding
        return b.ItemStackRequestCereal.SlotInfoData(
            full_container_name=b.FullContainerName(name=b.ContainerEnumName[label], dynamic_id=None),
            slot=index, net_id_variant=net_id or 0,
        )

    async def _request(self, request_id, actions, apply, *, timeout, queued, at_write=None):
        b = self.packets.protocol.binding
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id), actions=actions,
            strings_to_filter=[], strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )
        return await self.inventory._request_item_stack(
            request, apply, timeout=timeout, queued=queued, _at_write=at_write,
        )

    def _correct_table_slot(self, index, original, correction, *, fallback=None, output_changed=False):
        current = self._slots.get(index)
        if current is not original and (current is None or original is None or current.origin is not original.origin):
            # Earlier requests cannot overwrite a different stack or a clear.
            return False
        amount = int(correction.amount)
        if not amount:
            self._slots[index] = _TableStack(0, None, None)
            return current is not None and bool(current.amount)
        correction_id = correction.item_stack_net_id
        net_id = (int(correction_id.raw_id) if correction_id is not None
                  else None if current is None or output_changed else current.net_id)
        item = None if current is None else current.item
        if output_changed and item is not None:
            # Ordinary BDS enchantment confirms the new stack ID but omits its
            # metadata. Preserve the expected vanilla type and await any full
            # descriptor (Endstone sends one when a plugin replaces the item).
            item_id = int(item.id)
            if self._item_names.get(item_id) == "minecraft:book":
                item_id = next((key for key, name in self._item_names.items()
                                if name == "minecraft:enchanted_book"), None)
            item = (None if item_id is None else self.packets.protocol.binding.SerializedNetworkItemStackDescriptor(
                id=item_id, stack_size=amount, aux_value=int(item.aux_value),
                net_id_variant=net_id, block_runtime_id=int(item.block_runtime_id), user_data_buffer=b"",
            ))
        elif item is None:
            item = fallback
        if item is not None:
            item = _response_item(self.packets.protocol.binding, item, item, correction)
        if current is not None and current.amount and not output_changed:
            stack = _TableStack(amount, net_id, item, current.origin)
        else:
            stack = _TableStack(amount, net_id, item)
        self._slots[index] = stack
        return (None if current is None else current.identity) != stack.identity

    async def transfer(self, player_slot: int, table_slot: int, amount: int, *, into_table: bool,
                       timeout: float, queued: bool = False):
        window = self._require_window()
        if not 0 <= player_slot < 36 or table_slot not in ENCHANTING_SLOTS or not 1 <= amount <= 255:
            raise ValueError("invalid enchanting transfer slots or amount")
        inventory = self.inventory
        original_player = inventory.slots.get(player_slot)
        original_table = self._slots.get(table_slot)
        options = self._options
        if into_table:
            if original_player is None or not int(original_player.id):
                raise ValueError("player inventory source is unobserved or empty")
            moved = original_player
        else:
            if original_table is None or not original_table.amount or original_table.net_id is None:
                raise ValueError("enchanting table source is unobserved or empty")
            moved = original_table.item

        b = self.packets.protocol.binding
        player = self._slot_info(_PLAYER_SLOTS[0], player_slot,
                                 None if original_player is None else original_player.net_id_variant)
        label = ENCHANTING_SLOTS[table_slot]
        table = self._slot_info(label, table_slot, None if original_table is None else original_table.net_id)
        cereal = b.ItemStackRequestCereal
        action = (cereal.PlaceActionData(amount=amount, source=player, destination=table) if into_table
                  else cereal.TakeActionData(amount=amount, source=table, destination=player))
        request_id = inventory._allocate_request_id()

        def apply(response):
            success = response.result.name == "SUCCESS"
            for container in response.containers or ():
                name = container.full_container_name.name.name
                for slot in container.slots:
                    index = int(slot.slot)
                    if name == label and index == table_slot and self._sync_window() is window:
                        changed = self._correct_table_slot(index, original_table, slot, fallback=moved if success else None)
                        if index == 14 and changed:
                            if not into_table or self._options is options:
                                self._set_options(None)
                            elif success and (original_table is None or not original_table.amount):
                                # BDS can send the quote before confirming placement into the input slot.
                                self._set_options(self._options)
                    elif name in _PLAYER_SLOTS and index == player_slot:
                        current = inventory.slots.get(index)
                        if current is not original_player:
                            updated = _correct_observed_item(b, current, slot)
                        else:
                            empty = inventory.item_or_empty(-1)
                            previous = current or empty
                            fallback = moved if success and moved is not None else previous
                            updated = (None if int(slot.amount) > 0 and int(previous.id) == int(fallback.id) == 0
                                       else _response_item(b, previous, fallback, slot))
                        inventory._set_slot(index, updated)

        return await self._request(request_id, [action], apply, timeout=timeout, queued=queued)

    async def select(self, option: int, *, timeout: float, queued: bool = False):
        """Submit the active table's observed button and await its verdict."""
        window = self._require_window()
        b = self.packets.protocol.binding
        mode = None if self._game_mode is None else self._game_mode()
        creative = mode == b.GameType.CREATIVE
        options = self._current_choices() if creative else self._options
        if options is None or not 0 <= option < len(options):
            raise ValueError("enchant option has not been observed")
        chosen = options[option]
        if chosen.cost <= 0 or chosen.net_id <= 0:
            raise ValueError("the selected enchant option is unavailable")
        inputs = {index: self._slots.get(index) for index in ENCHANTING_SLOTS}
        item, lapis = inputs[14], inputs[15]
        if item is None or not item.amount or item.net_id is None:
            raise ValueError("enchanting input is unobserved or empty")
        if not creative and (lapis is None or lapis.amount < option + 1 or lapis.net_id is None):
            raise ValueError("insufficient observed enchanting material")
        cereal = b.ItemStackRequestCereal
        request_id = self.inventory._allocate_request_id()
        actions = [
            cereal.CraftRecipeActionData(recipe_net_id=chosen.net_id, num_crafts=1),
            cereal.ConsumeActionData(amount=1, source=self._slot_info(ENCHANTING_SLOTS[14], 14, item.net_id)),
        ]
        if not creative:
            actions.append(cereal.ConsumeActionData(
                amount=option + 1, source=self._slot_info(ENCHANTING_SLOTS[15], 15, lapis.net_id),
            ))
        actions.append(cereal.PlaceActionData(
            amount=1, source=self._slot_info("CREATED_OUTPUT_CONTAINER", 50, request_id),
            destination=self._slot_info(ENCHANTING_SLOTS[14], 14, 0),
        ))

        def still_available(index, original, count):
            current = self._slots.get(index)
            return (current is not None and current.origin is original.origin
                    and current.identity == original.identity and current.amount >= count)

        def at_write():
            if self._game_mode is not None and self._game_mode() != mode:
                raise RuntimeError("game mode changed before enchanting submission")
            if self._sync_window() is not window:
                raise RuntimeError("enchanting table window changed before submission")
            if self._options is not options or (creative and self._current_choices() is not options):
                raise RuntimeError("enchant options changed before submission")
            if not still_available(14, item, 1) or not creative and not still_available(15, lapis, option + 1):
                raise RuntimeError("enchanting inputs changed before submission")

        def apply(response):
            if self._sync_window() is not window:
                return
            success = response.result.name == "SUCCESS"
            for container in response.containers or ():
                label = container.full_container_name.name.name
                for slot in container.slots:
                    index = int(slot.slot)
                    if ENCHANTING_SLOTS.get(index) == label:
                        self._correct_table_slot(index, inputs[index], slot, output_changed=success and index == 14)
            if success:
                current_input = self._slots.get(14)
                if current_input is not None and current_input.origin is item.origin:
                    self._slots.pop(14, None)
                if self._options is options:
                    self._set_options(None)

        return await self._request(request_id, actions, apply, timeout=timeout, queued=queued, at_write=at_write)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._sync_window()
        self._changed.set()
        self._unsubscribe()
        if self._unsubscribe_end is not None:
            self._unsubscribe_end()
