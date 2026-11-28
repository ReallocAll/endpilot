import asyncio
import unittest

from endpilot import RangeTransferResult, RangeTransferStep, transfer_range

import test_range_transfer as range_tests


CHEST = range_tests.CHEST
PLAYER = range_tests.PLAYER
PAPER = range_tests.PAPER
CURSOR = "CURSOR_CONTAINER"


class ContainerCursorTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = range_tests.RangeTransferTests.asyncSetUp
    observe_block = range_tests.RangeTransferTests.observe_block
    observe_chest_pair = range_tests.RangeTransferTests.observe_chest_pair
    observe_chest_pair_root = range_tests.RangeTransferTests.observe_chest_pair_root
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

    def cursor(self, count=0, net_id=None):
        self.set_slot(self.b.ContainerID.PLAYER_ONLY_UI, 0, self.stack(PAPER, count, net_id) if count else self.stack())

    def close_chest(self):
        self.emit(self.b.ContainerClosePacket(container_id=2, container_type=self.b.ContainerType.CONTAINER))

    def assert_action(self, request, kind, amount, source, destination):
        action, = request.actions
        self.assertEqual((type(action).__name__, action.amount), (kind + "ActionData", amount))
        for slot, expected in ((action.source, source), (action.destination, destination)):
            address = slot.full_container_name
            self.assertEqual((address.name.name, slot.slot, int(slot.net_id_variant or 0)), expected)
            self.assertIsNone(address.dynamic_id)

    async def test_same_flow_works_on_an_observed_single_and_paired_double(self):
        pending = self.hold_requests()
        for width, target in ((27, 14), (54, 40)):
            with self.subTest(width=width):
                if width == 54:
                    self.observe_chest_pair()
                self.open_chest(width=None)
                self.cursor()
                slots = [self.stack() for _ in range(13)]
                slots[12] = self.stack(PAPER, 5, 31)
                self.content(*slots, width=width)
                self.player_slot(12, self.stack(PAPER, 20, 32))

                calls = (
                    (self.bot.take_container_to_cursor, (12,), {"half": True}, "Take", 3,
                     (CHEST, 12, 31), (CURSOR, 0, 0), [(CHEST, 12, 2, 31), (CURSOR, 0, 3, 33)]),
                    (self.bot.place_one_from_cursor, (12,), {}, "Place", 1,
                     (CURSOR, 0, 33), (PLAYER, 12, 32), [(CURSOR, 0, 2, 33), (PLAYER, 12, 21, 32)]),
                    (self.bot.place_one_from_cursor, (target,), {"destination_area": "container"}, "Place", 1,
                     (CURSOR, 0, 33), (CHEST, target, 0), [(CURSOR, 0, 1, 33), (CHEST, target, 1, 34)]),
                    (self.bot.return_cursor_to_container, (range(12, 13),), {}, "Place", 1,
                     (CURSOR, 0, 33), (CHEST, 12, 31), [(CURSOR, 0, 0, None), (CHEST, 12, 3, 31)]),
                )
                for call, args, kwargs, kind, amount, source, destination, corrections in calls:
                    move = self.start(call(*args, **kwargs))
                    request = await asyncio.wait_for(pending.get(), 1)
                    self.assert_action(request, kind, amount, source, destination)
                    for side, index, count, net_id in corrections:
                        if side == CHEST:
                            self.set_slot(2, index, self.stack(PAPER, count, net_id))
                    self.reply(request, corrections)
                    self.assertEqual(await move, RangeTransferResult(amount, (
                        RangeTransferStep(source[1], destination[1], amount, amount,
                                          request.client_request_id.raw_id, "SUCCESS"),
                    ), "complete"))
                self.assertEqual(self.bot.inventory_slots[12].stack_size, 21)
                self.assertEqual((self.bot.container_slots[12].stack_size, self.bot.container_slots[target].stack_size,
                                  self.bot.last_cursor_observation.count), (3, 1, 0))
                self.close_chest()

    async def test_main_inventory_take_and_range_return_work_in_chest(self):
        self.open_chest()
        self.cursor()
        self.player_slot(12, self.stack(PAPER, 2, 31))
        pending = self.hold_requests()
        take = self.start(self.bot.take_inventory_to_cursor(12))
        request = await asyncio.wait_for(pending.get(), 1)
        self.assert_action(request, "Take", 2, (PLAYER, 12, 31), (CURSOR, 0, 0))
        self.reply(request, [(PLAYER, 12, 0, None), (CURSOR, 0, 2, 31)])
        self.assertEqual((await take).confirmed_count, 2)
        returned = self.start(self.bot.return_cursor_to_inventory(range(12, 13)))
        request = await asyncio.wait_for(pending.get(), 1)
        self.assert_action(request, "Place", 2, (CURSOR, 0, 31), (PLAYER, 12, 0))
        self.reply(request, [(CURSOR, 0, 0, None), (PLAYER, 12, 2, 31)])
        self.assertEqual((await returned).confirmed_count, 2)

    async def test_unknown_cursor_layout_and_unauthorized_capacity_do_not_write(self):
        self.open_chest(width=None)
        self.cursor()
        self.assertEqual((await self.bot.take_container_to_cursor(0)).stop_reason, "unobserved")
        self.content(width=54)
        self.assertEqual((await self.bot.take_container_to_cursor(0)).stop_reason, "unobserved")
        self.open_chest(self.stack(PAPER, 64, 31), self.stack(range_tests.DIAMOND, 1, 32))
        self.assertEqual((await self.bot.take_container_to_cursor(0)).stop_reason, "unobserved")
        self.cursor()
        self.assertEqual((await self.bot.take_container_to_cursor(2)).stop_reason, "source_exhausted")
        self.assertEqual((await self.bot.place_one_from_cursor(2, destination_area="container")).stop_reason,
                         "source_exhausted")
        self.cursor(1, 33)
        for destination in (0, 1):
            result = await self.bot.place_one_from_cursor(destination, destination_area="container")
            self.assertEqual(result, RangeTransferResult(1, (), "destination_full"))
        with self.assertRaises(ValueError):
            await self.bot.return_cursor_to_container(range(27, 28))
        self.assertEqual(self.outgoing(), [])

    async def test_return_preserves_confirmed_prefix_when_the_authorized_chest_range_fills(self):
        self.open_chest(self.stack(PAPER, 63, 31))
        self.cursor(2, 32)
        pending = self.hold_requests()
        moving = self.start(self.bot.return_cursor_to_container(range(1)))
        request = await asyncio.wait_for(pending.get(), 1)
        self.assert_action(request, "Place", 1, (CURSOR, 0, 32), (CHEST, 0, 31))
        self.reply(request, [(CURSOR, 0, 1, 32), (CHEST, 0, 64, 31)])
        result = await moving
        self.assertEqual((result.requested, result.confirmed_count, result.stop_reason), (2, 1, "destination_full"))
        self.assertEqual(self.bot.container_slots[1].stack_size, 0)
        self.assertEqual(len(self.outgoing()), 1)

    async def test_take_requires_agreeing_receipts_and_source_observation_to_retry(self):
        self.open_chest(self.stack(PAPER, 5, 31))
        pending = self.hold_requests()
        for corrections in (
            [(CURSOR, 0, 3, 33)],
            [(CURSOR, 0, 3, 33), (CHEST, 0, 3, 31)],
        ):
            self.cursor()
            self.set_slot(2, 0, self.stack(PAPER, 5, 31))
            take = self.start(self.bot.take_container_to_cursor(0, half=True))
            request = await asyncio.wait_for(pending.get(), 1)
            self.reply(request, corrections)
            self.assertEqual(await take, RangeTransferResult(3, (
                RangeTransferStep(0, 0, 3, None, request.client_request_id.raw_id, "SUCCESS"),
            ), "unconfirmed"))
            self.cursor()
            sent = len(self.outgoing())
            self.assertEqual((await self.bot.take_container_to_cursor(0, half=True)).stop_reason, "unobserved")
            self.assertEqual(len(self.outgoing()), sent)

        self.set_slot(2, 0, self.stack(PAPER, 5, 40))
        take = self.start(self.bot.take_container_to_cursor(0, half=True))
        request = await asyncio.wait_for(pending.get(), 1)
        self.assert_action(request, "Take", 3, (CHEST, 0, 40), (CURSOR, 0, 0))
        self.reply(request, [(CHEST, 0, 2, 40), (CURSOR, 0, 3, 41)])
        self.assertEqual((await take).confirmed_count, 3)

    async def test_incomplete_main_take_blocks_ordinary_writes_until_that_slot_is_observed(self):
        self.open_chest()
        self.player_slot(9, self.stack(PAPER, 5, 31))
        self.player_slot(10, self.stack(PAPER, 1, 32))
        self.cursor()
        pending = self.hold_requests()
        take = self.start(self.bot.take_inventory_to_cursor(9, half=True))
        request = await asyncio.wait_for(pending.get(), 1)
        self.assert_action(request, "Take", 3, (PLAYER, 9, 31), (CURSOR, 0, 0))
        self.reply(request, [(CURSOR, 0, 3, 33)])
        self.assertEqual((await take).stop_reason, "unconfirmed")

        self.cursor(3, 33)
        self.assertEqual(self.bot.inventory_slots[9].net_id_variant, 31)
        for function, destination in ((self.bot.put_into_container, 1), (self.bot.move_inventory_item, 11)):
            attempt = self.start(function(9, destination, 5))
            submitted = self.start(pending.get())
            await asyncio.wait((attempt, submitted), timeout=1, return_when=asyncio.FIRST_COMPLETED)
            if submitted.done():
                action = range_tests.RangeTransferTests.action(submitted.result())
                self.fail(f"an unconfirmed slot reached the wire: {action}")
            self.assertTrue(attempt.done())
            submitted.cancel()
            with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
                await attempt
        self.assertEqual(len(self.outgoing()), 1)

        put = self.start(self.bot.put_into_container(10, 1, 1))
        request = await asyncio.wait_for(pending.get(), 1)
        self.assert_action(request, "Place", 1, (PLAYER, 10, 32), (CHEST, 1, 0))
        self.reply(request, [(PLAYER, 10, 0, None), (CHEST, 1, 1, 32)])
        self.assertEqual((await put).result.name, "SUCCESS")

        self.player_slot(9, self.stack(PAPER, 2, 34))
        put = self.start(self.bot.put_into_container(9, 2, 2))
        request = await asyncio.wait_for(pending.get(), 1)
        self.assert_action(request, "Place", 2, (PLAYER, 9, 34), (CHEST, 2, 0))
        self.reply(request, [(PLAYER, 9, 0, None), (CHEST, 2, 2, 34)])
        self.assertEqual((await put).result.name, "SUCCESS")

    async def test_return_skips_an_ordinary_request_slot_across_chest_areas(self):
        pending = self.hold_requests()
        for area in ("container", "inventory", "reopened_double"):
            with self.subTest(area=area):
                in_inventory = area == "inventory"
                reopened = area == "reopened_double"
                first = 40 if reopened else 0
                width = 54 if reopened else 27
                if reopened:
                    self.observe_chest_pair()
                stacks = ([self.stack()] * first
                          + [self.stack(PAPER, 20, 31), self.stack(PAPER, 20, 32)])
                self.open_chest(*stacks, width=width)
                self.cursor(2, 35)
                self.player_slot(9, self.stack(PAPER, 20, 33))
                self.player_slot(10, self.stack(PAPER, 20, 34))
                take = self.start(self.bot.take_from_container(first, 9, 1))
                held = await asyncio.wait_for(pending.get(), 1)
                self.assert_action(held, "Take", 1, (CHEST, first, 31), (PLAYER, 9, 33))

                if reopened:
                    self.close_chest()
                    self.open_chest(*stacks, position=range_tests.EAST, width=width)
                    self.cursor(2, 35)

                start = 9 if in_inventory else first
                destination_area = "inventory" if in_inventory else "container"
                family, next_id = (PLAYER, 34) if in_inventory else (CHEST, 32)
                move = self.bot.return_cursor_to_inventory if in_inventory else self.bot.return_cursor_to_container
                sent = len(self.outgoing())
                self.assertEqual(await move(range(start, start + 1)), RangeTransferResult(2, (), "unconfirmed"))
                self.assertEqual(await self.bot.place_one_from_cursor(start, destination_area=destination_area),
                                 RangeTransferResult(1, (), "unconfirmed"))
                self.assertEqual(len(self.outgoing()), sent)

                returning = self.start(move(range(start, start + 2)))
                submitted = self.start(pending.get())
                await asyncio.wait((returning, submitted), timeout=1, return_when=asyncio.FIRST_COMPLETED)
                if not submitted.done():
                    submitted.cancel()
                    self.reply(held, [(CHEST, first, 19, 31), (PLAYER, 9, 21, 33)])
                    await take
                    self.close_chest()
                    self.assertTrue(returning.done())
                    self.fail(f"did not use the available slot: {returning.result()}")
                request = submitted.result()
                self.assert_action(request, "Place", 2, (CURSOR, 0, 35), (family, start + 1, next_id))
                self.reply(request, [(CURSOR, 0, 0, None), (family, start + 1, 22, next_id)])
                self.assertEqual((await returning).confirmed_count, 2)
                self.assertEqual(len(self.outgoing()), sent + 1)
                self.reply(held, [(CHEST, first, 19, 31), (PLAYER, 9, 21, 33)])
                self.assertEqual((await take).result.name, "SUCCESS")
                self.close_chest()

    async def test_late_chest_receipt_does_not_revise_a_cancelled_range_result(self):
        self.open_chest(self.stack(PAPER, 63, 31))
        self.cursor(5, 32)
        pending = self.hold_requests()
        moving = self.start(self.bot.return_cursor_to_container(range(2)))
        merge = await asyncio.wait_for(pending.get(), 1)
        self.assert_action(merge, "Place", 1, (CURSOR, 0, 32), (CHEST, 0, 31))
        self.reply(merge, [(CURSOR, 0, 4, 32), (CHEST, 0, 64, 31)])
        fill = await asyncio.wait_for(pending.get(), 1)
        self.assert_action(fill, "Place", 4, (CURSOR, 0, 32), (CHEST, 1, 0))
        moving.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await moving
        progress = caught.exception.transfer_progress
        expected = RangeTransferResult(5, (
            RangeTransferStep(0, 0, 1, 1, merge.client_request_id.raw_id, "SUCCESS"),
            RangeTransferStep(0, 1, 4, None, fill.client_request_id.raw_id, None),
        ), "cancelled")
        self.assertEqual((progress, progress.confirmed_count), (expected, 1))

        self.reply(fill, [(CURSOR, 0, 0, None), (CHEST, 1, 4, 32)])
        self.assertEqual((self.bot.container_slots[1].stack_size, self.bot.last_cursor_observation.count), (4, 0))
        self.assertEqual((progress, progress.confirmed_count), (expected, 1))
        self.assertEqual(await self.bot.return_cursor_to_container(range(2)), RangeTransferResult(0, (), "complete"))
        self.assertEqual(len(self.outgoing()), 2)

    async def test_cancelled_old_receipt_leaves_reopened_physical_slot_and_cursor_intact(self):
        self.observe_chest_pair()
        self.open_chest(self.stack(PAPER, 5, 31), width=54)
        self.cursor()
        self.player_slot(0, self.stack())
        pending = self.hold_requests()
        old = self.start(self.bot.take_container_to_cursor(0, half=True))
        old_request = await asyncio.wait_for(pending.get(), 1)
        self.set_slot(2, 0, self.stack(PAPER, 2, 31))
        blocked = await transfer_range(self.bot, direction="take", item="minecraft:paper", count=1,
                                       source_slots=range(1), destination_slots=range(1))
        self.assertEqual(blocked, RangeTransferResult(1, (), "unconfirmed"))
        old.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await old
        progress = caught.exception.transfer_progress
        self.assertEqual(progress, RangeTransferResult(3, (
            RangeTransferStep(0, 0, 3, None, old_request.client_request_id.raw_id, None),
        ), "cancelled"))

        closing = self.start(self.bot.close_container())
        async with asyncio.timeout(1):
            while not any(isinstance(packet, self.b.ContainerClosePacket) for packet in self.stream.sent):
                await asyncio.sleep(0)
        self.assertEqual((await self.bot.take_container_to_cursor(0)).stop_reason, "window_changed")
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        self.close_chest()
        self.open_chest(self.stack(PAPER, 7, 40), position=range_tests.EAST, width=54)
        self.cursor()
        self.assertEqual((await self.bot.take_container_to_cursor(0, half=True)).stop_reason, "unconfirmed")
        self.assertEqual(len(self.outgoing()), 1)
        self.reply(old_request, [(CHEST, 0, 2, 31), (CURSOR, 0, 3, 32)])
        self.assertEqual((self.bot.container_slots[0].stack_size, self.bot.container_slots[0].net_id_variant), (7, 40))
        self.assertEqual(self.bot.last_cursor_observation.count, 3)

        current = self.start(self.bot.take_container_to_cursor(0, half=True))
        request = await asyncio.wait_for(pending.get(), 1)
        self.assert_action(request, "Take", 4, (CHEST, 0, 40), (CURSOR, 0, 0))
        self.reply(request, [(CHEST, 0, 3, 40), (CURSOR, 0, 4, 41)])
        self.assertEqual((await current).confirmed_count, 4)
