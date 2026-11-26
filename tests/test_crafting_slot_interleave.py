import unittest

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
