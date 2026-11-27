import asyncio
import unittest
from unittest.mock import patch

import test_container_slot_events as container_events


class CraftingSlotInterleaveTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = container_events.ContainerSlotEventsTests.asyncSetUp
    deliver = container_events.ContainerSlotEventsTests.deliver
    item = container_events.ContainerSlotEventsTests.item
    open_packet = container_events.ContainerSlotEventsTests.open_packet
    response = container_events.ContainerSlotEventsTests.response
    slot = container_events.ContainerSlotEventsTests.slot
    submit = container_events.ContainerSlotEventsTests.submit

    def carried_stacks(self, *indices):
        slots = self.bot.inventory_slots
        stacks = (slots[index] for index in indices)
        return tuple((stack.id, stack.stack_size, stack.net_id_variant) for stack in stacks)

    async def test_workbench_craft_preserves_an_unrelated_inventory_slot_update(self):
        b = self.b
        player = b.ContainerID.INVENTORY
        grid, destination, unrelated = 32, 8, 9
        log, planks, iron = 1, 2, 3
        recipe_id = 19
        await self.deliver(
            b.ItemRegistryPacket(items=[
                b.ItemData(id=log, name="minecraft:oak_log"),
                b.ItemData(id=planks, name="minecraft:oak_planks"),
                b.ItemData(id=iron, name="minecraft:iron_ingot"),
            ]),
            b.CraftingDataPacket(shapeless_recipes=[b.ShapelessRecipePayload(
                recipe_id="minecraft:oak_planks", net_id=recipe_id, tag="crafting_table",
                ingredients=[b.SerializedRecipeIngredient(descriptor={"name": "minecraft:oak_log"}, stack_size=1)],
                results=[b.SerializedNetworkItemInstanceDescriptor(id=planks, stack_size=4)],
            )]),
            self.open_packet("WORKBENCH"),
            self.slot(grid, self.item(1, 53, item_id=log), inventory_id=b.ContainerID.PLAYER_ONLY_UI),
            self.slot(destination, self.item(), inventory_id=player),
            self.slot(unrelated, self.item(2, 70, item_id=iron), inventory_id=player),
        )
        self.assertEqual(self.bot.open_window.container_type, "WORKBENCH")
        self.assertEqual(self.carried_stacks(destination, unrelated), ((0, 0, None), (iron, 2, 70)))

        pending, request = await self.submit(self.bot.craft_recipe_grid(recipe_id, destination, timeout=2))
        craft, _, consume, place = request.actions
        self.assertEqual(craft.recipe_net_id, recipe_id)
        self.assertEqual((consume.source.full_container_name.name.name, consume.source.slot,
                          consume.source.net_id_variant, consume.amount), ("CRAFTING_INPUT_CONTAINER", grid, 53, 1))
        self.assertEqual((place.destination.full_container_name.name.name, place.destination.slot, place.amount),
                         (container_events.PLAYER, destination, 4))

        await self.deliver(self.slot(unrelated, self.item(5, 71, item_id=iron), inventory_id=player))
        self.assertEqual(self.carried_stacks(destination, unrelated), ((0, 0, None), (iron, 5, 71)))
        self.assertFalse(pending.done())

        await self.deliver(b.ItemStackResponsePacket(responses=[self.response(
            request, ("CRAFTING_INPUT_CONTAINER", grid, 0, None), (container_events.PLAYER, destination, 4, 59),
        )]))
        reply = await pending
        self.assertEqual(reply.client_request_id.raw_id, request.client_request_id.raw_id)
        self.assertEqual(reply.result.name, "SUCCESS")
        self.assertEqual(self.carried_stacks(destination, unrelated), ((planks, 4, 59), (iron, 5, 71)))
        with self.assertRaisesRegex(ValueError, "no observed crafting ingredients"):
            await self.bot.craft_recipe_grid(recipe_id, destination, timeout=2)

    def two_result_catalog(self, *, reverse=False):
        b = self.b
        results = [
            b.SerializedNetworkItemInstanceDescriptor(id=420, stack_size=1, aux_value=2,
                                                      block_runtime_id=123, user_data_buffer=b"cake"),
            b.SerializedNetworkItemInstanceDescriptor(id=363, stack_size=3, user_data_buffer=b"bucket"),
        ]
        return b.CraftingDataPacket(shapeless_recipes=[b.ShapelessRecipePayload(
            recipe_id="minecraft:cake", net_id=3415, tag="crafting_table",
            ingredients=[b.SerializedRecipeIngredient(descriptor={"name": "minecraft:milk_bucket"}, stack_size=1)],
            results=results[::-1] if reverse else results,
        )])

    async def stage_two_result_recipe(self):
        b = self.b
        await self.deliver(
            b.ItemRegistryPacket(items=[
                b.ItemData(id=364, name="minecraft:milk_bucket"),
                b.ItemData(id=420, name="minecraft:cake"),
                b.ItemData(id=363, name="minecraft:bucket"),
            ]),
            self.two_result_catalog(),
            self.open_packet("WORKBENCH"),
            self.slot(32, self.item(1, 50, item_id=364), inventory_id=b.ContainerID.PLAYER_ONLY_UI),
            self.slot(9, self.item(), inventory_id=b.ContainerID.INVENTORY),
            self.slot(10, self.item(), inventory_id=b.ContainerID.INVENTORY),
        )

    async def test_multiple_results_use_recipe_order_and_apply_each_receipt(self):
        await self.stage_two_result_recipe()
        operation = self.bot.craft_recipe_grid(3415, 10, additional_destination_slots=(9,))
        pending, request = await self.submit(operation)
        craft, results, consume, first, primary, second, extra = request.actions
        self.assertEqual((craft.recipe_net_id, craft.num_crafts), (3415, 1))
        self.assertEqual([
            (out.item_descriptor.full_name, out.item_descriptor.aux_value, out.stack_size,
             out.block_runtime_id, bytes(out.user_data_buffer)) for out in results.craft_results
        ], [("minecraft:cake", 2, 1, 123, b"cake"), ("minecraft:bucket", 0, 3, 0, b"bucket")])
        self.assertEqual((consume.amount, consume.source.slot, consume.source.net_id_variant), (1, 32, 50))
        self.assertEqual((first.results_index, second.results_index), (0, 1))
        for action, slot, count in ((primary, 10, 1), (extra, 9, 3)):
            self.assertEqual((action.amount, action.source.full_container_name.name.name, action.source.slot,
                              action.source.net_id_variant),
                             (count, "CREATED_OUTPUT_CONTAINER", 50, request.client_request_id.raw_id))
            self.assertEqual((action.destination.full_container_name.name.name, action.destination.slot,
                              action.destination.net_id_variant), ("INVENTORY_CONTAINER", slot, 0))
        self.assertEqual(self.carried_stacks(9, 10), ((0, 0, None), (0, 0, None)))

        await self.deliver(self.b.ItemStackResponsePacket(responses=[self.response(
            request, ("CRAFTING_INPUT_CONTAINER", 32, 0, None),
            ("INVENTORY_CONTAINER", 9, 3, 61), ("INVENTORY_CONTAINER", 10, 1, 60),
        )]))
        self.assertEqual((await pending).result.name, "SUCCESS")
        self.assertEqual(self.carried_stacks(9, 10), ((363, 3, 61), (420, 1, 60)))
        primary = self.bot.inventory_slots[10]
        self.assertEqual((primary.aux_value, primary.block_runtime_id, bytes(primary.user_data_buffer)),
                         (2, 123, b"cake"))
        self.assertEqual(self.bot._recipes._grid_slots[32].id, 0)

    async def stage_honey_recipe(self):
        b = self.b
        await self.deliver(
            b.ItemRegistryPacket(items=[
                b.ItemData(id=728, name="minecraft:honey_bottle"),
                b.ItemData(id=419, name="minecraft:sugar"),
                b.ItemData(id=431, name="minecraft:glass_bottle"),
            ]),
            b.CraftingDataPacket(shaped_recipes=[b.ShapedRecipePayload(
                recipe_id="minecraft:honey_bottle_to_sugar", net_id=4241, tag="crafting_table",
                width=1, height=1,
                ingredients=[b.SerializedRecipeIngredient(descriptor={"name": "minecraft:honey_bottle"}, stack_size=1)],
                results=[b.SerializedNetworkItemInstanceDescriptor(id=419, stack_size=3),
                         b.SerializedNetworkItemInstanceDescriptor(id=431, stack_size=1)],
            )]),
            self.open_packet("INVENTORY"),
            self.slot(28, self.item(1, 3, item_id=728), inventory_id=b.ContainerID.PLAYER_ONLY_UI),
            self.slot(9, self.item(), inventory_id=b.ContainerID.INVENTORY),
            self.slot(10, self.item(), inventory_id=b.ContainerID.INVENTORY),
        )

    async def test_personal_grid_craft_requires_confirmed_destinations_and_applies_both_results(self):
        await self.stage_honey_recipe()
        b = self.b
        await self.deliver(self.slot(11, self.item(1, 6), inventory_id=b.ContainerID.INVENTORY))
        sent = list(self.wire.sent)
        for destination in (11, 12):  # Occupied and unknown.
            with self.subTest(destination=destination), self.assertRaisesRegex(ValueError, "observed empty"):
                await self.bot.craft_recipe_grid(4241, 9, additional_destination_slots=(destination,))
        self.assertEqual(self.wire.sent, sent)

        pending, request = await self.submit(self.bot.craft_recipe_grid(4241, 9, additional_destination_slots=(10,)))
        _, results, consume, _, sugar, _, bottle = request.actions
        self.assertEqual([out.item_descriptor.full_name for out in results.craft_results],
                         ["minecraft:sugar", "minecraft:glass_bottle"])
        self.assertEqual((consume.source.slot, consume.source.net_id_variant, consume.amount), (28, 3, 1))
        self.assertEqual([(action.destination.slot, action.amount) for action in (sugar, bottle)], [(9, 3), (10, 1)])
        await self.deliver(b.ItemStackResponsePacket(responses=[self.response(
            request, ("CRAFTING_INPUT_CONTAINER", 28, 0, None),
            ("INVENTORY_CONTAINER", 9, 3, 4), ("INVENTORY_CONTAINER", 10, 1, 5),
        )]))
        self.assertEqual((await pending).result.name, "SUCCESS")
        self.assertEqual(self.carried_stacks(9, 10), ((419, 3, 4), (431, 1, 5)))
        self.assertEqual(self.bot._recipes._grid_slots[28].id, 0)

    async def test_personal_grid_craft_receipt_leaves_reopened_grid_intact(self):
        await self.stage_honey_recipe()
        b = self.b
        original = self.bot.open_window
        pending, request = await self.submit(self.bot.craft_recipe_grid(4241, 9, additional_destination_slots=(10,)))
        await self.deliver(
            b.ContainerClosePacket(container_id=original.container_id, container_type=b.ContainerType.INVENTORY),
            self.open_packet("INVENTORY"),
            self.slot(28, self.item(1, 6, item_id=728), inventory_id=b.ContainerID.PLAYER_ONLY_UI),
        )
        self.assertEqual(self.bot.open_window, original)
        self.assertIsNot(self.bot.open_window, original)
        await self.deliver(b.ItemStackResponsePacket(responses=[self.response(
            request, ("CRAFTING_INPUT_CONTAINER", 28, 0, None),
            ("INVENTORY_CONTAINER", 9, 3, 4), ("INVENTORY_CONTAINER", 10, 1, 5),
        )]))
        self.assertEqual((await pending).result.name, "SUCCESS")
        self.assertEqual(self.carried_stacks(9, 10), ((419, 3, 4), (431, 1, 5)))
        self.assertEqual(self.bot._recipes._grid_slots[28].net_id_variant, 6)

    async def test_invalid_multi_result_destinations_do_not_send(self):
        await self.stage_two_result_recipe()
        await self.deliver(self.slot(11, self.item(1, 60, item_id=17), inventory_id=self.b.ContainerID.INVENTORY))
        sent = list(self.wire.sent)
        for extra, times, message in (
            ((), 1, "one destination slot per recipe result"),
            ((10, 11), 1, "one destination slot per recipe result"),
            ((9,), 1, "distinct"),
            ((11,), 1, "observed empty"),
            ((12,), 1, "observed empty"),
            ((36,), 1, "main inventory slots"),
            ((10,), 2, "one craft in an open inventory screen or workbench"),
        ):
            with self.subTest(extra=extra, times=times):
                with self.assertRaisesRegex(ValueError, message):
                    await self.bot.craft_recipe_grid(3415, 9, additional_destination_slots=extra, times=times)
        self.assertEqual(self.wire.sent, sent)

    async def test_multi_result_craft_rechecks_window_grid_and_destinations_before_writing(self):
        b = self.b
        for change, error in (
            (self.open_packet("WORKBENCH", inventory_id=6), "crafting window changed"),
            (self.slot(33, self.item(1, 61), inventory_id=b.ContainerID.PLAYER_ONLY_UI), "crafting inputs changed"),
            (self.slot(10, self.item(1, 61), inventory_id=b.ContainerID.INVENTORY), "confirmed empty"),
            (b.CraftingDataPacket(clear_recipes=True), "recipe changed before submission"),
            (self.two_result_catalog(reverse=True), "recipe changed before submission"),
        ):
            with self.subTest(change=type(change).__name__, error=error):
                await self.stage_two_result_recipe()
                sent = list(self.wire.sent)
                waiting, release = asyncio.Event(), asyncio.Event()
                send = self.bot.packets.send

                async def stall_request(packet, **kwargs):
                    if isinstance(packet, b.ItemStackRequestPacket):
                        waiting.set()
                        await release.wait()
                        await send(packet, **kwargs)
                        self.fail("the changed state did not prevent the craft write")
                    await send(packet, **kwargs)

                with patch.object(self.bot.packets, "send", new=stall_request):
                    task = asyncio.create_task(self.bot.craft_recipe_grid(
                        3415, 9, additional_destination_slots=(10,), timeout=1,
                    ))
                    try:
                        await asyncio.wait_for(waiting.wait(), 1)
                        await self.deliver(change)
                    finally:
                        release.set()
                    with self.assertRaisesRegex(RuntimeError, error):
                        await task
                self.assertEqual(self.wire.sent, sent)

    async def test_multi_result_rejection_and_late_success_keep_server_observations(self):
        await self.stage_two_result_recipe()
        b = self.b
        operation = self.bot.craft_recipe_grid
        pending, request = await self.submit(operation(3415, 9, additional_destination_slots=(10,)))
        await self.deliver(b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=request.client_request_id, result=b.ItemStackNetResult.ERROR,
        )]))
        self.assertEqual((await pending).result.name, "ERROR")
        self.assertEqual(self.carried_stacks(9, 10), ((0, 0, None), (0, 0, None)))
        self.assertEqual(self.bot._recipes._grid_slots[32].net_id_variant, 50)
        self.assertFalse(self.bot._inventory._slot_observers)

        pending, request = await self.submit(operation(3415, 9, additional_destination_slots=(10,)))
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(self.carried_stacks(9, 10), ((0, 0, None), (0, 0, None)))
        await self.deliver(
            self.slot(9, self.item(2, 70, item_id=17), inventory_id=b.ContainerID.INVENTORY),
            self.slot(32, self.item(1, 71, item_id=18), inventory_id=b.ContainerID.PLAYER_ONLY_UI),
            b.ItemStackResponsePacket(responses=[self.response(
                request, ("CRAFTING_INPUT_CONTAINER", 32, 0, None),
                ("INVENTORY_CONTAINER", 9, 1, 60), ("INVENTORY_CONTAINER", 10, 3, 61),
            )]),
        )
        self.assertEqual(self.carried_stacks(9, 10), ((17, 2, 70), (363, 3, 61)))
        self.assertEqual(self.bot._recipes._grid_slots[32].net_id_variant, 71)
        self.assertFalse(self.bot._inventory._slot_observers)
        self.assertEqual(sum(isinstance(packet, b.ItemStackRequestPacket) for packet in self.wire.sent), 2)

    async def test_identical_inventory_and_ui_reports_do_not_supersede_craft_reply(self):
        await self.stage_two_result_recipe()
        b = self.b
        pending, request = await self.submit(self.bot.craft_recipe_grid(3415, 9, additional_destination_slots=(10,)))
        await self.deliver(
            b.InventoryContentPacket(inventory_id=b.ContainerID.PLAYER_ONLY_UI,
                                     slots=[self.item()] * 32 + [self.item(1, 50, item_id=364)]),
            b.InventoryContentPacket(inventory_id=b.ContainerID.INVENTORY, slots=[self.item()] * 11),
            b.ItemStackResponsePacket(responses=[self.response(
                request, ("CRAFTING_INPUT_CONTAINER", 32, 0, None),
                ("INVENTORY_CONTAINER", 9, 1, 60), ("INVENTORY_CONTAINER", 10, 3, 61),
            )]),
        )
        self.assertEqual((await pending).result.name, "SUCCESS")
        self.assertEqual(self.carried_stacks(9, 10), ((420, 1, 60), (363, 3, 61)))
        self.assertEqual(self.bot._recipes._grid_slots[32].id, 0)

    async def test_observed_change_and_restore_supersede_craft_reply(self):
        await self.stage_two_result_recipe()
        b = self.b
        pending, request = await self.submit(self.bot.craft_recipe_grid(3415, 9, additional_destination_slots=(10,)))
        await self.deliver(
            self.slot(32, self.item(1, 71), inventory_id=b.ContainerID.PLAYER_ONLY_UI),
            self.slot(9, self.item(1, 70), inventory_id=b.ContainerID.INVENTORY),
        )
        await self.deliver(
            self.slot(32, self.item(1, 50, item_id=364), inventory_id=b.ContainerID.PLAYER_ONLY_UI),
            self.slot(9, self.item(), inventory_id=b.ContainerID.INVENTORY),
            b.ItemStackResponsePacket(responses=[self.response(
                request, ("CRAFTING_INPUT_CONTAINER", 32, 0, None),
                ("INVENTORY_CONTAINER", 9, 1, 60), ("INVENTORY_CONTAINER", 10, 3, 61),
            )]),
        )
        self.assertEqual((await pending).result.name, "SUCCESS")
        self.assertEqual(self.carried_stacks(9, 10), ((0, 0, None), (363, 3, 61)))
        self.assertEqual(self.bot._recipes._grid_slots[32].net_id_variant, 50)

    async def test_incomplete_or_conflicting_craft_reply_invalidates_only_ambiguous_slots(self):
        b = self.b
        for conflicts in (False, True):
            with self.subTest(conflicts=conflicts):
                await self.stage_two_result_recipe()
                await self.deliver(self.slot(33, self.item(1, 51, item_id=364),
                                             inventory_id=b.ContainerID.PLAYER_ONLY_UI))
                craft = self.bot.craft_recipe_grid
                pending, request = await self.submit(craft(3415, 9, additional_destination_slots=(10,)))
                corrections = [
                    ("CRAFTING_INPUT_CONTAINER", 33, 0, None),
                    ("INVENTORY_CONTAINER", 10, 3, 61),
                ]
                if conflicts:
                    corrections.extend((
                        ("CRAFTING_INPUT_CONTAINER", 32, 0, None),
                        ("CRAFTING_INPUT_CONTAINER", 32, 1, 50),
                        ("INVENTORY_CONTAINER", 9, 1, 60), ("INVENTORY_CONTAINER", 9, 1, 62),
                        ("INVENTORY_CONTAINER", 10, 3, 61),
                    ))
                await self.deliver(b.ItemStackResponsePacket(responses=[self.response(request, *corrections)]))
                self.assertEqual((await pending).result.name, "SUCCESS")
                self.assertNotIn(32, self.bot._recipes._grid_slots)
                self.assertEqual(self.bot._recipes._grid_slots[33].id, 0)
                self.assertNotIn(9, self.bot.inventory_slots)
                self.assertEqual(self.carried_stacks(10), ((363, 3, 61),))
                with self.assertRaisesRegex(RuntimeError, "inventory count is not current"):
                    await self.bot.wait_for_inventory_item_count(420, 1, timeout=1)

                await self.deliver(
                    self.slot(33, self.item(1, 71), inventory_id=b.ContainerID.PLAYER_ONLY_UI),
                    self.slot(9, self.item(), inventory_id=b.ContainerID.INVENTORY),
                    self.slot(10, self.item(), inventory_id=b.ContainerID.INVENTORY),
                )
                sent = list(self.wire.sent)
                with self.assertRaisesRegex(RuntimeError, "unconfirmed crafting inputs"):
                    await craft(3415, 9, additional_destination_slots=(10,))
                self.assertEqual(self.wire.sent, sent)
                await self.deliver(self.slot(32, self.item(), inventory_id=b.ContainerID.PLAYER_ONLY_UI))
                pending, recovered = await self.submit(craft(3415, 9, additional_destination_slots=(10,)))
                consumes = [action.source.slot for action in recovered.actions
                            if isinstance(action, b.ItemStackRequestCereal.ConsumeActionData)]
                self.assertEqual(consumes, [33])
                await self.deliver(b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
                    client_request_id=recovered.client_request_id, result=b.ItemStackNetResult.ERROR,
                )]))
                self.assertEqual((await pending).result.name, "ERROR")
