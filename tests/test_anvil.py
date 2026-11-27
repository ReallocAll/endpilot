import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from endpilot import AnvilPreview, Bot
from endpilot.containers import Containers
from endpilot.creative_inventory import CreativeInventory
from endpilot.inventory import Inventory
from endpilot.player_state import PlayerState
from endpilot.protocol import write_batch

from test_blocks import PacketStream
from test_container_take import item
from test_scheduled_input import ManualClock, Wire


PLAYER = "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"
INPUT = "ANVIL_INPUT_CONTAINER"
MATERIAL = "ANVIL_MATERIAL_CONTAINER"
DAMAGE_8_USER_DATA = b"\xff\xff\x01\x0a\0\0\x03\x06\0Damage\x08\0\0\0" + bytes(9)


def stack_response(b, request, *rows, result="SUCCESS"):
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


class AnvilPacketStream(PacketStream):
    async def send(self, packet, *, _before_send=None):
        if _before_send is not None:
            _before_send()
        await super().send(packet)


class AnvilTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("AnvilTest", protocol=2225)
        self.b = self.bot.protocol.binding
        self.wire = AnvilPacketStream(self.bot.protocol)
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

    def reply(self, request, *rows, result="SUCCESS"):
        self.emit(self.b.ItemStackResponsePacket(responses=[stack_response(self.b, request, *rows, result=result)]))

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

    async def test_apply_requires_supported_mode_and_observed_anvil(self):
        self.open("CONTAINER")
        with self.assertRaisesRegex(RuntimeError, "observed anvil"):
            await self.bot.apply_anvil(4, "No anvil")
        self.open()
        self.ui(item(self.b, 318, 1, 11))
        modes = self.b.GameType
        for mode in (modes.ADVENTURE, modes.SPECTATOR, modes.UNDEFINED):
            with self.subTest(mode=mode):
                self.emit(self.b.SetPlayerGameTypePacket(player_game_type=mode))
                with self.assertRaisesRegex(RuntimeError, "observed survival or creative"):
                    await self.bot.apply_anvil(4, "Unsupported mode")
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
            stack_response(b, sent_second, (MATERIAL, 2, 5, 11, "", 0)),
            stack_response(b, sent_first, (MATERIAL, 2, 4, 12, "", 0)),
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


class AnvilSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("PreviewAnvil", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)
        for packet in (
            b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
            b.StartGamePacket(runtime_id=7, entity_id=17, entity_game_type=b.GameType.SURVIVAL),
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        ):
            self.wire.push(packet)
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)
        self.wire.sent.clear()

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        received = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in (*packets, marker)))
        await received

    def window_packet(self, kind="ANVIL"):
        b = self.b
        return b.ContainerOpenPacket(container_id=2, type=b.ContainerType[kind],
                                     pos=b.BlockPos(x=1, y=100, z=0), entity_unique_id=-1)

    def ui_packet(self, *slots):
        b = self.b
        return b.InventoryContentPacket(
            inventory_id=b.ContainerID.PLAYER_ONLY_UI, slots=[item(b), *slots], storage_item=item(b),
            full_container_name=b.FullContainerName(name=b.ContainerEnumName[INPUT], dynamic_id=None),
        )

    def registry_packet(self, item_id=-41):
        return self.b.ItemRegistryPacket(items=[self.b.ItemData(id=item_id, name="minecraft:diamond_sword")])

    def sword(self, net_id=None, *, data=b"\0" * 10):
        stack = item(self.b, -41, 1, net_id)
        stack.user_data_buffer = data
        return stack

    async def stage_inputs(self, primary, material):
        b = self.b
        await self.deliver(
            self.registry_packet(), self.window_packet(), self.ui_packet(primary, material),
            b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=4, item=item(b)),
        )

    async def submit(self, operation):
        sent = asyncio.Event()
        unsubscribe = self.bot.packets.subscribe_sent(
            lambda packet_id: sent.set() if packet_id == self.b.ItemStackRequestPacket.ID else None,
        )
        pending = asyncio.create_task(operation)
        try:
            await asyncio.wait_for(sent.wait(), timeout=1)
        finally:
            unsubscribe()
        request, = self.wire.sent[-1].requests
        return pending, request

    async def complete(self, submitted, *rows, result="SUCCESS"):
        pending, request = submitted
        verdict = stack_response(self.b, request, *rows, result=result)
        await self.deliver(self.b.ItemStackResponsePacket(responses=[verdict]))
        self.assertEqual((await pending).result.name, result)

    async def test_anvil_mode_change_while_waiting_to_write_leaves_staging_intact(self):
        b = self.b
        await self.stage_inputs(self.sword(11), self.sword(12))
        await self.deliver(b.SetPlayerGameTypePacket(player_game_type=b.GameType.CREATIVE))
        ready = asyncio.Event()
        release = asyncio.Event()
        send = self.bot.packets.send

        async def delay_request(packet, *, _before_send=None):
            ready.set()
            await release.wait()
            await send(packet, _before_send=_before_send)

        with patch.object(self.bot.packets, "send", delay_request):
            applying = asyncio.create_task(self.bot.apply_anvil(4, "Creative rename", timeout=1))
            try:
                await asyncio.wait_for(ready.wait(), timeout=1)
                await self.deliver(b.SetPlayerGameTypePacket(player_game_type=b.GameType.SURVIVAL))
            finally:
                release.set()
            with self.assertRaisesRegex(RuntimeError, "game mode changed before anvil submission"):
                await applying
        self.assertEqual(self.wire.sent, [])
        self.assertEqual([self.bot.container_slots[slot].net_id_variant for slot in (1, 2)], [11, 12])
        self.assertEqual(self.bot.inventory_slots[4].id, 0)

    async def test_anvil_creative_keeps_matching_server_verdict_after_mode_changes(self):
        b = self.b
        await self.stage_inputs(self.sword(11), self.sword(12))
        await self.deliver(b.SetPlayerGameTypePacket(player_game_type=b.GameType.CREATIVE))
        submitted = await self.submit(self.bot.apply_anvil(4, "Creative rename"))
        _, request = submitted
        self.assertEqual(tuple(request.strings_to_filter), ("Creative rename",))
        self.assertEqual([action.source.net_id_variant for action in request.actions[1:3]], [11, 12])
        await self.deliver(b.SetPlayerGameTypePacket(player_game_type=b.GameType.SURVIVAL))
        await self.complete(submitted, (INPUT, 1, 0, None, "", 0), (MATERIAL, 2, 0, None, "", 0),
                            (PLAYER, 4, 1, 13, "Creative rename", 0))
        self.assertEqual([self.bot.container_slots[slot].id for slot in (1, 2)], [0, 0])
        self.assertEqual(self.bot.inventory_slots[4].net_id_variant, 13)

    async def test_rename_cost_uses_observed_item_registry_without_experience_destination_or_stack_id(self):
        b = self.b
        await self.deliver(self.window_packet(), self.ui_packet(self.sword(), item(b)))
        self.assertIsNone(self.bot.preview_anvil("New name"))
        await self.deliver(self.registry_packet())
        self.assertIsNone(self.bot.experience_level)
        self.assertEqual(self.bot.inventory_slots, {})
        preview = self.bot.preview_anvil("New name")
        self.assertEqual(preview, AnvilPreview(level_cost=1, material_count=0))
        self.assertIsNone(self.bot.preview_anvil("Diamond Sword"))
        self.assertEqual(self.bot.preview_anvil("   "), AnvilPreview(level_cost=1, material_count=0))
        with self.assertRaises(FrozenInstanceError):
            preview.level_cost = 2
        await self.deliver(self.registry_packet(41))
        self.assertIsNone(self.bot.preview_anvil("New name"))
        self.assertEqual(self.wire.sent, [])

    async def test_preview_requires_both_slots_in_the_current_anvil_window(self):
        b = self.b
        primary = self.sword(11)
        await self.deliver(self.registry_packet(), self.window_packet(), self.ui_packet(primary, item(b)))
        self.assertEqual(self.bot.preview_anvil("New name"), AnvilPreview(1, 0))
        await self.deliver(b.ContainerClosePacket(container_id=2, container_type=b.ContainerType.ANVIL))
        self.assertIsNone(self.bot.preview_anvil("New name"))
        await self.deliver(self.window_packet())
        self.assertIsNone(self.bot.preview_anvil("New name"))
        await self.deliver(self.ui_packet(primary))
        self.assertIsNone(self.bot.preview_anvil("New name"))
        await self.deliver(b.InventorySlotPacket(inventory_id=2, slot=1, item=item(b)))
        self.assertEqual(self.bot.preview_anvil("New name"), AnvilPreview(1, 0))
        await self.deliver(self.window_packet("CONTAINER"))
        self.assertIsNone(self.bot.preview_anvil("New name"))
        self.assertEqual(self.wire.sent, [])

    async def test_preview_rejects_unknown_metadata_and_non_survival_mode(self):
        b = self.b
        primary = self.sword(11, data=b"")
        await self.deliver(self.registry_packet(), self.window_packet(), self.ui_packet(primary, item(b)))
        self.assertIsNone(self.bot.preview_anvil("New name"))
        await self.deliver(self.ui_packet(self.sword(11), item(b)))
        self.assertEqual(self.bot.preview_anvil("New name"), AnvilPreview(1, 0))
        await self.deliver(b.SetPlayerGameTypePacket(player_game_type=b.GameType.CREATIVE))
        self.assertIsNone(self.bot.preview_anvil("New name"))
        for invalid in ("", 1):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "nonempty string"):
                self.bot.preview_anvil(invalid)
        self.assertEqual(self.wire.sent, [])
        await self.bot.close()
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            self.bot.preview_anvil("New name")

    async def test_preview_waits_for_fresh_input_after_take_omits_source_correction(self):
        b = self.b
        primary = self.sword(11)
        await self.stage_inputs(primary, item(b))
        self.assertEqual(self.bot.preview_anvil("New name"), AnvilPreview(1, 0))
        taking = await self.submit(self.bot.take_from_container(1, 4))
        await self.complete(taking, (PLAYER, 4, 1, 12, "", 0))
        self.assertEqual(self.bot.container_slots[1].net_id_variant, 11)
        self.assertIsNone(self.bot.preview_anvil("New name"))
        await self.deliver(b.InventorySlotPacket(inventory_id=2, slot=0, item=primary))
        self.assertEqual(self.bot.preview_anvil("New name"), AnvilPreview(1, 0))

    async def test_preview_waits_when_staging_omits_previously_empty_material_correction(self):
        b = self.b
        await self.stage_inputs(self.sword(11), item(b))
        await self.deliver(b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=0, item=self.sword(12)))
        self.assertEqual(self.bot.preview_anvil("New name"), AnvilPreview(1, 0))
        staging = await self.submit(self.bot.put_into_container(0, 2))
        await self.complete(staging, (PLAYER, 0, 0, None, "", 0))
        self.assertEqual(self.bot.container_slots[2].id, 0)
        self.assertIsNone(self.bot.preview_anvil("New name"))
        await self.deliver(b.InventorySlotPacket(inventory_id=2, slot=1, item=item(b)))
        self.assertEqual(self.bot.preview_anvil("New name"), AnvilPreview(1, 0))

    async def test_preview_keeps_newer_input_when_take_omits_correction(self):
        b = self.b
        for new_id in (11, 21):
            with self.subTest(new_id=new_id):
                await self.stage_inputs(self.sword(11), item(b))
                taking = await self.submit(self.bot.take_from_container(1, 4))
                await self.deliver(b.InventorySlotPacket(inventory_id=2, slot=0, item=self.sword(new_id)))
                await self.complete(taking, (PLAYER, 4, 1, 12, "", 0))
                self.assertEqual(self.bot.container_slots[1].net_id_variant, new_id)
                self.assertEqual(self.bot.preview_anvil("New name"), AnvilPreview(1, 0))

    async def test_preview_recovers_when_transfer_verdict_confirms_material_empty(self):
        b = self.b
        diamonds = item(b, 42, 2, 12)
        diamonds.user_data_buffer = bytes(10)
        await self.stage_inputs(self.sword(11, data=DAMAGE_8_USER_DATA), diamonds)
        await self.deliver(
            b.ItemRegistryPacket(items=[*self.registry_packet().items, b.ItemData(id=42, name="minecraft:diamond")]),
            b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=5, item=item(b)),
        )
        self.assertEqual(self.bot.preview_anvil(), AnvilPreview(1, 1))
        first = await self.submit(self.bot.take_from_container(2, 4))
        second = await self.submit(self.bot.take_from_container(2, 5))
        await self.complete(first, (PLAYER, 4, 1, 13, "", 0))
        self.assertIsNone(self.bot.preview_anvil())
        await self.complete(second, (MATERIAL, 2, 0, None, "", 0), (PLAYER, 5, 1, 14, "", 0))
        self.assertEqual(self.bot.container_slots[2].id, 0)
        self.assertEqual(self.bot.preview_anvil("New name"), AnvilPreview(1, 0))

    async def test_preview_requires_omitted_apply_material_even_with_confirmed_primary(self):
        b = self.b
        for full_response in (True, False):
            with self.subTest(full_response=full_response):
                await self.stage_inputs(self.sword(11, data=DAMAGE_8_USER_DATA), self.sword(12, data=DAMAGE_8_USER_DATA))
                self.assertEqual(self.bot.preview_anvil(), AnvilPreview(2, 1))
                applying = await self.submit(self.bot.apply_anvil(4))
                material = [(MATERIAL, 2, 0, None, "", 0)] if full_response else []
                await self.complete(applying, (INPUT, 1, 0, None, "", 0), (PLAYER, 4, 1, 13, "", 0), *material)
                self.assertEqual(self.bot.container_slots[1].id, 0)
                self.assertEqual(self.bot.container_slots[2].id, 0 if full_response else -41)
                new_input = self.sword(21, data=DAMAGE_8_USER_DATA)
                await self.deliver(b.InventorySlotPacket(inventory_id=2, slot=0, item=new_input))
                self.assertEqual(self.bot.preview_anvil("New name"), AnvilPreview(1, 0) if full_response else None)
                await self.deliver(self.ui_packet(new_input, item(b)))
                self.assertEqual(self.bot.preview_anvil("New name"), AnvilPreview(1, 0))

    async def test_preview_survives_apply_failure_and_a_newer_identical_observation(self):
        b = self.b
        primary = self.sword(11)
        await self.stage_inputs(primary, item(b))
        applying = await self.submit(self.bot.apply_anvil(4, "New name"))
        await self.complete(applying, result="INVALID_CRAFT_RESULT")
        self.assertEqual(self.bot.preview_anvil("New name"), AnvilPreview(1, 0))

        applying = await self.submit(self.bot.apply_anvil(4, "New name"))
        await self.deliver(self.ui_packet(primary, item(b)))
        await self.complete(applying, (PLAYER, 4, 1, 12, "New name", 0))
        self.assertEqual(self.bot.preview_anvil("New name"), AnvilPreview(1, 0))
