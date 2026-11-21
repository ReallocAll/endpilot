import asyncio
import unittest
from types import SimpleNamespace

from endpilot.protocol import Protocol, RawPacket
from endpilot.recipes import Recipes


def ingredient(name, count):
    return SimpleNamespace(descriptor={"name": name}, stack_size=count)


def recipe(name, net_id, *, count=1):
    return SimpleNamespace(
        recipe_id=name, net_id=net_id,
        ingredients=[ingredient("minecraft:planks", 2)],
        results=[SimpleNamespace(id=42, stack_size=count)],
        tag="crafting_table",
    )


def packet(**fields):
    fields.setdefault("clear_recipes", False)
    return SimpleNamespace(**fields)


class RecipeTests(unittest.IsolatedAsyncioTestCase):
    async def test_join_time_and_live_catalog_packets(self):
        b = Protocol(2193).binding
        initial = RawPacket(b.CraftingDataPacket.ID, b"initial")
        refreshed = RawPacket(b.CraftingDataPacket.ID, b"new")
        ignored = RawPacket(b.StartGamePacket.ID, b"ignored")
        decoded = {
            b"initial": packet(shapeless_recipes=[recipe("minecraft:stick", 7, count=4)]),
            b"new": packet(clear_recipes=True, shaped_recipes=[recipe("minecraft:pickaxe", 11)]),
        }
        subscribers = []
        packets = SimpleNamespace(
            protocol=SimpleNamespace(
                binding=b, decode=lambda raw: decoded[raw.body],
            ),
            subscribe_raw=lambda cb: subscribers.append(cb) or (lambda: subscribers.remove(cb)),
        )
        catalog = Recipes(packets, {42: "minecraft:stick"}, [initial])
        try:
            ready = await catalog.wait_loaded(timeout=0.01)
            self.assertEqual(len(ready), 1)
            self.assertEqual(ready[0].results[0].name, "minecraft:stick")
            self.assertEqual(ready[0].results[0].count, 4)
            self.assertEqual(ready[0].ingredients[0].count, 2)
            self.assertEqual(catalog.get("minecraft:stick"), catalog.get(7))
            subscribers[0](ignored)
            self.assertEqual(catalog.snapshot(), ready)
            subscribers[0](refreshed)
            self.assertIsNone(catalog.get(7))
            self.assertEqual(catalog.get(11).recipe_id, "minecraft:pickaxe")
            self.assertEqual(catalog.get(11).results[0].count, 1)
        finally:
            catalog.close()
        self.assertEqual(subscribers, [])

    async def test_no_packet_times_out_but_empty_catalog_is_valid(self):
        b = Protocol(2193).binding
        callbacks = []
        raw = RawPacket(b.CraftingDataPacket.ID, b"empty")
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda _: packet()),
            subscribe_raw=lambda cb: callbacks.append(cb) or (lambda: callbacks.remove(cb)),
        )
        catalog = Recipes(packets, {})
        try:
            with self.assertRaises(TimeoutError):
                await catalog.wait_loaded(timeout=0.001)
            callbacks[0](raw)
            self.assertEqual(await catalog.wait_loaded(timeout=0.01), ())
        finally:
            catalog.close()
