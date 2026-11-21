"""Server-issued crafting recipes, retained across the login/ready boundary."""

import asyncio
from dataclasses import dataclass


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


@dataclass(frozen=True, slots=True)
class Recipe:
    recipe_id: str
    net_id: int
    kind: str
    ingredients: tuple[RecipeIngredient, ...]
    results: tuple[RecipeResult, ...]
    tag: str


class Recipes:
    def __init__(self, packets, item_names: dict[int, str], initial_packets=()):
        self.packets = packets
        self.item_names = item_names
        self._by_id: dict[str, Recipe] = {}
        self._by_net_id: dict[int, Recipe] = {}
        self._loaded = asyncio.Event()
        # Process login-time packets before switching to the live subscriber.
        for raw in initial_packets:
            self._on_packet(raw)
        self._unsubscribe = packets.subscribe_raw(self._on_packet)

    def _on_packet(self, raw) -> None:
        if raw.packet_id != self.packets.protocol.binding.CraftingDataPacket.ID:
            return
        self.load(self.packets.protocol.decode(raw))

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
                for ingredient in getattr(entry, "ingredients", ()) or ():
                    if int(ingredient.stack_size) <= 0:
                        continue
                    descriptor = ingredient.descriptor or {}
                    ingredients.append(RecipeIngredient(
                        descriptor.get("name"), descriptor.get("item_tag"),
                        int(ingredient.stack_size),
                    ))
                results = tuple(
                    RecipeResult(int(item.id), self.item_names.get(int(item.id)),
                                 int(item.stack_size))
                    for item in getattr(entry, "results", ()) or ()
                )
                recipe = Recipe(str(recipe_id), int(net_id), kind, tuple(ingredients),
                                results, str(getattr(entry, "tag", "")))
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

    def close(self) -> None:
        self._unsubscribe()
