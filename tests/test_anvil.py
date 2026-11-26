import asyncio
import unittest

from endpilot import Bot
from endpilot.containers import Containers
from endpilot.creative_inventory import CreativeInventory
from endpilot.inventory import Inventory
from endpilot.player_state import PlayerState

from test_blocks import PacketStream
from test_container_take import item


PLAYER = "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"
INPUT = "ANVIL_INPUT_CONTAINER"
MATERIAL = "ANVIL_MATERIAL_CONTAINER"


class AnvilTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("AnvilTest", protocol=2225)
        self.b = self.bot.protocol.binding
        self.wire = PacketStream(self.bot.protocol)
        inventory = self.bot._inventory = Inventory(self.wire, 1)
        containers = self.bot._containers = Containers(self.wire, None, inventory)
        self.bot._creative = CreativeInventory(self.wire, inventory, containers, 9, {}, (
            self.b.StartGamePacket(entity_game_type=self.b.GameType.SURVIVAL),
        ))
        self.bot._player_state = PlayerState(self.wire, 1)
        self.addAsyncCleanup(self.bot.close)
        self.player_slot(4, item(self.b))

    def emit(self, packet):
        self.wire.emit(self.bot.protocol.encode(packet))

    def open(self, kind="ANVIL"):
        self.emit(self.b.ContainerOpenPacket(
            container_id=2, type=self.b.ContainerType[kind], pos=self.b.BlockPos(x=1, y=100, z=0),
            entity_unique_id=-1,
        ))
        self.ui()

    def ui(self, primary=None, material=None):
        b = self.b
        self.content(b.ContainerID.PLAYER_ONLY_UI, item(b), primary or item(b), material or item(b))

    def content(self, container, *slots):
        b = self.b
        self.emit(b.InventoryContentPacket(
            inventory_id=container, slots=slots,
            storage_item=item(b),
            full_container_name=b.FullContainerName(name=b.ContainerEnumName[INPUT], dynamic_id=None),
        ))

    def player_slot(self, index, descriptor):
        self.emit(self.b.InventorySlotPacket(
            inventory_id=self.b.ContainerID.INVENTORY, slot=index, item=descriptor,
        ))

    async def next_request(self):
        for _ in range(4):
            await asyncio.sleep(0)
            if self.wire.sent:
                return self.wire.sent.pop(0).requests[0]
        self.fail("no item-stack request was sent")

    def response(self, request, *rows, result="SUCCESS"):
        b = self.b
        containers = []
        if result == "SUCCESS":
            for label, index, count, net_id, name, damage in rows:
                containers.append(b.ItemStackResponseContainerInfo(
                    full_container_name=b.FullContainerName(name=b.ContainerEnumName[label], dynamic_id=None),
                    slots=[b.ItemStackResponseSlotInfo(
                        requested_slot=index, slot=index, amount=count,
                        item_stack_net_id=None if net_id is None else b.ItemStackNetId(raw_id=net_id),
                        custom_name=b.RedactableString(unredacted_string=name, redacted_string=None),
                        durability_correction=damage,
                    )],
                ))
        return b.ItemStackResponseInfo(
            client_request_id=request.client_request_id,
            result=b.ItemStackNetResult[result],
            containers=containers if result == "SUCCESS" else None,
        )

    def reply(self, request, *rows, result="SUCCESS"):
        self.emit(self.b.ItemStackResponsePacket(responses=[self.response(request, *rows, result=result)]))

    async def test_public_staging_and_rename_preserve_only_server_confirmed_data(self):
        b = self.b
        self.open()
        original = item(b, 318, 1, 11)
        original.user_data_buffer = b"prior metadata"
        self.player_slot(0, original)
        self.player_slot(4, item(b))

        staging = asyncio.create_task(self.bot.put_into_container(0, 1))
        request = await self.next_request()
        self.assertEqual(request.actions[0].destination.full_container_name.name.name, INPUT)
        self.reply(request, (INPUT, 1, 1, 11, "", 12), (PLAYER, 0, 0, None, "", 0))
        self.assertEqual((await staging).result.name, "SUCCESS")
        self.assertEqual(self.bot.inventory_slots[0].id, 0)
        self.assertEqual(self.bot.container_slots[1].user_data_buffer, b"prior metadata")

        applying = asyncio.create_task(self.bot.apply_anvil(4, "New name"))
        renamed = await self.next_request()
        self.assertEqual(int(renamed.client_request_id.raw_id), int(request.client_request_id.raw_id) - 2)
        self.assertEqual(tuple(renamed.strings_to_filter), ("New name",))
        optional, consume, take = renamed.actions
        self.assertIsInstance(optional, b.ItemStackRequestCereal.CraftRecipeOptionalActionData)
        self.assertEqual((optional.recipe_net_id, optional.filtered_string_index), (0, 0))
        self.assertEqual((consume.source.slot, consume.source.net_id_variant), (1, 11))
        self.assertEqual((take.source.slot, take.source.net_id_variant, take.destination.slot),
                         (50, int(renamed.client_request_id.raw_id), 4))
        self.reply(renamed, (INPUT, 1, 0, None, "", 0), (PLAYER, 4, 1, 12, "New name", 7))
        result = await applying
        self.assertEqual(result.containers[-1].slots[0].custom_name.unredacted_string, "New name")
        self.assertEqual(result.containers[-1].slots[0].durability_correction, 7)
        self.assertEqual(self.bot.inventory_slots[4].user_data_buffer, b"")

        confirmed = item(b, 318, 1, 12)
        confirmed.user_data_buffer = b"server item metadata"
        self.player_slot(4, confirmed)
        self.assertEqual(self.bot.inventory_slots[4].user_data_buffer, b"server item metadata")

    async def test_two_inputs_are_consumed_on_wire_and_native_refusal_leaves_them(self):
        b = self.b
        self.open()
        self.ui(item(b, 318, 1, 11), item(b, 318, 1, 12))
        self.emit(b.UpdateAttributesPacket(runtime_id=1, attribute_data=[
            b.AttributeData(name="minecraft:player.level", current_value=0),
        ]))
        applying = asyncio.create_task(self.bot.apply_anvil(4))
        request = await self.next_request()
        self.assertEqual(tuple(request.strings_to_filter), ())
        self.assertEqual([(entry.source.full_container_name.name.name, entry.source.slot,
                           entry.source.net_id_variant) for entry in request.actions[1:3]],
                         [(INPUT, 1, 11), (MATERIAL, 2, 12)])
        self.reply(request, result="INVALID_CRAFT_RESULT")
        self.assertEqual((await applying).result.name, "INVALID_CRAFT_RESULT")
        self.assertEqual(self.bot.experience_level, 0)
        self.assertEqual(self.bot.inventory_slots[4].id, 0)
        self.assertEqual([(value.id, value.net_id_variant) for value in self.bot.container_slots.values()],
                         [(318, 11), (318, 12)])

    async def test_explicit_material_count_uses_server_remainder_for_public_take_back(self):
        b = self.b
        self.open()
        self.ui(item(b, 299, 1, 11), item(b, 307, 4, 12))
        self.player_slot(6, item(b))
        applying = asyncio.create_task(self.bot.apply_anvil(4, material_count=1))
        request = await self.next_request()
        _, primary, material, output = request.actions
        self.assertIsInstance(material, b.ItemStackRequestCereal.ConsumeActionData)
        self.assertEqual((primary.amount, primary.source.slot, primary.source.net_id_variant), (1, 1, 11))
        self.assertEqual((material.amount, material.source.slot, material.source.net_id_variant), (1, 2, 12))
        self.assertEqual(material.source.full_container_name.name.name, MATERIAL)
        self.assertEqual((output.source.slot, output.destination.slot, tuple(request.strings_to_filter)), (50, 4, ()))
        self.assertEqual(self.bot.container_slots[2].stack_size, 4)

        self.reply(request, (INPUT, 1, 0, None, "", 0), (MATERIAL, 2, 3, 19, "", 0), (PLAYER, 4, 1, 13, "", 0))
        self.assertEqual((await applying).result.name, "SUCCESS")
        remaining = self.bot.container_slots[2]
        self.assertEqual((remaining.id, remaining.stack_size, remaining.net_id_variant), (307, 3, 19))
        self.assertEqual((self.bot.inventory_slots[4].id, self.bot.inventory_slots[4].net_id_variant), (299, 13))

        taking = asyncio.create_task(self.bot.take_from_container(2, 6, 3))
        request = await self.next_request()
        take, = request.actions
        self.assertEqual((take.amount, take.source.slot, take.source.net_id_variant, take.destination.slot), (3, 2, 19, 6))
        self.reply(request, (MATERIAL, 2, 0, None, "", 0), (PLAYER, 6, 3, 20, "", 0))
        self.assertEqual((await taking).result.name, "SUCCESS")
        self.assertEqual((self.bot.inventory_slots[6].id, self.bot.inventory_slots[6].stack_size), (307, 3))

    async def test_default_uses_the_full_material_stack_and_refusal_preserves_inputs(self):
        b = self.b
        for options, expected in (({}, 4), ({"material_count": 1}, 1), ({"material_count": 4}, 4)):
            with self.subTest(options=options):
                self.open()
                self.ui(item(b, 299, 1, 11), item(b, 307, 4, 12))
                applying = asyncio.create_task(self.bot.apply_anvil(4, "Keep filtered name", **options))
                request = await self.next_request()
                self.assertEqual(tuple(request.strings_to_filter), ("Keep filtered name",))
                self.assertEqual([action.amount for action in request.actions[1:3]], [1, expected])
                self.reply(request, result="ERROR")
                self.assertEqual((await applying).result.name, "ERROR")
                self.assertEqual([(value.id, value.stack_size, value.net_id_variant)
                                  for value in self.bot.container_slots.values()], [(299, 1, 11), (307, 4, 12)])
                self.assertEqual(self.bot.inventory_slots[4].id, 0)

    async def test_explicit_material_count_requires_a_positive_integer_and_observed_material(self):
        b = self.b
        self.open()
        self.ui(item(b, 299, 1, 11), item(b, 307, 4, 12))
        for count in (False, True, "1", 1.0, -1, 0, 5, 256):
            with self.subTest(count=count):
                with self.assertRaisesRegex(ValueError, "material_count"):
                    await self.bot.apply_anvil(4, material_count=count)
        for material in (None, item(b, 307, 4)):
            self.ui(item(b, 299, 1, 11), material)
            with self.assertRaisesRegex(ValueError, "material.*observed|observed.*material"):
                await self.bot.apply_anvil(4, "Name only", material_count=1)
        self.assertEqual(self.wire.sent, [])

    async def test_explicit_material_count_rechecks_count_and_network_id_before_send(self):
        b = self.b
        for count, net_id in ((3, 12), (4, 21)):
            with self.subTest(count=count, net_id=net_id):
                self.open()
                self.ui(item(b, 299, 1, 11), item(b, 307, 4, 12))
                applying = asyncio.create_task(self.bot.apply_anvil(4, material_count=1))
                await asyncio.sleep(0)
                self.ui(item(b, 299, 1, 11), item(b, 307, count, net_id))
                with self.assertRaisesRegex(RuntimeError, "inputs changed before submission"):
                    await applying
                self.assertEqual(self.wire.sent, [])
                remaining = self.bot.container_slots[2]
                self.assertEqual((remaining.stack_size, remaining.net_id_variant), (count, net_id))

    async def test_old_verdict_cannot_write_reopened_window_or_fresh_destination(self):
        b = self.b
        self.open()
        self.ui(item(b, 318, 1, 11))
        applying = asyncio.create_task(self.bot.apply_anvil(4, "Old window"))
        request = await self.next_request()
        self.open()
        self.ui(item(b, 320, 1, 21))
        fresh = item(b, 418, 1, 99)
        self.player_slot(4, fresh)
        self.reply(request, (INPUT, 1, 0, None, "", 0), (PLAYER, 4, 1, 12, "Old window", 0))
        self.assertEqual((await applying).result.name, "SUCCESS")
        self.assertEqual(self.bot.container_slots[1].net_id_variant, 21)
        self.assertEqual(self.bot.inventory_slots[4].net_id_variant, 99)

    async def test_anvil_submission_checks_window_again_before_transport(self):
        self.open()
        self.ui(item(self.b, 318, 1, 11))
        applying = asyncio.create_task(self.bot.apply_anvil(4, "Prepared"))
        await asyncio.sleep(0)
        self.open()
        self.ui(item(self.b, 318, 1, 11))
        with self.assertRaisesRegex(RuntimeError, "window changed before submission"):
            await applying
        self.assertEqual(self.wire.sent, [])

    async def test_apply_requires_survival_and_observed_anvil(self):
        self.open("CONTAINER")
        with self.assertRaisesRegex(RuntimeError, "observed anvil"):
            await self.bot.apply_anvil(4, "No anvil")
        self.open()
        self.ui(item(self.b, 318, 1, 11))
        self.emit(self.b.SetPlayerGameTypePacket(player_game_type=self.b.GameType.CREATIVE))
        with self.assertRaisesRegex(RuntimeError, "survival"):
            await self.bot.apply_anvil(4, "Creative mode")
        self.assertEqual(self.wire.sent, [])

    async def test_both_anvil_content_layouts_preserve_distinct_nonempty_input_and_material(self):
        b = self.b
        self.open()
        self.content(2, item(b, 318, 1, 11), item(b, 320, 1, 12), item(b, 303, 1, 13))
        self.assertEqual({index: value.id for index, value in self.bot.container_slots.items()}, {1: 318, 2: 320})
        self.ui(item(b, 317, 1, 21), item(b, 319, 2, 22))
        self.assertEqual({index: value.id for index, value in self.bot.container_slots.items()}, {1: 317, 2: 319})
        self.emit(b.InventorySlotPacket(inventory_id=2, slot=1, item=item(b, 322, 1, 23)))
        self.assertEqual({index: value.id for index, value in self.bot.container_slots.items()}, {1: 317, 2: 322})
        self.emit(b.InventorySlotPacket(inventory_id=2, slot=2, item=item(b, 323, 1, 24)))
        self.assertEqual({index: value.id for index, value in self.bot.container_slots.items()}, {1: 317, 2: 322})

    async def test_same_material_stack_receives_both_verdicts_in_wire_order(self):
        b = self.b
        self.open()
        self.ui(material=item(b, 320, 6, 10))
        first = asyncio.create_task(self.bot.take_from_container(2, 5))
        second = asyncio.create_task(self.bot.take_from_container(2, 6))
        sent_first = await self.next_request()
        sent_second = await self.next_request()
        self.ui(material=item(b, 320, 6, 10))
        self.emit(b.ItemStackResponsePacket(responses=[
            self.response(sent_second, (MATERIAL, 2, 5, 11, "", 0)),
            self.response(sent_first, (MATERIAL, 2, 4, 12, "", 0)),
        ]))
        self.assertEqual([(await first).containers[0].slots[0].amount,
                          (await second).containers[0].slots[0].amount], [4, 5])
        material = self.bot.container_slots[2]
        self.assertEqual((material.id, material.stack_size, material.net_id_variant), (320, 4, 12))

    async def test_new_anvil_observations_block_older_material_verdicts(self):
        b = self.b
        for replacement in ("different item", "different data", "different ID", "clear", "reopen"):
            with self.subTest(replacement=replacement):
                self.open()
                self.ui(material=item(b, 320, 6, 10))
                earlier = asyncio.create_task(self.bot.take_from_container(2, 5))
                taking = asyncio.create_task(self.bot.take_from_container(2, 6))
                earlier_request = await self.next_request()
                request = await self.next_request()
                self.reply(earlier_request, (MATERIAL, 2, 5, 11, "", 0))
                self.assertEqual(self.bot.container_slots[2].stack_size, 5)
                if replacement == "clear":
                    self.content(2)
                    observed = None
                elif replacement == "reopen":
                    self.open()
                    observed = item(b, 320, 6, 10)
                    self.ui(material=observed)
                else:
                    observed = item(b, 321 if replacement == "different item" else 320, 5,
                                    99 if replacement == "different ID" else 11)
                    if replacement == "different data":
                        observed.user_data_buffer = b"new item metadata"
                    self.ui(material=observed)
                self.reply(request, (MATERIAL, 2, 4, 12, "", 0))
                self.assertEqual((await taking).result.name, "SUCCESS")
                self.assertEqual((await earlier).result.name, "SUCCESS")
                current = self.bot.container_slots.get(2)
                if observed is None:
                    self.assertIsNone(current)
                else:
                    self.assertEqual((current.id, current.stack_size, current.net_id_variant, current.user_data_buffer),
                                     (observed.id, observed.stack_size, observed.net_id_variant, observed.user_data_buffer))

    async def test_apply_requires_observed_empty_destination(self):
        self.open()
        self.ui(item(self.b, 318, 1, 11))
        with self.assertRaisesRegex(RuntimeError, "destination.*unobserved"):
            await self.bot.apply_anvil(5, "Needs observed slot", timeout=0.05)
        self.assertEqual(self.wire.sent, [])
        self.player_slot(5, item(self.b))
        applying = asyncio.create_task(self.bot.apply_anvil(5, "Observed slot"))
        request = await self.next_request()
        self.reply(request, (INPUT, 1, 0, None, "", 0), (PLAYER, 5, 1, 12, "Observed slot", 0))
        self.assertEqual((await applying).result.name, "SUCCESS")
        self.assertEqual(self.bot.inventory_slots[5].net_id_variant, 12)
