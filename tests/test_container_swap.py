import asyncio
import unittest

from endpilot import RecipeItemAmount, transfer_container_range, transfer_inventory_range, transfer_item, transfer_range
from endpilot.harvest import equip_harvest_tool_from_chest

import test_range_transfer as range_tests


CHEST = range_tests.CHEST
PLAYER = range_tests.PLAYER
PAPER = range_tests.PAPER
STICK = range_tests.STICK
DIAMOND = range_tests.DIAMOND


class ContainerSwapTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = range_tests.RangeTransferTests.asyncSetUp
    observe_block = range_tests.RangeTransferTests.observe_block
    emit = range_tests.RangeTransferTests.emit
    stack = range_tests.RangeTransferTests.stack
    open_chest = range_tests.RangeTransferTests.open_chest
    open_window = range_tests.RangeTransferTests.open_window
    content = range_tests.RangeTransferTests.content
    set_slot = range_tests.RangeTransferTests.set_slot
    player_slot = range_tests.RangeTransferTests.player_slot
    reply = range_tests.RangeTransferTests.reply
    outgoing = range_tests.RangeTransferTests.outgoing
    hold_requests = range_tests.RangeTransferTests.hold_requests
    start = range_tests.RangeTransferTests.start

    def occupied_chest(self, *, width=27):
        self.open_chest(self.stack(PAPER, 5, 31), self.stack(STICK, 7, 39),
                        self.stack(DIAMOND, 2, 44), width=width)

    @staticmethod
    def descriptor(item):
        return item.id, item.stack_size, item.net_id_variant, bytes(item.user_data_buffer)

    def assert_action(self, request, source, destination):
        action, = request.actions
        self.assertIsInstance(action, self.b.ItemStackRequestCereal.SwapActionData)
        for operand, wanted in ((action.source, source), (action.destination, destination)):
            address = operand.full_container_name
            self.assertEqual((address.name.name, operand.slot, int(operand.net_id_variant)), wanted)
            self.assertIsNone(address.dynamic_id)

    async def test_box_swap_commits_both_slots_and_keeps_whole_descriptors(self):
        self.open_chest(self.stack(PAPER, 5, 31, user_data=b"paper"),
                        self.stack(STICK, 7, 39, user_data=b"stick"), self.stack(DIAMOND, 2, 44))
        pending = self.hold_requests()
        original = {slot: self.descriptor(item) for slot, item in self.bot.container_slots.items()}
        events = []
        self.bot.subscribe_container_slot_changes(lambda change: events.append(
            (change.slot, tuple(self.descriptor(self.bot.container_slots[i]) for i in (0, 1))),
        ))
        swap = self.start(self.bot.swap_container_items(0, 1))
        request = await asyncio.wait_for(pending.get(), 1)
        self.assert_action(request, (CHEST, 0, 31), (CHEST, 1, 39))
        self.reply(request, [(CHEST, 0, 7, 39), (CHEST, 1, 5, 31)])
        verdict = await swap
        self.assertIsInstance(verdict, self.b.ItemStackResponseInfo)
        self.assertEqual((verdict.client_request_id.raw_id, verdict.result.name), (request.client_request_id.raw_id, "SUCCESS"))
        expected = dict(original)
        expected[0], expected[1] = original[1], original[0]
        self.assertEqual({slot: self.descriptor(item) for slot, item in self.bot.container_slots.items()}, expected)
        self.assertEqual(events, [(0, (expected[0], expected[1])), (1, (expected[0], expected[1]))])
        self.assertEqual(len(self.outgoing()), 1)

    async def test_mixed_same_number_swap_uses_distinct_domains_and_observer_views(self):
        self.occupied_chest()
        self.player_slot(0, self.stack(DIAMOND, 9, 52))
        pending = self.hold_requests()
        original = tuple(self.descriptor(items[0]) for items in (self.bot.container_slots, self.bot.inventory_slots))
        events = []

        def record(area):
            events.append((area, tuple(self.descriptor(items[0])
                                       for items in (self.bot.container_slots, self.bot.inventory_slots))))

        self.bot.subscribe_container_slot_changes(lambda _: record("container"))
        self.bot.subscribe_inventory_changes(lambda _: record("inventory"))
        swap = self.start(self.bot.swap_container_items(0, 0, other_area="inventory"))
        request = await asyncio.wait_for(pending.get(), 1)
        self.assert_action(request, (CHEST, 0, 31), (PLAYER, 0, 52))
        self.reply(request, [(PLAYER, 0, 5, 31), (CHEST, 0, 9, 52)])
        self.assertEqual((await swap).result.name, "SUCCESS")
        self.assertEqual(events, [("container", original[::-1]), ("inventory", original[::-1])])
        self.assertEqual(self.descriptor(self.bot.container_slots[1]), (STICK, 7, 39, b""))
        self.assertEqual(len(self.outgoing()), 1)

    async def test_invalid_and_unobserved_slots_fail_before_sending(self):
        self.occupied_chest()
        for first, second, area in ((0, 0, "container"), (0, 1, "chest"), (27, 1, "container"),
                                    (0, 27, "container"), (0, 36, "inventory"), (True, 1, "container")):
            with self.subTest(first=first, second=second, area=area), self.assertRaises(ValueError):
                await self.bot.swap_container_items(first, second, other_area=area)
        for item in (self.stack(STICK, 7), self.stack(), self.stack(STICK, 7, 31)):
            self.set_slot(2, 1, item)
            with self.assertRaisesRegex(RuntimeError, "stack network IDs"):
                await self.bot.swap_container_items(0, 1)
        self.set_slot(2, 1, self.stack(PAPER, 7, 52))
        with self.assertRaisesRegex(ValueError, "different item types"):
            await self.bot.swap_container_items(0, 1)
        with self.assertRaisesRegex(RuntimeError, "observed occupied"):
            await self.bot.swap_container_items(0, 1, other_area="inventory")
        self.assertEqual(self.outgoing(), [])

    async def test_rejects_other_block_and_incomplete_or_double_window(self):
        for block, width in (("minecraft:chest", None), ("minecraft:chest", 26), ("minecraft:chest", 54),
                             ("minecraft:barrel", 27), ("minecraft:trapped_chest", 27)):
            with self.subTest(block=block, width=width):
                self.observe_block(block)
                self.occupied_chest(width=width)
                with self.assertRaisesRegex(RuntimeError, "ordinary single chest"):
                    await self.bot.swap_container_items(0, 1)
        self.assertEqual(self.outgoing(), [])

    async def test_before_write_checks_window_generation_and_fresh_slot(self):
        original = self.stream.send
        for area, change, message in (
            ("container", lambda: self.set_slot(2, 1, self.stack(STICK, 6, 48)), "slots changed"),
            ("inventory", lambda: self.player_slot(1, self.stack(STICK, 2, 61)), "slots changed"),
            ("container", self.occupied_chest, "window changed"),
            ("container", lambda: (self.observe_block("minecraft:air"), self.observe_block("minecraft:chest")),
             "window changed"),
        ):
            with self.subTest(area=area, message=message):
                self.occupied_chest()
                self.player_slot(1, self.stack(STICK, 5, 60))
                entered, release = asyncio.Event(), asyncio.Event()

                async def gated(packet, **kwargs):
                    if isinstance(packet, self.b.ItemStackRequestPacket):
                        entered.set()
                        await release.wait()
                    await original(packet, **kwargs)

                self.stream.send = gated
                swapping = self.start(self.bot.swap_container_items(0, 1, other_area=area))
                await asyncio.wait_for(entered.wait(), 1)
                change()
                release.set()
                with self.assertRaisesRegex(RuntimeError, message):
                    await swapping
        self.assertEqual(self.outgoing(), [])

    async def test_one_sided_or_wrong_identity_success_keeps_history_and_blocks_reuse(self):
        pending = self.hold_requests()
        for verdict, corrections in (
            ("ERROR", []),
            ("SUCCESS", [(CHEST, 0, 7, 39)]),
            ("SUCCESS", [(CHEST, 0, 7, 39), (CHEST, 1, 5, 39)]),
            ("SUCCESS", [(CHEST, 0, 7, 39), (CHEST, 1, 5, 31), (CHEST, 1, 5, 31)]),
        ):
            with self.subTest(verdict=verdict, corrections=corrections):
                self.occupied_chest()
                events = []
                self.bot.subscribe_container_slot_changes(events.append)
                swap = self.start(self.bot.swap_container_items(0, 1))
                request = await asyncio.wait_for(pending.get(), 1)
                self.reply(request, corrections, verdict=verdict)
                self.assertEqual((await swap).result.name, verdict)
                self.assertEqual(self.descriptor(self.bot.container_slots[0]), (PAPER, 5, 31, b""))
                self.assertEqual(self.descriptor(self.bot.container_slots[1]), (STICK, 7, 39, b""))
                self.assertEqual(events, [])
                self.set_slot(2, 0, self.stack(PAPER, 5, 33))
                with self.assertRaisesRegex(RuntimeError, "observed occupied"):
                    await self.bot.swap_container_items(0, 1)
        self.assertEqual(len(self.outgoing()), 4)

        self.set_slot(2, 1, self.stack(STICK, 7, 41))
        swap = self.start(self.bot.swap_container_items(0, 1))
        request = await asyncio.wait_for(pending.get(), 1)
        self.assert_action(request, (CHEST, 0, 33), (CHEST, 1, 41))
        self.reply(request, [(CHEST, 0, 7, 41), (CHEST, 1, 5, 33)])
        self.assertEqual((await swap).result.name, "SUCCESS")
        self.assertEqual(self.descriptor(self.bot.container_slots[0]), (STICK, 7, 41, b""))

    async def test_inflight_mixed_exchange_blocks_overlaps_and_preserves_newer_values(self):
        self.occupied_chest()
        self.player_slot(0, self.stack(DIAMOND, 9, 52))
        self.player_slot(1, self.stack())
        self.player_slot(2, self.stack(STICK, 5, 60))
        pending = self.hold_requests()
        swap = self.start(self.bot.swap_container_items(0, 0, other_area="inventory"))
        request = await asyncio.wait_for(pending.get(), 1)
        with self.assertRaisesRegex(RuntimeError, "unsettled"):
            await self.bot.swap_container_items(0, 1)
        with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
            await self.bot.move_inventory_item(0, 1)
        self.assertEqual(len(self.outgoing()), 1)
        ordinary = self.start(self.bot.move_inventory_item(2, 1))
        move = await asyncio.wait_for(pending.get(), 1)
        self.reply(move, [(PLAYER, 2, 4, 60), (PLAYER, 1, 1, 63)])
        self.assertEqual((await ordinary).result.name, "SUCCESS")

        self.player_slot(0, self.stack(DIAMOND, 6, 62))
        self.reply(request, [(CHEST, 0, 9, 52), (PLAYER, 0, 5, 31)])
        self.assertEqual((await swap).result.name, "SUCCESS")
        self.assertEqual(self.descriptor(self.bot.inventory_slots[0]), (DIAMOND, 6, 62, b""))
        self.assertEqual(self.descriptor(self.bot.container_slots[0]), (PAPER, 5, 31, b""))

    async def test_newer_matching_observation_is_retained_on_dual_receipt(self):
        self.occupied_chest()
        self.player_slot(0, self.stack(DIAMOND, 9, 52))
        pending = self.hold_requests()
        swap = self.start(self.bot.swap_container_items(0, 0, other_area="inventory"))
        request = await asyncio.wait_for(pending.get(), 1)
        self.player_slot(0, self.stack(PAPER, 5, 31))
        received = self.bot.inventory_slots[0]
        self.reply(request, [(CHEST, 0, 9, 52), (PLAYER, 0, 5, 31)])
        self.assertEqual((await swap).result.name, "SUCCESS")
        self.assertIs(self.bot.inventory_slots[0], received)
        self.assertEqual(self.descriptor(self.bot.container_slots[0]), (DIAMOND, 9, 52, b""))

    async def test_box_swap_and_explicit_armor_equip_only_lock_overlapping_endpoints(self):
        armor = "ARMOR_CONTAINER"
        helmet = 1236
        pending = self.hold_requests()
        for box_first in (True, False):
            with self.subTest(box_first=box_first):
                chest_item, player_item = (helmet, PAPER) if box_first else (PAPER, helmet)
                self.open_chest(self.stack(chest_item, 1, 31))
                self.player_slot(12, self.stack(player_item, 1, 52))
                self.player_slot(13, self.stack(helmet, 1, 53))
                self.set_slot(self.b.ContainerID.ARMOR, 0, self.stack())
                self.set_slot(self.b.ContainerID.ARMOR, 1, self.stack())

                if box_first:
                    exchange = self.start(self.bot.swap_container_items(0, 12, other_area="inventory"))
                    swap_request = await asyncio.wait_for(pending.get(), 1)
                    self.set_slot(2, 0, self.stack(PAPER, 1, 52))
                    self.player_slot(12, self.stack(helmet, 1, 31))
                    with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
                        await self.bot.equip_armor(12, 1)
                    self.assertTrue(pending.empty())
                    equip = self.start(self.bot.equip_armor(13, 0))
                    equip_request = await asyncio.wait_for(pending.get(), 1)
                    equipped_source = 13
                    swapped_player = 12
                    swap_receipts = [(CHEST, 0, 1, 52), (PLAYER, swapped_player, 1, 31)]
                else:
                    equip = self.start(self.bot.equip_armor(12, 0))
                    equip_request = await asyncio.wait_for(pending.get(), 1)
                    with self.assertRaisesRegex(RuntimeError, "unsettled item-stack request"):
                        await self.bot.swap_container_items(0, 12, other_area="inventory")
                    self.assertTrue(pending.empty())
                    swapped_player = 13
                    exchange = self.start(self.bot.swap_container_items(0, swapped_player, other_area="inventory"))
                    swap_request = await asyncio.wait_for(pending.get(), 1)
                    equipped_source = 12
                    swap_receipts = [(CHEST, 0, 1, 53), (PLAYER, swapped_player, 1, 31)]

                action, = equip_request.actions
                self.assertEqual((action.source.slot, action.destination.full_container_name.name.name,
                                  action.destination.slot), (equipped_source, armor, 0))
                self.reply(equip_request, [(PLAYER, equipped_source, 0, None), (armor, 0, 1, 61)])
                self.assertEqual((await equip).result.name, "SUCCESS")
                self.assertEqual(self.bot.armor_slots[0].id, helmet)
                self.assertFalse(exchange.done())
                self.reply(swap_request, swap_receipts)
                self.assertEqual((await exchange).result.name, "SUCCESS")
                self.assertEqual(self.bot.inventory_slots[swapped_player].id, chest_item)

    async def test_late_mixed_receipt_does_not_write_to_same_number_reopened_window(self):
        self.occupied_chest()
        self.player_slot(0, self.stack(DIAMOND, 9, 52))
        pending = self.hold_requests()
        swap = self.start(self.bot.swap_container_items(0, 0, other_area="inventory"))
        request = await asyncio.wait_for(pending.get(), 1)
        old = self.bot.open_window
        self.open_chest(self.stack(STICK, 4, 80), self.stack(DIAMOND, 2, 81))
        self.assertEqual(self.bot.open_window.container_id, old.container_id)
        self.assertIsNot(self.bot.open_window, old)
        self.player_slot(0, self.stack(PAPER, 3, 83))
        self.reply(request, [(CHEST, 0, 9, 52), (PLAYER, 0, 5, 31)])
        self.assertEqual((await swap).result.name, "SUCCESS")
        self.assertEqual(self.descriptor(self.bot.container_slots[0]), (STICK, 4, 80, b""))
        self.assertEqual(self.descriptor(self.bot.inventory_slots[0]), (PAPER, 3, 83, b""))

    async def test_close_submission_expires_swap_authority_before_acknowledgment(self):
        self.occupied_chest()
        pending = self.hold_requests()
        swap = self.start(self.bot.swap_container_items(0, 1))
        request = await asyncio.wait_for(pending.get(), 1)
        window = self.bot.open_window
        close = self.start(self.bot.close_container())
        async with asyncio.timeout(1):
            while not any(isinstance(packet, self.b.ContainerClosePacket) for packet in self.stream.sent):
                await asyncio.sleep(0)
        self.assertIs(self.bot.open_window, window)
        self.reply(request, [(CHEST, 0, 7, 39), (CHEST, 1, 5, 31)])
        self.assertEqual((await swap).result.name, "SUCCESS")
        self.assertEqual(self.descriptor(self.bot.container_slots[0]), (PAPER, 5, 31, b""))
        with self.assertRaisesRegex(RuntimeError, "ordinary single chest"):
            await self.bot.swap_container_items(0, 1)
        self.assertEqual(len(self.outgoing()), 1)
        self.emit(self.b.ContainerClosePacket(container_id=window.container_id,
                                              container_type=self.b.ContainerType.CONTAINER))
        self.assertIs(await close, window)

    async def test_incomplete_mixed_receipt_blocks_each_normal_write_until_its_slot_refreshes(self):
        self.occupied_chest()
        self.player_slot(0, self.stack(DIAMOND, 9, 52))
        self.player_slot(1, self.stack())
        self.player_slot(3, self.stack(STICK, 2, 55))
        pending = self.hold_requests()
        swap = self.start(self.bot.swap_container_items(0, 0, other_area="inventory"))
        request = await asyncio.wait_for(pending.get(), 1)
        queued_take = self.start(self.bot.take_from_container(0, 1, timeout=0.25))
        await asyncio.sleep(0)
        self.reply(request, [(CHEST, 0, 9, 52)])
        self.assertEqual((await swap).result.name, "SUCCESS")
        with self.assertRaisesRegex(RuntimeError, "fresh server observation"):
            await queued_take

        for operation in (
            lambda: self.bot.take_from_container(0, 1, timeout=0.25),
            lambda: self.bot.put_into_container(3, 0, timeout=0.25),
            lambda: self.bot.take_from_container(1, 0, timeout=0.25),
            lambda: self.bot.move_inventory_item(0, 1, timeout=0.25),
            lambda: self.bot.swap_inventory_items(0, 3, timeout=0.25),
            lambda: self.bot.drop_inventory_item(0, timeout=0.25),
        ):
            with self.assertRaisesRegex(RuntimeError, "fresh server observation"):
                await operation()
        self.assertEqual(len(self.outgoing()), 1)
        self.assertEqual(self.descriptor(self.bot.container_slots[0]), (PAPER, 5, 31, b""))
        self.assertEqual(self.descriptor(self.bot.inventory_slots[0]), (DIAMOND, 9, 52, b""))

        self.set_slot(2, 2, self.stack(PAPER, 3, 44))
        self.player_slot(2, self.stack())
        independent_range = self.start(transfer_range(
            self.bot, direction="take", item="minecraft:paper", count=1,
            source_slots=range(0, 3), destination_slots=range(2, 3),
        ))
        range_request = await asyncio.wait_for(pending.get(), 1)
        action, = range_request.actions
        self.assertEqual((action.source.slot, action.source.net_id_variant), (2, 44))
        self.reply(range_request, [(CHEST, 2, 2, 44), (PLAYER, 2, 1, 61)])
        self.assertEqual((await independent_range).confirmed_count, 1)

        self.set_slot(2, 0, self.stack(DIAMOND, 9))
        self.player_slot(0, self.stack(PAPER, 5))
        with self.assertRaisesRegex(RuntimeError, "fresh server observation"):
            await self.bot.move_inventory_item(0, 1, timeout=0.25)
        with self.assertRaisesRegex(RuntimeError, "fresh server observation"):
            await self.bot.take_from_container(0, 1, timeout=0.25)
        self.assertEqual(len(self.outgoing()), 2)

        independent = self.start(self.bot.take_from_container(1, 3))
        independent_request = await asyncio.wait_for(pending.get(), 1)
        self.reply(independent_request, [(CHEST, 1, 6, 39), (PLAYER, 3, 3, 55)])
        self.assertEqual((await independent).result.name, "SUCCESS")

        self.set_slot(2, 0, self.stack(DIAMOND, 9, 62))
        from_chest = self.start(self.bot.take_from_container(0, 1))
        take_request = await asyncio.wait_for(pending.get(), 1)
        action, = take_request.actions
        self.assertEqual((action.source.net_id_variant, action.destination.net_id_variant), (62, 0))
        self.reply(take_request, [(CHEST, 0, 8, 62), (PLAYER, 1, 1, 63)])
        self.assertEqual((await from_chest).result.name, "SUCCESS")
        with self.assertRaisesRegex(RuntimeError, "fresh server observation"):
            await self.bot.move_inventory_item(0, 3, timeout=0.25)

        self.content(self.stack(PAPER, 5, 64), width=36,
                     inventory_id=self.b.ContainerID.INVENTORY, name=PLAYER)
        retry = self.start(self.bot.swap_container_items(0, 0, other_area="inventory"))
        request = await asyncio.wait_for(pending.get(), 1)
        self.assert_action(request, (CHEST, 0, 62), (PLAYER, 0, 64))
        self.reply(request, [(CHEST, 0, 5, 64), (PLAYER, 0, 8, 62)])
        self.assertEqual((await retry).result.name, "SUCCESS")

    async def test_recipe_queries_count_only_confirmed_player_slots_after_mixed_swap(self):
        b = self.b
        self.open_chest(self.stack(PAPER, 1, 31))
        self.player_slot(12, self.stack(DIAMOND, 1, 52))
        self.player_slot(13, self.stack(PAPER, 1, 55))
        self.emit(b.GameRulesChangedPacket(rule_data=b.GameRulesChangedPacketData(
            rules=[b.GameRule(name="doLimitedCrafting", value=False)],
        )))
        self.emit(b.CraftingDataPacket(shapeless_recipes=[
            b.ShapelessRecipePayload(
                recipe_id=f"test:{material}", net_id=net_id, tag="crafting_table",
                ingredients=[b.SerializedRecipeIngredient(
                    descriptor={"name": f"minecraft:{material}"}, stack_size=1, aux_value=0,
                )],
                results=[b.SerializedNetworkItemInstanceDescriptor(id=result, stack_size=1)],
            )
            for material, net_id, result in (("diamond", 1, STICK), ("paper", 2, range_tests.SNOWBALL))
        ]))

        self.assertEqual(self.bot.recipes_for("minecraft:stick"), (self.bot.get_recipe(1),))
        self.assertEqual(self.bot.plan_recipe_delta(1).materials, ((RecipeItemAmount(
            DIAMOND, 0, 1, "minecraft:diamond",
        ),),))

        pending = self.hold_requests()
        swap = self.start(self.bot.swap_container_items(0, 12, other_area="inventory"))
        request = await asyncio.wait_for(pending.get(), 1)
        self.reply(request, [(CHEST, 0, 1, 52)])
        self.assertEqual((await swap).result.name, "SUCCESS")
        self.assertEqual(self.descriptor(self.bot.inventory_slots[12]), (DIAMOND, 1, 52, b""))
        self.assertEqual(self.bot.inventory_count(DIAMOND), 1)

        with self.subTest(query="discovery"):
            self.assertEqual(self.bot.recipes_for("minecraft:stick"), ())
        with self.subTest(query="allocation"):
            self.assertIsNone(self.bot.plan_recipe_delta(1))
        self.assertEqual(self.bot.recipes_for("minecraft:snowball"), (self.bot.get_recipe(2),))
        self.assertEqual(self.bot.plan_recipe_delta(2).materials, ((RecipeItemAmount(
            PAPER, 0, 1, "minecraft:paper",
        ),),))
        self.assertEqual(self.bot.recipes_for("minecraft:snowball", min_result_count=2), ())
        self.assertIsNone(self.bot.plan_recipe_delta(2, times=2))

        self.player_slot(12, self.stack(PAPER, 1, 61))
        self.assertEqual(self.bot.recipes_for("minecraft:stick"), ())
        self.assertIsNone(self.bot.plan_recipe_delta(1))
        self.assertEqual(self.bot.recipes_for("minecraft:snowball", min_result_count=2), (self.bot.get_recipe(2),))
        self.assertEqual(self.bot.plan_recipe_delta(2, times=2).materials, ((RecipeItemAmount(
            PAPER, 0, 2, "minecraft:paper",
        ),),))
        self.assertEqual(len(self.outgoing()), 1)

    async def test_harvest_selection_skips_mixed_swap_slot_until_a_complete_observation(self):
        b = self.b
        pickaxe = range_tests.IRON_PICKAXE
        target = (3, 100, 0)
        self.observe_block("minecraft:stone", target)
        self.open_chest(self.stack(PAPER, 1, 31))
        self.player_slot(0, self.stack())
        self.player_slot(12, self.stack(pickaxe, 1, 52))
        self.player_slot(13, self.stack(pickaxe, 1, 55))
        self.assertEqual(self.bot.best_harvest_tool(target).slot, 12)

        pending = self.hold_requests()
        swap = self.start(self.bot.swap_container_items(0, 12, other_area="inventory"))
        request = await asyncio.wait_for(pending.get(), 1)
        self.reply(request, [(CHEST, 0, 1, 52)])
        self.assertEqual((await swap).result.name, "SUCCESS")
        self.emit(b.ContainerClosePacket(container_id=2, container_type=b.ContainerType.CONTAINER))
        self.emit(b.ContainerOpenPacket(container_id=b.ContainerID.INVENTORY, type=b.ContainerType.INVENTORY))
        self.assertEqual(self.bot.inventory_slots[12].net_id_variant, 52)
        self.assertEqual(self.bot.best_harvest_tool(target).slot, 13)

        async def equip_from(source, net_id, equipped_id):
            equip = self.start(self.bot.equip_harvest_tool(target, timeout=1))
            request = await asyncio.wait_for(pending.get(), 1)
            action, = request.actions
            self.assertEqual((action.source.slot, action.source.net_id_variant, action.destination.slot),
                             (source, net_id, 0))
            self.reply(request, [(PLAYER, source, 0, None), (PLAYER, 0, 1, equipped_id)])
            self.assertEqual((await equip).slot, 0)
            self.assertEqual(self.stream.sent[-1].item.net_id_variant, equipped_id)

        await equip_from(13, 55, 61)
        self.player_slot(0, self.stack())
        sent = tuple(self.stream.sent)
        self.assertIsNone(self.bot.best_harvest_tool(target))
        self.assertIsNone(await self.bot.equip_harvest_tool(target))
        self.assertEqual(tuple(self.stream.sent), sent)

        self.player_slot(12, self.stack(pickaxe, 1, 62))
        self.assertEqual(self.bot.best_harvest_tool(target).slot, 12)
        await equip_from(12, 62, 63)

    async def test_chest_refill_skips_unconfirmed_tool_and_recovers_after_observation(self):
        pickaxe = range_tests.IRON_PICKAXE
        target = (3, 100, 0)
        self.observe_block("minecraft:stone", target)
        self.content(width=36, inventory_id=self.b.ContainerID.INVENTORY, name=PLAYER)
        self.open_chest(self.stack(pickaxe, 1, 52), self.stack(pickaxe, 1, 55), self.stack(PAPER, 1, 31))
        pending = self.hold_requests()
        swap = self.start(self.bot.swap_container_items(0, 2))
        request = await asyncio.wait_for(pending.get(), 1)
        self.reply(request, [(CHEST, 2, 1, 52)])
        self.assertEqual((await swap).result.name, "SUCCESS")
        self.assertEqual(self.bot.container_slots[0].net_id_variant, 52)

        async def refill(indices, *, max_tools=1):
            return await equip_harvest_tool_from_chest(
                self.bot, target, chest=range_tests.POSITION, chest_slots=indices, timeout=1, max_tools=max_tools,
            )

        async def take_once(indices, slot, network_id, received_id, *, max_tools=1):
            task = self.start(refill(indices, max_tools=max_tools))
            request = await asyncio.wait_for(pending.get(), 1)
            action, = request.actions
            self.assertEqual((action.source.slot, action.source.net_id_variant), (slot, network_id))
            self.reply(request, [(CHEST, slot, 0, None), (PLAYER, 0, 1, received_id)])
            return await task

        sent = tuple(self.stream.sent)
        self.assertEqual((await refill(range(1))).status, "unobserved")
        self.assertEqual(tuple(self.stream.sent), sent)
        result = await take_once(range(2), 1, 55, 61)
        self.assertEqual((result.status, result.tool.slot, result.transfer.confirmed_count), ("equipped", 0, 1))

        self.player_slot(0, self.stack())
        self.set_slot(2, 0, self.stack(pickaxe, 1))
        self.set_slot(2, 1, self.stack(pickaxe, 1, 56))
        result = await take_once(range(2), 1, 56, 64, max_tools=2)
        self.assertEqual((result.status, result.tool, result.transfer.confirmed_count), ("unobserved", None, 1))

        self.player_slot(0, self.stack())
        self.set_slot(2, 0, self.stack(pickaxe, 1, 62))
        result = await take_once(range(1), 0, 62, 63)
        self.assertEqual((result.status, result.tool.slot, result.transfer.confirmed_count), ("equipped", 0, 1))

    async def test_put_range_skips_unknown_endpoints_and_player_refresh_is_independent(self):
        self.open_chest(self.stack(PAPER, 5, 31))
        self.player_slot(12, self.stack(DIAMOND, 2, 52))
        self.player_slot(13, self.stack(DIAMOND, 2, 55))
        pending = self.hold_requests()
        swap = self.start(self.bot.swap_container_items(0, 12, other_area="inventory"))
        request = await asyncio.wait_for(pending.get(), 1)
        self.reply(request, [(CHEST, 0, 2, 52)])
        self.assertEqual((await swap).result.name, "SUCCESS")

        async def put(name, source, target):
            return await transfer_range(
                self.bot, direction="put", item=name, count=1, source_slots=source, destination_slots=target,
            )

        for source, target in ((range(12, 13), range(1, 2)), (range(13, 14), range(1))):
            result = await put("minecraft:diamond", source, target)
            self.assertEqual((result.stop_reason, result.steps), ("unobserved", ()))
        self.assertEqual(len(self.outgoing()), 1)

        task = self.start(put("minecraft:diamond", range(12, 14), range(2)))
        request = await asyncio.wait_for(pending.get(), 1)
        action, = request.actions
        self.assertEqual((action.source.slot, action.source.net_id_variant, action.destination.slot), (13, 55, 1))
        self.reply(request, [(PLAYER, 13, 1, 55), (CHEST, 1, 1, 61)])
        self.assertEqual((await task).confirmed_count, 1)

        self.player_slot(12, self.stack(PAPER, 5, 62))
        result = await put("minecraft:paper", range(12, 13), range(1))
        self.assertEqual((result.stop_reason, result.steps), ("unobserved", ()))
        self.assertEqual(len(self.outgoing()), 2)
        task = self.start(put("minecraft:paper", range(12, 13), range(3)))
        request = await asyncio.wait_for(pending.get(), 1)
        action, = request.actions
        self.assertEqual((action.source.slot, action.source.net_id_variant, action.destination.slot), (12, 62, 2))
        self.reply(request, [(PLAYER, 12, 4, 62), (CHEST, 2, 1, 63)])
        self.assertEqual((await task).confirmed_count, 1)

    async def range_request(self, pending, operation):
        packet = self.start(pending.get())
        ready, _ = await asyncio.wait((operation, packet), timeout=1, return_when=asyncio.FIRST_COMPLETED)
        if operation in ready:
            self.fail(f"range stopped before writing: {(await operation).stop_reason}")
        self.assertIn(packet, ready, "range request was not sent")
        return packet.result()

    async def test_cross_container_ranges_skip_slots_missing_ordinary_transfer_corrections(self):
        self.open_chest(self.stack(PAPER, 5, 31), self.stack(PAPER, 5, 39))
        self.player_slot(9, self.stack(PAPER, 60, 52))
        self.player_slot(10, self.stack())
        pending = self.hold_requests()
        ordinary = self.start(self.bot.take_from_container(0, 9, 1))
        self.reply(await asyncio.wait_for(pending.get(), 1))
        self.assertIsNone((await ordinary).containers)

        options = dict(direction="take", item="minecraft:paper", count=1)
        unavailable = await transfer_range(self.bot, source_slots=range(1), destination_slots=range(9, 10), **options)
        self.assertEqual((unavailable.stop_reason, unavailable.steps, len(self.outgoing())), ("unconfirmed", (), 1))
        bounds = dict(source_slots=range(2), destination_slots=range(9, 11))
        for operation, arguments, source_count, destination_count in (
            (transfer_range, bounds, 4, 1), (transfer_item, {}, 3, 2),
        ):
            taking = self.start(operation(self.bot, **options, **arguments))
            request = await self.range_request(pending, taking)
            action, = request.actions
            self.assertEqual((action.source.slot, action.source.net_id_variant, action.destination.slot), (1, 39, 10))
            self.reply(request, [(CHEST, 1, source_count, 39), (PLAYER, 10, destination_count, 61)])
            self.assertEqual((await taking).confirmed_count, 1)

    async def test_range_plans_skip_fresh_descriptors_owned_by_pending_exchange(self):
        self.open_chest(self.stack(PAPER, 5, 31), self.stack(DIAMOND, 5, 39))
        self.player_slot(0, self.stack(DIAMOND, 5, 52))
        self.player_slot(1, self.stack(PAPER, 5, 55))
        self.player_slot(2, self.stack())
        self.player_slot(3, self.stack())
        self.player_slot(4, self.stack())
        pending = self.hold_requests()
        swap = self.start(self.bot.swap_container_items(0, 0, other_area="inventory"))
        exchange = await asyncio.wait_for(pending.get(), 1)
        self.set_slot(2, 0, self.stack(DIAMOND, 5, 52))
        self.player_slot(0, self.stack(PAPER, 5, 31))
        self.assertEqual((self.bot.container_slots[0].id, self.bot.inventory_slots[0].id), (DIAMOND, PAPER))

        options = dict(item="minecraft:diamond", count=1, destination_slots=range(2, 3))
        blocked = await transfer_range(self.bot, direction="take", source_slots=range(1), **options)
        self.assertEqual((blocked.stop_reason, blocked.steps, len(self.outgoing())), ("unconfirmed", (), 1))
        operations = (
            (transfer_range, dict(direction="take", source_slots=range(2), **options),
             (CHEST, 1, 4, 39), (PLAYER, 2, 1, 61)),
            (transfer_container_range, dict(source_slots=range(2), **options),
             (CHEST, 1, 3, 39), (CHEST, 2, 1, 62)),
        )
        for operation, arguments, source, destination in operations:
            moving = self.start(operation(self.bot, **arguments))
            request = await self.range_request(pending, moving)
            action, = request.actions
            self.assertEqual((action.source.slot, action.source.net_id_variant), (source[1], source[3]))
            self.reply(request, [source, destination])
            self.assertEqual((await moving).confirmed_count, 1)

        b = self.b
        self.emit(b.ContainerClosePacket(container_id=2, container_type=b.ContainerType.CONTAINER))
        self.emit(b.ContainerOpenPacket(container_id=b.ContainerID.INVENTORY, type=b.ContainerType.INVENTORY))

        self.emit(b.GameRulesChangedPacket(rule_data=b.GameRulesChangedPacketData(
            rules=[b.GameRule(name="doLimitedCrafting", value=False)],
        )))
        self.emit(b.CraftingDataPacket(shapeless_recipes=[b.ShapelessRecipePayload(
            recipe_id="test:paper", net_id=3587, tag="crafting_table",
            ingredients=[b.SerializedRecipeIngredient(
                descriptor={"name": "minecraft:paper"}, stack_size=1, aux_value=0,
            )],
            results=[b.SerializedNetworkItemInstanceDescriptor(id=STICK, stack_size=1)],
        )]))
        self.assertEqual(self.bot.recipes_for("minecraft:stick", min_result_count=6), (self.bot.get_recipe(3587),))
        sent = len(self.outgoing())
        with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
            await self.bot.craft_recipe_auto(3587, 4, times=6)
        self.assertEqual(len(self.outgoing()), sent)
        crafting = self.start(self.bot.craft_recipe_auto(3587, 4))
        request = await asyncio.wait_for(pending.get(), 1)
        consumed = request.actions[2].source
        self.assertEqual((consumed.slot, consumed.net_id_variant), (1, 55))
        self.reply(request, [(PLAYER, 1, 4, 55), (PLAYER, 4, 1, 64)])
        self.assertEqual((await crafting).result.name, "SUCCESS")

        async def inventory_move(indices, slot, net_id, source_count, resulting_count):
            task = self.start(transfer_inventory_range(
                self.bot, item="minecraft:paper", count=1, source_slots=indices, destination_slots=range(3, 4),
            ))
            request = await asyncio.wait_for(pending.get(), 1)
            action, = request.actions
            self.assertEqual((action.source.slot, action.source.net_id_variant), (slot, net_id))
            self.reply(request, [(PLAYER, slot, source_count, net_id), (PLAYER, 3, resulting_count, 63)])
            self.assertEqual((await task).confirmed_count, 1)

        await inventory_move(range(2), 1, 55, 3, 1)
        self.assertFalse(swap.done())
        self.reply(exchange, [(CHEST, 0, 5, 52), (PLAYER, 0, 5, 31)])
        self.assertEqual((await swap).result.name, "SUCCESS")
        await inventory_move(range(1), 0, 31, 4, 2)

    async def test_chest_replaced_before_swap_needs_reopen_and_new_contents(self):
        self.occupied_chest()
        first_window = self.bot.open_window
        self.observe_block("minecraft:air")
        self.observe_block("minecraft:chest")
        with self.assertRaisesRegex(RuntimeError, "ordinary single chest"):
            await self.bot.swap_container_items(0, 1, timeout=0.25)
        with self.assertRaisesRegex(RuntimeError, "endpoint changed"):
            await self.bot.take_from_container(0, 5, timeout=0.25)
        new_first, new_second = self.stack(PAPER, 2, 71), self.stack(STICK, 11, 73)
        self.content(new_first, new_second, width=27)
        with self.assertRaisesRegex(RuntimeError, "ordinary single chest"):
            await self.bot.swap_container_items(0, 1, timeout=0.25)

        self.open_chest(width=None)
        self.assertIsNot(self.bot.open_window, first_window)
        self.assertEqual(self.bot.open_window.container_id, first_window.container_id)
        with self.assertRaisesRegex(RuntimeError, "ordinary single chest"):
            await self.bot.swap_container_items(0, 1, timeout=0.25)
        self.assertEqual(self.outgoing(), [])
        self.content(new_first, new_second, width=27)
        pending = self.hold_requests()
        swap = self.start(self.bot.swap_container_items(0, 1))
        request = await asyncio.wait_for(pending.get(), 1)
        self.assert_action(request, (CHEST, 0, 71), (CHEST, 1, 73))
        self.reply(request, [(CHEST, 0, 11, 73), (CHEST, 1, 2, 71)])
        self.assertEqual((await swap).result.name, "SUCCESS")
