import asyncio
import unittest
from unittest.mock import patch

from endpilot import Bot, Enchantment
from endpilot.authentication import OfflineIdentity
from endpilot.compression import PacketConnection
from endpilot.containers import Containers
from endpilot.creative_inventory import CreativeInventory
from endpilot.enchanting import Enchanting
from endpilot.inventory import Inventory
from endpilot.login import JoinSession
from endpilot.player_state import PlayerState
from endpilot.protocol import Protocol, write_batch

from test_beds import Transport
from test_blocks import PacketStream
from test_container_take import item
from test_scheduled_input import Wire


PLAYER = "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"
INPUT = "ENCHANTING_INPUT_CONTAINER"
MATERIAL = "ENCHANTING_MATERIAL_CONTAINER"
NAMES = {316: "minecraft:diamond_sword", 320: "minecraft:diamond_pickaxe",
         417: "minecraft:lapis_lazuli", 386: "minecraft:book", 595: "minecraft:enchanted_book"}


class EnchantingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.protocol = Protocol(2225)
        self.b = self.protocol.binding
        self.wire = PacketStream(self.protocol)
        self.bot = Bot("EnchantmentTest", protocol=2225)
        self.bot._inventory = Inventory(self.wire, 1)
        self.bot._containers = Containers(self.wire, None, self.bot._inventory)
        creative = self.bot._creative = CreativeInventory(
            self.wire, self.bot._inventory, self.bot._containers, 1, NAMES, (),
        )
        self.bot._enchanting = Enchanting(
            self.wire, self.bot._containers, self.bot._inventory, NAMES, game_mode=lambda: creative.game_mode,
        )
        self.bot._player_state = PlayerState(self.wire, 1)
        self.addAsyncCleanup(self.bot.close)

    def emit(self, packet):
        self.wire.emit(self.protocol.encode(packet))

    def open_table(self, *, kind="ENCHANTMENT"):
        b = self.b
        self.emit(b.ContainerOpenPacket(container_id=2, type=b.ContainerType[kind],
                                        pos=b.BlockPos(x=1, y=100, z=0), entity_unique_id=-1))

    def slot(self, container, index, item_id, amount, net_id, *, metadata=b""):
        descriptor = item(self.b, item_id, amount, net_id)
        descriptor.user_data_buffer = metadata
        self.emit(self.b.InventorySlotPacket(inventory_id=container, slot=index,
                  full_container_name=None, storage_item=None, item=descriptor))

    def ui_content(self, *slots):
        self.emit(self.b.InventoryContentPacket(
            inventory_id=124, slots=slots, storage_item=item(self.b),
            full_container_name=self.b.FullContainerName(name=self.b.ContainerEnumName[INPUT], dynamic_id=None),
        ))

    def choices(self, *, empty=False, unavailable=False, net_id_offset=0):
        b = self.b
        choices = []
        if not empty:
            for cost, net_id, groups in (
                (9, 44, [[(9, 2)], [(17, 1)], [(20, 1)]]),
                (0, 0, [[], [], []]),
                (6, 91, [[], [(12, 2)], []]),
            ):
                if unavailable:
                    cost, net_id = (cost, 0) if net_id == 44 else (0, net_id)
                elif net_id:
                    net_id += net_id_offset
                choices.append(b.ItemEnchantOption(cost=cost, enchant_net_id=net_id, enchant_name=f"offer-{net_id}",
                               enchants=b.ItemEnchants(slot=14, item_enchants=[[
                                   b.EnchantmentInstance(enchant_type=kind, level=level) for kind, level in group
                               ] for group in groups])))
        self.emit(b.PlayerEnchantOptionsPacket(options=choices))

    def response(self, request, corrections=(), *, result="SUCCESS"):
        b = self.b
        return b.ItemStackResponseInfo(
            client_request_id=b.ItemStackRequestId(raw_id=request), result=b.ItemStackNetResult[result],
            containers=[b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName[name], dynamic_id=None),
                slots=[b.ItemStackResponseSlotInfo(
                    requested_slot=index, slot=index, amount=amount,
                    item_stack_net_id=None if net_id is None else b.ItemStackNetId(raw_id=net_id),
                    custom_name=b.RedactableString(unredacted_string="", redacted_string=None),
                    durability_correction=0,
                )],
            ) for name, index, amount, net_id in corrections] if result == "SUCCESS" else None,
        )

    def reply(self, request, corrections=(), *, result="SUCCESS"):
        response = self.response(request, corrections, result=result)
        self.emit(self.b.ItemStackResponsePacket(responses=[response]))

    async def submitted(self):
        for _ in range(4):
            await asyncio.sleep(0)
            if self.wire.sent:
                return self.wire.sent.pop().requests[0]
        self.fail("no item-stack request was sent")

    def fill_ui(self, *, input_id=316):
        self.open_table()
        self.slot(124, 14, input_id, 1, 11, metadata=b"old item metadata")
        self.slot(124, 15, 417, 10, 12)
        self.choices()

    def mode(self, value):
        self.emit(self.b.SetPlayerGameTypePacket(player_game_type=value))

    async def queued_selection(self):
        request_id = await self.bot._enchanting.select(0, timeout=1, queued=True)
        self.assertEqual((await self.submitted()).client_request_id.raw_id, request_id)
        return request_id

    async def test_current_offers_keep_button_order_unavailable_entry_and_three_groups(self):
        self.choices()
        self.assertIsNone(self.bot.enchant_options)
        self.open_table()
        waiting = asyncio.create_task(self.bot.wait_for_enchant_options())
        await asyncio.sleep(0)
        self.choices()
        observed = await waiting
        self.assertEqual([(p.cost, p.net_id) for p in observed], [(9, 44), (0, 0), (6, 91)])
        self.assertEqual(observed[0].slot, 14)
        self.assertEqual(observed[0].enchants, ((Enchantment(9, 2),), (Enchantment(17, 1),), (Enchantment(20, 1),)))
        self.assertEqual((observed[1].cost, observed[1].name, observed[1].net_id), (0, "offer-0", 0))
        with self.assertRaisesRegex(ValueError, "unavailable"):
            await self.bot.enchant(1)
        self.open_table()
        waiting = asyncio.create_task(self.bot.wait_for_enchant_options())
        await asyncio.sleep(0)
        self.choices(empty=True)
        self.assertEqual(await waiting, ())

    async def test_wait_for_choices_requires_observed_input_and_a_usable_button(self):
        enchanting = self.bot._enchanting
        self.choices(empty=True)
        self.open_table()
        self.slot(124, 15, 417, 10, 12)
        waiting = asyncio.create_task(enchanting.wait_for_choices(timeout=1))
        await asyncio.sleep(0)
        self.choices(empty=True)
        self.assertEqual(await enchanting.wait_for_options(timeout=0), ())
        self.choices()
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())

        self.slot(124, 14, 316, 1, 11)
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        self.choices(unavailable=True)
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        self.choices(net_id_offset=100)
        self.choices(net_id_offset=200)
        self.assertEqual([p.net_id for p in await waiting], [144, 0, 191])
        self.assertEqual([p.net_id for p in await enchanting.wait_for_choices(timeout=0)], [244, 0, 291])

    async def test_wait_for_choices_accepts_quote_after_placement_is_confirmed(self):
        enchanting = self.bot._enchanting
        self.open_table()
        self.slot(0, 0, 316, 1, 11)
        waiting = asyncio.create_task(enchanting.wait_for_choices(timeout=1))
        staging = asyncio.create_task(enchanting.transfer(0, 14, 1, into_table=True, timeout=1))
        request = await self.submitted()
        self.choices()
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        self.assertEqual(len(await enchanting.wait_for_options(timeout=0)), 3)

        self.reply(int(request.client_request_id.raw_id), [(PLAYER, 0, 0, None), (INPUT, 14, 1, 11)])
        self.assertEqual((await staging).result.name, "SUCCESS")
        self.assertEqual([p.net_id for p in await waiting], [44, 0, 91])

    async def test_wait_for_choices_does_not_return_superseded_input_choices(self):
        enchanting = self.bot._enchanting
        self.open_table()
        self.slot(124, 14, 316, 1, 11)
        waiting = asyncio.create_task(enchanting.wait_for_choices(timeout=1))
        await asyncio.sleep(0)
        self.choices()
        self.slot(124, 14, 320, 1, 21)
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        self.choices(net_id_offset=100)
        self.assertEqual([p.net_id for p in await waiting], [144, 0, 191])

    async def test_public_staging_selection_and_retrieval_keep_only_verified_metadata(self):
        self.open_table()
        self.slot(0, 0, 316, 1, 11, metadata=b"old item metadata")
        self.slot(124, 14, 0, 0, None)
        self.slot(124, 15, 417, 10, 12)
        staging = asyncio.create_task(self.bot.stage_enchanting_item(0))
        request = await self.submitted()
        self.choices()
        self.reply(int(request.client_request_id.raw_id), [(PLAYER, 0, 0, None), (INPUT, 14, 1, 11)])
        self.assertEqual((await staging).result, self.b.ItemStackNetResult.SUCCESS)
        self.assertEqual(self.bot.enchanting_slots[14].user_data_buffer, b"old item metadata")

        selection = asyncio.create_task(self.bot.enchant(2))
        request = await self.submitted()
        request_id = int(request.client_request_id.raw_id)
        recipe, target, lapis, put_back = request.actions
        self.assertEqual(int(recipe.recipe_net_id), 91)
        self.assertEqual((target.amount, target.source.slot, lapis.amount, lapis.source.slot), (1, 14, 3, 15))
        self.assertEqual((put_back.source.slot, put_back.source.net_id_variant, put_back.destination.slot),
                         (50, request_id, 14))
        self.reply(request_id, [(INPUT, 14, 1, 20), (MATERIAL, 15, 7, 12)])
        self.assertEqual((await selection).result, self.b.ItemStackNetResult.SUCCESS)
        self.assertIsNone(self.bot.enchant_options)
        self.assertEqual(self.bot.enchanting_slots[14].user_data_buffer, b"")
        self.assertEqual(self.bot.enchanting_slots[15].stack_size, 7)

        taking = asyncio.create_task(self.bot.take_enchanting_item(14, 0))
        request = await self.submitted()
        self.assertEqual(request.actions[0].source.net_id_variant, 20)
        self.reply(int(request.client_request_id.raw_id), [(INPUT, 14, 0, None), (PLAYER, 0, 1, 20)])
        self.assertEqual((await taking).result, self.b.ItemStackNetResult.SUCCESS)
        self.assertEqual(self.bot.inventory_slots[0].id, 316)
        self.assertEqual(self.bot.inventory_slots[0].user_data_buffer, b"")

    async def test_creative_select_third_button_with_or_without_staged_lapis(self):
        b = self.b
        for lapis in (False, True):
            with self.subTest(lapis=lapis):
                self.mode(b.GameType.CREATIVE)
                self.fill_ui()
                if not lapis:
                    self.slot(124, 15, 0, 0, None)
                level = b.AttributeData(name="minecraft:player.level", current_value=1)
                self.emit(b.UpdateAttributesPacket(runtime_id=1, attribute_data=[level], tick=1))
                selection = asyncio.create_task(self.bot.enchant(2))
                request = await self.submitted()
                craft, consume, place = request.actions
                request_id = int(request.client_request_id.raw_id)
                self.assertEqual((craft.recipe_net_id, craft.num_crafts), (91, 1))
                self.assertEqual((consume.amount, consume.source.slot, consume.source.net_id_variant), (1, 14, 11))
                self.assertEqual((place.source.slot, place.source.net_id_variant, place.destination.slot),
                                 (50, request_id, 14))
                if lapis:
                    self.mode(b.GameType.SURVIVAL)
                self.reply(request_id, [(INPUT, 14, 1, 20)])
                self.assertEqual((await selection).result.name, "SUCCESS")
                self.assertEqual(self.bot.enchanting_slots[14].net_id_variant, 20)
                self.assertIsNone(self.bot.enchant_options)
                self.assertEqual(self.bot.experience_level, 1)
                if lapis:
                    self.assertEqual(self.bot.enchanting_slots[15].stack_size, 10)
                else:
                    self.assertNotIn(15, self.bot.enchanting_slots)

    async def test_unknown_mode_still_requires_lapis(self):
        self.fill_ui()
        self.slot(124, 15, 0, 0, None)
        self.assertIsNone(self.bot.game_mode)
        with self.assertRaisesRegex(ValueError, "insufficient observed enchanting material"):
            await self.bot.enchant(2)
        self.assertEqual(self.wire.sent, [])

    async def test_selection_checks_live_mode_window_choices_and_inputs_at_write(self):
        b = self.b
        creative, survival = b.GameType.CREATIVE, b.GameType.SURVIVAL
        scenarios = (
            (creative, lambda: self.mode(survival), "game mode changed"),
            (survival, lambda: self.mode(creative), "game mode changed"),
            (creative, lambda: self.mode(b.GameType.UNDEFINED), "game mode changed"),
            (creative, self.open_table, "enchanting table window changed"),
            (creative, lambda: self.choices(net_id_offset=100), "enchant options changed"),
            (survival, lambda: self.slot(124, 15, 417, 2, 12), "enchanting inputs changed"),
        )
        for mode, change, message in scenarios:
            with self.subTest(mode=mode.name, message=message):
                self.mode(mode)
                self.fill_ui()
                started = asyncio.get_running_loop().create_future()
                release = asyncio.Event()
                send = self.wire.send

                async def wait_to_write(packet, *, _before_send=None):
                    started.set_result(packet.requests[0])
                    await release.wait()
                    await send(packet, _before_send=_before_send)

                with patch.object(self.wire, "send", wait_to_write):
                    task = asyncio.create_task(self.bot.enchant(2, timeout=1))
                    try:
                        request = await asyncio.wait_for(started, 1)
                        self.reply(int(request.client_request_id.raw_id), [(INPUT, 14, 1, 20)])
                        self.assertFalse(task.done())
                        self.assertEqual(self.bot.enchanting_slots[14].net_id_variant, 11)
                        change()
                        release.set()
                        with self.assertRaisesRegex(RuntimeError, message):
                            await task
                        self.assertEqual(self.wire.sent, [])
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    async def test_rejected_enchant_can_retry_and_authoritative_replacement_wins(self):
        self.fill_ui()
        first = await self.queued_selection()
        self.assertIsNone(self.bot._inventory._qa_response(first))
        self.reply(first, result="FAILED_TO_ENCHANT")
        self.assertEqual(self.bot._inventory._qa_response(first).result.name, "FAILED_TO_ENCHANT")
        self.assertEqual(self.bot.enchanting_slots[15].stack_size, 10)
        self.assertEqual(len(self.bot.enchant_options), 3)
        second = await self.queued_selection()
        self.reply(second, [(INPUT, 14, 1, 20), (MATERIAL, 15, 9, 12)])
        self.slot(124, 14, 320, 1, 20, metadata=b"server replacement")
        result = self.bot.enchanting_slots[14]
        self.assertEqual((result.id, result.user_data_buffer), (320, b"server replacement"))
        self.assertEqual(self.bot._inventory._qa_response(second).result.name, "SUCCESS")

    async def test_plain_book_uses_enchanted_book_type_without_presuming_enchants(self):
        self.fill_ui(input_id=386)
        request = await self.queued_selection()
        self.reply(request, [(INPUT, 14, 1, 20)])
        result = self.bot.enchanting_slots[14]
        self.assertEqual((result.id, result.user_data_buffer), (595, b""))

    async def test_old_window_verdict_cannot_change_reopened_same_id_slots(self):
        self.fill_ui()
        first = await self.queued_selection()
        self.open_table()
        self.assertIsNone(self.bot.enchant_options)
        self.slot(124, 14, 320, 1, 22, metadata=b"new window")
        self.choices()
        self.reply(first, [(INPUT, 14, 1, 20)])
        self.assertEqual(self.bot._inventory._qa_response(first).result.name, "SUCCESS")
        new_item = self.bot.enchanting_slots[14]
        self.assertEqual((new_item.id, new_item.net_id_variant, new_item.user_data_buffer), (320, 22, b"new window"))
        self.assertEqual(len(self.bot.enchant_options), 3)
        self.open_table(kind="CONTAINER")
        self.assertIsNone(self.bot.enchant_options)
        self.assertEqual(self.bot.enchanting_slots, {})

    async def test_full_empty_ui_snapshot_clears_both_slots_and_precedes_pending_verdict(self):
        self.fill_ui()
        pending = await self.bot._enchanting.transfer(
            1, 15, 1, into_table=False, timeout=1, queued=True,
        )
        self.ui_content()
        self.assertEqual(self.bot.enchanting_slots, {})
        self.assertIsNone(self.bot.enchant_options)
        self.reply(pending, [(MATERIAL, 15, 9, 13)])
        self.assertEqual(self.bot._inventory._qa_response(pending).result.name, "SUCCESS")
        self.assertEqual(self.bot.enchanting_slots, {})
        self.assertIsNone(self.bot.enchant_options)

    async def test_rejected_take_keeps_quote_and_new_input_net_id_invalidates_it(self):
        self.fill_ui()
        offers = self.bot.enchant_options
        pending = await self.bot._enchanting.transfer(
            0, 14, 1, into_table=False, timeout=1, queued=True,
        )
        self.assertIs(self.bot.enchant_options, offers)
        self.reply(pending, result="ERROR")
        self.assertEqual(self.bot._inventory._qa_response(pending).result.name, "ERROR")
        self.assertIs(self.bot.enchant_options, offers)

        self.slot(124, 14, 316, 1, 11, metadata=b"old item metadata")
        self.assertIs(self.bot.enchant_options, offers)
        self.slot(124, 14, 316, 1, 21, metadata=b"old item metadata")
        self.assertIsNone(self.bot.enchant_options)
        self.choices()
        pending = await self.bot._enchanting.transfer(
            0, 14, 1, into_table=False, timeout=1, queued=True,
        )
        self.choices()
        self.reply(pending, [(INPUT, 14, 0, None)])
        self.assertIsNone(self.bot.enchant_options)

    async def test_material_verdicts_follow_packet_order_and_preserve_external_slot(self):
        self.fill_ui()
        first = await self.bot._enchanting.transfer(
            1, 15, 1, into_table=False, timeout=1, queued=True,
        )
        second = await self.bot._enchanting.transfer(
            2, 15, 1, into_table=False, timeout=1, queued=True,
        )
        self.emit(self.b.ItemStackResponsePacket(responses=[
            self.response(second, [(MATERIAL, 15, 9, 13)]),
            self.response(first, [(MATERIAL, 15, 8, 14)]),
        ]))
        lapis = self.bot.enchanting_slots[15]
        self.assertEqual((lapis.id, lapis.stack_size, lapis.net_id_variant), (417, 8, 14))
        replies = [self.bot._inventory._qa_response(request) for request in (first, second)]
        self.assertEqual([reply.containers[0].slots[0].amount for reply in replies], [8, 9])

        pending = await self.bot._enchanting.transfer(
            3, 15, 1, into_table=False, timeout=1, queued=True,
        )
        self.slot(124, 15, 417, 4, 40, metadata=b"fresh material")
        self.reply(pending, [(MATERIAL, 15, 7, 15)])
        self.assertEqual(self.bot._inventory._qa_response(pending).containers[0].slots[0].amount, 7)
        lapis = self.bot.enchanting_slots[15]
        self.assertEqual((lapis.id, lapis.stack_size, lapis.net_id_variant, lapis.user_data_buffer),
                         (417, 4, 40, b"fresh material"))

    async def test_material_retransmission_preserves_pending_verdict(self):
        for packet in ("slot", "content"):
            with self.subTest(packet=packet):
                self.fill_ui()
                request = await self.bot._enchanting.transfer(
                    1, 15, 1, into_table=False, timeout=1, queued=True,
                )
                if packet == "slot":
                    self.slot(124, 15, 417, 10, 12)
                else:
                    sword = item(self.b, 316, 1, 11)
                    sword.user_data_buffer = b"old item metadata"
                    self.ui_content(*([item(self.b)] * 14), sword, item(self.b, 417, 10, 12))
                self.reply(request, [(MATERIAL, 15, 9, 12)])
                self.assertEqual(self.bot._inventory._qa_response(request).containers[0].slots[0].amount, 9)
                lapis = self.bot.enchanting_slots[15]
                self.assertEqual((lapis.id, lapis.stack_size, lapis.net_id_variant), (417, 9, 12))

    async def test_changed_material_under_same_net_id_starts_a_new_stack(self):
        for item_id, data in ((320, b""), (417, b"replacement metadata")):
            with self.subTest(item_id=item_id, data=data):
                self.fill_ui()
                request = await self.bot._enchanting.transfer(
                    1, 15, 1, into_table=False, timeout=1, queued=True,
                )
                self.slot(124, 15, item_id, 4, 12, metadata=data)
                self.reply(request, [(MATERIAL, 15, 9, 12)])
                observed = self.bot.enchanting_slots[15]
                self.assertEqual((observed.id, observed.stack_size, observed.net_id_variant, observed.user_data_buffer),
                                 (item_id, 4, 12, data))

    async def test_retransmitted_input_is_not_the_enchanted_result(self):
        self.fill_ui()
        request = await self.queued_selection()
        self.slot(124, 14, 316, 1, 11, metadata=b"old item metadata")
        self.reply(request, [(MATERIAL, 15, 9, 12)])
        self.assertEqual(self.bot._inventory._qa_response(request).result.name, "SUCCESS")
        self.assertNotIn(14, self.bot.enchanting_slots)

    async def test_disconnect_ends_wait_and_unconfirmed_enchant(self):
        self.fill_ui()
        request = await self.queued_selection()
        self.open_table()
        waiting = asyncio.create_task(self.bot.wait_for_enchant_options())
        await asyncio.sleep(0)
        self.emit(self.b.DisconnectPacket())
        with self.assertRaises(ConnectionError):
            await waiting
        with self.assertRaises(ConnectionError):
            self.bot._inventory._qa_response(request)
        self.assertIsNone(self.bot.enchant_options)

    def test_experience_reports_only_own_player_attribute(self):
        attr = self.b.AttributeData(name="minecraft:player.level", current_value=30)
        self.assertIsNone(self.bot.experience_level)
        self.emit(self.b.UpdateAttributesPacket(runtime_id=3, attribute_data=[attr], tick=1))
        self.assertIsNone(self.bot.experience_level)
        self.emit(self.b.UpdateAttributesPacket(runtime_id=1, attribute_data=[attr], tick=2))
        self.assertEqual(self.bot.experience_level, 30)
        attr.current_value = 28
        self.emit(self.b.UpdateAttributesPacket(runtime_id=1, attribute_data=[attr], tick=3))
        self.assertEqual(self.bot.experience_level, 28)

    async def test_join_replays_local_experience_health_and_hunger_in_wire_order(self):
        b = self.b
        bot = Bot("EarlyAttributes", protocol=2225)
        self.addAsyncCleanup(bot.close)
        transport = Transport()

        def attrs(runtime_id, tick, **values):
            return b.UpdateAttributesPacket(
                runtime_id=runtime_id, tick=tick,
                attribute_data=[b.AttributeData(name=f"minecraft:{name}", current_value=value)
                                for name, value in values.items()],
            )

        def deliver(*packets):
            transport.incoming.put_nowait(write_batch(map(bot.protocol.encode, packets)))

        deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        deliver(
            attrs(42, 1, **{"player.level": 99, "player.hunger": 1}),
            b.StartGamePacket(runtime_id=42, entity_id=7, block_network_ids_are_hashes=True),
            attrs(42, 2, **{"health": 8, "player.level": 30, "player.hunger": 17}),
            b.SetHealthPacket(health=16),
            attrs(42, 3, **{"player.level": 28}),
            attrs(43, 4, **{"health": 2, "player.level": 5, "player.hunger": 1}),
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        )
        with patch("endpilot.transport.nethernet.connect", return_value=transport):
            await bot.join_nethernet("http://127.0.0.1:19132")
        self.assertEqual((bot.health, bot.hunger, bot.experience_level), (16, 17, 28))
        deliver(attrs(42, 5, **{"player.hunger": 15, "player.level": 27}))
        await asyncio.sleep(0)
        self.assertEqual((bot.health, bot.hunger, bot.experience_level), (16, 15, 27))


class JoinEnchantSeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_join_session_snapshots_native_signed_seed_across_connections(self):
        protocol = Protocol(2225)
        b = protocol.binding
        identity = OfflineIdentity("EnchantmentSeedTest")
        address = "http://127.0.0.1:19132"
        settings = b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE)
        unjoined = JoinSession(PacketConnection(Transport(), protocol, settings), identity, address)
        self.assertIsNone(unjoined.enchantment_seed_at_join)
        sessions = []
        for seed in (-2147483648, 0, 2147483647):
            with self.subTest(seed=seed):
                transport = Transport()
                for packet in (
                    settings,
                    b.StartGamePacket(runtime_id=42, entity_id=7, enchantment_seed=seed),
                    b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
                ):
                    transport.incoming.put_nowait(write_batch([protocol.encode(packet)]))
                with patch("endpilot.transport.nethernet.connect", return_value=transport):
                    session = await JoinSession.connect_nethernet(address, identity, protocol)
                async with asyncio.timeout(1):
                    await session.close()
                self.assertEqual(session.enchantment_seed_at_join, seed)
                sessions.append(session)

        self.assertEqual([session.enchantment_seed_at_join for session in sessions], [-2147483648, 0, 2147483647])
        with self.assertRaises(AttributeError):
            sessions[-1].enchantment_seed_at_join = 17


class BotEnchantingObservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Bot("EnchantmentObserver", protocol=2225)
        self.b = self.bot.protocol.binding
        self.addAsyncCleanup(self.bot.close)

    async def connect(self, seed=0):
        b = self.b
        wire = Wire(self.bot.protocol)
        wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        wire.push(b.StartGamePacket(runtime_id=42, entity_id=7, enchantment_seed=seed))
        wire.push(b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)
        return wire

    async def test_public_seed_is_only_available_for_the_active_join(self):
        self.assertIsNone(self.bot.enchantment_seed_at_join)
        await self.connect(seed=-37)
        self.assertEqual(self.bot.enchantment_seed_at_join, -37)
        await self.bot.close()
        self.assertIsNone(self.bot.enchantment_seed_at_join)

        wire = await self.connect(seed=0)
        self.assertEqual(self.bot.enchantment_seed_at_join, 0)
        wire.push(self.b.DisconnectPacket())
        await self.bot.wait_for_session_end(timeout=1)
        self.assertIsNone(self.bot.enchantment_seed_at_join)

    async def test_public_choices_wait_follows_the_observed_window(self):
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            await self.bot.wait_for_enchant_choices(timeout=0)
        wire = await self.connect()
        with self.assertRaisesRegex(RuntimeError, "no enchanting table is open"):
            await self.bot.wait_for_enchant_choices(timeout=0)
        b = self.b

        async def deliver(*packets):
            batch = [self.bot.protocol.encode(packet) for packet in packets]
            wire.incoming.put_nowait(write_batch(batch))
            last = batch[-1]
            await self.bot.packets.wait_for_packet(last.packet_id, match=lambda raw: raw == last, timeout=1)

        def quote(net_id):
            return b.PlayerEnchantOptionsPacket(options=[b.ItemEnchantOption(
                cost=5, enchant_net_id=net_id, enchant_name="offer",
                enchants=b.ItemEnchants(slot=16, item_enchants=[[], [], []]),
            )])

        window = b.ContainerOpenPacket(container_id=2, type=b.ContainerType.ENCHANTMENT,
                                       pos=b.BlockPos(x=1, y=100, z=0), entity_unique_id=-1)
        observed_input = b.InventorySlotPacket(inventory_id=124, slot=14, item=item(b, 316, 1, 11))
        empty = b.PlayerEnchantOptionsPacket(options=[])
        await deliver(window, observed_input, empty)
        self.assertEqual(await self.bot.wait_for_enchant_options(timeout=0), ())
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_enchant_choices(timeout=0)

        waiting = asyncio.create_task(self.bot.wait_for_enchant_choices(timeout=1))
        cancelled = asyncio.create_task(self.bot.wait_for_enchant_choices(timeout=1))
        await asyncio.sleep(0)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        await deliver(quote(44))
        self.assertEqual([option.net_id for option in await waiting], [44])

        await deliver(empty)
        waiting = asyncio.create_task(self.bot.wait_for_enchant_choices(timeout=1))
        await asyncio.sleep(0)
        replacement = b.InventorySlotPacket(inventory_id=124, slot=14, item=item(b, 320, 1, 21))
        await deliver(quote(41), replacement, quote(52), quote(53))
        self.assertEqual([option.net_id for option in await waiting], [52])
        self.assertEqual([option.net_id for option in await self.bot.wait_for_enchant_options(timeout=0)], [53])

        await deliver(empty)
        waiting = asyncio.create_task(self.bot.wait_for_enchant_choices(timeout=1))
        await asyncio.sleep(0)
        await deliver(window, observed_input, quote(91))
        with self.assertRaisesRegex(RuntimeError, "window changed"):
            await waiting
        self.assertEqual([option.net_id for option in await self.bot.wait_for_enchant_choices(timeout=0)], [91])

        await deliver(empty)
        waiting = asyncio.create_task(self.bot.wait_for_enchant_choices(timeout=1))
        await asyncio.sleep(0)
        wire.push(b.DisconnectPacket())
        with self.assertRaises(ConnectionError):
            await waiting
