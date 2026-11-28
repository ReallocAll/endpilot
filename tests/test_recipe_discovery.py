import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from endpilot import Bot, RecipeDeltaPlan, RecipeItemAmount
from endpilot.protocol import read_batch, write_batch


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

    def shapeless(self, recipe_id, net_id, ingredients, results, *, clear=False):
        b = self.b
        return b.CraftingDataPacket(clear_recipes=clear, shapeless_recipes=[
            b.ShapelessRecipePayload(
                recipe_id=recipe_id, net_id=net_id, tag="crafting_table",
                ingredients=[b.SerializedRecipeIngredient(descriptor=descriptor, stack_size=count, aux_value=aux)
                             for descriptor, count, aux in ingredients],
                results=[b.SerializedNetworkItemInstanceDescriptor(id=item_id, stack_size=count, aux_value=aux)
                         for item_id, count, aux in results],
            ),
        ])

    def plank_ring(self):
        b = self.b
        planks = b.SerializedRecipeIngredient(descriptor={"item_tag": "minecraft:planks"},
                                              stack_size=2, aux_value=32767)
        center = b.SerializedRecipeIngredient(descriptor={"name": "minecraft:stick"}, stack_size=1, aux_value=0)
        catalog = b.CraftingDataPacket(clear_recipes=False, shaped_recipes=[b.ShapedRecipePayload(
            recipe_id="test:plank_ring", net_id=17, width=3, height=3, tag="crafting_table",
            ingredients=[planks] * 4 + [center] + [planks] * 4,
            results=[b.SerializedNetworkItemInstanceDescriptor(id=4, stack_size=1)],
        )])
        woods = ("oak", "birch", "spruce", "jungle", "acacia", "dark_oak", "mangrove")
        registry = b.ItemRegistryPacket(items=[
            b.ItemData(id=3, name="minecraft:stick"),
            b.ItemData(id=4, name="minecraft:wooden_pickaxe"),
            *(b.ItemData(id=item_id, name=f"minecraft:{wood}_planks") for item_id, wood in enumerate(woods, 50)),
        ])
        counts = [(item_id, 3, 0) for item_id in range(50, 57)] + [(3, 1, 0)]
        more = [(50, 4, 0), *counts[1:]]
        return catalog, registry, self.inventory(*counts), self.inventory(*more)

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

    def watch_item_requests(self, on_request):
        b = self.b

        def sent(packet_id):
            if packet_id == b.ItemStackRequestPacket.ID:
                raw, = read_batch(self.bot.packets.transport.send.await_args.args[0])
                request, = self.bot.protocol.decode(raw).requests
                on_request(request)

        self.addCleanup(self.bot.packets.subscribe_sent(sent))

    def answer_item_request(self, request, *, rows=(), succeeds=False):
        b = self.b
        self.deliver(b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=request.client_request_id,
            result=b.ItemStackNetResult.SUCCESS if succeeds else b.ItemStackNetResult.ERROR,
            containers=[b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName[container]),
                slots=[b.ItemStackResponseSlotInfo(requested_slot=slot, slot=slot, amount=count,
                                                  item_stack_net_id=b.ItemStackNetId(raw_id=net_id))],
            ) for container, slot, count, net_id in rows] or None,
        )]))

    def crafting_slot(self, inventory_id, slot, item_id, count, net_id=None):
        b = self.b
        return b.InventorySlotPacket(inventory_id=inventory_id, slot=slot,
            item=b.SerializedNetworkItemStackDescriptor(id=item_id, stack_size=count, net_id_variant=net_id))

    async def test_public_return_rechecks_slots_at_transport_write(self):
        b = self.b
        await self.join(False)
        await self.delivered(b.ContainerOpenPacket(container_id=7, type=b.ContainerType.WORKBENCH))
        wire = []

        def respond(request):
            take, = request.actions
            wire.append((take.source.net_id_variant, take.destination.net_id_variant))
            self.answer_item_request(request)

        self.watch_item_requests(respond)
        send = self.bot.packets.send

        for changed in (self.crafting_slot(124, 32, 1, 2, 41),
                        self.crafting_slot(b.ContainerID.INVENTORY, 10, 1, 1, 81)):
            with self.subTest(slot=changed.slot):
                await self.delivered(self.crafting_slot(124, 32, 1, 2, 23),
                                     self.crafting_slot(b.ContainerID.INVENTORY, 10, 0, 0))

                async def revise(packet, **kwargs):
                    if isinstance(packet, b.ItemStackRequestPacket):
                        await self.delivered(changed)
                    return await send(packet, **kwargs)

                with (patch.object(self.bot.packets, "send", revise),
                      self.assertRaisesRegex(RuntimeError, "slots changed")):
                    await self.bot.return_crafting_ingredient(32, 10, timeout=1)
                self.assertEqual(wire, [])

    async def test_public_return_avoids_only_inventory_slots_with_pending_trade(self):
        from bedrock_protocol import nbt

        b = self.b
        await self.join(False)
        await self.delivered(b.UpdateTradePacket(
            container_id=7, type=b.ContainerType.TRADE, size=0, trader_tier=0,
            entity_unique_id=42, last_trading_player=1, display_name="Librarian",
            use_new_trade_screen=True, using_economy_trade=False,
            data=nbt.CompoundTag({"Recipes": nbt.ListTag([], int(nbt.Type.COMPOUND))}),
        ), self.crafting_slot(b.ContainerID.INVENTORY, 10, 1, 2, 81))
        requests = []
        staged = asyncio.Event()

        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                staged.set()
            elif request.actions[0].destination.slot == 11:
                self.answer_item_request(request, succeeds=True, rows=(
                    ("CRAFTING_INPUT_CONTAINER", 32, 1, 23),
                    ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 11, 1, 90),
                ))
            else:
                self.answer_item_request(request)

        self.watch_item_requests(respond)
        pending = asyncio.create_task(self.bot.stage_trade_ingredient(10, 1, timeout=2))
        try:
            await asyncio.wait_for(staged.wait(), 1)
            await self.delivered(b.ContainerOpenPacket(container_id=7, type=b.ContainerType.WORKBENCH),
                                 self.crafting_slot(124, 32, 1, 2, 23),
                                 self.crafting_slot(b.ContainerID.INVENTORY, 11, 0, 0))
            with self.assertRaisesRegex(RuntimeError, "pending item-stack request"):
                await self.bot.return_crafting_ingredient(32, 10, timeout=1)
            self.assertEqual([req.client_request_id.raw_id for req in requests], [-1])
            reply = await self.bot.return_crafting_ingredient(32, 11, timeout=1)
            self.assertEqual(reply.result.name, "SUCCESS")
            self.assertEqual(self.bot.inventory_slots[11].stack_size, 1)
            self.assertEqual([req.actions[0].destination.slot for req in requests[1:]], [11])
            self.assertFalse(pending.done())
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

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

    async def test_plan_uses_shared_capacity_in_recipe_order_without_sending(self):
        await self.join(True)
        send = self.bot.packets.transport.send
        before = send.await_count
        self.assertIsNone(self.bot.open_window)
        self.assertEqual(self.bot.recipes_for("stick", 0), ())
        plan = self.bot.plan_recipe_delta("test:mix")
        self.assertIsInstance(plan, RecipeDeltaPlan)
        self.assertIs(plan.recipe, self.bot.get_recipe(1))
        self.assertEqual(plan.times, 1)
        self.assertEqual(plan.materials, (
            (RecipeItemAmount(2, 0, 1, "minecraft:birch_planks"),),
            (RecipeItemAmount(1, 3, 1, "minecraft:oak_planks"),),
        ))
        self.assertEqual(plan.delta, (
            RecipeItemAmount(2, 0, -1, "minecraft:birch_planks"),
            RecipeItemAmount(1, 3, -1, "minecraft:oak_planks"),
            RecipeItemAmount(3, 0, 4, "minecraft:stick"),
        ))
        self.assertEqual(self.bot.plan_recipe_delta(2).recipe.recipe_id, "test:wide")
        self.assertEqual(send.await_count, before)
        self.assertIsNone(self.bot.open_window)

        await self.delivered(self.inventory((1, 1, 3), (999, 16, 0)))
        self.assertIsNone(self.bot.plan_recipe_delta("test:mix"))
        await self.delivered(self.inventory((1, 2, 3), (999, 16, 0)))
        satisfied = self.bot.plan_recipe_delta("test:mix")
        self.assertEqual(satisfied.materials, (
            (RecipeItemAmount(1, 3, 1, "minecraft:oak_planks"),),
            (RecipeItemAmount(1, 3, 1, "minecraft:oak_planks"),),
        ))
        self.assertEqual(satisfied.delta[0], RecipeItemAmount(1, 3, -2, "minecraft:oak_planks"))
        self.assertEqual(plan.delta[0].item_id, 2)

    async def test_plan_keeps_actual_item_id_and_aux_and_retains_zero(self):
        b = self.b
        await self.join(before_spawn=(
            b.ItemRegistryPacket(items=[
                b.ItemData(id=1, name="minecraft:oak_planks"),
                b.ItemData(id=7, name="minecraft:oak_planks"),
            ]),
            self.shapeless("test:variants", 7, [({"name": "minecraft:oak_planks:3"}, 1, 3)],
                           [(7, 1, 3), (1, 1, 3), (7, 2, 4), (7, 3, 4), (77, 1, 0)]),
            self.inventory((7, 1, 3), (1, 1, 5)),
        ))
        self.assertEqual(self.bot.item_registry.id_for_name("minecraft:oak_planks"), 1)
        plan = self.bot.plan_recipe_delta(7)
        self.assertEqual(plan.materials, ((RecipeItemAmount(7, 3, 1, "minecraft:oak_planks"),),))
        self.assertEqual(plan.delta, (
            RecipeItemAmount(7, 3, 0, "minecraft:oak_planks"),
            RecipeItemAmount(1, 3, 1, "minecraft:oak_planks"),
            RecipeItemAmount(7, 4, 5, "minecraft:oak_planks"),
            RecipeItemAmount(77, 0, 1, None),
        ))
        with self.assertRaises(AttributeError):
            plan.times = 7
        with self.assertRaises(AttributeError):
            plan.materials[0][0].count = 100
        with self.assertRaises(AttributeError):
            plan.recipe.recipe_id = "test:replaced"

    async def test_plan_can_choose_different_egg_variants_across_crafts(self):
        b = self.b
        recipe = self.shapeless("test:egg", 11, [({"item_tag": "minecraft:egg"}, 1, 32767)], [(3, 2, 0)])
        registry = b.ItemRegistryPacket(items=[
            b.ItemData(id=3, name="minecraft:stick"),
            b.ItemData(id=5, name="minecraft:egg"),
            b.ItemData(id=6, name="minecraft:blue_egg"),
        ])
        await self.join(before_spawn=(recipe, registry, self.inventory((5, 1, 0), (6, 1, 0))))
        one = self.bot.plan_recipe_delta(11)
        self.assertIn(one.materials, (
            ((RecipeItemAmount(5, 0, 1, "minecraft:egg"),),),
            ((RecipeItemAmount(6, 0, 1, "minecraft:blue_egg"),),),
        ))
        two = self.bot.plan_recipe_delta(11, times=2)
        self.assertEqual(two.times, 2)
        self.assertEqual(two.materials, ((
            RecipeItemAmount(5, 0, 1, "minecraft:egg"), RecipeItemAmount(6, 0, 1, "minecraft:blue_egg"),
        ),))
        self.assertEqual([(item.item_id, item.count) for item in two.delta], [(5, -1), (6, -1), (3, 4)])
        self.assertIsNone(self.bot.plan_recipe_delta(11, times=3))

        await self.bot.close()
        self.incoming = asyncio.Queue()
        await self.join(before_spawn=(recipe, registry, self.inventory((5, 1, 0), (6, 1, 0))), addon=True)
        self.assertIsNone(self.bot.plan_recipe_delta(11))

    async def test_plan_preserves_whole_positions_when_requirements_overlap(self):
        oak = {"name": "minecraft:oak_planks"}
        plank = {"item_tag": "minecraft:planks"}
        pairs = self.shapeless("test:pairs", 7, [(plank, 2, 32767), (oak, 2, 3)], [(3, 1, 0)])
        mixed = self.shapeless("test:mixed_count", 8, [(plank, 2, 32767), (oak, 1, 3)], [(3, 1, 0)])
        overlapping = self.shapeless("test:overlapping_pairs", 12, [
            (plank, 2, 32767), (oak, 2, 3), (plank, 2, 32767), ({"name": "minecraft:stick"}, 1, 0),
        ], [(4, 1, 0)])
        await self.join(False, (pairs, mixed, overlapping, self.inventory((1, 2, 3), (2, 2, 0))))
        sent = self.bot.packets.transport.send.await_count
        self.assertEqual(self.bot.plan_recipe_delta(7).materials, (
            (RecipeItemAmount(2, 0, 2, "minecraft:birch_planks"),),
            (RecipeItemAmount(1, 3, 2, "minecraft:oak_planks"),),
        ))
        self.assertEqual(self.bot.plan_recipe_delta(8).materials, (
            (RecipeItemAmount(2, 0, 2, "minecraft:birch_planks"),),
            (RecipeItemAmount(1, 3, 1, "minecraft:oak_planks"),),
        ))
        self.assertEqual(self.bot.packets.transport.send.await_count, sent)
        self.assertIsNone(self.bot.open_window)
        self.assertNotIn(self.bot.get_recipe(7), self.bot.recipes_for("stick"))
        self.assertNotIn(self.bot.get_recipe(8), self.bot.recipes_for("stick"))

        await self.delivered(self.inventory((1, 3, 3), (2, 3, 0)))
        self.assertIsNone(self.bot.plan_recipe_delta(8, times=2))
        await self.delivered(self.inventory((1, 4, 3), (2, 2, 0)))
        self.assertEqual(self.bot.plan_recipe_delta(8, times=2).materials, ((
            RecipeItemAmount(1, 3, 2, "minecraft:oak_planks"), RecipeItemAmount(2, 0, 2, "minecraft:birch_planks"),
        ), (RecipeItemAmount(1, 3, 2, "minecraft:oak_planks"),)))

        # The two whole-batch groups pass their individual bounds, but cannot
        # both consume the same oak. Unrelated inventory does not help.
        await self.delivered(self.inventory((1, 2, 3), (2, 2, 0), (3, 1, 0), (4, 10, 0)))
        self.assertIsNone(self.bot.plan_recipe_delta(12))
        await self.delivered(self.inventory((1, 4, 3), (2, 2, 0), (3, 1, 0)))
        plan = self.bot.plan_recipe_delta(12)
        self.assertEqual({plan.materials[i][0].item_id for i in (0, 2)}, {1, 2})
        self.assertEqual(plan.materials[1], (RecipeItemAmount(1, 3, 2, "minecraft:oak_planks"),))
        self.assertEqual(plan.materials[3], (RecipeItemAmount(3, 0, 1, "minecraft:stick"),))

    async def test_plan_never_mixes_concrete_ids_with_same_name_and_aux_in_a_batch(self):
        b = self.b
        oak = "minecraft:oak_planks"
        recipe = self.shapeless("test:concrete_ids", 7, [({"name": oak}, 2, 3)], [(3, 1, 0)])
        registry = b.ItemRegistryPacket(items=[b.ItemData(id=item_id, name=oak) for item_id in (1, 7)])
        await self.join(before_spawn=(recipe, registry, self.inventory((1, 1, 3), (7, 1, 3))))
        self.assertIsNone(self.bot.plan_recipe_delta(7))

        await self.delivered(self.inventory((1, 2, 3), (7, 2, 3)))
        self.assertIn(self.bot.plan_recipe_delta(7).materials, (
            ((RecipeItemAmount(1, 3, 2, oak),),),
            ((RecipeItemAmount(7, 3, 2, oak),),),
        ))
        two = self.bot.plan_recipe_delta(7, times=2)
        self.assertEqual(two.materials, ((RecipeItemAmount(1, 3, 2, oak), RecipeItemAmount(7, 3, 2, oak)),))
        self.assertEqual([(item.item_id, item.count) for item in two.delta], [(1, -2), (7, -2), (3, 2)])
        self.assertIsNone(self.bot.plan_recipe_delta(7, times=3))

    async def test_plan_counts_complete_batches_for_repeated_compatible_positions(self):
        catalog, registry, short, enough = self.plank_ring()
        await self.join(False, (catalog, registry, short))
        sent = self.bot.packets.transport.send
        before = sent.await_count
        self.assertIsNone(self.bot.plan_recipe_delta("test:plank_ring"))
        self.assertEqual(sent.await_count, before)

        await self.delivered(enough)
        before = sent.await_count
        plan = self.bot.plan_recipe_delta("test:plank_ring")
        self.assertEqual(len(plan.materials), 9)
        self.assertEqual(plan.materials[4], (RecipeItemAmount(3, 0, 1, "minecraft:stick"),))
        outside = plan.materials[:4] + plan.materials[5:]
        for group in outside:
            item, = group
            self.assertEqual((item.aux_value, item.count), (0, 2))
            self.assertIn(item.item_id, range(50, 57))
        self.assertEqual({item.item_id: item.count for item in plan.delta}, {
            50: -4, **{item_id: -2 for item_id in range(51, 57)}, 3: -1, 4: 1,
        })
        self.assertEqual(sent.await_count, before)
        self.assertIsNone(self.bot.open_window)

    async def test_plan_returns_none_for_unresolved_requirements_or_results(self):
        b = self.b
        oak = [({"name": "minecraft:oak_planks"}, 1, 3)]
        await self.join(before_spawn=(
            self.shapeless("test:no_output", 7, oak, []),
            self.shapeless("test:zero_output", 8, oak, [(3, 0, 0)]),
            self.shapeless("test:anonymous_output", 9, oak, [(0, 1, 0)]),
            self.shapeless("test:unknown_input", 10, [({"name": "test:missing"}, 1, 32767)], [(3, 1, 0)]),
            self.shapeless("test:unsupported_input", 11, [({"molang": "1"}, 1, 32767)], [(3, 1, 0)]),
            b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=36,
                                  item=b.SerializedNetworkItemStackDescriptor(id=1, stack_size=7, aux_value=3)),
        ))
        for key in ("test:unknown", "test:no_output", "test:zero_output", "test:anonymous_output",
                    "test:unknown_input", "test:unsupported_input", "test:absent", "test:pair"):
            with self.subTest(recipe=key):
                self.assertIsNone(self.bot.plan_recipe_delta(key))

    async def test_plan_uses_known_slots_while_a_drop_still_has_no_correction(self):
        b = self.b
        await self.join(before_spawn=(self.inventory((1, 2, 3)),))
        self.assertIsNotNone(self.bot.plan_recipe_delta(4))
        await self.bot.drop_selected_item(whole_stack=True)
        self.assertEqual(self.bot.inventory_slots[0].stack_size, 2)
        self.assertIsNone(self.bot.plan_recipe_delta(4))

        await self.delivered(b.InventorySlotPacket(
            inventory_id=b.ContainerID.INVENTORY, slot=5,
            item=b.SerializedNetworkItemStackDescriptor(id=1, stack_size=2, aux_value=3),
        ))
        self.assertEqual(self.bot.plan_recipe_delta(4).materials, (
            (RecipeItemAmount(1, 3, 2, "minecraft:oak_planks"),),
        ))
        self.assertIsNone(self.bot.plan_recipe_delta(4, times=2))

    async def test_plan_reads_replaced_catalog_and_does_not_survive_disconnect(self):
        b = self.b
        self.assertIsNone(self.bot.plan_recipe_delta(1))
        for invalid in (0, -1, True, False, 1.0, "1", None):
            with self.subTest(times=invalid):
                with self.assertRaisesRegex(ValueError, "times must be a positive integer"):
                    self.bot.plan_recipe_delta(1, times=invalid)

        await self.join()
        old = self.bot.plan_recipe_delta(1)
        recipe = self.shapeless("test:new", 1, [({"name": "minecraft:oak_planks"}, 1, 3)], [(4, 1, 0)], clear=True)
        await self.delivered(recipe)
        self.assertIsNone(self.bot.plan_recipe_delta("test:mix"))
        self.assertIsNone(self.bot.plan_recipe_delta(2))
        current = self.bot.plan_recipe_delta(1)
        self.assertEqual(current.recipe.recipe_id, "test:new")
        self.assertEqual(current.delta[-1], RecipeItemAmount(4, 0, 1, "minecraft:wooden_pickaxe"))
        self.assertEqual(old.recipe.recipe_id, "test:mix")
        self.assertEqual(old.delta[-1], RecipeItemAmount(3, 0, 4, "minecraft:stick"))

        self.deliver(b.DisconnectPacket(reason=b.DisconnectFailReason.KICKED))
        self.assertEqual((await self.bot.wait_for_session_end(timeout=1)).kind, "server_disconnect")
        self.assertIsNone(self.bot.plan_recipe_delta(1))
        await self.bot.close()
        self.assertIsNone(self.bot.plan_recipe_delta(1))
        self.incoming = asyncio.Queue()
        await self.join(before_spawn=(self.inventory((1, 1, 3)),))
        self.assertIsNone(self.bot.plan_recipe_delta("test:new"))
        self.assertIsNone(self.bot.plan_recipe_delta(1))
        self.assertEqual(current.recipe.recipe_id, "test:new")

    async def test_explicit_grid_queries_without_opening_or_sending(self):
        await self.join(False)
        wide = self.bot.get_recipe(2)
        send = self.bot.packets.transport.send
        before = send.await_count
        self.assertIsNone(self.bot.open_window)
        for query in (self.bot.recipes_all, self.bot.recipes_for):
            self.assertEqual(query("wooden_pickaxe"), ())
            self.assertEqual(query("wooden_pickaxe", grid_size=3), (wide,))
            self.assertEqual(query("wooden_pickaxe", grid_size=2, crafting_table=False), ())
        self.assertEqual(self.bot.recipes_for("wooden_pickaxe", min_result_count=2, grid_size=3), ())
        self.assertEqual(send.await_count, before)
        self.assertIsNone(self.bot.open_window)

        await self.delivered(self.b.ContainerOpenPacket(container_id=1, type=self.b.ContainerType.WORKBENCH))
        window = self.bot.open_window
        self.assertEqual(window.container_type, "WORKBENCH")
        before = send.await_count
        for query in (self.bot.recipes_all, self.bot.recipes_for):
            self.assertEqual(query("wooden_pickaxe"), (wide,))
            self.assertEqual(query("wooden_pickaxe", grid_size=2), ())
        self.assertEqual(send.await_count, before)
        self.assertIs(self.bot.open_window, window)

    async def test_invalid_grid_queries_raise_even_before_crafting_rule_is_known(self):
        await self.join()
        self.assertIsNone(self.bot.limited_crafting)
        self.assertEqual(self.bot.recipes_all("wooden_pickaxe", grid_size=3), (self.bot.get_recipe(2),))
        self.assertEqual(self.bot.recipes_for("wooden_pickaxe", grid_size=3), ())
        for query in (self.bot.recipes_all, self.bot.recipes_for):
            for invalid in (1, 4, 3.0, "3", True):
                with self.subTest(query=query.__name__, grid_size=invalid):
                    with self.assertRaisesRegex(ValueError, "grid_size must be 2, 3 or None"):
                        query("wooden_pickaxe", grid_size=invalid)
            with self.subTest(query=query.__name__):
                with self.assertRaisesRegex(ValueError, "grid_size=3 requires crafting_table=True"):
                    query("wooden_pickaxe", grid_size=3, crafting_table=False)

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
