import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from endpilot import Bot, BrewingProgress, FurnaceProgress
from endpilot.protocol import write_batch

from test_scheduled_input import ManualClock, Wire


WINDOW = 2
PLAYER = "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"
INGREDIENT = "BREWING_STAND_INPUT_CONTAINER"
BOTTLE = "BREWING_STAND_RESULT_CONTAINER"
FUEL = "BREWING_STAND_FUEL_CONTAINER"


class BrewingStandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("BrewingStand", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)
        for packet in (
            b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
            b.StartGamePacket(runtime_id=7, entity_id=17),
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        ):
            self.wire.push(packet)
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    def stack(self, item_id=0, count=0, net_id=None, *, aux=0, data=b""):
        return self.b.SerializedNetworkItemStackDescriptor(
            id=item_id, stack_size=count, aux_value=aux, net_id_variant=net_id, user_data_buffer=data,
        )

    def window_packet(self, kind="BREWING_STAND", *, window_id=WINDOW):
        b = self.b
        return b.ContainerOpenPacket(container_id=window_id, type=b.ContainerType[kind],
                                     pos=b.BlockPos(x=0, y=220, z=0), entity_unique_id=-1)

    def contents(self, *slots):
        b = self.b
        return b.InventoryContentPacket(
            inventory_id=WINDOW, slots=slots,
            full_container_name=b.FullContainerName(name=b.ContainerEnumName.ANVIL_INPUT_CONTAINER),
        )

    def data(self, field, value, *, window_id=WINDOW):
        return self.b.ContainerSetDataPacket(container_id=window_id, id=field, value=value)

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        received = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in (*packets, marker)))
        await received

    async def submit(self, operation):
        sent = asyncio.Event()
        unsubscribe = self.bot.packets.subscribe_sent(
            lambda packet_id: sent.set() if packet_id == self.b.ItemStackRequestPacket.ID else None,
        )
        task = asyncio.create_task(operation)

        async def cleanup():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        try:
            await asyncio.wait_for(sent.wait(), 1)
            request, = self.wire.sent[-1].requests
            return task, request
        finally:
            unsubscribe()

    def verdict(self, request_id, *rows):
        b = self.b
        return b.ItemStackResponseInfo(
            client_request_id=b.ItemStackRequestId(raw_id=request_id), result=b.ItemStackNetResult.SUCCESS,
            containers=[b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName[side]),
                slots=[b.ItemStackResponseSlotInfo(requested_slot=index, slot=index, amount=count,
                    item_stack_net_id=None if net_id is None else b.ItemStackNetId(raw_id=net_id))],
            ) for side, index, count, net_id in rows],
        )

    @staticmethod
    def address(slot):
        return slot.full_container_name.name.name, slot.slot, slot.net_id_variant

    async def test_all_five_slots_use_their_brewing_addresses_in_both_directions(self):
        b = self.b
        await self.deliver(self.window_packet(), self.contents(*(self.stack() for _ in range(5))))
        self.assertEqual(len(await self.bot.wait_for_container_slots()), 5)
        for index, name in enumerate((INGREDIENT, BOTTLE, BOTTLE, BOTTLE, FUEL)):
            with self.subTest(slot=index):
                item_id = 300 + index
                source_net, target_net = 20 + index, 40 + index
                await self.deliver(b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=0,
                                                         item=self.stack(item_id, 1, source_net, aux=index)))
                putting, request = await self.submit(self.bot.put_into_container(0, index))
                place, = request.actions
                self.assertIsInstance(place, b.ItemStackRequestCereal.PlaceActionData)
                self.assertEqual(self.address(place.source), (PLAYER, 0, source_net))
                self.assertEqual(self.address(place.destination), (name, index, 0))
                request_id = int(request.client_request_id.raw_id)
                await self.deliver(b.ItemStackResponsePacket(responses=[self.verdict(request_id - 2, (name, index, 0, None))]))
                self.assertFalse(putting.done())
                await self.deliver(b.ItemStackResponsePacket(responses=[self.verdict(request_id,
                    (PLAYER, 0, 0, None), (name, index, 1, target_net),
                    (BOTTLE if name != BOTTLE else INGREDIENT, index, 0, None), (BOTTLE, 5, 0, None),
                )]))
                self.assertEqual((await putting).client_request_id.raw_id, request_id)
                item = self.bot.container_slots[index]
                self.assertEqual((item.id, item.stack_size, item.aux_value, item.net_id_variant),
                                 (item_id, 1, index, target_net))
                self.assertNotIn(5, self.bot.container_slots)

                taking, request = await self.submit(self.bot.take_from_container(index, 0))
                take, = request.actions
                self.assertIsInstance(take, b.ItemStackRequestCereal.TakeActionData)
                self.assertEqual(self.address(take.source), (name, index, target_net))
                self.assertEqual(self.address(take.destination), (PLAYER, 0, 0))
                request_id = int(request.client_request_id.raw_id)
                await self.deliver(b.ItemStackResponsePacket(responses=[self.verdict(request_id,
                    (name, index, 0, None), (PLAYER, 0, 1, source_net),
                )]))
                self.assertEqual((await taking).client_request_id.raw_id, request_id)
                self.assertEqual(self.bot.container_slots[index].id, 0)
                item = self.bot.inventory_slots[0]
                self.assertEqual((item.id, item.stack_size, item.aux_value, item.net_id_variant),
                                 (item_id, 1, index, source_net))

        before = list(self.wire.sent)
        for call in (self.bot.put_into_container(0, 5), self.bot.take_from_container(5, 0)):
            with self.assertRaisesRegex(ValueError, "brewing stand slots"):
                await call
        self.assertEqual(self.wire.sent, before)

    async def test_brewing_snapshot_and_waiters_track_first_matching_report(self):
        with self.assertRaisesRegex(RuntimeError, "no brewing stand"):
            await self.bot.wait_for_brewing_progress(timeout=1)
        await self.deliver(self.window_packet())
        window = self.bot.open_window
        original = self.bot.brewing_progress
        self.assertEqual(original, BrewingProgress(window))
        self.assertIsNone(self.bot.furnace_progress)
        first = asyncio.create_task(self.bot.wait_for_brewing_progress(timeout=1))
        second = asyncio.create_task(self.bot.wait_for_brewing_progress(timeout=1))
        await asyncio.sleep(0)
        await self.deliver(self.data(2, 80, window_id=3), self.data(7, 80))
        self.assertFalse(first.done())
        await self.deliver(self.data(0, 400), self.data(1, 20), self.data(2, 20), self.data(0, 399))
        reported, same_report = await asyncio.gather(first, second)
        self.assertEqual(reported, BrewingProgress(window, 400))
        self.assertEqual(reported, same_report)
        self.assertIsNot(reported, same_report)
        with self.assertRaises(FrozenInstanceError):
            reported.brew_ticks = 0
        self.assertEqual(original, BrewingProgress(window))
        self.assertEqual(self.bot.brewing_progress, BrewingProgress(window, 399, 20, 20))
        await self.deliver(self.data(0, 0), self.data(1, 19))
        self.assertEqual(self.bot.brewing_progress, BrewingProgress(window, 0, 19, 20))

    async def test_close_reopen_and_dimension_change_end_the_old_progress_wait(self):
        b = self.b
        await self.deliver(self.window_packet(), self.data(0, 300))
        old = self.bot.brewing_progress
        pending = asyncio.create_task(self.bot.wait_for_brewing_progress(timeout=1))
        await asyncio.sleep(0)
        await self.deliver(b.ContainerClosePacket(container_id=WINDOW, container_type=b.ContainerType.BREWING_STAND),
                           self.window_packet())
        with self.assertRaisesRegex(RuntimeError, "brewing stand window closed or changed"):
            await pending
        self.assertIsNot(self.bot.open_window, old.window)
        self.assertEqual(self.bot.brewing_progress, BrewingProgress(self.bot.open_window))
        self.assertEqual(old.brew_ticks, 300)

        subsequent = asyncio.create_task(self.bot.wait_for_brewing_progress(timeout=1))
        await asyncio.sleep(0)
        await self.deliver(b.ChangeDimensionPacket(dimension_id=1), self.data(0, 200))
        with self.assertRaisesRegex(RuntimeError, "brewing stand window closed or changed"):
            await subsequent
        self.assertIsNone(self.bot.brewing_progress)

        await self.deliver(self.window_packet("FURNACE"), self.data(1, 7))
        self.assertIsNone(self.bot.brewing_progress)
        self.assertEqual(self.bot.furnace_progress, FurnaceProgress(self.bot.open_window, burn_remaining_ticks=7))

    async def test_late_brewing_verdict_preserves_a_newer_stack_and_a_same_id_reopened_window(self):
        b = self.b
        empty = self.stack()
        await self.deliver(self.window_packet(), self.contents(empty, self.stack(402, 3, 20), empty, empty, empty))
        first, request = await self.submit(self.bot.take_from_container(1, 0))
        await self.deliver(b.InventorySlotPacket(inventory_id=WINDOW, slot=1, item=self.stack(402, 7, 20, aux=4)),
                           b.ItemStackResponsePacket(responses=[self.verdict(request.client_request_id.raw_id,
                               (BOTTLE, 1, 2, 20), (PLAYER, 0, 1, 21),
                           )]))
        self.assertEqual((await first).result.name, "SUCCESS")
        observed = self.bot.container_slots[1]
        self.assertEqual((observed.stack_size, observed.aux_value, observed.net_id_variant), (7, 4, 20))

        second, request = await self.submit(self.bot.take_from_container(1, 1))
        old = self.bot.open_window
        await self.deliver(b.ContainerClosePacket(container_id=WINDOW, container_type=b.ContainerType.BREWING_STAND),
                           self.window_packet(), self.contents(empty, self.stack(402, 7, 20, aux=4), empty, empty, empty))
        self.assertEqual(self.bot.open_window, old)
        self.assertIsNot(self.bot.open_window, old)
        await self.deliver(b.ItemStackResponsePacket(responses=[self.verdict(request.client_request_id.raw_id,
            (BOTTLE, 1, 6, 20), (PLAYER, 1, 1, 23),
        )]))
        self.assertEqual((await second).result.name, "SUCCESS")
        self.assertEqual(self.bot.container_slots[1].stack_size, 7)
        self.assertEqual(self.bot.inventory_slots[1].aux_value, 4)

    async def test_brewing_slot_observations_take_precedence_over_a_partial_verdict(self):
        b = self.b
        original = self.stack(402, 4, 50)
        changed = self.stack(402, 2, 50)
        replacement = self.stack(403, 4, 50)
        cases = (
            ("fuel used before take verdict", 4, False, (changed,), (402, 2), [(402, 2)]),
            ("ingredient used before put verdict", 0, True, (changed,), (402, 2), [(402, 2)]),
            ("slot replaced and restored", 0, False, (replacement, original), (402, 4), [(403, 4), (402, 4)]),
            ("matching observation", 4, False, (self.stack(402, 3, 50),), (402, 3), [(402, 3)]),
            ("equivalent re-report", 4, False, (original,), (402, 3), [(402, 3)]),
        )
        for case, index, putting, observations, after, expected_changes in cases:
            with self.subTest(case=case):
                name = FUEL if index == 4 else INGREDIENT
                slots = [self.stack() for _ in range(5)]
                slots[index] = original
                player = self.stack(402, 4, 60) if putting else self.stack()
                await self.deliver(self.window_packet(), self.contents(*slots), b.InventorySlotPacket(
                    inventory_id=b.ContainerID.INVENTORY, slot=0, item=player,
                ))
                observed = []
                self.bot.subscribe_container_slot_changes(observed.append, slot=index)
                transfer = (self.bot.put_into_container(0, index) if putting
                            else self.bot.take_from_container(index, 0))
                pending, request = await self.submit(transfer)
                await self.deliver(*(b.InventorySlotPacket(inventory_id=WINDOW, slot=index, item=item)
                                     for item in observations))
                await self.deliver(b.ItemStackResponsePacket(responses=[self.verdict(
                    request.client_request_id.raw_id, (name, index, 5 if putting else 3, 50),
                    (PLAYER, 0, 3 if putting else 1, 60),
                )]))
                self.assertEqual((await pending).client_request_id.raw_id, request.client_request_id.raw_id)
                current = self.bot.container_slots[index]
                self.assertEqual((current.id, current.stack_size), after)
                self.assertEqual([(event.after.item_id, event.after.count) for event in observed], expected_changes)
                self.assertEqual(self.bot.inventory_slots[0].stack_size, 3 if putting else 1)
