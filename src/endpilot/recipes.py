"""Server-issued crafting recipes, retained across the login/ready boundary."""

import asyncio
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, replace
from functools import cache
from importlib.resources import files
import json
from operator import index
from typing import Literal

from .inventory import _PLAYER_CONTAINERS, _correct_observed_item, _response_item, _stack_snapshot


_RECIPE_KINDS = (
    "shaped_recipes", "shapeless_recipes", "user_data_shapeless_recipes",
    "shapeless_chemistry_recipes", "shaped_chemistry_recipes",
    "smithing_transform_recipes", "smithing_trim_recipes",
)
_ANY_AUX_VALUE = 32767


@cache
def _vanilla_recipe_tags() -> dict[str, frozenset[str]]:
    data = files("endpilot").joinpath("data", "recipe-tags-2225.json").read_text(encoding="utf-8")
    return {tag: frozenset(items) for tag, items in json.loads(data).items()}


@dataclass(frozen=True, slots=True)
class RecipeIngredient:
    name: str | None
    tag: str | None
    count: int
    aux_value: int | None = None

    @property
    def _item_name(self) -> str | None:
        # Keep the catalog spelling, e.g. minecraft:bed:6. Inventory and
        # stack requests carry the item name and its aux value separately.
        if self.name and self.aux_value not in (None, _ANY_AUX_VALUE) and self.name.count(":") == 2:
            return self.name.removesuffix(f":{self.aux_value}")
        return self.name


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
    width: int | None = None
    height: int | None = None
    shape: tuple[RecipeIngredient | None, ...] | None = None


