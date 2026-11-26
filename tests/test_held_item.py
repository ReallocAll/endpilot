import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from endpilot import Bot, HeldItemSnapshot, HeldItemStack
from endpilot.compression import PacketConnection
from endpilot.inventory import Inventory
from endpilot.protocol import Protocol, read_batch, write_batch
from test_scheduled_input import Wire


class HeldItemTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.protocol = Protocol(2225)
        self.b = self.protocol.binding
        self.wire = Wire(self.protocol)
        settings = self.b.NetworkSettingsPacket(compression_algorithm=self.b.PacketCompressionAlgorithm.NONE)
        self.packets = PacketConnection(self.wire, self.protocol, settings)
        self.inventory = Inventory(self.packets, 7)
        self.packets.start_receiving()
        self.reader_failure = None
        self.addAsyncCleanup(self.close_packets)
        self.addCleanup(self.inventory.close)

    async def close_packets(self):
        try:
            await self.packets.close()
        except ConnectionError as error:
            if error is not self.reader_failure:
                raise

    def item(self, item_id=0, count=0, net=None):
        return self.b.SerializedNetworkItemStackDescriptor(id=item_id, stack_size=count, net_id_variant=net)

    def slot(self, number, item, container="INVENTORY"):
        return self.b.InventorySlotPacket(inventory_id=self.b.ContainerID[container], slot=number, item=item)

    def equipment(self, slot, item, actor=7):
        return self.b.MobEquipmentPacket(runtime_id=actor, item=item, slot=slot,
                                         selected_slot=slot, container_id=self.b.ContainerID.INVENTORY)

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket(time=41)
        handled = asyncio.create_task(self.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(self.protocol.encode(packet) for packet in (*packets, marker)))
        await handled

    async def wait_change(self):
        pending = asyncio.create_task(self.inventory.wait_for_held_item_change(timeout=1))
        await asyncio.sleep(0)
        return pending

    async def test_default_unobserved_and_known_empty_are_distinct(self):
        self.assertEqual(self.inventory.held_item, HeldItemSnapshot(0, "default", None, None))
        empty_wait = await self.wait_change()
        await self.deliver(self.slot(0, self.item()))
        empty = await empty_wait
        self.assertEqual(empty.observed_item, HeldItemStack(0, 0, 0, None, 0, b""))
        self.assertEqual(empty.selection_source, "default")
        self.assertIsNone(empty.predicted_count)
        with self.assertRaises(FrozenInstanceError):
            empty.slot = 1
        with self.assertRaises(FrozenInstanceError):
            empty.observed_item.count = 3
        await self.deliver(self.slot(0, self.item(17, 2, 22)))
        self.assertEqual(empty.observed_item.count, 0)
        self.assertEqual(self.inventory.held_item.observed_item.count, 2)

    async def test_local_select_and_independent_server_confirmation(self):
        await self.deliver(self.slot(0, self.item(3, 1, 19)), self.slot(1, self.item(17, 2, 22)))
        local_wait = await self.wait_change()
        await self.inventory.select_hotbar(1)
        selected = await local_wait
        self.assertEqual((selected.slot, selected.selection_source, selected.observed_item.count), (1, "local", 2))
        sent, = self.wire.sent
        self.assertIsInstance(sent, self.b.MobEquipmentPacket)
        self.assertEqual((sent.selected_slot, sent.item.net_id_variant), (1, 22))

        server_wait = await self.wait_change()
        await self.inventory.select_hotbar(1)
        await self.deliver(
            self.slot(0, self.item(4, 1, 23)),
            self.slot(0, self.item(358, 1, 24), container="OFFHAND"),
            self.slot(1, self.item(17, 2, 22)),
            self.b.PlayerHotbarPacket(selected_slot=0, container_id=self.b.ContainerID.INVENTORY, should_select_slot=False),
            self.equipment(0, self.item(3, 1), actor=8),
        )
        self.assertFalse(server_wait.done())
        await self.deliver(self.equipment(1, self.item(17, 2)))
        confirmed = await server_wait
        self.assertEqual((confirmed.slot, confirmed.selection_source), (1, "server"))
        self.assertEqual(confirmed.observed_item, selected.observed_item)
        repeat_wait = await self.wait_change()
        await self.inventory.select_hotbar(1)
        await self.deliver(self.equipment(1, self.item(17, 2)))
        self.assertEqual(self.inventory.held_item, confirmed)
        self.assertFalse(repeat_wait.done())
        await self.deliver(self.equipment(0, self.item(4, 1)))
        corrected = await repeat_wait
        self.assertEqual((corrected.slot, corrected.selection_source, corrected.observed_item.item_id), (0, "server", 4))

    async def test_each_packet_has_a_snapshot_even_in_the_same_batch(self):
        await self.deliver(self.slot(1, self.item(17, 2, 22)))
        await self.inventory.select_hotbar(1)
        first_wait, second_wait = await self.wait_change(), await self.wait_change()
        content = self.b.InventoryContentPacket(inventory_id=self.b.ContainerID.INVENTORY,
                                                slots=[self.item(), self.item(308, 3, 24)])
        await self.deliver(content, self.slot(1, self.item(308, 4, 24)))
        first, second = await first_wait, await second_wait
        self.assertEqual(first, second)
        self.assertEqual((first.slot, first.selection_source, first.observed_item.item_id,
                          first.observed_item.count), (1, "local", 308, 3))
        self.assertEqual(self.inventory.held_item.observed_item.count, 4)

    async def test_failed_sends_and_authoritative_correction_during_send(self):
        await self.deliver(self.slot(0, self.item(17, 2, 22)), self.slot(1, self.item(308, 3, 24)))
        baseline = self.inventory.held_item
        waiting = await self.wait_change()

        async def fail_send():
            raise OSError("send failed")

        self.wire.before_send = fail_send
        with self.assertRaisesRegex(OSError, "send failed"):
            await self.inventory.select_hotbar(1)
        with self.assertRaisesRegex(OSError, "send failed"):
            await self.inventory.drop_selected_item(False, SimpleNamespace(window=None))
        self.assertEqual(self.inventory.held_item, baseline)
        self.assertFalse(waiting.done())

        async def reconcile():
            await self.deliver(self.equipment(0, self.item(17, 2)))

        self.wire.before_send = reconcile
        await self.inventory.select_hotbar(1)
        corrected = await waiting
        self.assertEqual((corrected.slot, corrected.selection_source), (0, "server"))
        self.assertEqual(self.inventory.held_item, corrected)

    async def test_drop_prediction_and_server_observation_remain_independent(self):
        await self.deliver(self.slot(0, self.item(17, 2, 22)))
        controls = SimpleNamespace(window=None)
        one_wait = await self.wait_change()
        await self.inventory.drop_selected_item(False, controls)
        one = await one_wait
        self.assertEqual((one.observed_item.count, one.predicted_count), (2, 1))
        empty_wait = await self.wait_change()
        await self.inventory.drop_selected_item(True, controls)
        empty = await empty_wait
        self.assertEqual((empty.observed_item.count, empty.predicted_count), (2, 0))
        self.assertEqual(self.inventory.item_or_empty(0).id, 0)
        confirmed = await self.wait_change()
        await self.deliver(self.slot(0, self.item()))
        latest = await confirmed
        self.assertEqual(latest.observed_item.count, 0)
        self.assertIsNone(latest.predicted_count)

    async def test_offhand_content_is_independent_of_selected_slot_correction(self):
        b = self.b
        await self.deliver(self.slot(0, self.item(358, 1, 22)), self.slot(0, self.item(), container="OFFHAND"))
        controls = SimpleNamespace(window=SimpleNamespace(container_type="INVENTORY"))
        waiting = await self.wait_change()
        equip = asyncio.create_task(self.inventory.equip_offhand(0, containers=controls))
        async with asyncio.timeout(1):
            while not self.wire.sent:
                await asyncio.sleep(0)
        request, = self.wire.sent[-1].requests
        await self.deliver(self.slot(0, self.item(358, 1, 22), container="OFFHAND"))
        self.assertFalse(waiting.done())
        await self.deliver(b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=request.client_request_id, result=b.ItemStackNetResult.SUCCESS,
            containers=[b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName.HOTBAR_CONTAINER),
                slots=[b.ItemStackResponseSlotInfo(requested_slot=0, slot=0, amount=0)],
            )],
        )]))
        self.assertEqual((await equip).result, b.ItemStackNetResult.SUCCESS)
        held = await waiting
        self.assertEqual(held.observed_item.item_id, 0)
        self.assertEqual(self.inventory.offhand_item.id, 358)

    async def test_timeout_cancel_and_disconnect_end_pending_waits(self):
        with self.assertRaises(TimeoutError):
            await self.inventory.wait_for_held_item_change(timeout=0)
        cancelled = await self.wait_change()
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        pending = await self.wait_change()
        await self.deliver(self.b.DisconnectPacket(), self.slot(0, self.item(17, 2, 22)))
        with self.assertRaises(ConnectionError):
            await pending
        with self.assertRaises(ConnectionError):
            await self.inventory.wait_for_held_item_change(timeout=0)
        self.assertFalse(self.inventory._held_item_waiters)

    async def test_reader_loss_ends_pending_wait(self):
        pending = await self.wait_change()
        self.reader_failure = ConnectionError("peer lost")
        self.wire.incoming.put_nowait(self.reader_failure)
        with self.assertRaises(ConnectionError) as caught:
            await pending
        self.assertIs(caught.exception.__cause__, self.reader_failure)


class PublicHeldItemTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("HeldApiTest", protocol=2225)
        self.wire = Wire(self.bot.protocol)
        self.b = self.bot.protocol.binding
        self.addAsyncCleanup(self.bot.close)

    def item(self, item_id, count, net_id):
        return self.b.SerializedNetworkItemStackDescriptor(id=item_id, stack_size=count, net_id_variant=net_id)

    async def join(self, *extra_slots):
        b = self.b
        for packet in (
            b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
            b.StartGamePacket(runtime_id=7, entity_id=9),
            b.InventoryContentPacket(inventory_id=b.ContainerID.INVENTORY,
                                     slots=[self.item(3, 1, 19), self.item(17, 3, 22), *extra_slots]),
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        ):
            self.wire.push(packet)
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    async def pending_change(self):
        pending = asyncio.create_task(self.bot.wait_for_held_item_change(timeout=None))
        await asyncio.sleep(0)
        return pending

    async def test_public_held_view_matches_the_next_input_and_failed_send(self):
        await self.join()
        initial = self.bot.held_item
        self.assertIsInstance(initial, HeldItemSnapshot)
        self.assertIsInstance(initial.observed_item, HeldItemStack)
        self.assertEqual((initial.slot, initial.selection_source), (0, "default"))
        selected = await self.pending_change()
        await self.bot.select_hotbar(1)
        self.assertEqual(await asyncio.wait_for(selected, timeout=1), self.bot.held_item)
        self.assertEqual((self.bot.held_item.slot, self.bot.held_item.selection_source), (1, "local"))
        changed = await self.pending_change()
        await self.bot.drop_selected_item(False)
        first = await asyncio.wait_for(changed, timeout=1)
        self.assertEqual((first.observed_item.count, first.predicted_count), (3, 2))
        await self.bot.drop_selected_item(True)
        last, = (packet for packet in self.wire.sent if isinstance(packet, self.b.InventoryTransactionPacket)
                 and packet.variant_transaction.transaction.actions[-1].from_item_descriptor.stack_size == 2)
        slot = last.variant_transaction.transaction.actions[-1]
        self.assertEqual((slot.slot, slot.from_item_descriptor.id, slot.from_item_descriptor.net_id_variant),
                         (first.slot, first.observed_item.item_id, first.observed_item.net_id))
        self.assertEqual(slot.from_item_descriptor.stack_size, first.predicted_count)
        baseline = self.bot.held_item
        self.assertEqual((baseline.observed_item.count, baseline.predicted_count), (3, 0))

        original_send = self.wire.send

        async def fail_select(batch):
            if any(raw.packet_id == self.b.MobEquipmentPacket.ID for raw in read_batch(batch)):
                raise OSError("select failed")
            await original_send(batch)

        self.wire.send = fail_select
        pending = await self.pending_change()
        try:
            with self.assertRaisesRegex(OSError, "select failed"):
                await self.bot.select_hotbar(0)
            self.assertEqual(self.bot.held_item, baseline)
            self.assertFalse(pending.done())
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_public_wait_ends_on_disconnect_and_close(self):
        with self.assertRaisesRegex(RuntimeError, "has not joined"):
            _ = self.bot.held_item
        with self.assertRaisesRegex(RuntimeError, "has not joined"):
            await self.bot.wait_for_held_item_change(timeout=0)
        await self.join()
        pending = await self.pending_change()
        packets = (
            self.b.DisconnectPacket(),
            self.b.InventorySlotPacket(inventory_id=self.b.ContainerID.INVENTORY, slot=0,
                                       item=self.item(17, 4, 30)),
        )
        self.wire.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in packets))
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(pending, timeout=1)
        with self.assertRaises(ConnectionError):
            await self.bot.wait_for_held_item_change(timeout=0)
        await self.bot.close()
        with self.assertRaisesRegex(RuntimeError, "has not joined"):
            _ = self.bot.held_item

    async def test_public_wait_captures_first_complete_verdict_in_one_response_packet(self):
        b = self.b
        await self.join(self.item(0, 0, None), self.item(0, 0, None))
        await self.bot.select_hotbar(1)
        pending = await self.pending_change()
        moves = [asyncio.create_task(self.bot.move_inventory_item(1, slot, timeout=1)) for slot in (2, 3)]
        async with asyncio.timeout(1):
            while len(requests := [packet.requests[0] for packet in self.wire.sent
                                    if isinstance(packet, b.ItemStackRequestPacket)]) < 2:
                await asyncio.sleep(0)
        by_destination = {request.actions[0].destination.slot: request for request in requests}
        self.assertEqual(set(by_destination), {2, 3})

        def container(name, *corrected):
            return b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName[name]),
                slots=[b.ItemStackResponseSlotInfo(requested_slot=slot, slot=slot, amount=amount,
                                                  item_stack_net_id=b.ItemStackNetId(raw_id=net))
                       for slot, amount, net in corrected],
            )

        def verdict(destination, *containers):
            return b.ItemStackResponseInfo(
                client_request_id=by_destination[destination].client_request_id,
                result=b.ItemStackNetResult.SUCCESS, containers=list(containers),
            )

        self.wire.push(b.ItemStackResponsePacket(responses=[
            # The two aliases are applied within one verdict. Its final held count is 2.
            verdict(3, container("HOTBAR_CONTAINER", (1, 1, 22)),
                    container("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", (1, 2, 22), (3, 1, 24))),
            verdict(2, container("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", (1, 1, 22), (2, 1, 23))),
        ]))
        replies = await asyncio.gather(*moves)
        self.assertTrue(all(reply.result == b.ItemStackNetResult.SUCCESS for reply in replies))
        first = await asyncio.wait_for(pending, timeout=1)
        self.assertEqual(first.observed_item.count, 2)
        self.assertEqual(self.bot.held_item.observed_item.count, 1)
        self.assertEqual([(self.bot.inventory_slots[i].stack_size, self.bot.inventory_slots[i].net_id_variant)
                          for i in (2, 3)], [(1, 23), (1, 24)])
