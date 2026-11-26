import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from endpilot import Bot, ContainerSlotChange, HeldItemStack
from endpilot.inventory import _stack_snapshot
from endpilot.protocol import write_batch

from test_scheduled_input import ManualClock, Wire


WINDOW_ID = 5
CHEST = "LEVEL_ENTITY_CONTAINER"
PLAYER = "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"


class ContainerSlotEventsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("ContainerSlotEvents", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)
        self.wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.wire.push(b.StartGamePacket(runtime_id=7, entity_id=17))
        self.wire.push(b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    def item(self, count=0, net_id=None, *, item_id=None, user_data=b""):
        return self.b.SerializedNetworkItemStackDescriptor(
            id=(17 if count else 0) if item_id is None else item_id, stack_size=count,
            net_id_variant=net_id, user_data_buffer=user_data,
        )

    def open_packet(self, kind="CONTAINER", *, inventory_id=WINDOW_ID):
        b = self.b
        return b.ContainerOpenPacket(container_id=inventory_id, type=b.ContainerType[kind],
                                     pos=b.BlockPos(x=1, y=100, z=0), entity_unique_id=-1)

    def content(self, *slots, inventory_id=WINDOW_ID):
        return self.b.InventoryContentPacket(inventory_id=inventory_id, slots=slots)

    def slot(self, index, item, *, inventory_id=WINDOW_ID):
        return self.b.InventorySlotPacket(inventory_id=inventory_id, slot=index, item=item)

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        received = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in (*packets, marker)))
        await received

    def counts(self, *indices):
        slots = self.bot.container_slots
        return tuple(slots[index].stack_size if index in slots else None for index in indices)

    def transaction(self, *updates):
        b = self.b
        return b.InventoryTransactionPacket(
            legacy_request_id=b.ItemStackLegacyRequestId(raw_id=0),
            variant_transaction=b.NormalTransactionData(transaction=b.InventoryTransaction(actions=[
                b.InventoryAction(
                    source=b.InventorySource(type=b.InventorySourceType.CONTAINER_INVENTORY, container_id=inventory),
                    slot=index, from_item_descriptor=self.item(old, net), to_item_descriptor=self.item(new, net),
                ) for inventory, index, old, new, net in updates
            ])),
        )

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

    def response(self, request, *corrections):
        b = self.b
        return b.ItemStackResponseInfo(
            client_request_id=request.client_request_id,
            result=b.ItemStackNetResult.SUCCESS,
            containers=[b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName[side]),
                slots=[b.ItemStackResponseSlotInfo(
                    requested_slot=index, slot=index, amount=count,
                    item_stack_net_id=None if net is None else b.ItemStackNetId(raw_id=net),
                )],
            ) for side, index, count, net in corrections],
        )

    async def test_content_is_atomic_and_distinguishes_unknown_from_empty(self):
        subscribe = self.bot.subscribe_container_slot_changes
        with self.assertRaisesRegex(RuntimeError, "no container is open"):
            subscribe(lambda _: None)
        await self.deliver(self.open_packet())
        window = self.bot.open_window
        self.assertIsNone(self.bot.container_slots)
        events, selected = [], []
        unsubscribe = subscribe(lambda change: events.append((change, self.counts(0, 1))))
        cancel = subscribe(selected.append, slot=1)
        with self.assertRaises(ValueError):
            subscribe(selected.append, slot=True)
        sent = list(self.wire.sent)
        await self.deliver(self.content(self.item(), self.item(3, 20, user_data=b"metadata")))
        self.assertEqual([observed for _, observed in events], [(0, 3), (0, 3)])
        self.assertIs(events[0][0].window, window)
        self.assertEqual(events[0][0], ContainerSlotChange(window, 0, None, HeldItemStack(0, 0, 0, None, 0, b"")))
        self.assertEqual(selected, [ContainerSlotChange(window, 1, None, HeldItemStack(17, 3, 0, 20, 0, b"metadata"))])
        with self.assertRaises(FrozenInstanceError):
            selected[0].slot = 0

        await self.deliver(self.content(self.item(), self.item(3, 20, user_data=b"metadata")),
                           self.slot(1, self.item(3, 20, user_data=b"metadata")),
                           self.slot(1, self.item(4, 21), inventory_id=self.b.ContainerID.INVENTORY))
        self.assertEqual(len(events), 2)
        await self.deliver(self.content(self.item()))
        self.assertEqual((events[-1][0].slot, events[-1][0].before.count, events[-1][0].after), (1, 3, None))
        cancel()
        cancel()
        unsubscribe()
        unsubscribe()
        await self.deliver(self.slot(0, self.item(2, 24)))
        self.assertEqual((len(events), len(selected)), (3, 2))
        self.assertEqual(self.wire.sent, sent)

    async def test_transaction_coalesces_slots_and_freezes_the_subscriber_group(self):
        await self.deliver(self.open_packet(), self.content(self.item(3, 20), self.item(5, 21), self.item(7, 22)))
        events, old_subscriber, new_subscriber = [], [], []
        subscribe = self.bot.subscribe_container_slot_changes
        unsubscribe = subscribe(old_subscriber.append, slot=1)
        subscribe(lambda change: events.append((change, self.counts(0, 1, 2))))

        def on_first_change(_change):
            unsubscribe()
            subscribe(new_subscriber.append, slot=1)

        subscribe(on_first_change, slot=0)
        await self.deliver(self.transaction((WINDOW_ID, 0, 3, 2, 20), (WINDOW_ID, 1, 5, 6, 21),
                                            (WINDOW_ID, 0, 2, 1, 20), (WINDOW_ID, 2, 7, 8, 22),
                                            (WINDOW_ID, 2, 8, 7, 22)))
        self.assertEqual([(event.slot, event.before.count, event.after.count, state) for event, state in events], [
            (0, 3, 1, (1, 6, 7)), (1, 5, 6, (1, 6, 7)),
        ])
        self.assertEqual((old_subscriber, new_subscriber), ([], []))
        await self.deliver(self.slot(1, self.item(4, 21)))
        self.assertEqual([(event.slot, event.before.count, event.after.count) for event in new_subscriber], [(1, 6, 4)])

    async def test_matched_verdicts_publish_after_both_caches_and_skip_late_windows(self):
        b = self.b
        await self.deliver(self.open_packet(), self.content(self.item(5, 21), self.item(4, 22)),
                           self.content(self.item(), self.item(), inventory_id=b.ContainerID.INVENTORY))
        observed = []
        self.bot.subscribe_container_slot_changes(lambda change: observed.append((
            change, self.counts(0, 1), tuple(self.bot.inventory_slots[i].stack_size for i in (0, 1)),
        )))
        first, a = await self.submit(self.bot.take_from_container(0, 0, 2))
        second, c = await self.submit(self.bot.take_from_container(1, 1, 2))
        self.assertFalse(observed)
        stray = self.response(a, (CHEST, 0, 1, 21))
        stray.client_request_id = b.ItemStackRequestId(raw_id=-99)
        await self.deliver(b.ItemStackResponsePacket(responses=[stray,
            self.response(a, (CHEST, 0, 3, 21), (PLAYER, 0, 2, 24)),
            self.response(c, (PLAYER, 1, 2, 25), (CHEST, 1, 2, 22)),
        ]))
        self.assertEqual((await first).result.name, "SUCCESS")
        self.assertEqual((await second).result.name, "SUCCESS")
        self.assertEqual([(event.slot, event.before.count, event.after.count, box, bag)
                          for event, box, bag in observed], [(0, 5, 3, (3, 4), (2, 0)), (1, 4, 2, (3, 2), (2, 2))])
        late, request = await self.submit(self.bot.take_from_container(0, 0, 1))
        old_window = self.bot.open_window
        await self.deliver(self.open_packet(), self.content(self.item(6, 42)))
        self.assertEqual(self.bot.open_window, old_window)
        self.assertIsNot(self.bot.open_window, old_window)
        replacement = []
        self.bot.subscribe_container_slot_changes(replacement.append)
        await self.deliver(b.ItemStackResponsePacket(responses=[self.response(request, (CHEST, 0, 2, 21))]))
        self.assertEqual((await late).result.name, "SUCCESS")
        self.assertEqual(self.counts(0), (6,))
        self.assertEqual((len(observed), replacement), (2, []))

    async def test_furnace_smelting_updates_arrive_before_pending_fuel_take_verdict(self):
        b = self.b
        fuel = "FURNACE_FUEL_CONTAINER"
        result = "FURNACE_RESULT_CONTAINER"
        raw_iron, coal, iron_ingot = 516, 304, 307
        ore_net_id, fuel_net_id, product_net_id = 11, 12, 3
        await self.deliver(
            self.open_packet("FURNACE"),
            self.content(self.item(4, ore_net_id, item_id=raw_iron),
                         self.item(2, fuel_net_id, item_id=coal), self.item()),
            self.slot(3, self.item(), inventory_id=b.ContainerID.INVENTORY),
            self.slot(4, self.item(), inventory_id=b.ContainerID.INVENTORY),
        )
        self.assertEqual(self.counts(0, 1, 2), (4, 2, 0))
        observed = []
        self.bot.subscribe_container_slot_changes(lambda change: observed.append((
            change.slot, change.before.count, change.after.count, self.counts(0, 1, 2),
        )))

        taking_coal, fuel_request = await self.submit(self.bot.take_from_container(1, 3, amount=1))
        fuel_take, = fuel_request.actions
        self.assertIsInstance(fuel_take, b.ItemStackRequestCereal.TakeActionData)
        self.assertEqual((fuel_take.amount, fuel_take.source.full_container_name.name.name,
                          fuel_take.source.slot, fuel_take.source.net_id_variant), (1, fuel, 1, fuel_net_id))
        self.assertEqual((fuel_take.destination.full_container_name.name.name, fuel_take.destination.slot),
                         (PLAYER, 3))
        await self.deliver(
            self.slot(0, self.item(3, ore_net_id, item_id=raw_iron)),
            self.slot(2, self.item(1, product_net_id, item_id=iron_ingot)),
        )
        self.assertFalse(taking_coal.done())
        ingredient_update = (0, 4, 3, (3, 2, 0))
        result_update = (2, 0, 1, (3, 2, 1))
        self.assertEqual(observed, [ingredient_update, result_update])
        self.assertEqual(self.counts(0, 1, 2), (3, 2, 1))
        self.assertEqual(self.bot.inventory_slots[3].stack_size, 0)

        await self.deliver(b.ItemStackResponsePacket(responses=[self.response(
            fuel_request, (fuel, 1, 1, fuel_net_id), (PLAYER, 3, 1, 13),
        )]))
        self.assertEqual((await taking_coal).result.name, "SUCCESS")
        self.assertEqual(observed, [ingredient_update, result_update, (1, 2, 1, (3, 1, 1))])
        self.assertEqual(self.counts(0, 1, 2), (3, 1, 1))
        carried_coal = self.bot.inventory_slots[3]
        self.assertEqual((carried_coal.id, carried_coal.stack_size, carried_coal.net_id_variant), (coal, 1, 13))

        taking_product, result_request = await self.submit(self.bot.take_from_container(2, 4, amount=1))
        result_take, = result_request.actions
        self.assertIsInstance(result_take, b.ItemStackRequestCereal.TakeActionData)
        self.assertEqual((result_take.amount, result_take.source.full_container_name.name.name,
                          result_take.source.slot, result_take.source.net_id_variant), (1, result, 2, product_net_id))
        self.assertEqual((result_take.destination.full_container_name.name.name, result_take.destination.slot),
                         (PLAYER, 4))
        await self.deliver(b.ItemStackResponsePacket(responses=[self.response(
            result_request, (result, 2, 0, None), (PLAYER, 4, 1, 14),
        )]))
        self.assertEqual((await taking_product).result.name, "SUCCESS")
        self.assertEqual(self.counts(0, 1, 2), (3, 1, 0))
        carried_product = self.bot.inventory_slots[4]
        self.assertEqual((carried_product.id, carried_product.stack_size, carried_product.net_id_variant),
                         (iron_ingot, 1, 14))

    async def test_incidental_verdict_requires_a_matching_stack_identity(self):
        b = self.b
        await self.deliver(self.open_packet(), self.content(
            self.item(5, 21, user_data=b"requested stack"),
            self.item(2, 75, item_id=27, user_data=b"old sticks"),
            self.item(), self.item(6, 81, item_id=30, user_data=b"same stack"),
        ), self.slot(9, self.item(), inventory_id=b.ContainerID.INVENTORY))
        window = self.bot.open_window
        observed = []
        self.bot.subscribe_container_slot_changes(observed.append)
        taking, request = await self.submit(self.bot.take_from_container(0, 9, 2))
        await self.deliver(b.ItemStackResponsePacket(responses=[self.response(request,
            (CHEST, 0, 3, 21), (PLAYER, 9, 2, 41),
            (CHEST, 1, 4, 99), (CHEST, 2, 4, 100), (CHEST, 3, 7, 81), (CHEST, 4, 0, None),
        )]))
        self.assertEqual((await taking).result.name, "SUCCESS")
        self.assertEqual(self.counts(0, 1, 2, 3, 4), (3, None, None, 7, 0))
        self.assertEqual(self.bot.inventory_slots[9].user_data_buffer, b"requested stack")
        self.assertEqual(observed[1:], [
            ContainerSlotChange(window, 1, HeldItemStack(27, 2, 0, 75, 0, b"old sticks"), None),
            ContainerSlotChange(window, 2, HeldItemStack(0, 0, 0, None, 0, b""), None),
            ContainerSlotChange(window, 3, HeldItemStack(30, 6, 0, 81, 0, b"same stack"),
                                HeldItemStack(30, 7, 0, 81, 0, b"same stack")),
            ContainerSlotChange(window, 4, None, HeldItemStack(0, 0, 0, None, 0, b"")),
        ])

        await self.deliver(self.slot(1, self.item(4, 99, item_id=39, user_data=b"new stack")))
        self.assertEqual(observed[-1], ContainerSlotChange(
            window, 1, None, HeldItemStack(39, 4, 0, 99, 0, b"new stack"),
        ))

    async def test_anvil_aliases_and_verdicts_use_logical_slots(self):
        b = self.b
        await self.deliver(self.open_packet("ANVIL"))
        observed = []
        self.bot.subscribe_container_slot_changes(lambda change: observed.append((change, self.counts(1, 2))))
        await self.deliver(self.content(self.item(1, 20), self.item(3, 21)),
                           self.slot(2, self.item(2, 21), inventory_id=b.ContainerID.PLAYER_ONLY_UI),
                           self.slot(1, self.item(2, 21)),
                           self.slot(0, self.item(), inventory_id=b.ContainerID.INVENTORY))
        self.assertEqual([(change.slot, change.after.count, counts) for change, counts in observed], [
            (1, 1, (1, 3)), (2, 3, (1, 3)), (2, 2, (1, 2)),
        ])
        applying, request = await self.submit(self.bot.apply_anvil(0, "New name"))
        await self.deliver(b.ItemStackResponsePacket(responses=[self.response(request,
            ("ANVIL_INPUT_CONTAINER", 1, 0, None), ("ANVIL_MATERIAL_CONTAINER", 2, 1, 21), (PLAYER, 0, 1, 29),
        )]))
        self.assertEqual((await applying).result.name, "SUCCESS")
        self.assertEqual([(change.slot, change.after.count, counts) for change, counts in observed[3:]], [
            (1, 0, (0, 1)), (2, 1, (0, 1)),
        ])

    async def test_scope_ends_on_close_dimension_and_disconnect(self):
        b = self.b
        close = b.ContainerClosePacket(container_id=WINDOW_ID, container_type=b.ContainerType.CONTAINER,
                                       server_initiated_close=False)
        for invalidate in (close, b.ChangeDimensionPacket(dimension_id=1), b.DisconnectPacket()):
            await self.deliver(self.open_packet(), self.content(self.item()))
            seen = []
            unsubscribe = self.bot.subscribe_container_slot_changes(seen.append)
            await self.deliver(invalidate, self.slot(0, self.item(9, 24)))
            with self.assertRaisesRegex(RuntimeError, "no container is open"):
                self.bot.subscribe_container_slot_changes(seen.append)
            await self.deliver(self.open_packet(), self.content(self.item(3, 21)))
            self.assertFalse(seen)
            unsubscribe()
            unsubscribe()

    async def test_sparse_observers_only_project_changed_subscribed_slots(self):
        await self.deliver(self.open_packet())
        initial = [self.item(1, index + 20) for index in range(10)]
        later = [self.item(2, index + 20) for index in range(10)]
        with patch("endpilot.containers._stack_snapshot", wraps=_stack_snapshot) as snapshot:
            await self.deliver(self.content(*initial))
            snapshot.assert_not_called()
            seen = []
            self.bot.subscribe_container_slot_changes(seen.append, slot=7)
            await self.deliver(self.slot(4, self.item(8, 24)))
            snapshot.assert_not_called()
            await self.deliver(self.content(*later))
            self.assertEqual(snapshot.call_count, 2)
            self.assertEqual([(change.slot, change.before.count, change.after.count) for change in seen], [(7, 1, 2)])

    async def test_take_preserves_unrelated_slot_updates_in_flight(self):
        await self.check_chest_transfer_interleave(into_chest=False)

    async def test_put_preserves_unrelated_slot_updates_in_flight(self):
        await self.check_chest_transfer_interleave(into_chest=True)

    async def check_chest_transfer_interleave(self, *, into_chest):
        b = self.b
        inventory = b.ContainerID.INVENTORY
        if into_chest:
            transfer = self.bot.put_into_container
            action_type = b.ItemStackRequestCereal.PlaceActionData
        else:
            transfer = self.bot.take_from_container
            action_type = b.ItemStackRequestCereal.TakeActionData

        async def submit_transfer(chest_slot, chest_net, player_slot, player_net, amount):
            chest = (CHEST, chest_slot, chest_net)
            player = (PLAYER, player_slot, player_net)
            source, destination = (player, chest) if into_chest else (chest, player)
            task, request = await self.submit(transfer(source[1], destination[1], amount=amount))
            action, = request.actions
            self.assertIsInstance(action, action_type)
            self.assertEqual(action.amount, amount)
            for endpoint, expected in ((action.source, source), (action.destination, destination)):
                self.assertEqual((endpoint.full_container_name.name.name, endpoint.slot, endpoint.net_id_variant),
                                 expected)
            return task, request

        def snapshot():
            chest, player = self.bot.container_slots, self.bot.inventory_slots
            return tuple((stack.id, stack.stack_size, stack.net_id_variant)
                         for stack in (chest[1], player[9], chest[4], player[12]))

        contents = [self.item() for _ in range(27)]
        contents[1] = self.item(5, 31)
        contents[4] = self.item(3, 51, item_id=27)
        await self.deliver(
            self.open_packet(), self.content(*contents),
            self.slot(9, self.item(4, 41), inventory_id=inventory),
            self.slot(12, self.item(2, 61, item_id=27), inventory_id=inventory),
        )
        task, request = await submit_transfer(1, 31, 9, 41, amount=2)
        self.assertEqual(snapshot(), ((17, 5, 31), (17, 4, 41), (27, 3, 51), (27, 2, 61)))

        await self.deliver(self.slot(4, self.item(7, 52, item_id=30)))
        self.assertFalse(task.done())
        self.assertEqual(snapshot(), ((17, 5, 31), (17, 4, 41), (30, 7, 52), (27, 2, 61)))

        await self.deliver(self.slot(12, self.item(6, 62, item_id=30), inventory_id=inventory))
        self.assertFalse(task.done())
        interleaved = ((17, 5, 31), (17, 4, 41), (30, 7, 52), (30, 6, 62))
        self.assertEqual(snapshot(), interleaved)

        stray = self.response(request, (CHEST, 1, 4, 31), (PLAYER, 9, 5, 41))
        stray.client_request_id = b.ItemStackRequestId(raw_id=-99)
        await self.deliver(b.ItemStackResponsePacket(responses=[stray]))
        self.assertFalse(task.done())
        self.assertEqual(snapshot(), interleaved)

        chest_count, player_count = (7, 2) if into_chest else (3, 6)
        await self.deliver(b.ItemStackResponsePacket(responses=[self.response(request,
            (CHEST, 1, chest_count, 31), (PLAYER, 9, player_count, 41),
        )]))
        verdict = await asyncio.wait_for(task, 1)
        self.assertEqual((verdict.client_request_id.raw_id, verdict.result.name),
                         (request.client_request_id.raw_id, "SUCCESS"))
        first_pair = ((17, chest_count, 31), (17, player_count, 41))
        self.assertEqual(snapshot(), first_pair + interleaved[2:])

        next_task, next_request = await submit_transfer(4, 52, 12, 62, amount=1)
        self.assertNotEqual(request.client_request_id.raw_id, next_request.client_request_id.raw_id)
        chest_count, player_count = (8, 5) if into_chest else (6, 7)
        await self.deliver(b.ItemStackResponsePacket(responses=[self.response(next_request,
            (CHEST, 4, chest_count, 52), (PLAYER, 12, player_count, 62),
        )]))
        verdict = await asyncio.wait_for(next_task, 1)
        self.assertEqual((verdict.client_request_id.raw_id, verdict.result.name),
                         (next_request.client_request_id.raw_id, "SUCCESS"))
        self.assertEqual(snapshot(), first_pair + ((30, chest_count, 52), (30, player_count, 62)))
