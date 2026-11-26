import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from endpilot import Bot
from endpilot.protocol import write_batch


class RecipeDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("RecipeTester", protocol=2225)
        self.b = self.bot.protocol.binding
        self.incoming = asyncio.Queue()
        self.addAsyncCleanup(self.bot.close)

    def rule(self, value):
        b = self.b
        return b.GameRulesChangedPacketData(rules=[b.GameRule(name="doLimitedCrafting", value=value)])

    def unlock(self, kind, *recipe_ids):
        packet = self.b.UnlockedRecipesPacket
        return packet(packet_type=packet.PacketType[kind], unlocked_recipes=recipe_ids)

    def inventory(self, *items):
        b = self.b
        return b.InventoryContentPacket(inventory_id=b.ContainerID.INVENTORY, slots=[
            b.SerializedNetworkItemStackDescriptor(id=item, stack_size=count, aux_value=aux)
            for item, count, aux in items
        ])

    def catalog(self):
        b = self.b
        tag = b.SerializedRecipeIngredient(descriptor={"item_tag": "minecraft:planks"},
                                           stack_size=1, aux_value=32767)
        oak = b.SerializedRecipeIngredient(descriptor={"name": "minecraft:oak_planks"},
                                           stack_size=1, aux_value=3)
        unknown = b.SerializedRecipeIngredient(descriptor={"item_tag": "test:mystery"},
                                               stack_size=1, aux_value=32767)
        oak_pair = b.SerializedRecipeIngredient(descriptor={"name": "minecraft:oak_planks"},
                                                stack_size=2, aux_value=3)
        blank = b.SerializedRecipeIngredient(stack_size=0, aux_value=32767)
        sticks = b.SerializedNetworkItemInstanceDescriptor(id=3, stack_size=4)
        special_stick = b.SerializedNetworkItemInstanceDescriptor(id=3, stack_size=1, aux_value=2)
        pickaxe = b.SerializedNetworkItemInstanceDescriptor(id=4, stack_size=1)
        return b.CraftingDataPacket(shaped_recipes=[
            b.ShapedRecipePayload(recipe_id="test:mix", net_id=1, width=2, height=2,
                                  ingredients=[tag, blank, oak, blank], results=[sticks], tag="crafting_table"),
            b.ShapedRecipePayload(recipe_id="test:wide", net_id=2, width=3, height=1,
                                  ingredients=[oak, blank, tag], results=[pickaxe], tag="crafting_table"),
        ], shapeless_recipes=[
            b.ShapelessRecipePayload(recipe_id="test:unknown", net_id=3,
                                     ingredients=[unknown], results=[special_stick], tag="crafting_table"),
            b.ShapelessRecipePayload(recipe_id="test:pair", net_id=4,
                                     ingredients=[oak_pair], results=[special_stick], tag="crafting_table"),
        ])

    def deliver(self, *packets):
        self.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in packets))

    async def delivered(self, *packets):
        received = asyncio.create_task(self.bot.packets.wait_for_packet(packets[-1].ID, timeout=1))
        await asyncio.sleep(0)
        self.deliver(*packets)
        await received

    async def join(self, initial_rule=None, before_spawn=(), *, addon=False):
        b = self.b
        self.deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        settings = b.LevelSettings() if initial_rule is None else b.LevelSettings(game_rules=self.rule(initial_rule))
        self.deliver(
            b.ResourcePacksInfoPacket(has_addon_packs=addon), b.ResourcePackStackPacket(),
            b.StartGamePacket(runtime_id=10, entity_id=11, server_version="1.26.66", settings=settings),
            self.catalog(),
            b.ItemRegistryPacket(items=[b.ItemData(id=i, name=f"minecraft:{name}") for i, name in enumerate(
                ("oak_planks", "birch_planks", "stick", "wooden_pickaxe"), start=1,
            )]),
            self.inventory((1, 1, 3), (2, 1, 0)), *before_spawn, b.PlayStatusPacket(status=3),
        )
        transport = SimpleNamespace(recv=self.incoming.get, send=AsyncMock(), close=AsyncMock())
        with patch("endpilot.transport.nethernet.connect", return_value=transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    async def test_shapes_aux_and_overlapping_inventory_assignments(self):
        await self.join(False)
        mixed = self.bot.get_recipe(1)
        self.assertEqual((mixed.width, mixed.height, mixed.shape), (
            2, 2, (mixed.ingredients[0], None, mixed.ingredients[1], None),
        ))
        self.assertEqual((mixed.ingredients[0].tag, mixed.ingredients[1].aux_value), ("minecraft:planks", 3))
        self.assertEqual(self.bot.recipes_all("stick", 0), (mixed,))
        self.assertEqual(self.bot.recipes_all("stick", 1), ())
        self.assertEqual(len(self.bot.recipes_all("stick", 2)), 2)
        self.assertEqual(self.bot.recipes_for("stick", 0, 4), (mixed,))
        self.assertEqual(self.bot.recipes_for("stick", 0, 5), ())
        self.assertEqual(self.bot.recipes_for("stick", 2), ())
        self.assertEqual(self.bot.recipes_all("wooden_pickaxe"), ())
        await self.delivered(self.b.ContainerOpenPacket(container_id=1, type=self.b.ContainerType.WORKBENCH))
        self.assertEqual(self.bot.recipes_for("wooden_pickaxe"), (self.bot.get_recipe(2),))
        self.assertEqual(self.bot.recipes_for("wooden_pickaxe", crafting_table=False), ())
        await self.delivered(self.b.ContainerClosePacket(container_id=1))
        self.assertEqual(self.bot.recipes_all("wooden_pickaxe"), ())
        await self.delivered(self.inventory((1, 1, 3)))
        self.assertEqual(self.bot.recipes_for("stick", 0), ())
        await self.delivered(self.inventory((1, 1, 3), (2, 1, 0), (1, 1, 3), (2, 1, 0)))
        self.assertEqual(self.bot.recipes_for("stick", min_result_count=8), (mixed,))
        self.assertEqual(self.bot.recipes_for("stick", 2), (self.bot.get_recipe(4),))
        await self.delivered(self.inventory((1, 1, 2), (2, 1, 0)))
        self.assertEqual(self.bot.recipes_for("stick"), ())

    async def test_unlocked_updates_across_join_and_rule_authority(self):
        b = self.b
        await self.join(False, (
            self.unlock("INITIALLY_UNLOCKED_RECIPES", "test:mix"),
            self.unlock("NEWLY_UNLOCKED_RECIPES", "test:wide"),
            b.GameRulesChangedPacket(rule_data=self.rule(True)),
            self.unlock("REMOVE_UNLOCKED_RECIPES", "test:mix"),
        ))
        self.assertIs(self.bot.limited_crafting, True)
        self.assertEqual(self.bot.unlocked_recipe_ids, {"test:wide"})
        self.assertEqual(self.bot.recipes_for("stick"), ())
        await self.delivered(self.unlock("NEWLY_UNLOCKED_RECIPES", "test:mix"))
        self.assertEqual(self.bot.recipes_for("stick"), (self.bot.get_recipe(1),))
        await self.delivered(self.unlock("REMOVE_ALL_UNLOCKED_RECIPES"))
        self.assertEqual(self.bot.unlocked_recipe_ids, frozenset())
        self.assertEqual(self.bot.recipes_for("stick"), ())
        await self.delivered(b.GameRulesChangedPacket(rule_data=self.rule(False)))
        self.assertEqual(self.bot.recipes_for("stick"), (self.bot.get_recipe(1),))
        await self.delivered(b.GameRulesChangedPacket(rule_data=self.rule(2)))
        self.assertIsNone(self.bot.limited_crafting)
        self.assertEqual(self.bot.recipes_for("stick"), ())

    async def test_addon_tags_and_unknown_rules_do_not_claim_craftability(self):
        await self.join(addon=True)
        self.assertIsNone(self.bot.limited_crafting)
        self.assertTrue(self.bot.recipes_all("stick"))
        self.assertEqual(self.bot.recipes_for("stick"), ())
        await self.delivered(self.b.GameRulesChangedPacket(rule_data=self.rule(False)))
        self.assertEqual(self.bot.recipes_for("stick", 0), ())
        await self.delivered(self.inventory((1, 2, 3)))
        self.assertEqual(self.bot.recipes_for("stick", 2), (self.bot.get_recipe(4),))

    async def test_dimension_change_discards_workbench_until_server_reopens_it(self):
        b = self.b
        await self.join(False, (self.unlock("INITIALLY_UNLOCKED_RECIPES", "test:wide"),))
        carried = self.bot.inventory_slots
        personal, wide = self.bot.get_recipe(1), self.bot.get_recipe(2)
        opened = b.ContainerOpenPacket(container_id=1, type=b.ContainerType.WORKBENCH)
        old_slots = b.InventoryContentPacket(inventory_id=1, slots=[
            b.SerializedNetworkItemStackDescriptor(id=1, stack_size=2),
        ])
        await self.delivered(opened, old_slots)
        window = self.bot.open_window
        self.assertEqual(window.container_type, "WORKBENCH")
        self.assertEqual(self.bot.container_slots[0].stack_size, 2)
        self.assertEqual(self.bot.recipes_for("wooden_pickaxe"), (wide,))

        await self.delivered(b.ChangeDimensionPacket(dimension_id=1), old_slots)
        self.assertIsNone(self.bot.open_window)
        self.assertIsNone(self.bot.container_slots)
        self.assertEqual(self.bot.recipes_all("wooden_pickaxe"), ())
        self.assertEqual(self.bot.recipes_for("wooden_pickaxe"), ())
        self.assertEqual(self.bot.recipes_for("stick"), (personal,))
        self.assertEqual(self.bot.inventory_slots, carried)
        self.assertEqual(self.bot.unlocked_recipe_ids, {"test:wide"})

        await self.delivered(opened)
        self.assertEqual(self.bot.open_window, window)
        self.assertIsNot(self.bot.open_window, window)
        self.assertIsNone(self.bot.container_slots)
        self.assertEqual(self.bot.recipes_all("wooden_pickaxe"), (wide,))
        self.assertEqual(self.bot.recipes_for("wooden_pickaxe"), (wide,))
