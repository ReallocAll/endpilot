"""Server-issued crafting recipes, retained across the login/ready boundary."""

import asyncio
from dataclasses import dataclass

from .inventory import _response_item


_RECIPE_KINDS = (
    "shaped_recipes", "shapeless_recipes", "user_data_shapeless_recipes",
    "shapeless_chemistry_recipes", "shaped_chemistry_recipes",
    "smithing_transform_recipes", "smithing_trim_recipes",
)


@dataclass(frozen=True, slots=True)
class RecipeIngredient:
    name: str | None
    tag: str | None
    count: int


@dataclass(frozen=True, slots=True)
class RecipeResult:
    item_id: int
    name: str | None
    count: int
    aux_value: int
    block_runtime_id: int
    user_data_buffer: bytes


@dataclass(frozen=True, slots=True)
class Recipe:
    recipe_id: str
    net_id: int
    kind: str
    ingredients: tuple[RecipeIngredient, ...]
    results: tuple[RecipeResult, ...]
    tag: str


class Recipes:
    def __init__(self, packets, item_names: dict[int, str], initial_packets=(), inventory=None, containers=None):
        self.packets = packets
        self.item_names = item_names
        self.inventory = inventory
        self.containers = containers
        self._grid_slots = {}  # Server-described player UI slots (124), plus confirmed request slots.
        self._by_id: dict[str, Recipe] = {}
        self._by_net_id: dict[int, Recipe] = {}
        self._loaded = asyncio.Event()
        # Process login-time packets before switching to the live subscriber.
        for raw in initial_packets:
            self._on_packet(raw)
        self._unsubscribe = packets.subscribe_raw(self._on_packet)

    def _on_packet(self, raw) -> None:
        b = self.packets.protocol.binding
        if raw.packet_id == b.CraftingDataPacket.ID:
            self.load(self.packets.protocol.decode(raw))
        elif raw.packet_id == b.InventoryContentPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.inventory_id) == 124:  # Player-only UI in EndstoneMC/client.
                self._grid_slots = {
                    slot: item for slot, item in enumerate(packet.slots)
                    if 28 <= slot <= 40
                }
        elif raw.packet_id == b.InventorySlotPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.inventory_id) == 124 and 28 <= int(packet.slot) <= 40:
                self._grid_slots[int(packet.slot)] = packet.item
        elif raw.packet_id == b.InventoryTransactionPacket.ID:
            packet = self.packets.protocol.decode(raw)
            transaction = getattr(packet.variant_transaction, "transaction", None)
            for action in getattr(transaction, "actions", ()):
                source = action.source
                if (source.type.name == "CONTAINER_INVENTORY"
                        and source.container_id is not None
                        and int(source.container_id) == 124 and 28 <= int(action.slot) <= 40):
                    self._grid_slots[int(action.slot)] = action.to_item_descriptor
        elif raw.packet_id == b.ContainerClosePacket.ID:
            self._grid_slots.clear()

    def load(self, packet) -> None:
        if packet.clear_recipes:
            self._by_id.clear()
            self._by_net_id.clear()
        for kind in _RECIPE_KINDS:
            for entry in getattr(packet, kind, ()) or ():
                recipe_id, net_id = getattr(entry, "recipe_id", None), getattr(entry, "net_id", None)
                if recipe_id is None or net_id is None:
                    continue  # A multi-recipe has only a UUID and network ID.
                ingredients = []
                source_ingredients = getattr(entry, "ingredients", None)
                if source_ingredients is None and kind.startswith("smithing_"):
                    source_ingredients = (
                        entry.template_ingredient, entry.base_ingredient, entry.addition_ingredient
                    )
                for ingredient in source_ingredients or ():
                    if int(ingredient.stack_size) <= 0:
                        continue
                    descriptor = ingredient.descriptor or {}
                    ingredients.append(RecipeIngredient(
                        descriptor.get("name"), descriptor.get("item_tag"),
                        int(ingredient.stack_size),
                    ))
                source_results = getattr(entry, "results", None)
                if source_results is None and kind == "smithing_transform_recipes":
                    source_results = (entry.result,)
                results = tuple(
                    RecipeResult(
                        int(item.id), self.item_names.get(int(item.id)),
                        int(item.stack_size), int(item.aux_value),
                        int(item.block_runtime_id), bytes(item.user_data_buffer),
                    )
                    for item in source_results or ()
                )
                recipe = Recipe(str(recipe_id), int(net_id), kind, tuple(ingredients),
                                results, str(getattr(entry, "tag", "")))
                # An incremental update may replace either ID. Keep both indexes consistent.
                old_id = self._by_id.get(recipe.recipe_id)
                if old_id is not None:
                    self._by_net_id.pop(old_id.net_id, None)
                old_net = self._by_net_id.get(recipe.net_id)
                if old_net is not None:
                    self._by_id.pop(old_net.recipe_id, None)
                self._by_id[recipe.recipe_id] = recipe
                self._by_net_id[recipe.net_id] = recipe
        self._loaded.set()

    def snapshot(self) -> tuple[Recipe, ...]:
        return tuple(self._by_id.values())

    def get(self, recipe_id: str | int) -> Recipe | None:
        return (self._by_net_id.get(recipe_id) if isinstance(recipe_id, int)
                else self._by_id.get(recipe_id))

    async def wait_loaded(self, *, timeout: float = 15) -> tuple[Recipe, ...]:
        async with asyncio.timeout(timeout):
            await self._loaded.wait()
        return self.snapshot()


    async def craft_auto(
        self, recipe_id: str | int, destination_slot: int, *,
        times: int = 1, timeout: float = 15,
    ):
        """Craft from observed player inventory and return the matching BDS item-stack verdict.

        Match the upstream client's CraftRecipeAuto/CraftResults/Consume/Place
        sequence. Only response slots, not a local prediction, update inventory.
        BDS 1.26.52 requires an open player inventory window for this action.
        This entry point currently requires named ingredients and one output stack.
        """
        recipe = self.get(recipe_id)
        if recipe is None:
            raise KeyError(f"the server sent no recipe {recipe_id!r}")
        if self.inventory is None:
            raise RuntimeError("crafting requires player inventory")
        if not 0 <= destination_slot <= 255:
            raise ValueError("destination slot must fit in a byte")
        if not 1 <= times <= 255:
            raise ValueError("craft count must be between 1 and 255")
        if len(recipe.results) != 1 or not recipe.ingredients:
            raise ValueError("automatic crafting requires ingredients and one result stack")
        output = recipe.results[0]
        if not 1 <= output.count * times <= 255:
            raise ValueError("result stack count must fit in a byte")

        b = self.packets.protocol.binding
        cereal = b.ItemStackRequestCereal
        inventory = self.inventory
        items = inventory.slots
        remaining = {slot: int(item.stack_size) for slot, item in items.items()}
        consumes = []
        ingredients = []

        def slot_info(name, slot, net_id):
            return cereal.SlotInfoData(
                full_container_name=b.FullContainerName(
                    name=b.ContainerEnumName[name], dynamic_id=None,
                ),
                slot=slot, net_id_variant=net_id,
            )

        for ingredient in recipe.ingredients:
            if ingredient.name is not None:
                descriptor = cereal.ItemNameDescriptorData(
                    full_name=ingredient.name, aux_value=32767,
                )
            else:
                descriptor = cereal.ItemTagDescriptorData(item_tag=ingredient.tag or "")
            ingredients.append(cereal.RecipeIngredientData(
                item_descriptor=descriptor, stack_size=ingredient.count,
            ))
            # The available player item identities are numerical. There is no
            # local tag registry to resolve a tag-only ingredient to a stack.
            if ingredient.name is None:
                raise ValueError("tag-only ingredients require explicit grid crafting")
            needed = ingredient.count * times
            for slot, item in items.items():
                if needed <= 0:
                    break
                if (not 0 <= slot <= 255 or remaining[slot] <= 0
                        or self.item_names.get(int(item.id)) != ingredient.name):
                    continue
                used = min(needed, remaining[slot])
                consumes.append(cereal.ConsumeActionData(
                    amount=used,
                    source=slot_info(
                        "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                        slot, int(item.net_id_variant or 0),
                    ),
                ))
                remaining[slot] -= used
                needed -= used
            if needed:
                raise ValueError(f"not enough {ingredient.name} to craft {recipe.recipe_id}")

        request_id = inventory._allocate_request_id()
        output_descriptor = b.SerializedNetworkItemStackDescriptor(
            id=output.item_id, stack_size=output.count * times,
            aux_value=output.aux_value, net_id_variant=None,
            block_runtime_id=output.block_runtime_id,
            user_data_buffer=output.user_data_buffer,
        )
        result_items = [
            cereal.NetworkItemInstanceDescriptorData(
                item_descriptor=cereal.ItemNameDescriptorData(
                    full_name=output.name or "", aux_value=output.aux_value,
                ),
                stack_size=output.count,
                block_runtime_id=output.block_runtime_id,
                user_data_buffer=output.user_data_buffer,
            )
        ]
        destination = inventory.item_or_empty(destination_slot)
        actions = [
            cereal.CraftRecipeAutoActionData(
                recipe_net_id=recipe.net_id, num_crafts=times,
                ingredients=ingredients,
            ),
            cereal.CraftResultsActionData(
                craft_results=result_items, num_crafts=times,
            ),
            *consumes,
            cereal.PlaceActionData(
                amount=output.count * times,
                source=slot_info("CREATED_OUTPUT_CONTAINER", 50, request_id),
                destination=slot_info(
                    "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                    destination_slot, int(destination.net_id_variant or 0),
                ),
            ),
        ]
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=actions, strings_to_filter=[],
            strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )
        touched = {int(action.source.slot) for action in consumes}
        touched.add(destination_slot)
        original = {slot: items.get(slot) or inventory.item_or_empty(slot) for slot in touched}

        def matches(raw):
            packet = self.packets.protocol.decode(raw)
            return any(int(reply.client_request_id.raw_id) == request_id
                       for reply in packet.responses)

        pending = asyncio.create_task(self.packets.wait_for_packet(
            b.ItemStackResponsePacket.ID, timeout=timeout, match=matches,
        ))
        try:
            await asyncio.sleep(0)
            await self.packets.send(b.ItemStackRequestPacket(requests=[request]))
            packet = self.packets.protocol.decode(await pending)
            response = next(reply for reply in packet.responses
                            if int(reply.client_request_id.raw_id) == request_id)
            from .inventory import _response_item

            for container in response.containers or ():
                if container.full_container_name.name.name not in (
                    "HOTBAR_CONTAINER", "INVENTORY_CONTAINER",
                    "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                ):
                    continue
                for slot in container.slots:
                    index = int(slot.slot)
                    if index not in touched:
                        continue
                    previous = inventory.item_or_empty(index)
                    fallback = original[index]
                    if index == destination_slot and response.result.name == "SUCCESS":
                        previous = output_descriptor
                        fallback = output_descriptor
                    inventory._set_slot(index, _response_item(b, previous, fallback, slot))
            return response
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


    async def stage_grid(self, source_slot: int, grid_slot: int, amount: int = 1, *,
                         timeout: float = 15):
        """Place an observed inventory item into the active 2x2 or workbench 3x3 grid.

        Return the matching BDS ItemStackResponse. Grid state comes from the
        player UI packets or the server's corrected request slots.
        """
        if self.containers is None or self.containers.window is None:
            raise RuntimeError("open the inventory screen before staging crafting inputs")
        grid_slots = (range(32, 41) if self.containers.window.container_type == "WORKBENCH"
                      else range(28, 32))
        if grid_slot not in grid_slots:
            raise ValueError(f"active crafting grid slots are {grid_slots.start} through {grid_slots.stop - 1}")
        if not 0 <= source_slot <= 255:
            raise ValueError("source slot must fit in a byte")
        if not 1 <= amount <= 255:
            raise ValueError("amount must be between 1 and 255")
        source = self.inventory.slots.get(source_slot) if self.inventory is not None else None
        if source is None or int(source.id) == 0:
            raise ValueError("source slot has no observed item")
        b = self.packets.protocol.binding
        cereal = b.ItemStackRequestCereal

        def slot_info(name, slot, net_id):
            return cereal.SlotInfoData(
                full_container_name=b.FullContainerName(
                    name=b.ContainerEnumName[name], dynamic_id=None,
                ), slot=slot, net_id_variant=net_id,
            )

        destination = self._grid_slots.get(grid_slot)
        request_id = self.inventory._allocate_request_id()
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=[cereal.PlaceActionData(
                amount=amount,
                source=slot_info("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                                 source_slot, int(source.net_id_variant or 0)),
                destination=slot_info("CRAFTING_INPUT_CONTAINER", grid_slot,
                                      int(destination.net_id_variant or 0)
                                      if destination is not None else 0),
            )],
            strings_to_filter=[], strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )

        def matches(raw):
            packet = self.packets.protocol.decode(raw)
            return any(int(reply.client_request_id.raw_id) == request_id
                       for reply in packet.responses)

        pending = asyncio.create_task(self.packets.wait_for_packet(
            b.ItemStackResponsePacket.ID, timeout=timeout, match=matches,
        ))
        try:
            await asyncio.sleep(0)
            await self.packets.send(b.ItemStackRequestPacket(requests=[request]))
            packet = self.packets.protocol.decode(await pending)
            reply = next(reply for reply in packet.responses
                         if int(reply.client_request_id.raw_id) == request_id)
            for container in reply.containers or ():
                name = container.full_container_name.name.name
                for slot in container.slots:
                    index = int(slot.slot)
                    if (name in ("HOTBAR_CONTAINER", "INVENTORY_CONTAINER",
                                 "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER")
                            and index == source_slot):
                        current = self.inventory.item_or_empty(index)
                        self.inventory._set_slot(
                            index, _response_item(b, current, source, slot),
                        )
                    elif name == "CRAFTING_INPUT_CONTAINER" and index == grid_slot:
                        previous = self._grid_slots.get(index)
                        if previous is None:
                            previous = self.inventory.item_or_empty(-1)
                        # A successful placement identifies the resulting grid
                        # stack as the source item; an error does not.
                        fallback = source if reply.result.name == "SUCCESS" else previous
                        self._grid_slots[index] = _response_item(b, previous, fallback, slot)
            return reply
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def craft_grid(self, recipe_id: str | int, destination_slot: int, *,
                         times: int = 1, timeout: float = 15):
        """Craft from the observed 2x2 or workbench 3x3 grid; return BDS's response.

        Uses the upstream CraftRecipe/CraftResults/Consume/Place sequence.
        The server decides whether the staged layout matches the chosen recipe.
        """
        recipe = self.get(recipe_id)
        if recipe is None:
            raise KeyError(f"the server sent no recipe {recipe_id!r}")
        if self.inventory is None or self.containers is None or self.containers.window is None:
            raise RuntimeError("open the inventory screen before grid crafting")
        if not 0 <= destination_slot <= 255:
            raise ValueError("destination slot must fit in a byte")
        if not 1 <= times <= 255:
            raise ValueError("craft count must be between 1 and 255")
        if len(recipe.results) != 1:
            raise ValueError("grid crafting currently requires one output stack")
        output = recipe.results[0]
        if not 1 <= output.count * times <= 255:
            raise ValueError("result stack count must fit in a byte")
        grid_slots = (range(32, 41) if self.containers.window.container_type == "WORKBENCH"
                      else range(28, 32))
        grid = {
            slot: item for slot, item in self._grid_slots.items()
            if slot in grid_slots and int(item.id) != 0
        }
        if not grid:
            raise ValueError("no observed crafting ingredients in the active grid")
        b = self.packets.protocol.binding
        cereal = b.ItemStackRequestCereal

        def slot_info(name, slot, net_id):
            return cereal.SlotInfoData(
                full_container_name=b.FullContainerName(
                    name=b.ContainerEnumName[name], dynamic_id=None,
                ), slot=slot, net_id_variant=net_id,
            )

        request_id = self.inventory._allocate_request_id()
        destination = self.inventory.item_or_empty(destination_slot)
        output_descriptor = b.SerializedNetworkItemStackDescriptor(
            id=output.item_id, stack_size=output.count * times,
            aux_value=output.aux_value, net_id_variant=None,
            block_runtime_id=output.block_runtime_id,
            user_data_buffer=output.user_data_buffer,
        )
        actions = [
            cereal.CraftRecipeActionData(recipe_net_id=recipe.net_id, num_crafts=times),
            cereal.CraftResultsActionData(
                craft_results=[cereal.NetworkItemInstanceDescriptorData(
                    item_descriptor=cereal.ItemNameDescriptorData(
                        full_name=output.name or "", aux_value=output.aux_value,
                    ),
                    stack_size=output.count,
                    block_runtime_id=output.block_runtime_id,
                    user_data_buffer=output.user_data_buffer,
                )], num_crafts=times,
            ),
            *[
                cereal.ConsumeActionData(
                    amount=times,
                    source=slot_info("CRAFTING_INPUT_CONTAINER", slot,
                                     int(item.net_id_variant or 0)),
                )
                for slot, item in sorted(grid.items())
            ],
            cereal.PlaceActionData(
                amount=output.count * times,
                source=slot_info("CREATED_OUTPUT_CONTAINER", 50, request_id),
                destination=slot_info("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                                      destination_slot, int(destination.net_id_variant or 0)),
            ),
        ]
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=actions, strings_to_filter=[],
            strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )

        def matches(raw):
            packet = self.packets.protocol.decode(raw)
            return any(int(reply.client_request_id.raw_id) == request_id
                       for reply in packet.responses)

        pending = asyncio.create_task(self.packets.wait_for_packet(
            b.ItemStackResponsePacket.ID, timeout=timeout, match=matches,
        ))
        try:
            await asyncio.sleep(0)
            await self.packets.send(b.ItemStackRequestPacket(requests=[request]))
            packet = self.packets.protocol.decode(await pending)
            reply = next(reply for reply in packet.responses
                         if int(reply.client_request_id.raw_id) == request_id)
            for container in reply.containers or ():
                name = container.full_container_name.name.name
                for slot in container.slots:
                    index = int(slot.slot)
                    if name == "CRAFTING_INPUT_CONTAINER" and index in grid:
                        previous = self._grid_slots.get(index) or grid[index]
                        self._grid_slots[index] = _response_item(b, previous, grid[index], slot)
                    elif (name in ("HOTBAR_CONTAINER", "INVENTORY_CONTAINER",
                                   "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER")
                          and index == destination_slot):
                        current = self.inventory.item_or_empty(index)
                        fallback = output_descriptor if reply.result.name == "SUCCESS" else destination
                        self.inventory._set_slot(
                            index, _response_item(b, current, fallback, slot),
                        )
            return reply
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    def close(self) -> None:
        self._unsubscribe()
