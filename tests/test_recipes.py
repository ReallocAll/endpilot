import unittest
from types import SimpleNamespace

from endpilot.protocol import Protocol, RawPacket
from endpilot.qa import PacketTimeline
from endpilot.recipes import Recipes


def ingredient(name, count):
    return SimpleNamespace(descriptor={"name": name}, stack_size=count)


def result(item_id=42, count=1, *, aux=3, block=17, data=b"result-nbt"):
    return SimpleNamespace(
        id=item_id, stack_size=count, aux_value=aux,
        block_runtime_id=block, user_data_buffer=data,
    )


def recipe(name, net_id, *, count=1):
    return SimpleNamespace(
        recipe_id=name, net_id=net_id,
        ingredients=[ingredient("minecraft:planks", 2)],
        results=[result(count=count)],
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
            self.assertEqual(ready[0].results[0].aux_value, 3)
            self.assertEqual(ready[0].results[0].block_runtime_id, 17)
            self.assertEqual(ready[0].results[0].user_data_buffer, b"result-nbt")
            self.assertEqual(ready[0].ingredients[0].count, 2)
            self.assertEqual(catalog.get("minecraft:stick"), catalog.get(7))
            subscribers[0](ignored)
            self.assertEqual(catalog.snapshot(), ready)
            subscribers[0](refreshed)
            self.assertIsNone(catalog.get(7))
            self.assertEqual(catalog.get(11).recipe_id, "minecraft:pickaxe")
            self.assertEqual(catalog.get(11).results[0].count, 1)
            self.assertEqual(ready[0].recipe_id, "minecraft:stick")
        finally:
            catalog.close()
        self.assertEqual(subscribers, [])

    async def test_smithing_transform_and_trim_use_named_ingredients(self):
        b = Protocol(2193).binding
        subscribers = []
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: None),
            subscribe_raw=lambda cb: subscribers.append(cb) or (lambda: subscribers.remove(cb)),
        )
        catalog = Recipes(packets, {-203: "minecraft:diamond_sword"})
        try:
            ingredients = {
                "template_ingredient": ingredient("minecraft:netherite_upgrade_smithing_template", 1),
                "base_ingredient": ingredient("minecraft:diamond_sword", 1),
                "addition_ingredient": ingredient("minecraft:netherite_ingot", 1),
            }
            catalog.load(packet(smithing_transform_recipes=[SimpleNamespace(
                recipe_id="smithing_transform", net_id=23, tag="smithing_table",
                result=result(-203, aux=7, block=-1, data=b"smithing"),
                **ingredients,
            )], smithing_trim_recipes=[SimpleNamespace(
                recipe_id="smithing_trim", net_id=24, tag="smithing_table",
                **ingredients,
            )]))
            transform = catalog.get(23)
            self.assertEqual(transform.kind, "smithing_transform_recipes")
            self.assertEqual(len(transform.ingredients), 3)
            self.assertEqual(transform.ingredients[2].name, "minecraft:netherite_ingot")
            self.assertEqual(transform.results[0].item_id, -203)
            self.assertEqual(transform.results[0].name, "minecraft:diamond_sword")
            self.assertEqual(transform.results[0].aux_value, 7)
            self.assertEqual(transform.results[0].block_runtime_id, -1)
            self.assertEqual(transform.results[0].user_data_buffer, b"smithing")
            trim = catalog.get("smithing_trim")
            self.assertEqual(len(trim.ingredients), 3)
            self.assertEqual(trim.results, ())
        finally:
            catalog.close()
        self.assertEqual(subscribers, [])

    async def test_incremental_recipe_replacements_reconcile_both_indexes(self):
        b = Protocol(2193).binding
        subscribers = []
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: None),
            subscribe_raw=lambda cb: subscribers.append(cb) or (lambda: subscribers.remove(cb)),
        )
        catalog = Recipes(packets, {})
        try:
            catalog.load(packet(shapeless_recipes=[recipe("a", 3), recipe("b", 4)]))
            catalog.load(packet(shapeless_recipes=[recipe("a", 5)]))
            self.assertIsNone(catalog.get(3))
            self.assertEqual(catalog.get("a"), catalog.get(5))
            catalog.load(packet(shapeless_recipes=[recipe("c", 4)]))
            self.assertIsNone(catalog.get("b"))
            self.assertEqual(catalog.get("c"), catalog.get(4))
            self.assertEqual(len(catalog.snapshot()), 2)
            self.assertIsNone(catalog._qa_recipe_aliases)
            self.assertIsNone(catalog._get_qa_recipe(3))
            self.assertIsNone(catalog._get_qa_recipe("b"))
        finally:
            catalog.close()

    async def test_qa_recipe_keys_retain_displaced_entries_independently(self):
        packets = SimpleNamespace(
            _timeline=PacketTimeline(_retain_legacy_recipes=True),
            subscribe_raw=lambda _: lambda: None,
        )
        catalog = Recipes(packets, {})
        try:
            catalog.load(packet(shapeless_recipes=[recipe("a", 3), recipe("b", 4)]))
            first_a, first_b = catalog.get(3), catalog.get(4)
            self.assertEqual(catalog._qa_recipe_aliases, {})
            catalog.load(packet(shapeless_recipes=[recipe("a", 5), recipe("c", 4)]))
            self.assertIsNone(catalog.get(3))
            self.assertIsNone(catalog.get("b"))
            self.assertIs(catalog._get_qa_recipe(3), first_a)
            self.assertIs(catalog._get_qa_recipe("b"), first_b)
            self.assertIs(catalog._get_qa_recipe("a"), catalog.get(5))
            self.assertIs(catalog._get_qa_recipe(4), catalog.get("c"))

            latest_a = catalog.get("a")
            catalog.load(packet(shapeless_recipes=[recipe("b", 5, count=2)]))
            self.assertIsNone(catalog.get("a"))
            self.assertIs(catalog._get_qa_recipe("a"), latest_a)
            self.assertIs(catalog._get_qa_recipe(5), catalog.get("b"))
            self.assertEqual(set(catalog._qa_recipe_aliases), {3, "a"})

            catalog.load(packet(shapeless_recipes=[recipe("a", 3, count=6)]))
            self.assertEqual(catalog._qa_recipe_aliases, {})
            self.assertIs(catalog._get_qa_recipe(3), catalog.get("a"))
            self.assertEqual(catalog._get_qa_recipe("a").results[0].count, 6)

            catalog.load(packet(shapeless_recipes=[recipe("a", 6)]))
            self.assertIsNotNone(catalog._get_qa_recipe(3))
            catalog.load(packet(clear_recipes=True))
            self.assertIsNone(catalog._get_qa_recipe(3))
            self.assertIsNone(catalog._get_qa_recipe("a"))
            self.assertEqual(catalog.snapshot(), ())
        finally:
            catalog.close()

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
