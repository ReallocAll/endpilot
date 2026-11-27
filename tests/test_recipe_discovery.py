import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from endpilot import Bot
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
