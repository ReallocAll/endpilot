"""Player inventory range transfers through the public API and real packet dispatch."""

import asyncio
import unittest
from unittest.mock import patch

from endpilot import Bot, HeldItemStack, transfer_inventory_range
import test_inventory_slot_events as slot_events
from test_scheduled_input import ManualClock, Wire


_PAPER = 389
_NAMED = b"\x01fixture-paper-tag\x00"


class InventoryRangeTests(unittest.IsolatedAsyncioTestCase):
    deliver = slot_events.InventorySlotEventsTests.deliver
    content = slot_events.InventorySlotEventsTests.content
    slot = slot_events.InventorySlotEventsTests.slot
    verdict = slot_events.InventorySlotEventsTests.verdict
    cancel = staticmethod(slot_events.InventorySlotEventsTests.cancel)

    async def asyncSetUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("InvRangeTest", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)
        self.wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.wire.push(b.StartGamePacket(runtime_id=7, entity_id=17))
        self.wire.push(b.ItemRegistryPacket(items=[b.ItemData(id=_PAPER, name="minecraft:paper")]))
        self.wire.push(b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132")

    def paper(self, count=0, net_id=None, *, data=b""):
        return self.b.SerializedNetworkItemStackDescriptor(
            id=_PAPER if count else 0, stack_size=count, net_id_variant=net_id, user_data_buffer=data,
        )

    def window(self, kind="INVENTORY"):
        b = self.b
        return b.ContainerOpenPacket(container_id=7, type=b.ContainerType[kind], pos=b.BlockPos(x=0, y=0, z=0),
                                     entity_unique_id=-1)

    async def seed(self, *, open_window=True, **indices):
        items = [self.paper() for _ in range(36)]
        for name, descriptor in indices.items():
            items[int(name.removeprefix("slot"))] = descriptor
        packets = ([self.window()] if open_window else []) + [self.content(*items)]
        await self.deliver(*packets)

    @property
    def requests(self):
        return [packet.requests[0] for packet in self.wire.sent if isinstance(packet, self.b.ItemStackRequestPacket)]

    async def request(self, index=0):
        async with asyncio.timeout(2):
            while len(self.requests) <= index:
                await asyncio.sleep(0)
        return self.requests[index]

    def start(self, *, count=1, source=range(9, 10), destination=range(18, 19), match=None):
        task = asyncio.create_task(transfer_inventory_range(
            self.bot, item="minecraft:paper", count=count, source_slots=source, destination_slots=destination,
            match=match,
        ))
        self.addAsyncCleanup(self.cancel, task)
        return task

    async def correct(self, request, *adjustments):
        await self.deliver(self.b.ItemStackResponsePacket(responses=[self.verdict(request, *adjustments)]))

    def pause_range_send(self):
        entered, release = asyncio.Event(), asyncio.Event()
        send = self.bot.packets.send

        async def pause(packet, **kwargs):
            if isinstance(packet, self.b.ItemStackRequestPacket) and kwargs.get("_before_send") is not None:
                entered.set()
                await release.wait()
            return await send(packet, **kwargs)

        self.enterContext(patch.object(self.bot.packets, "send", pause))
        return entered, release

    async def test_selects_plain_paper_fills_then_splits_and_counts_actual_66_of_70(self):
        await self.seed(slot9=self.paper(7, 20), slot10=self.paper(64, 21), slot11=self.paper(5, 22, data=_NAMED),
                        slot18=self.paper(62, 23), slot20=self.paper(6, 24, data=_NAMED), slot21=self.paper(60, 25))

        def plain(stack):
            self.assertIsInstance(stack, HeldItemStack)
            return not stack.user_data_buffer

        moving = self.start(count=70, source=range(9, 12), destination=range(18, 21), match=plain)
        for i, (origin, target, request_amount, before, after) in enumerate((
            (9, 18, 2, (9, 6, 20), (18, 63, 23)),  # SUCCESS confirms just one.
            (9, 18, 1, (9, 5, 20), (18, 64, 23)),
            (9, 19, 5, (9, 0, 0), (19, 5, 26)),
            (10, 19, 59, (10, 5, 21), (19, 64, 26)),
        )):
            request = await self.request(i)
            action, = request.actions
            self.assertEqual((action.source.slot, action.destination.slot, action.amount), (origin, target, request_amount))
            self.assertEqual(action.source.full_container_name.name.name, "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER")
            self.assertEqual(action.destination.full_container_name.name.name, "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER")
            await self.correct(request, before, after)
        result = await moving
        self.assertEqual((result.confirmed_count, result.stop_reason), (66, "destination_full"))
        self.assertEqual([step.confirmed for step in result.steps], [1, 1, 5, 59])
        self.assertEqual(len(self.requests), 4)
        slots = self.bot.inventory_slots
        self.assertEqual([(slots[i].stack_size, bytes(slots[i].user_data_buffer)) for i in (11, 20, 21)],
                         [(5, _NAMED), (6, _NAMED), (60, b"")])

    async def test_default_keeps_variants_separate_and_prefers_the_compatible_later_slot(self):
        await self.seed(slot9=self.paper(2, 20, data=_NAMED), slot19=self.paper(3, 21, data=_NAMED),
                        slot20=self.paper(8, 22))
        moving = self.start(count=2, destination=range(18, 21))
        request = await self.request()
        self.assertEqual(request.actions[0].destination.slot, 19)
        await self.correct(request, (9, 0, 0), (19, 5, 21))
        self.assertEqual((await moving).confirmed_count, 2)
        self.assertEqual(bytes(self.bot.inventory_slots[19].user_data_buffer), _NAMED)
        self.assertEqual(self.bot.inventory_slots[20].stack_size, 8)

    async def test_requires_inventory_window_and_keeps_unknown_destination_unknown(self):
        await self.seed(open_window=False, slot9=self.paper(2, 20))
        with self.assertRaisesRegex(RuntimeError, "open the player inventory"):
            await self.start()
        await self.deliver(self.window(), self.content(*(self.paper() for _ in range(9)), self.paper(2, 20)))
        result = await self.start()
        self.assertEqual(result.stop_reason, "unobserved")
        self.assertEqual(self.requests, [])

    async def test_replacement_or_dimension_change_preserves_late_receipt_without_writeback(self):
        for change_dimension in (False, True):
            with self.subTest(change_dimension=change_dimension):
                await self.seed(slot9=self.paper(1, 20))
                moving = self.start()
                request = await self.request(len(self.requests))
                if change_dimension:
                    await self.deliver(self.b.ChangeDimensionPacket(dimension_id=1))
                await self.seed(slot9=self.paper(4, 30, data=_NAMED), slot18=self.paper(2, 31, data=_NAMED))
                await self.correct(request, (9, 0, 0), (18, 1, 24))
                result = await moving
                self.assertEqual(result.stop_reason, "window_changed")
                step, = result.steps
                self.assertEqual((step.request_id, step.verdict, step.confirmed),
                                 (int(request.client_request_id.raw_id), "SUCCESS", None))
                slots = self.bot.inventory_slots
                self.assertEqual((slots[9].stack_size, slots[18].stack_size), (4, 2))

    async def test_same_window_echo_can_confirm_but_other_changes_are_preserved(self):
        for early_count in (3, 2):
            with self.subTest(early_count=early_count):
                await self.seed(slot9=self.paper(4, 20), slot18=self.paper(4, 21))
                moving = self.start()
                request = await self.request(len(self.requests))
                await self.deliver(self.slot(9, self.paper(early_count, 20)))
                await self.correct(request, (9, 3, 20), (18, 5, 21))
                result = await moving
                self.assertEqual(result.stop_reason, "complete" if early_count == 3 else "unconfirmed")
                self.assertEqual(self.bot.inventory_slots[9].stack_size, early_count)

    async def test_write_boundary_rechecks_window_and_overlapping_range_sends_zero(self):
        await self.seed(slot9=self.paper(3, 20))
        entered, release = self.pause_range_send()
        first = self.start()
        await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual((await self.start()).stop_reason, "unconfirmed")
        await self.deliver(self.window())
        release.set()
        result = await first
        self.assertEqual((result.stop_reason, result.steps), ("window_changed", ()))
        self.assertEqual(self.requests, [])

    async def test_unsubmitted_range_does_not_overlap_an_ordinary_move(self):
        await self.seed(slot9=self.paper(3, 20))
        entered, release = self.pause_range_send()
        moving = self.start()
        await asyncio.wait_for(entered.wait(), 2)
        ordinary = asyncio.create_task(self.bot.move_inventory_item(9, 20))
        self.addAsyncCleanup(self.cancel, ordinary)
        request = await self.request()
        release.set()
        result = await moving
        self.assertEqual((result.stop_reason, result.steps), ("unconfirmed", ()))
        await self.correct(request, (9, 2, 20), (20, 1, 21))
        self.assertEqual((await ordinary).result.name, "SUCCESS")
        self.assertEqual(len(self.requests), 1)

    async def test_cancel_before_write_releases_the_unsent_endpoints(self):
        await self.seed(slot9=self.paper(3, 20))
        entered, release = self.pause_range_send()
        moving = self.start()
        await asyncio.wait_for(entered.wait(), 2)
        moving.cancel()
        with self.assertRaises(asyncio.CancelledError) as failure:
            await moving
        progress = failure.exception.transfer_progress
        self.assertEqual((progress.stop_reason, progress.steps[0].confirmed), ("cancelled", None))
        release.set()
        retry = self.start()
        request = await self.request()
        await self.correct(request, (9, 2, 20), (18, 1, 21))
        self.assertEqual((await retry).confirmed_count, 1)
        self.assertEqual(len(self.requests), 1)

    async def test_cancel_from_slot_observer_preserves_the_applied_partial_receipt(self):
        await self.seed(slot9=self.paper(3, 20))
        moving = self.start(count=3)
        request = await self.request()
        unsubscribe = self.bot.subscribe_inventory_changes(
            lambda change: moving.cancel("stop after slot update"), slot=18,
        )
        try:
            await self.correct(request, (9, 1, 20), (18, 2, 21))
        finally:
            unsubscribe()
        with self.assertRaisesRegex(asyncio.CancelledError, "stop after slot update") as failure:
            await moving
        progress = failure.exception.transfer_progress
        step, = progress.steps
        self.assertEqual((progress.stop_reason, progress.confirmed_count), ("cancelled", 2))
        self.assertEqual((step.request_id, step.verdict, step.requested, step.confirmed),
                         (request.client_request_id.raw_id, "SUCCESS", 3, 2))
        self.assertEqual(len(self.requests), 1)

    async def test_cancelled_sent_move_holds_endpoints_until_its_late_verdict(self):
        await self.seed(slot9=self.paper(3, 20))
        moving = self.start()
        first = await self.request()
        moving.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await moving
        self.assertEqual((await self.start()).stop_reason, "unconfirmed")
        await self.correct(first, (9, 2, 20), (18, 1, 21))
        retry = self.start()
        second = await self.request(1)
        self.assertEqual(second.actions[0].destination.net_id_variant, 21)
        await self.correct(second, (9, 1, 20), (18, 2, 21))
        self.assertEqual((await retry).confirmed_count, 1)

    async def test_missing_dual_correction_leaves_inventory_unconfirmed(self):
        await self.seed(slot9=self.paper(4, 20))
        moving = self.start()
        request = await self.request()
        await self.correct(request, (18, 1, 21))
        result = await moving
        self.assertEqual((result.stop_reason, result.steps[0].confirmed), ("unconfirmed", None))
        self.assertEqual((await self.start()).stop_reason, "unobserved")
        self.assertEqual(len(self.requests), 1)
