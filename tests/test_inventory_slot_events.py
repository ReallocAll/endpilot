import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from endpilot import Bot, HeldItemStack, InventorySlotChange
from endpilot.protocol import write_batch
from test_scheduled_input import ManualClock, Wire


class InventorySlotEventsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("SlotEvents", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)
        self.wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.wire.push(b.StartGamePacket(runtime_id=7, entity_id=17))
        self.wire.push(b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132")

    def item(self, count=0, net_id=None, *, item_id=None):
        return self.b.SerializedNetworkItemStackDescriptor(
            id=(17 if count else 0) if item_id is None else item_id, stack_size=count, net_id_variant=net_id,
        )

    def content(self, *slots, container="INVENTORY"):
        return self.b.InventoryContentPacket(inventory_id=self.b.ContainerID[container], slots=slots)

    def slot(self, index, item, container="INVENTORY"):
        return self.b.InventorySlotPacket(inventory_id=self.b.ContainerID[container], slot=index, item=item)

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        received = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in (*packets, marker)))
        await received

    def read_counts(self, *indices):
        slots = self.bot.inventory_slots
        return tuple(slots[index].stack_size if index in slots else None for index in indices)

    async def start_move(self, source, destination):
        fired = asyncio.Event()
        unsubscribe = self.bot.packets.subscribe_sent(
            lambda packet_id: fired.set() if packet_id == self.b.ItemStackRequestPacket.ID else None,
        )
        try:
            task = asyncio.create_task(self.bot.move_inventory_item(source, destination, timeout=2))
            self.addAsyncCleanup(self.cancel, task)
            await asyncio.wait_for(fired.wait(), 1)
            request, = self.wire.sent[-1].requests
            return task, request
        finally:
            unsubscribe()

    @staticmethod
    async def cancel(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def verdict(self, request, *corrections):
        b = self.b
        return b.ItemStackResponseInfo(
            client_request_id=request.client_request_id, result=b.ItemStackNetResult.SUCCESS,
            containers=[b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName.COMBINED_HOTBAR_AND_INVENTORY_CONTAINER),
                slots=[b.ItemStackResponseSlotInfo(requested_slot=index, slot=index, amount=count,
                                                     item_stack_net_id=b.ItemStackNetId(raw_id=net))
                       for index, count, net in corrections],
            )],
        )

    def transaction(self, *updates):
        b = self.b
        source = b.InventorySource(type=b.InventorySourceType.CONTAINER_INVENTORY, container_id=b.ContainerID.INVENTORY)
        return b.InventoryTransactionPacket(legacy_request_id=b.ItemStackLegacyRequestId(raw_id=0),
            variant_transaction=b.NormalTransactionData(transaction=b.InventoryTransaction(actions=[
                b.InventoryAction(source=source, slot=index,
                                  from_item_descriptor=self.item(old, net), to_item_descriptor=self.item(new, net))
                for index, old, new, net in updates
            ])))

    async def test_content_commits_before_callbacks_and_removes_omitted_observations(self):
        changes, filtered = [], []
        subscribe = self.bot.subscribe_inventory_changes
        unsubscribe = subscribe(lambda change: changes.append((change, self.read_counts(0, 7, 35))))
        stop_one = subscribe(filtered.append, slot=7)
        for invalid in (-1, 36, True, "7"):
            with self.assertRaisesRegex(ValueError, "0..35"):
                subscribe(filtered.append, slot=invalid)
        self.assertEqual(changes, [])
        sent_before = list(self.wire.sent)
        slots = [self.item() for _ in range(36)]
        slots[7], slots[35] = self.item(3, 20), self.item(2, 21)
        await self.deliver(self.content(*slots))
        self.assertEqual([change.slot for change, _ in changes], list(range(36)))
        self.assertEqual({counts for _, counts in changes}, {(0, 3, 2)})
        self.assertEqual(changes[0][0], InventorySlotChange(0, None, HeldItemStack(0, 0, 0, None, 0, b"")))
        self.assertEqual(filtered, [changes[7][0]])
        with self.assertRaises(FrozenInstanceError):
            filtered[0].slot = 0
        with self.assertRaises(FrozenInstanceError):
            filtered[0].after.count = 0

        await self.deliver(self.content(*slots), self.slot(7, self.item(3, 20)),
                           self.slot(36, self.item(4, 25)), self.content(self.item(2, 24), container="ARMOR"))
        self.assertEqual(len(changes), 36)
        stop_one()
        stop_one()
        await self.deliver(self.content(*slots[:7], self.item(1, 20)))
        self.assertEqual(changes[36][0].slot, 7)
        self.assertIsNone(changes[-1][0].after)
        self.assertEqual(changes[-1][0].slot, 35)
        self.assertEqual(len(filtered), 1)
        unsubscribe()
        unsubscribe()
        await self.deliver(self.slot(7, self.item(2, 20)))
        self.assertEqual(changes[-1][0].slot, 35)
        self.assertEqual(self.wire.sent, sent_before)

    async def test_one_inventory_transaction_coalesces_repeated_slots(self):
        b = self.b
        await self.deliver(self.content(self.item(3, 20), self.item(1, 21), self.item(5, 22)))
        events = []
        self.bot.subscribe_inventory_changes(lambda change: events.append((change, self.read_counts(0, 1, 2))))

        def action(index, before, after, net, container="INVENTORY"):
            return b.InventoryAction(source=b.InventorySource(type=b.InventorySourceType.CONTAINER_INVENTORY,
                container_id=b.ContainerID[container]), slot=index,
                from_item_descriptor=self.item(before, net), to_item_descriptor=self.item(after, net))

        actions = [action(0, 3, 2, 20), action(1, 1, 2, 21), action(0, 2, 1, 20),
                   action(2, 5, 6, 22), action(2, 6, 5, 22), action(0, 1, 2, 25, container="OFFHAND")]
        await self.deliver(b.InventoryTransactionPacket(
            legacy_request_id=b.ItemStackLegacyRequestId(raw_id=0),
            variant_transaction=b.NormalTransactionData(transaction=b.InventoryTransaction(actions=actions)),
        ))
        self.assertEqual([(r.slot, r.before.count, r.after.count, state) for r, state in events], [
            (0, 3, 1, (1, 2, 5)), (1, 1, 2, (1, 2, 5)),
        ])

    async def test_public_moves_publish_each_matching_verdict_in_the_same_packet(self):
        b = self.b
        await self.deliver(self.content(self.item(5, 20), self.item(2, 21), self.item(4, 22)))
        events = []
        self.bot.subscribe_inventory_changes(lambda change: events.append((change, self.read_counts(0, 1, 2))))
        first, take_first = await self.start_move(0, 1)
        second, take_second = await self.start_move(2, 1)
        self.assertFalse(events)
        unmatched = self.verdict(take_first, (0, 6, 20))
        unmatched.client_request_id = b.ItemStackRequestId(raw_id=-99)
        await self.deliver(b.ItemStackResponsePacket(responses=[unmatched,
            self.verdict(take_first, (0, 4, 20), (1, 3, 21)), self.verdict(take_second, (2, 3, 22), (1, 4, 21)),
        ]))
        self.assertEqual((await first).result.name, "SUCCESS")
        self.assertEqual((await second).result.name, "SUCCESS")
        self.assertEqual([(r.slot, r.before.count, r.after.count, state) for r, state in events], [
            (0, 5, 4, (4, 3, 4)), (1, 2, 3, (4, 3, 4)),
            (1, 3, 4, (4, 4, 3)), (2, 4, 3, (4, 4, 3)),
        ])

    async def test_prediction_and_selection_are_not_inventory_changes(self):
        await self.deliver(self.content(self.item(3, 20), self.item(2, 21)))
        observed = []
        self.bot.subscribe_inventory_changes(observed.append)
        await self.bot.select_hotbar(1)
        await self.bot.select_hotbar(0)
        await self.bot.drop_selected_item()
        self.assertEqual(self.bot.held_item.predicted_count, 2)
        self.assertEqual(self.read_counts(0), (3,))
        self.assertFalse(observed)
        await self.deliver(self.slot(0, self.item(2, 20)))
        change, = observed
        self.assertEqual((change.slot, change.before.count, change.after.count), (0, 3, 2))

    async def test_absent_observers_skip_stack_projection_and_disconnect_clears_subscriptions(self):
        with patch("endpilot.inventory._stack_snapshot", side_effect=AssertionError("unexpected snapshot")) as snapshot:
            await self.deliver(self.content(self.item()), self.slot(0, self.item(3, 20)))
            snapshot.assert_not_called()
        observed = []
        unsubscribe = self.bot.subscribe_inventory_changes(observed.append)
        await self.deliver(self.b.DisconnectPacket(), self.slot(0, self.item(4, 20)))
        self.assertFalse(observed)
        unsubscribe()
        with self.assertRaisesRegex(ConnectionError, "inventory observation ended"):
            self.bot.subscribe_inventory_changes(observed.append)

    async def test_observer_failure_is_reported_and_the_next_observer_receives_the_change(self):
        loop = asyncio.get_running_loop()
        errors, observed = [], []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, details: errors.append(details["exception"]))
        self.addCleanup(loop.set_exception_handler, previous)

        def cancel(_change):
            raise asyncio.CancelledError("subscriber cancelled")

        self.bot.subscribe_inventory_changes(cancel, slot=0)
        self.bot.subscribe_inventory_changes(observed.append, slot=0)
        await self.deliver(self.slot(0, self.item(3, 20)))
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], asyncio.CancelledError)
        self.assertEqual(observed[0].after.count, 3)

    async def test_publication_excludes_new_subscribers_and_respects_mid_group_unsubscribe(self):
        before_group, added_inside_group = [], []
        unsubscribe = self.bot.subscribe_inventory_changes(before_group.append, slot=1)

        def change_slot_zero(_event):
            unsubscribe()
            unsubscribe()
            self.bot.subscribe_inventory_changes(added_inside_group.append, slot=1)

        self.bot.subscribe_inventory_changes(change_slot_zero, slot=0)
        await self.deliver(self.content(self.item(2, 21), self.item(3, 22)))
        self.assertEqual(before_group, [])
        self.assertEqual(added_inside_group, [])
        await self.deliver(self.slot(1, self.item(4, 22)))
        change, = added_inside_group
        self.assertEqual((change.slot, change.before.count, change.after.count), (1, 3, 4))

    async def test_sparse_updates_do_not_copy_the_inventory_or_scan_unrelated_slots(self):
        class TrackedSlots(dict):
            def __init__(self, values):
                super().__init__(values)
                self.copy_calls = 0
                self.reads = []

            def copy(self):
                self.copy_calls += 1
                return super().copy()

            def get(self, key, default=None):
                self.reads.append(key)
                return super().get(key, default)

        slots = [self.item() for _ in range(36)]
        slots[4], slots[7] = self.item(2, 20), self.item(3, 21)
        await self.deliver(self.content(*slots))
        actual = self.bot._inventory
        backing = TrackedSlots(actual.slots)
        observed = []
        self.bot.subscribe_inventory_changes(observed.append, slot=7)
        with patch.object(actual, "_slots", backing):
            await self.deliver(self.slot(4, self.item(3, 20)))
            self.assertEqual(backing.copy_calls, 0)
            self.assertEqual(backing.reads, [])
            self.assertEqual(observed, [])

            await self.deliver(self.transaction((4, 3, 4, 20), (7, 3, 2, 21), (7, 2, 1, 21)))
            self.assertEqual(backing.copy_calls, 0)
            self.assertEqual(backing.reads, [7, 7])
            change, = observed
            self.assertEqual((change.before.count, change.after.count), (3, 1))

            move, request = await self.start_move(7, 8)
            backing.reads.clear()
            await self.deliver(self.b.ItemStackResponsePacket(responses=[self.verdict(request, (7, 0, 21), (8, 1, 23))]))
            self.assertEqual((await move).result.name, "SUCCESS")
            self.assertEqual(backing.copy_calls, 0)
            self.assertLessEqual(set(backing.reads), {7, 8})
        self.assertEqual([(change.before.count, change.after.count) for change in observed], [(3, 1), (1, 0)])
