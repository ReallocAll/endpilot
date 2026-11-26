import asyncio
import unittest
from types import SimpleNamespace

from endpilot import Bot
from endpilot.compression import PacketConnection
from endpilot.containers import Containers
from endpilot.inventory import Inventory
from endpilot.protocol import Protocol, RawPacket
from test_scheduled_input import Wire


def item(b, item_id, count, net_id=None):
    return b.SerializedNetworkItemStackDescriptor(
        id=item_id, stack_size=count, aux_value=0, net_id_variant=net_id,
        block_runtime_id=0, user_data_buffer=b"",
    )


class OffhandObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_offhand_content_slot_and_transaction_observations(self):
        codec = Protocol(2193)
        b = codec.binding
        callbacks = []
        content = SimpleNamespace(
            inventory_id=b.ContainerID.OFFHAND, slots=[item(b, 358, 1, 22)],
        )
        slot = SimpleNamespace(
            inventory_id=b.ContainerID.OFFHAND, slot=0,
            item=item(b, 450, 1, 23),
        )
        action = SimpleNamespace(
            source=SimpleNamespace(
                type=SimpleNamespace(name="CONTAINER_INVENTORY"),
                container_id=b.ContainerID.OFFHAND,
            ),
            slot=0, to_item_descriptor=item(b, 358, 1, 24),
        )
        transaction = SimpleNamespace(
            variant_transaction=SimpleNamespace(
                transaction=SimpleNamespace(actions=[action]),
            ),
        )
        decoded = {
            b"content": content, b"slot": slot, b"transaction": transaction,
        }
        stream = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: decoded[raw.body]),
            subscribe_raw=lambda cb: callbacks.append(cb)
            or (lambda: callbacks.remove(cb)),
        )
        inventory = Inventory(stream, 7)
        try:
            self.assertIsNone(inventory.offhand_item)
            inventory._on_packet(RawPacket(b.InventoryContentPacket.ID, b"content"))
            self.assertEqual(int(inventory.offhand_item.id), 358)
            self.assertEqual(int(inventory.offhand_item.net_id_variant), 22)
            inventory._on_packet(RawPacket(b.InventorySlotPacket.ID, b"slot"))
            self.assertEqual(int(inventory.offhand_item.id), 450)
            inventory._on_packet(RawPacket(b.InventoryTransactionPacket.ID, b"transaction"))
            self.assertEqual(int(inventory.offhand_item.id), 358)
            self.assertEqual(int(inventory.offhand_item.net_id_variant), 24)
        finally:
            inventory.close()
        self.assertEqual(callbacks, [])

    async def test_empty_content_and_unrelated_inventory_leave_unknown_offhand(self):
        codec = Protocol(2193)
        b = codec.binding
        callbacks = []
        decoded = {
            b"empty": SimpleNamespace(inventory_id=b.ContainerID.OFFHAND, slots=[]),
            b"inventory": SimpleNamespace(
                inventory_id=b.ContainerID.INVENTORY, slots=[item(b, 4, 1)],
            ),
        }
        stream = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: decoded[raw.body]),
            subscribe_raw=lambda cb: callbacks.append(cb)
            or (lambda: callbacks.remove(cb)),
        )
        inventory = Inventory(stream, 7)
        try:
            self.assertIsNone(inventory.offhand_item)
            inventory._on_packet(RawPacket(b.InventoryContentPacket.ID, b"empty"))
            self.assertIsNone(inventory.offhand_item)
            inventory._on_packet(RawPacket(b.InventoryContentPacket.ID, b"inventory"))
            self.assertIsNone(inventory.offhand_item)
            self.assertEqual(int(inventory.slots[0].id), 4)
        finally:
            inventory.close()


class OffhandEquipmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.protocol = Protocol(2225)
        self.b = self.protocol.binding
        self.wire = Wire(self.protocol)
        settings = self.b.NetworkSettingsPacket(compression_algorithm=self.b.PacketCompressionAlgorithm.NONE)
        self.packets = PacketConnection(self.wire, self.protocol, settings)
        self.inventory = Inventory(self.packets, 7)
        self.containers = Containers(self.packets, None, self.inventory)
        self.bot = Bot("OffhandReplay", protocol=2225)
        self.bot._inventory, self.bot._containers = self.inventory, self.containers
        self.packets.start_receiving()
        self.addAsyncCleanup(self.packets.close)
        self.addCleanup(self.inventory.close)
        self.addCleanup(self.containers.close)

    def window(self, kind="INVENTORY"):
        return self.b.ContainerOpenPacket(container_id=4, type=self.b.ContainerType[kind])

    def close_packet(self):
        return self.b.ContainerClosePacket(container_id=4, container_type=self.b.ContainerType.INVENTORY)

    def player(self, item_id=0, count=0, net_id=None):
        return self.b.InventorySlotPacket(inventory_id=self.b.ContainerID.INVENTORY, slot=3,
                                          item=item(self.b, item_id, count, net_id))

    def offhand(self, item_id=0, count=0, net_id=None):
        return self.b.InventoryContentPacket(inventory_id=self.b.ContainerID.OFFHAND,
                                             slots=[item(self.b, item_id, count, net_id)])

    async def deliver(self, *packets):
        barrier = self.b.SetTimePacket(time=37)
        waiter = asyncio.create_task(self.packets.wait_for_packet(barrier.ID, timeout=1))
        await asyncio.sleep(0)
        for packet in (*packets, barrier):
            self.wire.push(packet)
        await waiter

    async def sent_request(self):
        async with asyncio.timeout(1):
            while not self.wire.sent:
                await asyncio.sleep(0)
        request, = self.wire.sent.pop(0).requests
        return request

    def response(self, request, result, *slots):
        b = self.b
        return b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=request.client_request_id, result=b.ItemStackNetResult[result],
            containers=[
                b.ItemStackResponseContainerInfo(
                    full_container_name=b.FullContainerName(name=b.ContainerEnumName[name]),
                    slots=[b.ItemStackResponseSlotInfo(
                        requested_slot=index, slot=index, amount=amount,
                        item_stack_net_id=None if net_id is None else b.ItemStackNetId(raw_id=net_id),
                    )],
                )
                for name, index, amount, net_id in slots
            ] if result == "SUCCESS" else None,
        )])

    async def test_moves_a_whole_stack_through_the_matching_verdict(self):
        b = self.b
        await self.deliver(self.window(), self.player(17, 16, 71), self.offhand())
        equip = asyncio.create_task(self.inventory.equip_offhand(3, containers=self.containers))
        placing = await self.sent_request()
        action, = placing.actions
        self.assertIsInstance(action, b.ItemStackRequestCereal.PlaceActionData)
        self.assertEqual((action.amount, action.source.slot, action.source.net_id_variant), (16, 3, 71))
        self.assertEqual(action.destination.full_container_name.name, b.ContainerEnumName.OFFHAND_CONTAINER)
        self.assertEqual((action.destination.slot, action.destination.net_id_variant), (1, 0))
        wrong = self.response(SimpleNamespace(client_request_id=b.ItemStackRequestId(raw_id=-99)), "CANNOT_PLACE_ITEM")
        await self.deliver(wrong)
        self.assertFalse(equip.done())
        await self.deliver(self.response(placing, "SUCCESS", ("OFFHAND_CONTAINER", 1, 16, 73),
                                         ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 3, 0, None)))
        self.assertEqual((await equip).result.name, "SUCCESS")
        self.assertEqual((self.inventory.offhand_item.id, self.inventory.offhand_item.stack_size,
                          self.inventory.offhand_item.net_id_variant), (17, 16, 73))
        self.assertEqual(self.inventory.slots[3].id, 0)

        unequip = asyncio.create_task(self.inventory.unequip_offhand(3, containers=self.containers))
        taking = await self.sent_request()
        action, = taking.actions
        self.assertIsInstance(action, b.ItemStackRequestCereal.TakeActionData)
        self.assertEqual((action.amount, action.source.slot, action.source.net_id_variant), (16, 1, 73))
        self.assertEqual(action.source.full_container_name.name, b.ContainerEnumName.OFFHAND_CONTAINER)
        await self.deliver(self.response(taking, "SUCCESS", ("OFFHAND_CONTAINER", 1, 0, None),
                                         ("HOTBAR_CONTAINER", 3, 16, 76)))
        self.assertEqual((await unequip).result.name, "SUCCESS")
        self.assertEqual(self.inventory.offhand_item.id, 0)
        self.assertEqual((self.inventory.slots[3].id, self.inventory.slots[3].stack_size,
                          self.inventory.slots[3].net_id_variant), (17, 16, 76))

    async def test_swaps_or_preserves_real_updates_in_each_direction(self):
        b = self.b
        for into_offhand in (True, False):
            with self.subTest(into_offhand=into_offhand):
                await self.deliver(self.window(), self.player(578, 1, 20), self.offhand(358, 1, 22))
                method = self.inventory.equip_offhand if into_offhand else self.inventory.unequip_offhand
                task = asyncio.create_task(method(3, containers=self.containers))
                request = await self.sent_request()
                action, = request.actions
                self.assertIsInstance(action, b.ItemStackRequestCereal.SwapActionData)
                self.assertEqual((action.source.slot, action.destination.slot),
                                 (3, 1) if into_offhand else (1, 3))
                await self.deliver(self.offhand(578, 1, 20))
                # Independent player-slot update before the older swap verdict.
                await self.deliver(self.player(4, 3, 37))
                hand = self.inventory.offhand_item
                player = self.inventory.slots[3]
                await self.deliver(self.response(request, "SUCCESS", ("OFFHAND_CONTAINER", 1, 1, 20),
                                                 ("INVENTORY_CONTAINER", 3, 1, 22)))
                self.assertEqual((await task).result.name, "SUCCESS")
                self.assertIs(self.inventory.offhand_item, hand)
                self.assertIs(self.inventory.slots[3], player)

    async def test_rejection_returns_the_matching_result_without_predicting(self):
        await self.deliver(self.window(), self.player(3, 1, 20), self.offhand())
        hand, player = self.inventory.offhand_item, self.inventory.slots[3]
        task = asyncio.create_task(self.bot.equip_offhand(3))
        request = await self.sent_request()
        await self.deliver(self.close_packet(), self.response(request, "CANNOT_PLACE_ITEM"))
        self.assertEqual((await task).result.name, "CANNOT_PLACE_ITEM")
        self.assertIs(self.inventory.offhand_item, hand)
        self.assertIs(self.inventory.slots[3], player)

    async def test_late_equip_verdict_clears_source_after_window_closes(self):
        await self.deliver(self.window(), self.player(17, 16, 71), self.offhand())
        task = asyncio.create_task(self.bot.equip_offhand(3))
        request = await self.sent_request()
        await self.deliver(self.offhand(17, 16, 71), self.close_packet())
        self.assertIsNone(self.containers.window)
        hand = self.bot.offhand_item
        await self.deliver(self.response(request, "SUCCESS", ("OFFHAND_CONTAINER", 1, 16, 71),
                                         ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 3, 0, None)))
        self.assertEqual((await task).result.name, "SUCCESS")
        self.assertIs(self.bot.offhand_item, hand)
        self.assertEqual(self.bot.offhand_item.stack_size, 16)
        self.assertEqual((self.bot.inventory_slots[3].id, self.bot.inventory_slots[3].stack_size), (0, 0))

    async def test_late_unequip_verdict_preserves_independent_update_after_same_id_reopen(self):
        b = self.b
        await self.deliver(self.window(), self.player(), self.offhand(17, 16, 71))
        window = self.containers.window
        task = asyncio.create_task(self.bot.unequip_offhand(3))
        request = await self.sent_request()
        await self.deliver(self.close_packet(), self.window(), self.player(578, 1, 83))
        self.assertIsNot(self.containers.window, window)
        self.assertEqual(self.containers.window.container_id, window.container_id)
        player = self.bot.inventory_slots[3]
        corrections = (("OFFHAND_CONTAINER", 1, 0, None), ("HOTBAR_CONTAINER", 3, 16, 71))
        unrelated = SimpleNamespace(client_request_id=b.ItemStackRequestId(raw_id=-99))
        await self.deliver(self.response(unrelated, "SUCCESS", *corrections))
        self.assertFalse(task.done())
        self.assertEqual(self.bot.offhand_item.stack_size, 16)
        await self.deliver(self.response(request, "SUCCESS", *corrections))
        self.assertEqual((await task).result.name, "SUCCESS")
        self.assertEqual((self.bot.offhand_item.id, self.bot.offhand_item.stack_size), (0, 0))
        self.assertIs(self.bot.inventory_slots[3], player)

    async def test_rejects_unobserved_empty_or_wrong_window_before_send(self):
        equip, unequip = self.inventory.equip_offhand, self.inventory.unequip_offhand
        await self.deliver(self.window("CONTAINER"), self.player(358, 1, 22), self.offhand())
        with self.assertRaisesRegex(RuntimeError, "open the player inventory"):
            await equip(3, containers=self.containers)
        await self.deliver(self.window(), self.b.InventoryContentPacket(inventory_id=self.b.ContainerID.OFFHAND, slots=[]))
        with self.assertRaisesRegex(RuntimeError, "offhand slot is unobserved"):
            await equip(3, containers=self.containers)
        await self.deliver(self.offhand())
        with self.assertRaisesRegex(ValueError, "source slot is empty"):
            await unequip(3, containers=self.containers)
        with self.assertRaisesRegex(RuntimeError, "inventory slot is unobserved"):
            await equip(2, containers=self.containers)
        await self.deliver(self.player(358, 1))
        with self.assertRaisesRegex(RuntimeError, "no observed identity"):
            await equip(3, containers=self.containers)
        self.assertEqual(self.wire.sent, [])

    async def test_rechecks_reused_window_and_both_slots_at_send(self):
        for replaced in ("window", "inventory", "offhand"):
            with self.subTest(replaced=replaced):
                await self.deliver(self.window(), self.player(358, 1, 22), self.offhand())
                task = asyncio.create_task(self.inventory.equip_offhand(3, containers=self.containers))
                await asyncio.sleep(0)
                if replaced == "window":
                    self.containers._on_packet(self.protocol.encode(self.window()))
                else:
                    replacement = self.player(578, 1, 37) if replaced == "inventory" else self.offhand(578, 1, 37)
                    self.inventory._on_packet(self.protocol.encode(replacement))
                with self.assertRaisesRegex(RuntimeError, "changed before submission"):
                    await task
                self.assertEqual(self.wire.sent, [])
