import asyncio
import unittest
from unittest.mock import patch

from endpilot import Bot
from endpilot.protocol import write_batch

from test_scheduled_input import ManualClock, Wire


OAK = "minecraft:oak_planks"
BIRCH = "minecraft:birch_planks"
TABLE = "minecraft:crafting_table"
WOOD = "minecraft:planks"


class RecipeAutoTagTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(ManualClock().patch_scheduler())

    async def join(self, *, stack_toggles=(), game_toggles=(), item_names=(OAK, BIRCH, TABLE)):
        self.bot = Bot("RecipeAuto", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)

        def experiments(toggles):
            return b.Experiments(
                toggles=[b.ExperimentToggle(name=name, enabled=enabled) for name, enabled in toggles],
                experiments_ever_toggled=bool(toggles),
            )

        for packet in (
            b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
            b.ResourcePacksInfoPacket(), b.ResourcePackStackPacket(experiments=experiments(stack_toggles)),
            b.StartGamePacket(runtime_id=7, entity_id=17, server_version="1.26.66", settings=b.LevelSettings(
                game_rules=b.GameRulesChangedPacketData(rules=[b.GameRule(name="doLimitedCrafting", value=False)]),
                experiments=experiments(game_toggles),
            )),
            b.ItemRegistryPacket(items=[b.ItemData(id=index, name=name) for index, name in enumerate(item_names, 1)]),
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        ):
            self.wire.push(packet)
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)
        await self.deliver(b.ContainerOpenPacket(container_id=b.ContainerID.INVENTORY, type=b.ContainerType.INVENTORY))

    def item(self, item_id=0, count=0, aux=0, net_id=None, *, data=b"", runtime_id=0):
        return self.b.SerializedNetworkItemStackDescriptor(
            id=item_id, stack_size=count, aux_value=aux, net_id_variant=net_id,
            user_data_buffer=data, block_runtime_id=runtime_id,
        )

    def recipe(self, *descriptors, result_id=3, result_aux=0):
        b = self.b
        ingredients = [b.SerializedRecipeIngredient(descriptor=descriptor, stack_size=1, aux_value=aux)
                       for descriptor, aux in descriptors]
        return b.CraftingDataPacket(shapeless_recipes=[b.ShapelessRecipePayload(
            recipe_id="test:auto", net_id=40, tag="crafting_table", ingredients=ingredients,
            results=[b.SerializedNetworkItemInstanceDescriptor(id=result_id, stack_size=1, aux_value=result_aux)],
        )])

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        received = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in (*packets, marker)))
        await received

    async def arrange(self, *items, recipe):
        await self.deliver(recipe, self.b.InventoryContentPacket(inventory_id=self.b.ContainerID.INVENTORY, slots=items))

    async def submit(self, destination, *, times=1):
        sent = asyncio.Event()
        unsubscribe = self.bot.packets.subscribe_sent(
            lambda packet_id: sent.set() if packet_id == self.b.ItemStackRequestPacket.ID else None,
        )
        task = asyncio.create_task(self.bot.craft_recipe_auto(40, destination, times=times, timeout=2))

        async def cleanup():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        try:
            await asyncio.wait_for(sent.wait(), 1)
            request, = self.wire.sent[-1].requests
            return task, request
        finally:
            unsubscribe()

    def verdict(self, request_id, *slots, result="SUCCESS"):
        b = self.b
        return b.ItemStackResponseInfo(
            client_request_id=b.ItemStackRequestId(raw_id=request_id), result=b.ItemStackNetResult[result],
            containers=[b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName.COMBINED_HOTBAR_AND_INVENTORY_CONTAINER),
                slots=[b.ItemStackResponseSlotInfo(
                    requested_slot=index, slot=index, amount=count,
                    item_stack_net_id=None if net is None else b.ItemStackNetId(raw_id=net),
                ) for index, count, net in slots],
            )] if slots else None,
        )

    def actions(self, request):
        recipe, results, *consumes, place = request.actions
        inputs = [(row.item_descriptor.full_name, row.item_descriptor.aux_value, row.stack_size)
                  for row in recipe.ingredients]
        sources = [(row.source.slot, row.source.net_id_variant, row.amount) for row in consumes]
        return recipe, results, inputs, sources, place

    async def test_vanilla_tags_require_compatible_experiments_in_both_join_packets(self):
        beta = (("gametest", True),)
        unknown = (("unverified_experiment", True),)
        disabled = (("unverified_experiment", False),)
        for stack, game, can_craft in (
            ((), (), True),
            (beta, beta, True),
            (disabled, disabled, True),
            (beta + disabled, beta + disabled, True),
            (unknown, (), False),
            ((), unknown, False),
            (beta + unknown, beta, False),
            (beta, beta + unknown, False),
        ):
            with self.subTest(stack=stack, game=game):
                await self.join(stack_toggles=stack, game_toggles=game)
                await self.arrange(self.item(1, 1, 0, 50), self.item(),
                                   recipe=self.recipe(({"item_tag": WOOD}, 32767)))
                self.assertEqual([recipe.net_id for recipe in self.bot.recipes_for(TABLE)], [40] if can_craft else [])
                if can_craft:
                    pending, request = await self.submit(1)
                    self.assertEqual(self.actions(request)[2], [(OAK, 0, 1)])
                    await self.deliver(self.b.ItemStackResponsePacket(responses=[self.verdict(
                        request.client_request_id.raw_id, (0, 0, None), (1, 1, 70),
                    )]))
                    self.assertEqual((await pending).result.name, "SUCCESS")
                    self.assertEqual([self.bot.inventory_slots[i].stack_size for i in range(2)], [0, 1])
                else:
                    outgoing = list(self.wire.sent)
                    with self.assertRaisesRegex(ValueError, "unknown recipe tag 'minecraft:planks'"):
                        await self.bot.craft_recipe_auto(40, 1)
                    self.assertEqual(self.wire.sent, outgoing)
            await self.bot.close()

    async def test_bed_dye_selects_the_advertised_color_and_leaves_another_bed_alone(self):
        bed, dye = "minecraft:bed", "minecraft:gray_dye"
        await self.join(item_names=(bed, dye))
        # Vanilla bed_dye_8_9 on BDS 1.26.66.1 advertises a pink bed as bed:6.
        recipe = self.recipe(({"name": bed + ":6"}, 6), ({"name": dye}, 32767), result_id=1, result_aux=7)
        wrong, pink, gray = self.item(1, 1, 3, 51), self.item(1, 1, 6, 52), self.item(2, 1, 0, 53)
        await self.arrange(wrong, self.item(), gray, recipe=recipe)
        self.assertEqual(self.bot.get_recipe(40).ingredients[0].name, bed + ":6")
        self.assertEqual(self.bot.recipes_for(bed, 7), ())
        outgoing = list(self.wire.sent)
        with self.assertRaisesRegex(ValueError, "not enough"):
            await self.bot.craft_recipe_auto(40, 8)
        self.assertEqual(self.wire.sent, outgoing)

        await self.arrange(wrong, pink, gray, recipe=recipe)
        self.assertEqual([r.net_id for r in self.bot.recipes_for(bed, 7)], [40])
        _, request = await self.submit(8)
        inputs, sources = self.actions(request)[2:4]
        self.assertEqual(inputs, [(bed, 6, 1), (dye, 32767, 1)])
        self.assertEqual(sources, [(1, 52, 1), (2, 53, 1)])

    async def test_named_wildcard_leaves_the_only_exact_variant_for_a_later_position(self):
        await self.join()
        recipe = self.recipe(({"name": OAK}, 32767), ({"name": OAK}, 3))
        await self.arrange(self.item(1, 2, 3, 51), self.item(1, 1, 7, 52), self.item(1, 1, 8, 53),
                           recipe=recipe)
        self.assertEqual([r.net_id for r in self.bot.recipes_for(TABLE, min_result_count=2)], [40])
        _, request = await self.submit(8, times=2)
        inputs, sources = self.actions(request)[2:4]
        self.assertEqual(inputs, [(OAK, 32767, 1), (OAK, 3, 1)])
        self.assertEqual(sources, [(1, 52, 1), (2, 53, 1), (0, 51, 2)])

    async def test_tag_assignment_reserves_the_fixed_ingredient_and_applies_only_the_matching_verdict(self):
        await self.join()
        await self.arrange(self.item(1, 1, 3, 75), self.item(2, 1, 7, 99), self.item(1, 4, 1, 93), self.item(),
                           recipe=self.recipe(({"item_tag": WOOD}, 32767), ({"name": OAK}, 3)))
        self.assertEqual([recipe.net_id for recipe in self.bot.recipes_for(TABLE)], [40])
        pending, request = await self.submit(3)
        request_id = request.client_request_id.raw_id
        recipe, results, inputs, sources, place = self.actions(request)
        self.assertEqual((recipe.recipe_net_id, recipe.num_crafts, results.num_crafts), (40, 1, 1))
        self.assertEqual(inputs, [(BIRCH, 7, 1), (OAK, 3, 1)])
        self.assertEqual(sources, [(1, 99, 1), (0, 75, 1)])
        self.assertEqual((place.source.full_container_name.name.name, place.source.slot, place.source.net_id_variant,
                          place.destination.slot, place.amount), ("CREATED_OUTPUT_CONTAINER", 50, request_id, 3, 1))
        await self.deliver(self.b.ItemStackResponsePacket(responses=[self.verdict(request_id - 2, (0, 0, None))]))
        self.assertFalse(pending.done())
        self.assertEqual([self.bot.inventory_slots[i].stack_size for i in range(4)], [1, 1, 4, 0])
        await self.deliver(self.b.ItemStackResponsePacket(responses=[self.verdict(
            request_id, (0, 0, None), (1, 0, None), (2, 0, None), (3, 1, 91),
        )]))
        self.assertEqual((await pending).result.name, "SUCCESS")
        self.assertEqual([(self.bot.inventory_slots[i].id, self.bot.inventory_slots[i].stack_size) for i in range(4)],
                         [(0, 0), (0, 0), (1, 4), (3, 1)])

    async def test_multiple_rounds_reuse_each_described_variant_and_consume_the_real_source_stacks(self):
        await self.join()
        await self.arrange(self.item(1, 1, 0, 51), self.item(1, 3, 0, 52), self.item(2, 4, 1, 53), self.item(),
                           recipe=self.recipe(*[({"item_tag": WOOD}, 32767)] * 4))
        pending, request = await self.submit(3, times=2)
        recipe, results, inputs, sources, place = self.actions(request)
        self.assertEqual((recipe.num_crafts, results.num_crafts, results.craft_results[0].stack_size, place.amount),
                         (2, 2, 1, 2))
        self.assertEqual(inputs, [(OAK, 0, 1), (OAK, 0, 1), (BIRCH, 1, 1), (BIRCH, 1, 1)])
        self.assertEqual(sources, [(0, 51, 1), (1, 52, 1), (1, 52, 2), (2, 53, 2), (2, 53, 2)])
        await self.deliver(self.b.ItemStackResponsePacket(responses=[self.verdict(
            request.client_request_id.raw_id, (0, 0, None), (1, 0, None), (2, 0, None), (3, 2, 19),
        )]))
        self.assertEqual((await pending).result.name, "SUCCESS")
        self.assertEqual(self.bot.inventory_slots[3].stack_size, 2)

    async def test_unknown_tag_or_unrepresentable_materials_are_rejected_before_sending(self):
        await self.join()
        for tag, oak_net, birch_net, times, error in (
            ("test:unknown", 50, 51, 1, "unknown recipe tag"),
            (WOOD, 50, 51, 2, "not enough compatible ingredients"),
            (WOOD, None, None, 1, "not enough compatible ingredients"),
        ):
            with self.subTest(tag=tag, times=times, oak_net=oak_net):
                await self.arrange(self.item(1, 1, 0, oak_net), self.item(2, 1, 0, birch_net), self.item(),
                                   recipe=self.recipe(({"item_tag": tag}, 32767)))
                outgoing = list(self.wire.sent)
                with self.assertRaisesRegex(ValueError, error):
                    await self.bot.craft_recipe_auto(40, 2, times=times)
                self.assertEqual(self.wire.sent, outgoing)

    async def test_replaced_source_does_not_submit_the_saved_net_stack(self):
        await self.join()
        await self.arrange(self.item(1, 1, 0, 50), self.item(), recipe=self.recipe(({"item_tag": WOOD}, 32767)))
        original = self.bot._inventory._request_item_stack

        async def change_before_submit(*args, **kwargs):
            await self.deliver(self.b.InventorySlotPacket(
                inventory_id=self.b.ContainerID.INVENTORY, slot=0, item=self.item(1, 1, 0, 60),
            ))
            return await original(*args, **kwargs)

        outgoing = list(self.wire.sent)
        with patch.object(self.bot._inventory, "_request_item_stack", side_effect=change_before_submit):
            with self.assertRaisesRegex(RuntimeError, "crafting inventory changed"):
                await self.bot.craft_recipe_auto(40, 1, timeout=1)
        self.assertEqual(self.wire.sent, outgoing)

    async def test_late_verdict_keeps_a_replaced_slot_but_applies_to_an_equivalent_reobservation(self):
        await self.join()
        b = self.b
        await self.arrange(self.item(1, 1, 0, 50), self.item(2, 1, 0, 51), self.item(),
                           recipe=self.recipe(({"item_tag": WOOD}, 32767), ({"item_tag": WOOD}, 32767)))
        pending, request = await self.submit(2)
        await self.deliver(
            b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=0, item=self.item(1, 1, 0, 50)),
            b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=1, item=self.item(2, 4, 0, 61)),
            b.ItemStackResponsePacket(responses=[self.verdict(request.client_request_id.raw_id,
                (0, 0, None), (1, 0, None), (2, 1, 70),
            )]),
        )
        self.assertEqual((await pending).result.name, "SUCCESS")
        slots = self.bot.inventory_slots
        self.assertEqual((slots[0].id, slots[1].stack_size, slots[1].net_id_variant, slots[2].id), (0, 4, 61, 3))

    async def test_late_consumption_does_not_shrink_a_different_item_with_the_same_stack_id(self):
        await self.join()
        b = self.b
        for item_id, aux, data, runtime_id, count in (
            (2, 0, b"", 0, 9),       # Different item name.
            (1, 5, b"", 0, 9),       # Different auxiliary value.
            (1, 0, b"new", 0, 9),    # Different user data.
            (1, 0, b"", 14, 9),      # Different block runtime ID.
            (1, 0, b"", 0, 2),       # The original identity can take the correction.
        ):
            with self.subTest(item_id=item_id, aux=aux, data=data, runtime_id=runtime_id):
                await self.arrange(self.item(1, 3, 0, 50), self.item(), recipe=self.recipe(({"item_tag": WOOD}, 32767)))
                pending, request = await self.submit(1)
                observed = self.item(item_id, 9, aux, 50, data=data, runtime_id=runtime_id)
                await self.deliver(
                    b.InventoryContentPacket(inventory_id=b.ContainerID.INVENTORY, slots=[observed, self.item()]),
                    b.ItemStackResponsePacket(responses=[self.verdict(request.client_request_id.raw_id,
                        (0, 2, 50), (1, 1, 70),
                    )]),
                )
                self.assertEqual((await pending).result.name, "SUCCESS")
                source, output = (self.bot.inventory_slots[i] for i in (0, 1))
                self.assertEqual((source.id, source.stack_size, source.aux_value, source.net_id_variant,
                                  bytes(source.user_data_buffer), source.block_runtime_id),
                                 (item_id, count, aux, 50, data, runtime_id))
                self.assertEqual((output.id, output.stack_size, output.net_id_variant), (3, 1, 70))

    async def test_delivery_correction_requires_the_observed_result_identity_and_stack_id(self):
        await self.join()
        b = self.b
        for aux, net_id, count in ((0, 80, 2), (8, 80, 1), (0, 81, 1)):
            with self.subTest(aux=aux, net_id=net_id):
                await self.arrange(self.item(1, 3, 0, 50), self.item(), recipe=self.recipe(({"item_tag": WOOD}, 32767)))
                pending, request = await self.submit(1, times=2)
                await self.deliver(
                    b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=1, item=self.item(3, 1, aux, net_id)),
                    b.ItemStackResponsePacket(responses=[self.verdict(request.client_request_id.raw_id,
                        (0, 1, 50), (1, 2, 80),
                    )]),
                )
                self.assertEqual((await pending).result.name, "SUCCESS")
                source, output = (self.bot.inventory_slots[i] for i in (0, 1))
                self.assertEqual((source.id, source.stack_size), (1, 1))
                self.assertEqual((output.id, output.stack_size, output.aux_value, output.net_id_variant), (3, count, aux, net_id))
