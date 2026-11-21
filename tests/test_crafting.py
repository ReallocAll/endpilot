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
