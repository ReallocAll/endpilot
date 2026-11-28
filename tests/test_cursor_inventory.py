import asyncio
import unittest
from unittest.mock import patch

from endpilot import Bot, CursorObservation
import test_inventory_range as inventory_range
import test_inventory_slot_events as slot_events
from test_scheduled_input import ManualClock, Wire


_APPLE = 883
_MAIN = "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"
_CURSOR = "CURSOR_CONTAINER"


class CursorInventoryTests(unittest.IsolatedAsyncioTestCase):
    deliver = slot_events.InventorySlotEventsTests.deliver
    content = slot_events.InventorySlotEventsTests.content
    slot = slot_events.InventorySlotEventsTests.slot
    read_counts = slot_events.InventorySlotEventsTests.read_counts
    cancel = staticmethod(slot_events.InventorySlotEventsTests.cancel)
    requests = inventory_range.InventoryRangeTests.requests
    request = inventory_range.InventoryRangeTests.request

    async def asyncSetUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("CursorInventory", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)
        for packet in (b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
                       b.StartGamePacket(runtime_id=7, entity_id=17),
                       b.ItemRegistryPacket(items=[b.ItemData(id=_APPLE, name="minecraft:apple")]),
                       b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN)):
            self.wire.push(packet)
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132")

    def apple(self, count=0, net_id=None, *, aux=0):
        return self.b.SerializedNetworkItemStackDescriptor(
            id=_APPLE if count else 0, stack_size=count, net_id_variant=net_id, aux_value=aux,
        )

    def window(self):
        b = self.b
        return b.ContainerOpenPacket(container_id=2, type=b.ContainerType.WORKBENCH,
                                     pos=b.BlockPos(x=8, y=220, z=9), entity_unique_id=-1)

    def cursor(self, item):
        return self.slot(0, item, "PLAYER_ONLY_UI")

    async def seed(self, *, fresh=True, cursor_first=False):
        items = [self.apple() for _ in range(36)]
        items[12], items[13] = self.apple(5, 1), self.apple(63, 2)
        packets = [self.content(*items), self.window()]
        if fresh:
            packets.insert(1 if cursor_first else len(packets), self.content(self.apple(), container="PLAYER_ONLY_UI"))
        await self.deliver(*packets)

    def start(self, operation):
        task = asyncio.create_task(operation)
        self.addAsyncCleanup(self.cancel, task)
        return task

    def assert_request(self, request, operation, source, destination, amount):
        action, = request.actions
        self.assertEqual(type(action).__name__, operation + "ActionData")
        self.assertEqual(action.amount, amount)
        for part, expected in ((action.source, source), (action.destination, destination)):
            self.assertEqual((part.full_container_name.name.name, part.slot, int(part.net_id_variant or 0)), expected)
            self.assertIsNone(part.full_container_name.dynamic_id)

    async def verdict(self, request, *corrections, result="SUCCESS", request_id=None, duplicate=False):
        b = self.b
        groups = []
        for name, index, count, net_id, *requested in corrections:
            groups.append(b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName[name]),
                slots=[b.ItemStackResponseSlotInfo(slot=index, requested_slot=requested[0] if requested else index,
                                                 amount=count, item_stack_net_id=b.ItemStackNetId(raw_id=net_id))],
            ))
        response = b.ItemStackResponseInfo(
            client_request_id=request.client_request_id if request_id is None else b.ItemStackRequestId(raw_id=request_id),
            result=b.ItemStackNetResult[result], containers=groups,
        )
        await self.deliver(b.ItemStackResponsePacket(responses=[response] * (2 if duplicate else 1)))

    async def take(self):
        index = len(self.requests)
        taking = self.start(self.bot.take_inventory_to_cursor(12))
        request = await self.request(index)
        self.assert_request(request, "Take", (_MAIN, 12, 1), (_CURSOR, 0, 0), 5)
        await self.verdict(request, (_MAIN, 12, 0, 0), (_CURSOR, 0, 5, 1))
        result = await taking
        self.assertEqual((result.requested, result.confirmed_count, result.stop_reason), (5, 5, "complete"))
        self.assertEqual(self.bot.last_cursor_observation, CursorObservation("item_stack_response", 5, 1, None))

    def pause_target(self, index):
        entered, release = asyncio.Event(), asyncio.Event()
        send = self.bot.packets.send

        async def pause(packet, **kwargs):
            if isinstance(packet, self.b.ItemStackRequestPacket) and packet.requests[0].actions[0].destination.slot == index:
                entered.set()
                await release.wait()
            return await send(packet, **kwargs)

        self.enterContext(patch.object(self.bot.packets, "send", pause))
        return entered, release

    async def test_half_take_rounds_up_the_observed_source_quantity(self):
        await self.seed()
        for index, (before, taken) in enumerate(((5, 3), (6, 3), (1, 1))):
            with self.subTest(before=before):
                await self.deliver(self.slot(12, self.apple(before, 1)), self.cursor(self.apple()))
                taking = self.start(self.bot.take_inventory_to_cursor(12, half=True))
                request = await self.request(index)
                self.assert_request(request, "Take", (_MAIN, 12, 1), (_CURSOR, 0, 0), taken)
                after = before - taken
                await self.verdict(request, (_MAIN, 12, after, 1 if after else 0), (_CURSOR, 0, taken, 5))
                result = await taking
                self.assertEqual((result.requested, result.confirmed_count, result.stop_reason),
                                 (taken, taken, "complete"))
                self.assertEqual(self.read_counts(12), (after,))
                self.assertEqual(self.bot.last_cursor_observation,
                                 CursorObservation("item_stack_response", taken, 5, None))

    async def test_place_one_targets_the_requested_empty_or_compatible_slot(self):
        await self.seed()
        await self.deliver(self.cursor(self.apple(3, 5)))
        for index, (slot, old_id, after, new_id) in enumerate(((14, 0, 1, 6), (13, 2, 64, 2))):
            with self.subTest(slot=slot):
                placing = self.start(self.bot.place_one_from_cursor(slot))
                request = await self.request(index)
                self.assert_request(request, "Place", (_CURSOR, 0, 5), (_MAIN, slot, old_id), 1)
                await self.verdict(request, (_CURSOR, 0, 2 - index, 5), (_MAIN, slot, after, new_id))
                result = await placing
                self.assertEqual((result.requested, result.confirmed_count, result.stop_reason), (1, 1, "complete"))
                step, = result.steps
                self.assertEqual((step.source_slot, step.destination_slot, step.confirmed), (0, slot, 1))
        self.assertEqual(self.read_counts(12, 13, 14, 15), (5, 64, 1, 0))
        self.assertEqual(self.bot.last_cursor_observation, CursorObservation("item_stack_response", 1, 5, None))

    async def test_split_and_exact_place_fail_without_writing(self):
        await self.seed()
        result = await self.bot.take_inventory_to_cursor(14, half=True)
        self.assertEqual((result.requested, result.stop_reason, result.steps), (0, "source_exhausted", ()))
        result = await self.bot.place_one_from_cursor(14)
        self.assertEqual((result.requested, result.stop_reason, result.steps), (1, "source_exhausted", ()))

        await self.deliver(
            self.cursor(self.apple(3, 5)), self.slot(13, self.apple(64, 2)),
            self.slot(14, self.apple(1, 6, aux=1)), self.slot(15, self.apple(1)),
        )
        self.assertEqual((await self.bot.take_inventory_to_cursor(12, half=True)).stop_reason, "cursor_occupied")
        for slot, reason in ((13, "destination_full"), (14, "destination_full"), (15, "unobserved")):
            with self.subTest(slot=slot):
                result = await self.bot.place_one_from_cursor(slot)
                self.assertEqual((result.requested, result.stop_reason, result.steps), (1, reason, ()))
        with self.assertRaisesRegex(ValueError, "destination_slot"):
            await self.bot.place_one_from_cursor(36)
        with self.assertRaisesRegex(ValueError, "half"):
            await self.bot.take_inventory_to_cursor(12, half=1)
        self.assertEqual(self.requests, [])

    async def test_cancelled_exact_place_keeps_its_request_and_accepts_late_receipt(self):
        await self.seed()
        await self.deliver(self.cursor(self.apple(2, 5)))
        placing = self.start(self.bot.place_one_from_cursor(14))
        request = await self.request(0)
        placing.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await placing
        progress = caught.exception.transfer_progress
        self.assertEqual((progress.requested, progress.confirmed_count, progress.stop_reason), (1, 0, "cancelled"))
        step, = progress.steps
        self.assertEqual((step.request_id, step.confirmed), (request.client_request_id.raw_id, None))
        self.assertEqual((await self.bot.place_one_from_cursor(15)).stop_reason, "unconfirmed")
        self.assertEqual(len(self.requests), 1)
        await self.verdict(request, (_CURSOR, 0, 1, 5), (_MAIN, 14, 1, 6))
        self.assertEqual(self.read_counts(14, 15), (1, 0))
        self.assertEqual(self.bot.last_cursor_observation, CursorObservation("item_stack_response", 1, 5, None))

    async def test_whole_stack_then_merge_and_empty_track_each_verdict(self):
        await self.seed()
        await self.take()
        self.assertEqual(self.read_counts(12, 13, 14), (0, 63, 0))
        moving = self.start(self.bot.return_cursor_to_inventory(range(13, 15)))
        merge = await self.request(1)
        self.assert_request(merge, "Place", (_CURSOR, 0, 1), (_MAIN, 13, 2), 1)
        await self.verdict(merge, (_CURSOR, 0, 4, 1), (_MAIN, 13, 64, 2))
        empty = await self.request(2)
        self.assert_request(empty, "Place", (_CURSOR, 0, 1), (_MAIN, 14, 0), 4)
        await self.verdict(empty, (_CURSOR, 0, 0, 0), (_MAIN, 14, 4, 1))
        result = await moving
        self.assertEqual((result.requested, result.confirmed_count, result.stop_reason), (5, 5, "complete"))
        self.assertEqual([(s.source_slot, s.destination_slot, s.confirmed, s.request_id, s.verdict) for s in result.steps],
                         [(0, 13, 1, -3, "SUCCESS"), (0, 14, 4, -5, "SUCCESS")])
        self.assertEqual(self.read_counts(12, 13, 14), (0, 64, 4))
        self.assertEqual(self.bot.inventory_slots[14].id, _APPLE)
        self.assertEqual(self.bot.last_cursor_observation.count, 0)
        self.assertEqual(len(self.requests), 3)

    async def test_prefers_compatible_later_slot_and_never_writes_outside_range(self):
        await self.seed()
        await self.take()
        moving = self.start(self.bot.return_cursor_to_inventory(range(13, 14)))
        merge = await self.request(1)
        self.assert_request(merge, "Place", (_CURSOR, 0, 1), (_MAIN, 13, 2), 1)
        await self.verdict(merge, (_CURSOR, 0, 4, 1), (_MAIN, 13, 64, 2))
        result = await moving
        self.assertEqual((result.requested, result.confirmed_count, result.stop_reason), (5, 1, "destination_full"))
        self.assertEqual(self.read_counts(12, 13, 14, 15), (0, 64, 0, 0))
        self.assertEqual(self.bot.last_cursor_observation.count, 4)
        self.assertEqual(len(self.requests), 2)

        await self.deliver(self.slot(13, self.apple()), self.slot(14, self.apple(63, 8)))
        moving = self.start(self.bot.return_cursor_to_inventory(range(13, 15)))
        merge = await self.request(2)
        self.assert_request(merge, "Place", (_CURSOR, 0, 1), (_MAIN, 14, 8), 1)
        await self.verdict(merge, (_CURSOR, 0, 3, 1), (_MAIN, 14, 64, 8))
        empty = await self.request(3)
        self.assert_request(empty, "Place", (_CURSOR, 0, 1), (_MAIN, 13, 0), 3)
        await self.verdict(empty, (_CURSOR, 0, 0, 0), (_MAIN, 13, 3, 1))
        self.assertEqual((await moving).confirmed_count, 4)

    async def test_destination_change_before_submission_sends_nothing(self):
        await self.seed()
        await self.take()
        entered, release = self.pause_target(13)
        moving = self.start(self.bot.return_cursor_to_inventory(range(13, 15)))
        await asyncio.wait_for(entered.wait(), 1)
        await self.deliver(self.slot(13, self.apple(62, 5)))
        release.set()
        result = await moving
        self.assertEqual((result.stop_reason, result.steps), ("unconfirmed", ()))
        self.assertEqual(self.read_counts(13, 14), (62, 0))
        self.assertEqual(len(self.requests), 1)

    async def test_rejection_waits_for_new_complete_descriptor(self):
        await self.seed()
        await self.take()
        before = self.bot.last_cursor_observation
        moving = self.start(self.bot.return_cursor_to_inventory(range(13, 15)))
        merge = await self.request(1)
        await self.deliver(self.slot(13, self.apple(62, 5)))
        await self.verdict(merge, result="FAILED_TO_VALIDATE_DST_SLOT")
        result = await moving
        step, = result.steps
        self.assertEqual((result.stop_reason, step.confirmed, step.request_id, step.verdict),
                         ("rejected", 0, -3, "FAILED_TO_VALIDATE_DST_SLOT"))
        self.assertIs(self.bot.last_cursor_observation, before)
        self.assertEqual((await self.bot.return_cursor_to_inventory(range(13, 15))).stop_reason, "unobserved")
        await self.verdict(merge, (_CURSOR, 0, 5, 1), request_id=-77)
        await self.deliver(self.cursor(self.apple(5)))
        self.assertEqual((await self.bot.return_cursor_to_inventory(range(13, 15))).stop_reason, "unobserved")
        self.assertEqual(len(self.requests), 2)

        await self.deliver(self.cursor(self.apple(5, 1)))
        moving = self.start(self.bot.return_cursor_to_inventory(range(13, 14)))
        merge = await self.request(2)
        self.assert_request(merge, "Place", (_CURSOR, 0, 1), (_MAIN, 13, 5), 2)
        await self.verdict(merge, (_CURSOR, 0, 3, 1), (_MAIN, 13, 64, 5))
        self.assertEqual((await moving).confirmed_count, 2)

    async def test_rejected_place_needs_fresh_destination_even_after_cursor_refresh(self):
        await self.seed()
        await self.take()
        allowed = range(13, 14)
        moving = self.start(self.bot.return_cursor_to_inventory(allowed))
        merge = await self.request(1)
        self.assert_request(merge, "Place", (_CURSOR, 0, 1), (_MAIN, 13, 2), 1)
        await self.verdict(merge, result="FAILED_TO_VALIDATE_DST_SLOT")
        self.assertEqual((await moving).stop_reason, "rejected")

        await self.deliver(self.cursor(self.apple(5, 9)))
        blocked = await self.bot.return_cursor_to_inventory(allowed, timeout=0.25)
        self.assertEqual((blocked.stop_reason, blocked.steps, len(self.requests)), ("unobserved", (), 2))
        await self.deliver(self.slot(13, self.apple(62, 5)))
        moving = self.start(self.bot.return_cursor_to_inventory(allowed))
        merge = await self.request(2)
        self.assert_request(merge, "Place", (_CURSOR, 0, 9), (_MAIN, 13, 5), 2)
        await self.verdict(merge, (_CURSOR, 0, 3, 9), (_MAIN, 13, 64, 5))
        self.assertEqual((await moving).confirmed_count, 2)

    async def test_rejected_take_needs_fresh_source_but_other_main_slots_stay_writable(self):
        await self.seed()
        taking = self.start(self.bot.take_inventory_to_cursor(12))
        sample = self.bot.last_cursor_observation
        await self.verdict(await self.request(), result="FAILED_TO_VALIDATE_SRC_SLOT")
        self.assertEqual((await taking).stop_reason, "rejected")
        self.assertIs(self.bot.last_cursor_observation, sample)

        await self.deliver(self.cursor(self.apple()))
        blocked = await self.bot.take_inventory_to_cursor(12, timeout=0.25)
        self.assertEqual((blocked.stop_reason, blocked.steps, len(self.requests)), ("unobserved", (), 1))
        taking = self.start(self.bot.take_inventory_to_cursor(13))
        independent = await self.request(1)
        self.assert_request(independent, "Take", (_MAIN, 13, 2), (_CURSOR, 0, 0), 63)
        await self.verdict(independent, (_MAIN, 13, 0, 0), (_CURSOR, 0, 63, 2))
        self.assertEqual((await taking).confirmed_count, 63)

    async def test_rejected_place_correction_requires_a_known_stack_identity(self):
        for valid, net_id in ((False, 8), (True, 2)):
            with self.subTest(net_id=net_id):
                await self.seed()
                await self.take()
                index = len(self.requests)
                allowed = range(13, 14)
                moving = self.start(self.bot.return_cursor_to_inventory(allowed))
                merge = await self.request(index)
                await self.verdict(merge, (_MAIN, 13, 62, net_id), result="FAILED_TO_VALIDATE_DST_SLOT")
                result = await moving
                self.assertEqual((result.stop_reason, result.steps[0].confirmed), ("rejected", 0))
                self.assertEqual(self.read_counts(13), (62 if valid else 63,))
                await self.deliver(self.cursor(self.apple(5, 9)))
                if not valid:
                    blocked = await self.bot.return_cursor_to_inventory(allowed, timeout=0.25)
                    self.assertEqual((blocked.stop_reason, blocked.steps, len(self.requests)),
                                     ("unobserved", (), index + 1))
                    continue
                moving = self.start(self.bot.return_cursor_to_inventory(allowed))
                merge = await self.request(index + 1)
                self.assert_request(merge, "Place", (_CURSOR, 0, 9), (_MAIN, 13, 2), 2)
                await self.verdict(merge, (_CURSOR, 0, 3, 9), (_MAIN, 13, 64, 2))
                self.assertEqual((await moving).confirmed_count, 2)

    async def test_take_needs_current_empty_cursor_and_unique_valid_dual_receipts(self):
        await self.seed(cursor_first=True)
        before = self.bot.last_cursor_observation
        self.assertEqual((await self.bot.take_inventory_to_cursor(12)).stop_reason, "unobserved")
        self.assertIs(self.bot.last_cursor_observation, before)
        await self.deliver(self.cursor(self.apple(1, 9)))
        self.assertEqual((await self.bot.take_inventory_to_cursor(12)).stop_reason, "cursor_occupied")
        for index, corrections in enumerate((
            ((_CURSOR, 0, 5, 1),),
            ((_MAIN, 12, 0, 0), (_CURSOR, 0, 5, 1), (_CURSOR, 0, 5, 1)),
            ((_MAIN, 12, 0, 0), (_CURSOR, 0, 5, 1, 1)),
            ((_MAIN, 12, 0, 0), (_CURSOR, 0, 5, 0)),
            ((_MAIN, 12, 1, 1), (_CURSOR, 0, 4, 6)),
        )):
            with self.subTest(corrections=corrections):
                await self.deliver(self.cursor(self.apple()), self.slot(12, self.apple(5, 1)))
                taking = self.start(self.bot.take_inventory_to_cursor(12))
                request = await self.request(index)
                await self.verdict(request, *corrections)
                result = await taking
                self.assertEqual((result.stop_reason, result.confirmed_count), ("unconfirmed", 0))
                self.assertIsNone(result.steps[0].confirmed)
                self.assertEqual((await self.bot.return_cursor_to_inventory(range(13, 15))).stop_reason, "unobserved")
        self.assertEqual(len(self.requests), 5)

        await self.deliver(self.cursor(self.apple()), self.slot(12, self.apple(5, 1)))
        taking = self.start(self.bot.take_inventory_to_cursor(12))
        request = await self.request(5)
        await self.verdict(request, (_MAIN, 12, 0, 0), (_CURSOR, 0, 5, 1), duplicate=True)
        with self.assertRaisesRegex(ValueError, "duplicate verdict") as caught:
            await taking
        result = caught.exception.transfer_progress
        self.assertEqual((result.stop_reason, result.confirmed_count), ("unconfirmed", 0))
        self.assertEqual(result.steps[0].request_id, int(request.client_request_id.raw_id))
        self.assertEqual((await self.bot.return_cursor_to_inventory(range(13, 15))).stop_reason, "unobserved")

    async def test_unsubmitted_second_step_cancellation_preserves_prefix_and_can_retry(self):
        await self.seed()
        await self.take()
        entered, release = self.pause_target(14)
        moving = self.start(self.bot.return_cursor_to_inventory(range(13, 15)))
        merge = await self.request(1)
        await self.verdict(merge, (_CURSOR, 0, 4, 1), (_MAIN, 13, 64, 2))
        await asyncio.wait_for(entered.wait(), 1)
        moving.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await moving
        progress = caught.exception.transfer_progress
        self.assertEqual((progress.requested, progress.confirmed_count, progress.stop_reason), (5, 1, "cancelled"))
        self.assertEqual([(s.request_id, s.confirmed) for s in progress.steps], [(-3, 1), (None, None)])
        self.assertEqual(len(self.requests), 2)
        release.set()
        retry = self.start(self.bot.return_cursor_to_inventory(range(13, 15)))
        empty = await self.request(2)
        await self.verdict(empty, (_CURSOR, 0, 0, 0), (_MAIN, 14, 4, 1))
        self.assertEqual((await retry).confirmed_count, 4)

    async def test_submitted_second_step_cancellation_preserves_id_and_reserves_both_slots(self):
        await self.seed()
        await self.take()
        moving = self.start(self.bot.return_cursor_to_inventory(range(13, 15)))
        merge = await self.request(1)
        await self.verdict(merge, (_CURSOR, 0, 4, 1), (_MAIN, 13, 64, 2))
        empty = await self.request(2)
        moving.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await moving
        progress = caught.exception.transfer_progress
        self.assertEqual((progress.requested, progress.confirmed_count, progress.stop_reason), (5, 1, "cancelled"))
        self.assertEqual([(s.request_id, s.confirmed) for s in progress.steps], [(-3, 1), (-5, None)])
        self.assertEqual((await self.bot.return_cursor_to_inventory(range(15, 16))).stop_reason, "unconfirmed")
        with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
            await self.bot.move_inventory_item(13, 14)
        self.assertEqual(len(self.requests), 3)

        await self.verdict(empty, (_CURSOR, 0, 0, 0), (_MAIN, 14, 4, 1))
        self.assertEqual(self.read_counts(13, 14), (64, 4))
        self.assertEqual((await self.bot.return_cursor_to_inventory(range(15, 16))).requested, 0)
        self.assertEqual(progress.confirmed_count, 1)

    async def test_submitted_second_step_timeout_preserves_prefix_and_id(self):
        await self.seed()
        await self.take()
        moving = self.start(self.bot.return_cursor_to_inventory(range(13, 15), timeout=0.5))
        merge = await self.request(1)
        await self.verdict(merge, (_CURSOR, 0, 4, 1), (_MAIN, 13, 64, 2))
        empty = await self.request(2)
        with self.assertRaises(TimeoutError) as caught:
            await moving
        progress = caught.exception.transfer_progress
        self.assertEqual((progress.requested, progress.confirmed_count, progress.stop_reason), (5, 1, "unconfirmed"))
        self.assertEqual([(s.request_id, s.confirmed) for s in progress.steps],
                         [(-3, 1), (int(empty.client_request_id.raw_id), None)])
        self.assertEqual(len(self.requests), 3)
        await self.verdict(empty, (_CURSOR, 0, 0, 0), (_MAIN, 14, 4, 1))

    async def test_ordinary_pending_main_slot_blocks_cursor_but_other_slots_can_move(self):
        await self.seed()
        ordinary = self.start(self.bot.move_inventory_item(12, 15))
        outgoing = await self.request()
        blocked = await self.bot.take_inventory_to_cursor(12)
        self.assertEqual((blocked.stop_reason, blocked.steps, len(self.requests)), ("unconfirmed", (), 1))
        taking = self.start(self.bot.take_inventory_to_cursor(13))
        independent = await self.request(1)
        self.assert_request(independent, "Take", (_MAIN, 13, 2), (_CURSOR, 0, 0), 63)
        await self.verdict(outgoing, (_MAIN, 12, 4, 1), (_MAIN, 15, 1, 3))
        self.assertEqual((await ordinary).result.name, "SUCCESS")
        await self.verdict(independent, (_MAIN, 13, 0, 0), (_CURSOR, 0, 63, 2))
        self.assertEqual((await taking).confirmed_count, 63)

    async def test_close_write_and_late_same_number_reply_cannot_authorize_reopened_window(self):
        await self.seed()
        taking = self.start(self.bot.take_inventory_to_cursor(12))
        pending = await self.request()
        old = self.bot.open_window
        close = self.start(self.bot.close_container())
        async with asyncio.timeout(1):
            while not any(isinstance(packet, self.b.ContainerClosePacket) for packet in self.wire.sent):
                await asyncio.sleep(0)
        self.assertIs(self.bot.open_window, old)
        self.assertEqual((await self.bot.take_inventory_to_cursor(13)).stop_reason, "window_changed")
        await self.deliver(self.b.ContainerClosePacket(container_id=2, container_type=self.b.ContainerType.WORKBENCH,
                                                       server_initiated_close=True), self.window())
        self.assertIs(await close, old)
        self.assertIsNot(self.bot.open_window, old)
        await self.deliver(self.slot(12, self.apple(7, 8)))
        await self.verdict(pending, (_MAIN, 12, 0, 0), (_CURSOR, 0, 5, 1))
        stale = await taking
        self.assertEqual((stale.stop_reason, stale.confirmed_count, stale.steps[0].request_id), ("window_changed", 0, -1))
        self.assertEqual(self.bot.last_cursor_observation, CursorObservation("item_stack_response", 5, 1, None))
        self.assertEqual(self.read_counts(12), (7,))
        self.assertEqual((await self.bot.return_cursor_to_inventory(range(13, 15))).stop_reason, "unobserved")
        self.assertEqual(len(self.requests), 1)
        await self.deliver(self.cursor(self.apple(3, 9)))
        await self.verdict(pending, (_CURSOR, 0, 5, 1))
        moving = self.start(self.bot.return_cursor_to_inventory(range(14, 15)))
        fresh = await self.request(1)
        self.assert_request(fresh, "Place", (_CURSOR, 0, 9), (_MAIN, 14, 0), 3)
        await self.verdict(fresh, (_CURSOR, 0, 0, 0), (_MAIN, 14, 3, 9))
        self.assertEqual((await moving).confirmed_count, 3)

    async def test_dimension_and_session_end_remove_cursor_authority(self):
        await self.seed()
        await self.take()
        before = self.bot.last_cursor_observation
        await self.deliver(self.b.ChangeDimensionPacket(dimension_id=1), self.window())
        self.assertIs(self.bot.last_cursor_observation, before)
        self.assertEqual((await self.bot.return_cursor_to_inventory(range(13, 15))).stop_reason, "unobserved")
        await self.deliver(self.b.DisconnectPacket())
        self.assertIsNone(self.bot.last_cursor_observation)
        with self.assertRaisesRegex(RuntimeError, "workbench"):
            await self.bot.return_cursor_to_inventory(range(13, 15))
        self.assertEqual(len(self.requests), 1)
