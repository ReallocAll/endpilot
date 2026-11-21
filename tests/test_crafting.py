import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from endpilot.protocol import Protocol, RawPacket
from endpilot.recipes import Recipes


def item(b, item_id, count, net_id):
    return b.SerializedNetworkItemStackDescriptor(
        id=item_id, stack_size=count, aux_value=0, net_id_variant=net_id,
        block_runtime_id=0, user_data_buffer=b"",
    )


def server_recipe():
    return SimpleNamespace(
        recipe_id="minecraft:planks", net_id=19,
        ingredients=[SimpleNamespace(
            descriptor={"name": "minecraft:oak_log"}, stack_size=1,
        )],
        results=[SimpleNamespace(
            id=2, stack_size=4, aux_value=3,
            block_runtime_id=17, user_data_buffer=b"recipe-data",
        )],
        tag="crafting_table",
    )


class CraftingTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_craft_sends_client_actions_and_reconciles_response(self):
        b = Protocol(2193).binding
        slots = {
            0: item(b, 1, 2, 23),
            2: item(b, 0, 0, 0),
        }
        inventory = SimpleNamespace(
            slots=slots,
            item_or_empty=lambda index: slots.get(index, item(b, 0, 0, 0)),
            _allocate_request_id=Mock(return_value=-1),
            _set_slot=lambda index, value: slots.__setitem__(index, value),
        )
        reply = SimpleNamespace(
            client_request_id=SimpleNamespace(raw_id=-1),
            result=SimpleNamespace(name="SUCCESS"),
            containers=[SimpleNamespace(
                full_container_name=SimpleNamespace(
                    name=b.ContainerEnumName.COMBINED_HOTBAR_AND_INVENTORY_CONTAINER,
                ),
                slots=[
                    SimpleNamespace(slot=0, amount=1,
                                    item_stack_net_id=SimpleNamespace(raw_id=44)),
                    SimpleNamespace(slot=2, amount=4,
                                    item_stack_net_id=SimpleNamespace(raw_id=45)),
                ],
            )],
        )
        other = SimpleNamespace(client_request_id=SimpleNamespace(raw_id=-3))
        raw = RawPacket(b.ItemStackResponsePacket.ID, b"reply")
        unrelated = RawPacket(b.ItemStackResponsePacket.ID, b"unrelated")
        decoded = {
            b"reply": SimpleNamespace(responses=[other, reply]),
            b"unrelated": SimpleNamespace(responses=[other]),
        }

        async def wait(packet_id, *, match, timeout):
            self.assertEqual(packet_id, b.ItemStackResponsePacket.ID)
            self.assertEqual(timeout, 3)
            self.assertFalse(match(unrelated))
            self.assertTrue(match(raw))
            return raw

        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: decoded[raw.body]),
            subscribe_raw=lambda cb: lambda: None,
            wait_for_packet=wait, send=AsyncMock(),
        )
        recipes = Recipes(packets, {1: "minecraft:oak_log", 2: "minecraft:oak_planks"},
                          inventory=inventory)
        recipes.load(SimpleNamespace(
            clear_recipes=True, shapeless_recipes=[server_recipe()],
        ))
        try:
            self.assertIs(await recipes.craft_auto("minecraft:planks", 2, timeout=3), reply)
            request = packets.send.await_args.args[0].requests[0]
            self.assertEqual(int(request.client_request_id.raw_id), -1)
            actions = request.actions
            self.assertEqual([type(a).__name__ for a in actions], [
                "CraftRecipeAutoActionData", "CraftResultsActionData",
                "ConsumeActionData", "PlaceActionData",
            ])
            self.assertEqual(actions[0].recipe_net_id, 19)
            self.assertEqual(actions[0].ingredients[0].item_descriptor.full_name,
                             "minecraft:oak_log")
            self.assertEqual(actions[0].ingredients[0].item_descriptor.aux_value, 32767)
            self.assertEqual(actions[1].craft_results[0].item_descriptor.full_name,
                             "minecraft:oak_planks")
            self.assertEqual(actions[1].craft_results[0].block_runtime_id, 17)
            self.assertEqual(actions[1].craft_results[0].user_data_buffer, b"recipe-data")
            self.assertEqual((actions[2].amount, actions[2].source.slot,
                              actions[2].source.net_id_variant), (1, 0, 23))
            self.assertEqual(actions[3].source.full_container_name.name.name,
                             "CREATED_OUTPUT_CONTAINER")
            self.assertEqual((actions[3].source.slot, actions[3].source.net_id_variant),
                             (50, -1))
            self.assertEqual(actions[3].destination.slot, 2)
            self.assertEqual((int(slots[0].id), int(slots[0].stack_size),
                              int(slots[0].net_id_variant)), (1, 1, 44))
            self.assertEqual((int(slots[2].id), int(slots[2].stack_size),
                              int(slots[2].net_id_variant)), (2, 4, 45))
        finally:
            recipes.close()

    async def test_missing_material_does_not_submit_and_rejection_applies_correction(self):
        b = Protocol(2193).binding
        slots = {0: item(b, 1, 1, 23), 2: item(b, 0, 0, 0)}
        inventory = SimpleNamespace(
            slots=slots,
            item_or_empty=lambda index: slots.get(index, item(b, 0, 0, 0)),
            _allocate_request_id=Mock(return_value=-3),
            _set_slot=lambda index, value: slots.__setitem__(index, value),
        )
        reply = SimpleNamespace(
            client_request_id=SimpleNamespace(raw_id=-3),
            result=SimpleNamespace(name="ERROR"),
            containers=[SimpleNamespace(
                full_container_name=SimpleNamespace(
                    name=b.ContainerEnumName.COMBINED_HOTBAR_AND_INVENTORY_CONTAINER,
                ),
                slots=[SimpleNamespace(slot=0, amount=1,
                                       item_stack_net_id=SimpleNamespace(raw_id=99))],
            )],
        )
        packets = SimpleNamespace(
            protocol=SimpleNamespace(
                binding=b, decode=lambda raw: SimpleNamespace(responses=[reply]),
            ),
            subscribe_raw=lambda cb: lambda: None,
            wait_for_packet=AsyncMock(return_value=RawPacket(
                b.ItemStackResponsePacket.ID, b"reply",
            )),
            send=AsyncMock(),
        )
        recipes = Recipes(packets, {1: "minecraft:oak_log", 2: "minecraft:oak_planks"},
                          inventory=inventory)
        recipes.load(SimpleNamespace(
            clear_recipes=True, shapeless_recipes=[server_recipe()],
        ))
        try:
            with self.assertRaisesRegex(ValueError, "not enough"):
                await recipes.craft_auto(19, 2, times=2)
            packets.send.assert_not_awaited()
            self.assertIs(await recipes.craft_auto(19, 2), reply)
            self.assertEqual((int(slots[0].id), int(slots[0].stack_size),
                              int(slots[0].net_id_variant)), (1, 1, 99))
            self.assertEqual(int(slots[2].id), 0)
            with self.assertRaises(KeyError):
                await recipes.craft_auto("missing", 2)
        finally:
            recipes.close()

    async def test_grid_staging_then_craft_reconciles_server_corrections(self):
        b = Protocol(2193).binding
        from unittest.mock import AsyncMock, Mock

        slots = {0: item(b, 1, 2, 23), 8: item(b, 0, 0, 0)}
        inventory = SimpleNamespace(
            slots=slots,
            item_or_empty=lambda index: slots.get(index, item(b, 0, 0, 0)),
            _allocate_request_id=Mock(side_effect=[-1, -3]),
            _set_slot=lambda index, value: slots.__setitem__(index, value),
        )
        window = SimpleNamespace(container_id=2, container_type="INVENTORY")
        def response_slot(index, amount, net_id):
            return SimpleNamespace(
                slot=index, amount=amount,
                item_stack_net_id=SimpleNamespace(raw_id=net_id) if net_id else None,
            )
        def response_container(name, *slots):
            return SimpleNamespace(
                full_container_name=SimpleNamespace(name=b.ContainerEnumName[name]),
                slots=slots,
            )
        staged = SimpleNamespace(
            client_request_id=SimpleNamespace(raw_id=-1),
            result=SimpleNamespace(name="SUCCESS"),
            containers=[
                response_container("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                                   response_slot(0, 1, 44)),
                response_container("CRAFTING_INPUT_CONTAINER", response_slot(28, 1, 50)),
            ],
        )
        crafted = SimpleNamespace(
            client_request_id=SimpleNamespace(raw_id=-3),
            result=SimpleNamespace(name="SUCCESS"),
            containers=[
                response_container("CRAFTING_INPUT_CONTAINER", response_slot(28, 0, None)),
                response_container("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER",
                                   response_slot(8, 4, 51)),
            ],
        )
        other = SimpleNamespace(client_request_id=SimpleNamespace(raw_id=-5))
        raws = [
            RawPacket(b.ItemStackResponsePacket.ID, b"stage"),
            RawPacket(b.ItemStackResponsePacket.ID, b"craft"),
        ]
        decoded = {
            b"stage": SimpleNamespace(responses=[other, staged]),
            b"craft": SimpleNamespace(responses=[other, crafted]),
            b"other": SimpleNamespace(responses=[other]),
        }
        pending = iter(raws)
        async def wait(packet_id, *, timeout, match):
            self.assertEqual(packet_id, b.ItemStackResponsePacket.ID)
            self.assertEqual(timeout, 3)
            self.assertFalse(match(RawPacket(packet_id, b"other")))
            raw = next(pending)
            self.assertTrue(match(raw))
            return raw
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: decoded[raw.body]),
            subscribe_raw=lambda cb: lambda: None,
            wait_for_packet=wait, send=AsyncMock(),
        )
        recipes = Recipes(
            packets, {1: "minecraft:oak_log", 2: "minecraft:oak_planks"},
            inventory=inventory, containers=SimpleNamespace(window=window),
        )
        recipes.load(SimpleNamespace(
            clear_recipes=True, shapeless_recipes=[server_recipe()],
        ))
        try:
            self.assertIs(await recipes.stage_grid(0, 28, timeout=3), staged)
            stage_action = packets.send.await_args.args[0].requests[0].actions[0]
            self.assertEqual(type(stage_action).__name__, "PlaceActionData")
            self.assertEqual(stage_action.source.net_id_variant, 23)
            self.assertEqual((stage_action.destination.slot,
                              stage_action.destination.full_container_name.name.name),
                             (28, "CRAFTING_INPUT_CONTAINER"))
            self.assertEqual((slots[0].stack_size, recipes._grid_slots[28].stack_size,
                              recipes._grid_slots[28].net_id_variant), (1, 1, 50))

            self.assertIs(await recipes.craft_grid(19, 8, timeout=3), crafted)
            actions = packets.send.await_args.args[0].requests[0].actions
            self.assertEqual([type(action).__name__ for action in actions], [
                "CraftRecipeActionData", "CraftResultsActionData",
                "ConsumeActionData", "PlaceActionData",
            ])
            self.assertEqual((actions[0].recipe_net_id, actions[0].num_crafts), (19, 1))
            self.assertEqual((actions[2].source.slot, actions[2].source.net_id_variant),
                             (28, 50))
            self.assertEqual((actions[3].source.slot, actions[3].source.net_id_variant),
                             (50, -3))
            self.assertEqual((slots[8].id, slots[8].stack_size, slots[8].net_id_variant),
                             (2, 4, 51))
            self.assertEqual(recipes._grid_slots[28].stack_size, 0)
        finally:
            recipes.close()

    async def test_grid_action_requires_open_inventory_and_observed_ingredient(self):
        b = Protocol(2193).binding
        from unittest.mock import AsyncMock
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b),
            subscribe_raw=lambda cb: lambda: None,
            send=AsyncMock(),
        )
        slots = {0: item(b, 1, 1, 23)}
        inventory = SimpleNamespace(slots=slots)
        containers = SimpleNamespace(window=None)
        recipes = Recipes(packets, {1: "minecraft:oak_log"}, inventory=inventory,
                          containers=containers)
        recipes.load(SimpleNamespace(clear_recipes=True, shapeless_recipes=[server_recipe()]))
        try:
            with self.assertRaisesRegex(RuntimeError, "open the inventory screen"):
                await recipes.stage_grid(0, 28)
            containers.window = SimpleNamespace(container_type="INVENTORY")
            with self.assertRaisesRegex(ValueError, "slots are 28 through 31"):
                await recipes.stage_grid(0, 32)
            with self.assertRaisesRegex(ValueError, "no observed crafting ingredients"):
                await recipes.craft_grid(19, 8)
            packets.send.assert_not_awaited()
        finally:
            recipes.close()