class Recipes:
    def __init__(self, packets, item_names: dict[int, str], initial_packets=(), inventory=None, containers=None,
                 *, environment=None, vanilla_recipe_tags=False):
        self.packets = packets
        self.item_names = item_names
        self.inventory = inventory
        self.containers = containers
        self.environment = environment
        self._tag_members = _vanilla_recipe_tags() if vanilla_recipe_tags else {}
        self._unlocked: set[str] = set()
        self._grid_slots = {}  # Server-described player UI slots (124), plus confirmed request slots.
        # A semantic change still supersedes a receipt if the slot later returns to its original value.
        self._grid_versions: dict[int, int] = {}
        self._unconfirmed_grid_slots: set[int] = set()
        self._grid_window = None if containers is None else containers.window
        self._by_id: dict[str, Recipe] = {}
        self._by_net_id: dict[int, Recipe] = {}
        timeline = getattr(packets, "_timeline", None)
        self._qa_recipe_aliases: dict[str | int, Recipe] | None = (
            {} if getattr(timeline, "_retain_legacy_recipes", False) else None
        )
        self._loaded = asyncio.Event()
        # Process login-time packets before switching to the live subscriber.
        for raw in initial_packets:
            self._on_packet(raw)
        self._unsubscribe = packets.subscribe_raw(self._on_packet)

    def _sync_grid_window(self):
        window = None if self.containers is None else self.containers.window
        if self._grid_window is not window:
            self._grid_slots.clear()
            self._grid_versions.clear()
            self._unconfirmed_grid_slots.clear()
            self._grid_window = window
        return window

    def _set_grid_slot(self, slot, item):
        previous = self._grid_slots.get(slot)
        if previous is not item and _stack_snapshot(previous) != _stack_snapshot(item):
            self._grid_versions[slot] = self._grid_versions.get(slot, 0) + 1
        if item is None:
            self._grid_slots.pop(slot, None)
        else:
            self._grid_slots[slot] = item
            self._unconfirmed_grid_slots.discard(slot)

    def _on_packet(self, raw) -> None:
        b = self.packets.protocol.binding
        if raw.packet_id == b.CraftingDataPacket.ID:
            self.load(self.packets.protocol.decode(raw))
        elif raw.packet_id == b.ItemRegistryPacket.ID:
            self._refresh_result_names()
        elif raw.packet_id == b.UnlockedRecipesPacket.ID:
            self._load_unlocked(self.packets.protocol.decode(raw))
        elif raw.packet_id == b.InventoryContentPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.inventory_id) == 124:  # Player-only UI container.
                self._sync_grid_window()
                slots = {
                    slot: item for slot, item in enumerate(packet.slots)
                    if 28 <= slot <= 40
                }
                for slot in self._grid_slots.keys() | slots.keys():
                    self._set_grid_slot(slot, slots.get(slot))
        elif raw.packet_id == b.InventorySlotPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.inventory_id) == 124 and 28 <= int(packet.slot) <= 40:
                self._sync_grid_window()
                self._set_grid_slot(int(packet.slot), packet.item)
        elif raw.packet_id == b.InventoryTransactionPacket.ID:
            packet = self.packets.protocol.decode(raw)
            transaction = getattr(packet.variant_transaction, "transaction", None)
            for action in getattr(transaction, "actions", ()):
                source = action.source
                if (source.type.name == "CONTAINER_INVENTORY"
                        and source.container_id is not None
                        and int(source.container_id) == 124 and 28 <= int(action.slot) <= 40):
                    self._sync_grid_window()
                    self._set_grid_slot(int(action.slot), action.to_item_descriptor)
        elif raw.packet_id in (b.ContainerOpenPacket.ID, b.ContainerClosePacket.ID,
                               b.ChangeDimensionPacket.ID, b.DisconnectPacket.ID):
            self._sync_grid_window()

    def _refresh_result_names(self):
        for recipe_id, recipe in self._by_id.items():
            names = tuple(self.item_names.get(result.item_id) for result in recipe.results)
            if names != tuple(result.name for result in recipe.results):
                updated = replace(recipe, results=tuple(
                    replace(result, name=name) for result, name in zip(recipe.results, names)
                ))
                self._by_id[recipe_id] = self._by_net_id[recipe.net_id] = updated

    def _load_unlocked(self, packet) -> None:
        kinds = type(packet).PacketType
        if packet.packet_type == kinds.INITIALLY_UNLOCKED_RECIPES:
            self._unlocked = set(packet.unlocked_recipes)
        elif packet.packet_type == kinds.NEWLY_UNLOCKED_RECIPES:
            self._unlocked.update(packet.unlocked_recipes)
        elif packet.packet_type == kinds.REMOVE_UNLOCKED_RECIPES:
            self._unlocked.difference_update(packet.unlocked_recipes)
        elif packet.packet_type == kinds.REMOVE_ALL_UNLOCKED_RECIPES:
            self._unlocked.clear()

    @property
    def unlocked_ids(self) -> frozenset[str]:
        return frozenset(self._unlocked)

    def load(self, packet) -> None:
        aliases = self._qa_recipe_aliases
        if packet.clear_recipes:
            self._by_id.clear()
            self._by_net_id.clear()
            if aliases is not None:
                aliases.clear()
        for kind in _RECIPE_KINDS:
            for entry in getattr(packet, kind, ()) or ():
                recipe_id, net_id = getattr(entry, "recipe_id", None), getattr(entry, "net_id", None)
                if recipe_id is None or net_id is None:
                    continue  # A multi-recipe has only a UUID and network ID.
                ingredient_slots = []
                source_ingredients = getattr(entry, "ingredients", None)
                if source_ingredients is None and kind.startswith("smithing_"):
                    source_ingredients = (
                        entry.template_ingredient, entry.base_ingredient, entry.addition_ingredient
                    )
                for ingredient in source_ingredients or ():
                    count = int(ingredient.stack_size)
                    if count <= 0:
                        ingredient_slots.append(None)
                        continue
                    descriptor = ingredient.descriptor or {}
                    aux = (getattr(ingredient, "aux_value", None)
                           if descriptor.keys() in ({"name"}, {"item_tag"}) else None)
                    ingredient_slots.append(RecipeIngredient(
                        descriptor.get("name"), descriptor.get("item_tag"), count, aux,
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
                width, height = getattr(entry, "width", None), getattr(entry, "height", None)
                recipe = Recipe(
                    str(recipe_id), int(net_id), kind, tuple(ing for ing in ingredient_slots if ing is not None),
                    results, str(getattr(entry, "tag", "")), width, height,
                    tuple(ingredient_slots) if width is not None and height is not None else None,
                )
                # An incremental update may replace either ID. Keep both indexes consistent.
                old_id = self._by_id.get(recipe.recipe_id)
                if old_id is not None:
                    self._by_net_id.pop(old_id.net_id, None)
                    if aliases is not None:
                        aliases[old_id.net_id] = old_id
                old_net = self._by_net_id.get(recipe.net_id)
                if old_net is not None:
                    self._by_id.pop(old_net.recipe_id, None)
                    if aliases is not None:
                        aliases[old_net.recipe_id] = old_net
                self._by_id[recipe.recipe_id] = recipe
                self._by_net_id[recipe.net_id] = recipe
                if aliases is not None:
                    # Old string and integer keys remain valid until replaced independently.
                    aliases.pop(recipe.recipe_id, None)
                    aliases.pop(recipe.net_id, None)
        self._loaded.set()

    def snapshot(self) -> tuple[Recipe, ...]:
        return tuple(self._by_id.values())

    def get(self, recipe_id: str | int) -> Recipe | None:
        return (self._by_net_id.get(recipe_id) if isinstance(recipe_id, int)
                else self._by_id.get(recipe_id))

    def _get_qa_recipe(self, recipe_id: str | int) -> Recipe | None:
        aliases = self._qa_recipe_aliases
        if aliases is not None and recipe_id in aliases:
            return aliases[recipe_id]
        return self.get(recipe_id)

    def _recipe_grid_size(self, crafting_table: bool, grid_size: Literal[2, 3] | None) -> int:
        if grid_size is not None:
            if not isinstance(grid_size, int) or grid_size not in (2, 3):
                raise ValueError("grid_size must be 2, 3 or None")
            if grid_size == 3 and not crafting_table:
                raise ValueError("grid_size=3 requires crafting_table=True")
            return grid_size
        window = None if self.containers is None else self.containers.window
        return 3 if crafting_table and window is not None and window.container_type == "WORKBENCH" else 2

    def _grid_recipes(self, item: str, aux_value: int | None, size: int):
        name = item if ":" in item else f"minecraft:{item}"
        for recipe in self._by_id.values():
            if recipe.tag != "crafting_table" or not recipe.ingredients:
                continue
            if recipe.kind == "shaped_recipes":
                if (recipe.shape is None or not (1 <= recipe.width <= size and 1 <= recipe.height <= size)
                        or len(recipe.shape) != recipe.width * recipe.height):
                    continue
            elif recipe.kind not in ("shapeless_recipes", "user_data_shapeless_recipes"):
                continue
            if len(recipe.ingredients) > size * size:
                continue
            output = sum(result.count for result in recipe.results if result.name == name
                         and (aux_value is None or aux_value == result.aux_value))
            if output > 0:
                yield recipe, output

    def recipes_all(self, item: str, aux_value: int | None = None, *,
                    crafting_table: bool = True, grid_size: Literal[2, 3] | None = None) -> tuple[Recipe, ...]:
        size = self._recipe_grid_size(crafting_table, grid_size)
        return tuple(recipe for recipe, _ in self._grid_recipes(item, aux_value, size))

    def recipes_for(self, item: str, aux_value: int | None = None, min_result_count: int = 1, *,
                    crafting_table: bool = True, grid_size: Literal[2, 3] | None = None) -> tuple[Recipe, ...]:
        size = self._recipe_grid_size(crafting_table, grid_size)
        requested = index(min_result_count)
        if requested <= 0:
            raise ValueError("min_result_count must be positive")
        limited = None if self.environment is None else self.environment.limited_crafting
        if limited is None or self.inventory is None:
            return ()
        materials = Counter()
        for stack in self.inventory.slots.values():
            name = self.item_names.get(int(stack.id)) if int(stack.id) != 0 else None
            if name is not None and stack.stack_size > 0:
                materials[name, int(stack.aux_value)] += int(stack.stack_size)
        return tuple(
            recipe for recipe, output in self._grid_recipes(item, aux_value, size)
            if (not limited or recipe.recipe_id in self._unlocked)
            and self._allocate_materials(recipe, materials, (requested + output - 1) // output) is not None
        )

    def _allocate_materials(self, recipe: Recipe, materials: Counter, crafts: int):
        # Each inventory variant contributes a single shared capacity, even if
        # several ingredients can use it. Residual edges allow reassignment.
        source, sink = object(), object()
        edges = defaultdict(dict)
        edges[source].update(materials)
        needed = 0
        for slot, ingredient in enumerate(recipe.ingredients):
            names = (self._tag_members.get(ingredient.tag, ()) if ingredient.tag is not None
                     else (ingredient._item_name,))
            variants = [
                item for item, amount in materials.items()
                if amount >= ingredient.count and item[0] in names
                and ingredient.aux_value in (_ANY_AUX_VALUE, item[1])
            ]
            if not variants or (ingredient.count > 1 and len(variants) > 1):
                # A multi-item slot needs a single concrete variant per craft.
                # Vanilla grids only use one item per occupied slot.
                return None
            demand = ingredient.count * crafts
            for item in variants:
                edges[item][slot] = min(demand, materials[item])
            edges[slot][sink] = demand
            needed += demand
        if needed > materials.total():
            return None
        while needed:
            parent = {source: None}
            queue = deque([source])
            while queue and sink not in parent:
                node = queue.popleft()
                for neighbor, capacity in edges[node].items():
                    if capacity and neighbor not in parent:
                        parent[neighbor] = node
                        queue.append(neighbor)
            if sink not in parent:
                return None
            flow = needed
            node = sink
            while node is not source:
                previous = parent[node]
                flow = min(flow, edges[previous][node])
                node = previous
            node = sink
            while node is not source:
                previous = parent[node]
                edges[previous][node] -= flow
                edges[node][previous] = edges[node].get(previous, 0) + flow
                node = previous
            needed -= flow
        return tuple(
            tuple((variant, edges[slot][variant]) for variant in materials if edges[slot].get(variant, 0))
            for slot in range(len(recipe.ingredients))
        )

    def _tagged_auto_materials(self, recipe, items, times):
        materials = Counter()
        slots_by_variant = defaultdict(list)
        for slot, stack in sorted(items.items()):
            name = self.item_names.get(int(stack.id)) if stack.id else None
            if name is not None and 0 <= slot <= 255 and stack.stack_size > 0 and stack.net_id_variant:
                variant = name, int(stack.aux_value)
                slots_by_variant[variant].append(slot)
                materials[variant] += int(stack.stack_size)
        for ingredient in recipe.ingredients:
            if ingredient.tag is not None and not self._tag_members.get(ingredient.tag):
                raise ValueError(f"unknown recipe tag {ingredient.tag!r}")
        # The request has a single description for each recipe position. Plan
        # one craft using the number of complete rounds each variant can cover.
        per_craft = Counter({variant: amount // times for variant, amount in materials.items() if amount >= times})
        allocation = self._allocate_materials(recipe, per_craft, 1)
        if allocation is None:
            raise ValueError(f"not enough compatible ingredients for {times} crafts of {recipe.recipe_id}")
        return tuple((variant, slots_by_variant[variant]) for ((variant, _),) in allocation)

    async def wait_loaded(self, *, timeout: float = 15) -> tuple[Recipe, ...]:
        async with asyncio.timeout(timeout):
            await self._loaded.wait()
        return self.snapshot()


    async def craft_auto(
        self, recipe_id: str | int, destination_slot: int, *,
        times: int = 1, timeout: float = 15, _queued: bool = False,
    ):
        """Craft from observed player inventory and return the matching BDS item-stack verdict.

        Send the CraftRecipeAuto/CraftResults/Consume/Place sequence.
        Update inventory only from response slots.
        BDS 1.26.52 requires an open player inventory window for this action.
        Each recipe position uses one concrete variant for a tagged recipe.
        Automatic crafting requires one output stack.
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
        consumes_by_ingredient = [[] for _ in recipe.ingredients]
        ingredients = [None] * len(recipe.ingredients)

        def slot_info(name, slot, net_id):
            return cereal.SlotInfoData(
                full_container_name=b.FullContainerName(
                    name=b.ContainerEnumName[name], dynamic_id=None,
                ),
                slot=slot, net_id_variant=net_id,
            )

        tagged = any(ingredient.tag is not None for ingredient in recipe.ingredients)
        window = None if self.containers is None else self.containers.window
        if tagged:
            materials = self._tagged_auto_materials(recipe, items, times)
        else:
            materials = tuple(((i._item_name, _ANY_AUX_VALUE if i.aux_value is None else i.aux_value), items)
                              for i in recipe.ingredients)

        # Reserve exact variants before a wildcard can take them. The packet
        # still describes and consumes ingredients in the advertised order.
        for pos in sorted(range(len(materials)), key=lambda i: materials[i][0][1] == _ANY_AUX_VALUE):
            ingredient = recipe.ingredients[pos]
            (name, aux), slots = materials[pos]
            if name is None:
                raise ValueError("automatic crafting requires a known ingredient descriptor")
            ingredients[pos] = cereal.RecipeIngredientData(
                item_descriptor=cereal.ItemNameDescriptorData(full_name=name, aux_value=aux),
                stack_size=ingredient.count,
            )
            needed = ingredient.count * times
            for slot in slots:
                if needed <= 0:
                    break
                item = items[slot]
                if (not 0 <= slot <= 255 or remaining[slot] <= 0
                        or self.item_names.get(int(item.id)) != name
                        or aux not in (_ANY_AUX_VALUE, int(item.aux_value))):
                    continue
                used = min(needed, remaining[slot])
                consumes_by_ingredient[pos].append(cereal.ConsumeActionData(
                    amount=used,
                    source=slot_info(
                        "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                        slot, int(item.net_id_variant or 0),
                    ),
                ))
                remaining[slot] -= used
                needed -= used
            if needed:
                raise ValueError(f"not enough {name} to craft {recipe.recipe_id}")

        consumes = [action for group in consumes_by_ingredient for action in group]
        request_id = inventory._allocate_request_id()
        # Recipe data can expose this unsigned runtime ID as a signed int32.
        output_descriptor = b.SerializedNetworkItemStackDescriptor(
            id=output.item_id, stack_size=output.count * times,
            aux_value=output.aux_value, net_id_variant=None,
            block_runtime_id=output.block_runtime_id & 0xFFFFFFFF,
            user_data_buffer=output.user_data_buffer,
        )
        result_items = [
            cereal.NetworkItemInstanceDescriptorData(
                item_descriptor=cereal.ItemNameDescriptorData(
                    full_name=output.name or "", aux_value=output.aux_value,
                ),
                stack_size=output.count,
                block_runtime_id=output.block_runtime_id & 0xFFFFFFFF,
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
        planned_slots = {slot: _stack_snapshot(items.get(slot)) for slot in touched} if tagged else None
        result_stack = _stack_snapshot(output_descriptor) if tagged else None

        def before_send():
            if self.containers is not None and self.containers.window is not window:
                raise RuntimeError("the crafting window changed before submission")
            if self.get(recipe_id) is not recipe:
                raise RuntimeError("the recipe changed before submission")
            current = inventory.slots
            if any(_stack_snapshot(current.get(slot)) != planned for slot, planned in planned_slots.items()):
                raise RuntimeError("the crafting inventory changed before submission")

        def apply(response):
            current_slots = inventory.slots
            success = response.result.name == "SUCCESS"
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
                    current = current_slots.get(index)
                    delivering = success and index == destination_slot
                    if planned_slots is not None:
                        planned = planned_slots[index]
                        observed = _stack_snapshot(current)
                        if observed != planned:
                            # A receipt with only an amount and net ID cannot
                            # identify a replacement for a submitted stack.
                            expected = result_stack if delivering else planned
                            if expected is None or observed is None or not observed.net_id or not slot.amount:
                                continue
                            normalized = replace(
                                observed, count=expected.count,
                                net_id=expected.net_id if delivering else observed.net_id,
                            )
                            if normalized == expected:
                                corrected = _correct_observed_item(b, current, slot)
                                if corrected is not None:
                                    inventory._set_slot(index, corrected)
                            continue
                    elif current is not items.get(index):
                        inventory._set_slot(index, _correct_observed_item(b, current, slot))
                        continue
                    previous = current or inventory.item_or_empty(index)
                    fallback = previous
                    if delivering:
                        previous = output_descriptor
                        fallback = output_descriptor
                    inventory._set_slot(index, _response_item(b, previous, fallback, slot))

        return await inventory._request_item_stack(
            request, apply, timeout=timeout, queued=_queued, before_send=before_send if planned_slots is not None else None,
        )

    async def stage_grid(self, source_slot: int, grid_slot: int, amount: int = 1, *,
                         timeout: float = 15, _queued: bool = False):
        """Place an observed inventory item into the active 2x2 or workbench 3x3 grid.

        Return the matching BDS ItemStackResponse. Grid state comes from the
        player UI packets or the server's corrected request slots.
        """
        return await self._transfer_grid(source_slot, grid_slot, amount, into_grid=True,
                                         timeout=timeout, queued=_queued)

    async def return_grid(self, grid_slot: int, destination_slot: int, amount: int = 1, *,
                          timeout: float = 15, _queued: bool = False):
        """Return observed material from the active crafting grid to the player inventory."""
        return await self._transfer_grid(destination_slot, grid_slot, amount, into_grid=False,
                                         timeout=timeout, queued=_queued)

    async def _transfer_grid(self, player_slot, grid_slot, amount, *, into_grid, timeout, queued):
        window = self._sync_grid_window()
        if not into_grid and (window is None or window.container_type not in ("INVENTORY", "WORKBENCH")):
            raise RuntimeError("open the inventory screen or a workbench before returning crafting inputs")
        if window is None:
            raise RuntimeError("open the inventory screen before staging crafting inputs")
        inventory = self.inventory
        if inventory is None:
            raise RuntimeError("crafting requires player inventory")
        grid_slots = (range(32, 41) if window.container_type == "WORKBENCH"
                      else range(28, 32))
        if grid_slot not in grid_slots:
            raise ValueError(f"active crafting grid slots are {grid_slots.start} through {grid_slots.stop - 1}")
        if not 0 <= player_slot <= 255:
            raise ValueError("player slot must fit in a byte")
        if not 1 <= amount <= 255:
            raise ValueError("amount must be between 1 and 255")
        original_player = inventory.slots.get(player_slot)
        original_grid = self._grid_slots.get(grid_slot)
        source = original_player if into_grid else original_grid
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

        player_label = "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"
        grid_label = "CRAFTING_INPUT_CONTAINER"
        player = slot_info(player_label, player_slot, int(original_player.net_id_variant or 0) if original_player else 0)
        grid = slot_info(grid_label, grid_slot, int(original_grid.net_id_variant or 0) if original_grid else 0)
        action = (cereal.PlaceActionData(amount=amount, source=player, destination=grid) if into_grid
                  else cereal.TakeActionData(amount=amount, source=grid, destination=player))
        request_id = inventory._allocate_request_id()
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=[action],
            strings_to_filter=[], strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )

        def apply(reply):
            def correct(current, original, slot):
                if current is not original:
                    return _correct_observed_item(b, current, slot)
                previous = current or inventory.item_or_empty(-1)
                fallback = source if reply.result.name == "SUCCESS" else previous
                if int(slot.amount) > 0 and int(previous.id) == int(fallback.id) == 0:
                    return None
                return _response_item(b, previous, fallback, slot)

            for container in reply.containers or ():
                name = container.full_container_name.name.name
                for slot in container.slots:
                    index = int(slot.slot)
                    if name in ("HOTBAR_CONTAINER", "INVENTORY_CONTAINER", player_label) and index == player_slot:
                        current = inventory.slots.get(index)
                        inventory._set_slot(index, correct(current, original_player, slot))
                    elif (name == grid_label and index == grid_slot
                          and self.containers.window is window):
                        current = self._grid_slots.get(index)
                        self._set_grid_slot(index, correct(current, original_grid, slot))

        def check_return_window():
            if self.containers.window is not window:
                raise RuntimeError("the crafting window changed before returning ingredients")

        public_return = not into_grid and not queued
        expected = (_stack_snapshot(original_grid), _stack_snapshot(original_player)) if public_return else None

        def check_return_write():
            nonlocal original_grid, original_player
            check_return_window()
            grid_item = self._grid_slots.get(grid_slot)
            player_item = inventory.slots.get(player_slot)
            if (_stack_snapshot(grid_item), _stack_snapshot(player_item)) != expected:
                raise RuntimeError("crafting transfer slots changed before returning ingredients")
            owner = inventory._pending_requests[request_id]
            if any(pending is not owner for pending in inventory._requests_for_endpoints({(None, player_slot)})):
                raise RuntimeError("the player slot has a pending item-stack request")
            original_grid, original_player = grid_item, player_item

        return await inventory._request_item_stack(
            request, apply, timeout=timeout, queued=queued,
            before_send=check_return_window if not into_grid and queued else None,
            _at_write=check_return_write if public_return else None,
        )

    async def craft_grid(self, recipe_id: str | int, destination_slot: int, *,
                         additional_destination_slots: tuple[int, ...] = (),
                         times: int = 1, timeout: float = 15, _queued: bool = False):
        """Craft from the observed 2x2 or workbench 3x3 grid; return BDS's response.

        The server decides whether the staged layout matches the chosen recipe.
        """
        recipe = self.get(recipe_id)
        if recipe is None:
            raise KeyError(f"the server sent no recipe {recipe_id!r}")
        window = self._sync_grid_window()
        inventory = self.inventory
        if inventory is None or window is None:
            raise RuntimeError("open the inventory screen before grid crafting")
        if not 0 <= destination_slot <= 255:
            raise ValueError("destination slot must fit in a byte")
        if not 1 <= times <= 255:
            raise ValueError("craft count must be between 1 and 255")
        destination_slots = (destination_slot, *additional_destination_slots)
        if len(recipe.results) != len(destination_slots):
            raise ValueError("grid crafting requires one destination slot per recipe result")
        multiple = len(destination_slots) > 1
        if multiple:
            if window.container_type != "WORKBENCH" or times != 1:
                raise ValueError("multiple results require one craft in an open workbench")
            if any(type(slot) is not int or not 0 <= slot < 36 for slot in destination_slots):
                raise ValueError("result destinations must be main inventory slots (0..35)")
            if len(set(destination_slots)) != len(destination_slots):
                raise ValueError("result destinations must be distinct")
            if any(not result.item_id or not result.name for result in recipe.results):
                raise ValueError("all recipe results must have observed item identities")
        if any(not 1 <= output.count * times <= 255 for output in recipe.results):
            raise ValueError("result stack count must fit in a byte")
        grid_slots = (range(32, 41) if window.container_type == "WORKBENCH"
                      else range(28, 32))
        if not self._unconfirmed_grid_slots.isdisjoint(grid_slots):
            raise RuntimeError("unconfirmed crafting inputs require a new observation")

        def observed_grid():
            return {slot: item for slot, item in self._grid_slots.items()
                    if slot in grid_slots and int(item.id) != 0}

        grid = observed_grid()
        if not grid:
            raise ValueError("no observed crafting ingredients in the active grid")
        carried = inventory.slots
        observed_destinations = {slot: carried.get(slot) for slot in destination_slots}
        if multiple:
            if any(item is None or item.id or item.stack_size or item.net_id_variant
                   for item in observed_destinations.values()):
                raise ValueError("result destinations must be observed empty")
            if any(not item.net_id_variant or item.net_id_variant < 0 for item in grid.values()):
                raise ValueError("crafting inputs must have observed stack IDs")
        destinations = (observed_destinations if multiple else
                        {destination_slot: inventory.item_or_empty(destination_slot)})
        b = self.packets.protocol.binding
        cereal = b.ItemStackRequestCereal

        def slot_info(name, slot, net_id):
            return cereal.SlotInfoData(
                full_container_name=b.FullContainerName(
                    name=b.ContainerEnumName[name], dynamic_id=None,
                ), slot=slot, net_id_variant=net_id,
            )

        request_id = inventory._allocate_request_id()
        output_descriptors = {
            slot: b.SerializedNetworkItemStackDescriptor(
                id=output.item_id, stack_size=output.count * times,
                aux_value=output.aux_value, net_id_variant=None,
                block_runtime_id=output.block_runtime_id & 0xFFFFFFFF,
                user_data_buffer=output.user_data_buffer,
            )
            for slot, output in zip(destination_slots, recipe.results)
        }
        actions = [
            cereal.CraftRecipeActionData(recipe_net_id=recipe.net_id, num_crafts=times),
            cereal.CraftResultsActionData(
                craft_results=[cereal.NetworkItemInstanceDescriptorData(
                    item_descriptor=cereal.ItemNameDescriptorData(
                        full_name=output.name or "", aux_value=output.aux_value,
                    ),
                    stack_size=output.count,
                    block_runtime_id=output.block_runtime_id & 0xFFFFFFFF,
                    user_data_buffer=output.user_data_buffer,
                ) for output in recipe.results], num_crafts=times,
            ),
            *[
                cereal.ConsumeActionData(
                    amount=times,
                    source=slot_info("CRAFTING_INPUT_CONTAINER", slot,
                                     int(item.net_id_variant or 0)),
                )
                for slot, item in sorted(grid.items())
            ],
        ]
        for result_index, (slot, output) in enumerate(output_descriptors.items()):
            container = "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"
            if multiple:
                actions.append(cereal.CreateActionData(results_index=result_index))
                container = "HOTBAR_CONTAINER" if slot < 9 else "INVENTORY_CONTAINER"
            actions.append(cereal.PlaceActionData(
                amount=int(output.stack_size),
                source=slot_info("CREATED_OUTPUT_CONTAINER", 50, request_id),
                destination=slot_info(container, slot, int(destinations[slot].net_id_variant or 0)),
            ))
        request = b.ItemStackRequestPacketData.RequestData(
            client_request_id=b.ItemStackRequestId(raw_id=request_id),
            actions=actions, strings_to_filter=[],
            strings_to_filter_origin=b.TextProcessingEventOrigin.UNKNOWN,
        )

        def apply(reply):
            for container in reply.containers or ():
                name = container.full_container_name.name.name
                for slot in container.slots:
                    index = int(slot.slot)
                    if (name == "CRAFTING_INPUT_CONTAINER" and index in grid
                            and self.containers.window is window):
                        current = self._grid_slots.get(index)
                        source = grid[index]
                        updated = (_response_item(b, source, source, slot) if current is source
                                   else _correct_observed_item(b, current, slot))
                        self._set_grid_slot(index, updated)
                    elif name in _PLAYER_CONTAINERS and index in output_descriptors:
                        current = inventory.slots.get(index)
                        destination = destinations[index]
                        output = output_descriptors[index]
                        if current is not observed_destinations[index]:
                            updated = _correct_observed_item(b, current, slot)
                        else:
                            fallback = output if reply.result.name == "SUCCESS" else destination
                            updated = _response_item(b, destination, fallback, slot)
                        inventory._set_slot(index, updated)

        if not multiple:
            return await inventory._request_item_stack(request, apply, timeout=timeout, queued=_queued)

        expected_grid = {slot: _stack_snapshot(item) for slot, item in grid.items()}
        expected_destinations = {slot: _stack_snapshot(item) for slot, item in observed_destinations.items()}
        # A second multi-result request cannot use the workbench until this request is adjudicated.
        endpoints = frozenset({(None, slot) for slot in destination_slots} | {(id(window), -1)})
        submitted_grid_versions = {}
        changed_destinations = set()

        def observe_destination(change):
            if change.slot in expected_destinations:
                changed_destinations.add(change.slot)

        def at_write():
            if self._sync_grid_window() is not window:
                raise RuntimeError("the crafting window changed before submission")
            if self.get(recipe_id) != recipe:
                raise RuntimeError("the crafting recipe changed before submission")
            if (not self._unconfirmed_grid_slots.isdisjoint(grid_slots)
                    or {slot: _stack_snapshot(item) for slot, item in observed_grid().items()} != expected_grid):
                raise RuntimeError("crafting inputs changed before submission")
            carried = inventory.slots
            current_destinations = {slot: carried.get(slot) for slot in destination_slots}
            if ({slot: _stack_snapshot(item) for slot, item in current_destinations.items()} != expected_destinations
                    or any(slot in inventory._unconfirmed_transfer_slots or slot in inventory._count_observation_gaps
                           for slot in destination_slots)):
                raise RuntimeError("crafting destinations are no longer confirmed empty")
            owner = inventory._pending_requests[request_id]
            if any(pending is not owner for pending in inventory._requests_for_endpoints(endpoints)):
                raise RuntimeError("the crafting window or a result destination has a pending item-stack request")
            submitted_grid_versions.update((slot, self._grid_versions.get(slot, 0)) for slot in grid)
            unsubscribe = inventory.subscribe_inventory_changes(observe_destination)
            # A submitted request can still receive its verdict after the caller stops waiting.
            owner.reply.add_done_callback(lambda _: unsubscribe())

        def unique_receipt(rows):
            if rows and rows[0].slot == rows[0].requested_slot and all(row == rows[0] for row in rows[1:]):
                return rows[0]
            return None

        def apply_multiple(reply):
            if reply.result.name != "SUCCESS":
                return
            inputs, outputs = defaultdict(list), defaultdict(list)
            for group in reply.containers or ():
                name = group.full_container_name.name.name
                if name == "CRAFTING_INPUT_CONTAINER":
                    receipts = inputs
                elif name in _PLAYER_CONTAINERS:
                    receipts = outputs
                else:
                    continue
                for slot in group.slots:
                    receipts[int(slot.slot)].append(slot)
                    if slot.requested_slot != slot.slot:
                        receipts[int(slot.requested_slot)].append(slot)

            if self.containers.window is window:
                for index, version in submitted_grid_versions.items():
                    if self._grid_versions.get(index, 0) != version:
                        continue
                    row = unique_receipt(inputs.get(index))
                    current = self._grid_slots.get(index)
                    item = None if row is None else _correct_observed_item(b, current, row)
                    self._set_grid_slot(index, item)
                    if item is None:
                        self._unconfirmed_grid_slots.add(index)

            for index, output in output_descriptors.items():
                if index in changed_destinations:
                    continue
                row = unique_receipt(outputs.get(index))
                net_id = None if row is None else row.item_stack_net_id
                if row is not None and row.amount == output.stack_size and net_id is not None and net_id.raw_id > 0:
                    inventory._set_slot(index, _response_item(b, destinations[index], output, row))
                else:
                    inventory._count_observation_gaps.add(index)
                    inventory._set_slot(index, None)

        return await inventory._request_item_stack(request, apply_multiple, timeout=timeout, queued=_queued,
                                                   _endpoints=endpoints, _at_write=at_write)

    def close(self) -> None:
        self._unsubscribe()
        if self._qa_recipe_aliases is not None:
            self._qa_recipe_aliases.clear()
