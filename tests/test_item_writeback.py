"""Item-stack verdicts and newer slot data from the same packet stream."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from endpilot import Bot
from endpilot.compression import PacketConnection
from endpilot.containers import Containers
from endpilot.inventory import Inventory
from endpilot.protocol import Protocol, RawPacket
from endpilot.recipes import Recipes

from test_blocks import PacketStream
from test_container_take import item
from test_crafting import server_recipe


PLAYER = "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"
CHEST = "LEVEL_ENTITY_CONTAINER"
GRID = "CRAFTING_INPUT_CONTAINER"


class ItemWritebackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.codec = Protocol(2225)
        self.binding = self.codec.binding
        self.stream = PacketStream(self.codec)
        self.inventory = Inventory(self.stream, 1)
        self.containers = Containers(self.stream, None, self.inventory)
        self.recipes = Recipes(self.stream, {1: "minecraft:oak_log", 2: "minecraft:oak_planks"},
                               inventory=self.inventory, containers=self.containers)
        self.addCleanup(self.inventory.close)
        self.addCleanup(self.containers.close)
        self.addCleanup(self.recipes.close)

    def emit(self, packet):
        self.stream.emit(self.codec.encode(packet))

    def open_window(self, kind="INVENTORY"):
        b = self.binding
        self.emit(b.ContainerOpenPacket(container_id=2, type=b.ContainerType[kind],
                                        pos=b.BlockPos(x=1, y=100, z=0), entity_unique_id=-1))

    def change_slot(self, inventory_id, slot, value):
        b = self.binding
        self.emit(b.InventorySlotPacket(inventory_id=inventory_id, slot=slot,
                                        full_container_name=None, storage_item=None, item=value))

    def reply_to_request(self, corrections, *, before=None, after=None):
        b = self.binding
        original_send = self.stream.send

        async def send(packet, *, _before_send=None):
            if _before_send is not None:
                _before_send()
            await original_send(packet)
            request = packet.requests[0]
            if before is not None:
                before()
            self.emit(b.ItemStackResponsePacket(responses=[
                self.verdict(int(request.client_request_id.raw_id), corrections),
            ]))
            if after is not None:
                after()

        self.stream.send = send

    def verdict(self, request_id, corrections):
        b = self.binding
        return b.ItemStackResponseInfo(
            client_request_id=b.ItemStackRequestId(raw_id=request_id), result=b.ItemStackNetResult.SUCCESS,
            containers=[b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName[container], dynamic_id=None),
                slots=[b.ItemStackResponseSlotInfo(
                    requested_slot=index, slot=index, amount=amount,
                    item_stack_net_id=None if net_id is None else b.ItemStackNetId(raw_id=net_id),
                    custom_name=b.RedactableString(unredacted_string="", redacted_string=None),
                    durability_correction=0,
                )],
            ) for container, index, amount, net_id in corrections],
        )

    async def test_same_slot_verdicts_apply_in_array_order_not_submission_order(self):
        b = self.binding
        self.change_slot(b.ContainerID.INVENTORY, 2, item(b, 1, 6, 39))
        first = await self.inventory.drop_item(2, 1, _queued=True)
        second = await self.inventory.drop_item(2, 1, _queued=True)
        self.assertEqual((first, second), (-1, -3))
        self.assertIsNone(self.inventory._qa_response(first))
        await asyncio.sleep(0)
        self.assertEqual(len(self.stream.sent), 2)
        self.codec.decode = Mock(wraps=self.codec.decode)
        self.emit(b.ItemStackResponsePacket(responses=[
            self.verdict(second, [(PLAYER, 2, 5, 40)]),
            self.verdict(first, [(PLAYER, 2, 4, 41)]),
        ]))
        self.codec.decode.assert_called_once()
        final = self.inventory.slots[2]
        self.assertEqual((final.id, final.stack_size, final.net_id_variant), (1, 4, 41))
        self.assertEqual(self.inventory._qa_response(first).containers[0].slots[0].amount, 4)
        reply = self.inventory._qa_response(second)
        self.assertEqual(reply.containers[0].slots[0].amount, 5)
        self.assertIs(self.inventory._qa_response(second), reply)

    async def test_inventory_slot_replacement_breaks_the_drop_stack_identity(self):
        b = self.binding
        self.change_slot(b.ContainerID.INVENTORY, 2, item(b, 1, 6, 39))
        first = await self.inventory.drop_item(2, 1, _queued=True)
        second = await self.inventory.drop_item(2, 1, _queued=True)
        await asyncio.sleep(0)
        self.emit(b.ItemStackResponsePacket(responses=[self.verdict(first, [(PLAYER, 2, 5, 40)])]))
        self.assertEqual(self.inventory.slots[2].id, 1)
        self.change_slot(b.ContainerID.INVENTORY, 2, item(b, 306, 3, 50))
        self.emit(b.ItemStackResponsePacket(responses=[self.verdict(second, [(PLAYER, 2, 4, 41)])]))
        self.assertEqual(self.inventory._qa_response(second).result.name, "SUCCESS")
        self.assertIsNone(self.inventory.slots.get(2))

    async def test_different_slot_verdicts_survive_one_cancelled_wait(self):
        b = self.binding
        self.change_slot(b.ContainerID.INVENTORY, 2, item(b, 1, 6, 39))
        self.change_slot(b.ContainerID.INVENTORY, 3, item(b, 5, 8, 51))
        first = asyncio.create_task(self.inventory.drop_item(2, 1))
        second = asyncio.create_task(self.inventory.drop_item(3, 2))
        async with asyncio.timeout(1):
            while len(self.stream.sent) != 2:
                await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.emit(b.ItemStackResponsePacket(responses=[
            self.verdict(-3, [(PLAYER, 3, 6, 53)]),
            self.verdict(-1, [(PLAYER, 2, 5, 41)]),
        ]))
        self.assertEqual(int((await second).client_request_id.raw_id), -3)
        self.assertEqual((self.inventory.slots[2].stack_size, self.inventory.slots[3].stack_size), (5, 6))
        self.assertIsNone(self.inventory._qa_response(-1))

    async def test_disconnect_reports_unconfirmed_queued_request(self):
        b = self.binding
        self.change_slot(b.ContainerID.INVENTORY, 2, item(b, 1, 6, 39))
        request = await self.inventory.drop_item(2, _queued=True)
        await asyncio.sleep(0)
        self.stream.emit(RawPacket(b.DisconnectPacket.ID, b""))
        with self.assertRaisesRegex(ConnectionError, "before an item-stack verdict"):
            self.inventory._qa_response(request)

    async def test_a_callback_error_does_not_consume_other_verdicts(self):
        b = self.binding
        for index in (2, 3):
            self.change_slot(b.ContainerID.INVENTORY, index, item(b, 1, 6, 39 + index))
        first = await self.inventory.drop_item(2, _queued=True)
        second = await self.inventory.drop_item(3, _queued=True)

        def fail(_):
            raise ValueError("test correction failure")

        self.inventory._pending_requests[first].apply = fail
        await asyncio.sleep(0)
        self.emit(b.ItemStackResponsePacket(responses=[
            self.verdict(first, [(PLAYER, 2, 5, 41)]),
            self.verdict(second, [(PLAYER, 3, 5, 42)]),
        ]))
        with self.assertRaisesRegex(ValueError, "test correction failure"):
            self.inventory._qa_response(first)
        self.assertEqual(self.inventory._qa_response(second).result.name, "SUCCESS")
        self.assertEqual(self.inventory.slots[3].stack_size, 5)

    async def test_response_decode_failure_is_observable_and_later_requests_can_complete(self):
        b = self.binding
        for index in (2, 3):
            self.change_slot(b.ContainerID.INVENTORY, index, item(b, 1, 6, 39 + index))
        queued = await self.inventory.drop_item(2, _queued=True)
        waiting = asyncio.create_task(self.inventory.drop_item(3))
        async with asyncio.timeout(1):
            while len(self.stream.sent) != 2:
                await asyncio.sleep(0)
        failure = ValueError("controlled native decode failure")
        raw = RawPacket(b.ItemStackResponsePacket.ID, b"")
        decode = self.codec.decode

        def decode_response(packet):
            if packet is raw:
                raise failure
            return decode(packet)

        self.codec.decode = decode_response
        self.stream.emit(raw)
        with self.assertRaisesRegex(ValueError, "could not decode item-stack response") as queued_error:
            self.inventory._qa_response(queued)
        with self.assertRaisesRegex(ValueError, "could not decode item-stack response") as sdk_error:
            await asyncio.wait_for(waiting, 1)
        self.assertIs(queued_error.exception.__cause__, failure)
        self.assertIs(sdk_error.exception.__cause__, failure)
        self.assertEqual(self.inventory._pending_requests, {})
        self.assertEqual((self.inventory.slots[2].stack_size, self.inventory.slots[3].stack_size), (6, 6))

        next_id = await self.inventory.drop_item(2, _queued=True)
        await asyncio.sleep(0)
        self.emit(b.ItemStackResponsePacket(responses=[self.verdict(next_id, [(PLAYER, 2, 5, 51)])]))
        self.assertEqual(self.inventory._qa_response(next_id).result.name, "SUCCESS")
        self.assertEqual(self.inventory.slots[2].stack_size, 5)

    async def test_timeout_stops_waiting_without_losing_the_later_writeback(self):
        b = self.binding
        self.change_slot(b.ContainerID.INVENTORY, 2, item(b, 1, 6, 39))
        with self.assertRaises(TimeoutError):
            await self.inventory.drop_item(2, timeout=0.01)
        self.assertEqual(len(self.stream.sent), 1)
        self.emit(b.ItemStackResponsePacket(responses=[self.verdict(-1, [(PLAYER, 2, 5, 40)])]))
        self.assertEqual(self.inventory.slots[2].stack_size, 5)

    async def test_reader_loss_reports_connection_error_and_releases_subscriptions(self):
        queue = asyncio.Queue()

        async def receive():
            return await queue.get()

        transport = SimpleNamespace(recv=receive, send=AsyncMock(), close=AsyncMock())
        connection = PacketConnection(transport, self.codec, SimpleNamespace(
            compression_algorithm=0, compression_threshold=1,
        ))
        inventory = Inventory(connection, 1)
        inventory._set_slot(2, item(self.binding, 1, 6, 39))
        connection.start_receiving()
        request = await inventory.drop_item(2, _queued=True)
        await asyncio.sleep(0)
        queue.put_nowait(b"")
        try:
            async with asyncio.timeout(1):
                while not inventory._request_stream_ended:
                    await asyncio.sleep(0)
            with self.assertRaisesRegex(ConnectionError, "before an item-stack verdict"):
                inventory._qa_response(request)
        finally:
            inventory.close()
            await inventory._wait_for_request_sends()
            with self.assertRaisesRegex(ConnectionError, "empty compressed batch"):
                await connection.close()
        self.assertIsNone(inventory._qa_response(request))
        self.assertEqual((connection._subscribers, connection._reader_end_subscribers), ([], []))

    async def test_barrel_queue_reports_native_reply_after_both_slots_update(self):
        b = self.binding
        self.open_window("CONTAINER")
        self.emit(b.InventoryContentPacket(
            inventory_id=2, slots=[item(b, 1, 4, 17)],
            full_container_name=b.FullContainerName(name=b.ContainerEnumName.BARREL_CONTAINER, dynamic_id=None),
            storage_item=item(b),
        ))
        request = await self.containers._transfer(
            0, 2, 2, into_container=False, timeout=1, _queued=True, block_container="BARREL_CONTAINER",
        )
        self.assertIsNone(self.inventory._qa_response(request))
        await asyncio.sleep(0)
        action = self.stream.sent[0].requests[0].actions[0]
        self.assertEqual(action.source.full_container_name.name.name, "BARREL_CONTAINER")
        self.emit(b.ItemStackResponsePacket(responses=[self.verdict(
            request, [("BARREL_CONTAINER", 0, 2, 17), (PLAYER, 2, 2, 21)],
        )]))
        reply = self.inventory._qa_response(request)
        self.assertEqual(reply.result.name, "SUCCESS")
        self.assertEqual([
            (container.full_container_name.name.name, slot.slot, slot.amount, slot.item_stack_net_id.raw_id)
            for container in reply.containers for slot in container.slots
        ], [("BARREL_CONTAINER", 0, 2, 17), (PLAYER, 2, 2, 21)])
        self.assertEqual(self.containers.slots[0].stack_size, 2)
        self.assertEqual(self.inventory.slots[2].id, 1)

    async def test_returns_material_left_after_repeated_grid_crafting(self):
        b = self.binding
        self.open_window()
        self.recipes.load(SimpleNamespace(clear_recipes=True, shapeless_recipes=[server_recipe()]))
        self.change_slot(124, 28, item(b, 1, 4, 23))
        crafted = await self.recipes.craft_grid(19, 10, times=2, _queued=True)
        await asyncio.sleep(0)
        self.emit(b.ItemStackResponsePacket(responses=[self.verdict(
            crafted, [(GRID, 28, 2, 23), (PLAYER, 10, 8, 52)],
        )]))
        self.assertEqual(self.inventory._qa_response(crafted).containers[0].slots[0].amount, 2)
        returned = await self.recipes.return_grid(28, 20, 2, _queued=True)
        await asyncio.sleep(0)
        action = self.stream.sent[-1].requests[0].actions[0]
        self.assertIsInstance(action, b.ItemStackRequestCereal.TakeActionData)
        self.assertEqual(int(action.source.net_id_variant), 23)
        self.emit(b.ItemStackResponsePacket(responses=[self.verdict(
            returned, [(GRID, 28, 0, None), (PLAYER, 20, 2, 53)],
        )]))
        self.assertEqual(self.inventory._qa_response(returned).result.name, "SUCCESS")
        self.assertEqual(self.inventory.slots[20].stack_size, 2)
        self.assertEqual(self.recipes._grid_slots[28].stack_size, 0)

    async def test_public_return_takes_only_the_excess_in_the_same_workbench(self):
        self.open_window("WORKBENCH")
        window = self.containers.window
        bot = Bot("Crafting")
        bot._recipes = self.recipes
        self.change_slot(124, 32, item(self.binding, 1, 2, 23))
        self.recipes.load(SimpleNamespace(clear_recipes=True, shapeless_recipes=[server_recipe()]))
        send = self.stream.send
        self.reply_to_request([(GRID, 32, 1, 23), (PLAYER, 10, 1, 51)])

        reply = await bot.return_crafting_ingredient(32, 10)
        take, = self.stream.sent[-1].requests[0].actions
        self.assertIsInstance(take, self.binding.ItemStackRequestCereal.TakeActionData)
        self.assertEqual((take.source.full_container_name.name.name, take.source.slot, take.source.net_id_variant,
                          take.destination.full_container_name.name.name, take.destination.slot, take.amount),
                         (GRID, 32, 23, PLAYER, 10, 1))
        returned = self.inventory.slots[10]
        self.assertEqual((reply.result.name, returned.id, returned.stack_size), ("SUCCESS", 1, 1))

        self.stream.send = send
        self.reply_to_request([(GRID, 32, 0, None), (PLAYER, 11, 4, 52)])
        crafted = await bot.craft_recipe_grid(19, 11)
        consume = next(a for a in self.stream.sent[-1].requests[0].actions
                       if isinstance(a, self.binding.ItemStackRequestCereal.ConsumeActionData))
        self.assertEqual((consume.amount, consume.source.net_id_variant), (1, 23))
        self.assertIs(self.containers.window, window)
        output = self.inventory.slots[11]
        self.assertEqual((crafted.result.name, output.id, output.stack_size), ("SUCCESS", 2, 4))

    async def test_public_return_does_not_send_after_workbench_is_replaced(self):
        self.open_window("WORKBENCH")
        bot = Bot("Crafting")
        bot._recipes = self.recipes
        self.change_slot(124, 32, item(self.binding, 1, 2, 23))
        self.reply_to_request([])
        returning = asyncio.create_task(bot.return_crafting_ingredient(32, 10))
        await asyncio.sleep(0)
        self.open_window("WORKBENCH")
        self.change_slot(124, 32, item(self.binding, 1, 2, 24))
        with self.assertRaisesRegex(RuntimeError, "crafting window changed"):
            await returning
        self.assertEqual(self.stream.sent, [])

    async def test_drop_returns_verdict_and_keeps_a_later_same_slot_packet(self):
        b = self.binding
        self.change_slot(b.ContainerID.INVENTORY, 2, item(b, 1, 6, 39))
        self.reply_to_request([(PLAYER, 2, 2, 40)], after=lambda: self.change_slot(
            b.ContainerID.INVENTORY, 2, item(b, 306, 3, 50),
        ))
        result = await self.inventory.drop_item(2, 4)
        self.assertEqual(result.result.name, "SUCCESS")
        observed = self.inventory.slots[2]
        self.assertEqual((observed.id, observed.stack_size, observed.net_id_variant), (306, 3, 50))

    async def test_drop_only_corrects_a_replaced_stack_with_a_matching_net_id(self):
        b = self.binding
        base_send = self.stream.send
        for reply_net_id in (50, 40):
            with self.subTest(reply_net_id=reply_net_id):
                self.stream.send = base_send
                self.change_slot(b.ContainerID.INVENTORY, 2, item(b, 1, 6, 39))
                self.reply_to_request([(PLAYER, 2, 2, reply_net_id)], before=lambda: self.change_slot(
                    b.ContainerID.INVENTORY, 2, item(b, 306, 3, 50),
                ))
                result = await self.inventory.drop_item(2, 4)
                self.assertEqual(result.result.name, "SUCCESS")
                observed = self.inventory.slots.get(2)
                if reply_net_id == 50:
                    self.assertEqual((observed.id, observed.stack_size, observed.net_id_variant), (306, 2, 50))
                else:
                    self.assertIsNone(observed)

    async def test_container_transfer_respects_newer_packets_in_both_inventories(self):
        b = self.binding
        self.open_window("CONTAINER")
        self.emit(b.InventoryContentPacket(
            inventory_id=2, slots=[item(b, 1, 4, 17)],
            full_container_name=b.FullContainerName(name=b.ContainerEnumName.LEVEL_ENTITY_CONTAINER,
                                                   dynamic_id=None),
            storage_item=item(b),
        ))

        def later_updates():
            self.change_slot(2, 0, item(b, 306, 3, 50))
            self.change_slot(b.ContainerID.INVENTORY, 2, item(b, 5, 8, 51))

        self.reply_to_request([(CHEST, 0, 2, 17), (PLAYER, 2, 2, 21)], after=later_updates)
        result = await self.containers.take_from_container(0, 2, 2)
        self.assertEqual(result.result.name, "SUCCESS")
        chest = self.containers.slots[0]
        player = self.inventory.slots[2]
        self.assertEqual((chest.id, chest.stack_size, chest.net_id_variant), (306, 3, 50))
        self.assertEqual((player.id, player.stack_size, player.net_id_variant), (5, 8, 51))

    async def test_queued_container_move_keeps_request_id_and_reads_after_range_writeback(self):
        b = self.binding
        self.open_window("CONTAINER")
        self.emit(b.InventoryContentPacket(inventory_id=2, slots=[item(b, 1, 6, 17)]))
        self.change_slot(b.ContainerID.INVENTORY, 2, item(b))
        expected = self.containers.slots[0], self.inventory.slots[2]
        taking = asyncio.create_task(self.containers._transfer(
            0, 2, 2, into_container=False, timeout=1, _expected_slots=expected,
        ))
        async with asyncio.timeout(1):
            while not self.stream.sent:
                await asyncio.sleep(0)
        first = int(self.stream.sent[0].requests[0].client_request_id.raw_id)

        queued = await self.containers._transfer(2, 0, 1, into_container=True, timeout=1, _queued=True)
        await asyncio.sleep(0)
        self.assertEqual(len(self.stream.sent), 1)
        self.assertIsNone(self.inventory._qa_response(queued))
        self.emit(b.ItemStackResponsePacket(responses=[self.verdict(
            first, [(CHEST, 0, 4, 17), (PLAYER, 2, 2, 41)],
        )]))
        self.assertEqual((await taking).result.name, "SUCCESS")
        async with asyncio.timeout(1):
            while len(self.stream.sent) < 2:
                await asyncio.sleep(0)
        request, = self.stream.sent[1].requests
        self.assertEqual(int(request.client_request_id.raw_id), queued)
        action, = request.actions
        self.assertEqual((action.source.net_id_variant, action.destination.net_id_variant), (41, 17))
        self.emit(b.ItemStackResponsePacket(responses=[self.verdict(
            queued, [(PLAYER, 2, 1, 41), (CHEST, 0, 5, 17)],
        )]))
        self.assertEqual(self.inventory._qa_response(queued).result.name, "SUCCESS")
        self.assertEqual((self.containers.slots[0].stack_size, self.inventory.slots[2].stack_size), (5, 1))

    async def test_independent_inventory_move_marks_a_sent_range_unconfirmed_without_suppressing_replies(self):
        b = self.binding
        self.open_window("CONTAINER")
        self.emit(b.InventoryContentPacket(inventory_id=2, slots=[item(b, 1, 5, 17)]))
        self.change_slot(b.ContainerID.INVENTORY, 2, item(b))
        self.change_slot(b.ContainerID.INVENTORY, 4, item(b, 1, 2, 42))
        expected = self.containers.slots[0], self.inventory.slots[2]
        affected = asyncio.Event()
        taking = asyncio.create_task(self.containers._transfer(
            0, 2, 1, into_container=False, timeout=1,
            _expected_slots=expected, _on_unconfirmed=affected.set,
        ))
        async with asyncio.timeout(1):
            while not self.stream.sent:
                await asyncio.sleep(0)
        moving = asyncio.create_task(self.inventory.move_item(4, 2, 1))
        async with asyncio.timeout(1):
            while len(self.stream.sent) < 2:
                await asyncio.sleep(0)
        self.assertTrue(affected.is_set())
        self.assertFalse(taking.done())
        take_id, move_id = (int(packet.requests[0].client_request_id.raw_id) for packet in self.stream.sent)
        self.emit(b.ItemStackResponsePacket(responses=[
            self.verdict(move_id, [(PLAYER, 4, 1, 42), (PLAYER, 2, 1, 41)]),
            self.verdict(take_id, [(CHEST, 0, 4, 17), (PLAYER, 2, 1, 41)]),
        ]))
        self.assertEqual((await moving).client_request_id.raw_id, move_id)
        self.assertEqual((await taking).client_request_id.raw_id, take_id)

    async def test_planned_container_transfer_rechecks_player_quantity_at_actual_send(self):
        b = self.binding
        self.open_window("CONTAINER")
        self.emit(b.InventoryContentPacket(
            inventory_id=2, slots=[item(b, 1, 4, 17)],
            full_container_name=b.FullContainerName(name=b.ContainerEnumName.LEVEL_ENTITY_CONTAINER),
            storage_item=item(b),
        ))
        self.change_slot(b.ContainerID.INVENTORY, 2, item(b, 1, 2, 21))
        expected = self.containers.slots[0], self.inventory.slots[2]
        taking = asyncio.create_task(self.containers._transfer(
            0, 2, 2, into_container=False, timeout=1, _expected_slots=expected,
        ))
        await asyncio.sleep(0)
        self.change_slot(b.ContainerID.INVENTORY, 2, item(b, 1, 5, 21))
        with self.assertRaisesRegex(RuntimeError, "slots changed before submission"):
            await taking
        self.assertEqual(self.stream.sent, [])
        self.assertEqual(self.inventory.slots[2].stack_size, 5)
        self.assertEqual(self.containers.slots[0].stack_size, 4)

    async def test_success_with_missing_corrections_blocks_range_until_fresh_slots(self):
        b = self.binding
        original_send = self.stream.send
        incomplete = ([(CHEST, 0, 4, 17)], [(PLAYER, 2, 2, 41)], [])
        for queued in (False, True):
            for corrections in incomplete:
                with self.subTest(queued=queued, corrections=corrections):
                    self.stream.send = original_send
                    self.open_window("CONTAINER")
                    self.emit(b.InventoryContentPacket(inventory_id=2, slots=[item(b, 1, 6, 17)]))
                    self.change_slot(b.ContainerID.INVENTORY, 2, item(b))
                    self.reply_to_request(corrections)
                    if queued:
                        request_id = await self.containers._transfer(
                            0, 2, 2, into_container=False, timeout=1, _queued=True,
                        )
                        await asyncio.sleep(0)
                        self.assertEqual(self.inventory._qa_response(request_id).result.name, "SUCCESS")
                    else:
                        result = await self.containers.take_from_container(0, 2, 2)
                        self.assertEqual(result.result.name, "SUCCESS")
                    sent = len(self.stream.sent)

                    expected = self.containers.slots[0], self.inventory.slots[2]
                    blocked = await self.containers._transfer(
                        0, 2, 1, into_container=False, timeout=1, _expected_slots=expected,
                    )
                    self.assertIsNone(blocked)
                    self.assertEqual(len(self.stream.sent), sent)

                    self.change_slot(2, 0, item(b, 1, 4, 17))
                    self.change_slot(b.ContainerID.INVENTORY, 2, item(b, 1, 2, 41))
                    self.stream.send = original_send
                    self.reply_to_request([(CHEST, 0, 3, 17), (PLAYER, 2, 3, 41)])
                    expected = self.containers.slots[0], self.inventory.slots[2]
                    retried = await self.containers._transfer(
                        0, 2, 1, into_container=False, timeout=1, _expected_slots=expected,
                    )
                    self.assertEqual(retried.result.name, "SUCCESS")
                    self.assertEqual(len(self.stream.sent), sent + 1)

    async def test_range_distinguishes_intermediate_quantities_from_baseline_and_final_echoes(self):
        b = self.binding
        original_send = self.stream.send
        for reports, ambiguous in ((((9, 14),), False), (((7, 16),), False),
                                   (((8, 15),), True), (((8, 15), (7, 16)), True)):
            with self.subTest(reports=reports):
                self.stream.send = original_send
                self.open_window("CONTAINER")
                self.emit(b.InventoryContentPacket(inventory_id=2, slots=[item(b, 1, 9, 17)]))
                self.change_slot(b.ContainerID.INVENTORY, 2, item(b, 1, 14, 31))
                expected = self.containers.slots[0], self.inventory.slots[2]
                affected = asyncio.Event()

                def report():
                    for chest_amount, player_amount in reports:
                        self.change_slot(2, 0, item(b, 1, chest_amount, 17))
                        self.change_slot(b.ContainerID.INVENTORY, 2, item(b, 1, player_amount, 31))

                self.reply_to_request([(CHEST, 0, 7, 17), (PLAYER, 2, 16, 31)], before=report)
                reply = await self.containers._transfer(
                    0, 2, 2, into_container=False, timeout=1,
                    _expected_slots=expected, _on_unconfirmed=affected.set,
                )
                self.assertEqual(reply.result.name, "SUCCESS")
                self.assertEqual(affected.is_set(), ambiguous)
                self.assertEqual((self.containers.slots[0].stack_size, self.inventory.slots[2].stack_size), (7, 16))

    async def test_zero_verdict_preserves_a_replaced_source_and_corrects_the_other_endpoint(self):
        b = self.binding
        original_send = self.stream.send
        for into_container in (False, True):
            with self.subTest(into_container=into_container):
                self.stream.send = original_send
                self.open_window("CONTAINER")
                self.emit(b.InventoryContentPacket(
                    inventory_id=2, slots=[item(b, 1, 4, 17)],
                    full_container_name=b.FullContainerName(name=b.ContainerEnumName.LEVEL_ENTITY_CONTAINER),
                    storage_item=item(b),
                ))
                self.change_slot(b.ContainerID.INVENTORY, 2, item(b, 1, 2, 21))

                chest_verdict = (CHEST, 0, 6, 17) if into_container else (CHEST, 0, 0, None)
                player_verdict = (PLAYER, 2, 0, None) if into_container else (PLAYER, 2, 6, 21)
                source_window, source_slot = (b.ContainerID.INVENTORY, 2) if into_container else (2, 0)
                self.reply_to_request([chest_verdict, player_verdict], before=lambda: self.change_slot(
                    source_window, source_slot, item(b, 306, 8, 75),
                ))
                if into_container:
                    result = await self.containers.put_into_container(2, 0, 2)
                    untouched = self.inventory.slots[2]
                    corrected = self.containers.slots[0]
                else:
                    result = await self.containers.take_from_container(0, 2, 4)
                    untouched = self.containers.slots[0]
                    corrected = self.inventory.slots[2]
                self.assertEqual(result.result.name, "SUCCESS")
                self.assertEqual((untouched.id, untouched.stack_size, untouched.net_id_variant), (306, 8, 75))
                self.assertEqual((corrected.id, corrected.stack_size), (1, 6))

    async def test_zero_verdict_clears_equivalent_source_observations_in_both_directions(self):
        b = self.binding
        original_send = self.stream.send
        paper = item(b, 1230, 1, 51)
        paper.user_data_buffer = b"\x00" * 10
        for into_container in (False, True):
            with self.subTest(into_container=into_container):
                self.stream.send = original_send
                self.open_window("CONTAINER")
                self.emit(b.InventoryContentPacket(
                    inventory_id=2, slots=[item(b) if into_container else paper],
                    full_container_name=b.FullContainerName(name=b.ContainerEnumName.LEVEL_ENTITY_CONTAINER),
                    storage_item=item(b),
                ))
                self.change_slot(b.ContainerID.INVENTORY, 2, paper if into_container else item(b))
                def source():
                    return self.inventory.slots[2] if into_container else self.containers.slots[0]

                original = source()

                def reobserve():
                    inventory_id, slot = (b.ContainerID.INVENTORY, 2) if into_container else (2, 0)
                    self.change_slot(inventory_id, slot, paper)
                    self.assertIsNot(source(), original)
                    self.assertEqual(bytes(source().user_data_buffer), bytes(original.user_data_buffer))

                chest_verdict = (CHEST, 0, 1, 58) if into_container else (CHEST, 0, 0, None)
                player_verdict = (PLAYER, 2, 0, None) if into_container else (PLAYER, 2, 1, 58)
                self.reply_to_request([chest_verdict, player_verdict], before=reobserve)
                if into_container:
                    response = await self.containers.put_into_container(2, 0)
                    destination = self.containers.slots[0]
                else:
                    response = await self.containers.take_from_container(0, 2)
                    destination = self.inventory.slots[2]
                self.assertEqual(response.result.name, "SUCCESS")
                self.assertEqual((source().id, source().stack_size), (0, 0))
                self.assertEqual((destination.id, destination.stack_size, destination.net_id_variant), (1230, 1, 58))
                self.assertEqual(bytes(destination.user_data_buffer), bytes(paper.user_data_buffer))

    async def test_stage_ack_cannot_write_into_a_reopened_inventory_window(self):
        b = self.binding
        self.open_window()
        old_window = self.containers.window
        self.change_slot(b.ContainerID.INVENTORY, 0, item(b, 1, 2, 23))

        def reopen():
            self.emit(b.ContainerClosePacket(container_id=2, container_type=b.ContainerType.INVENTORY,
                                             server_initiated_close=False))
            self.open_window()

        self.reply_to_request([(PLAYER, 0, 1, 44), (GRID, 28, 1, 50)], before=reopen)
        result = await self.recipes.stage_grid(0, 28)
        self.assertEqual(result.result.name, "SUCCESS")
        self.assertEqual(self.containers.window, old_window)
        self.assertIsNot(self.containers.window, old_window)
        self.assertEqual(self.recipes._grid_slots, {})
        self.assertEqual(self.inventory.slots[0].stack_size, 1)

    async def test_late_grid_verdict_after_dimension_change_only_updates_carried_slot(self):
        b = self.binding
        self.open_window("WORKBENCH")
        self.change_slot(b.ContainerID.INVENTORY, 0, item(b, 1, 2, 23))
        self.change_slot(124, 33, item(b, 1, 2, 31))

        def reopen():
            self.emit(b.ChangeDimensionPacket(dimension_id=1))
            self.assertIsNone(self.containers.window)
            self.assertEqual(self.recipes._grid_slots, {})
            self.open_window("WORKBENCH")
            self.change_slot(124, 32, item(b, 5, 2, 61))

        self.reply_to_request([(PLAYER, 0, 1, 44), (GRID, 32, 1, 50)], before=reopen)
        result = await self.recipes.stage_grid(0, 32)
        self.assertEqual(result.result.name, "SUCCESS")
        carried = self.inventory.slots[0]
        grid = self.recipes._grid_slots[32]
        self.assertEqual((carried.id, carried.stack_size, carried.net_id_variant), (1, 1, 44))
        self.assertEqual((grid.id, grid.stack_size, grid.net_id_variant), (5, 2, 61))

    async def test_grid_craft_keeps_later_slot_data_after_a_matching_verdict(self):
        b = self.binding
        self.open_window()
        self.recipes.load(SimpleNamespace(clear_recipes=True, shapeless_recipes=[server_recipe()]))
        self.change_slot(124, 28, item(b, 1, 2, 23))

        def later_updates():
            self.change_slot(124, 28, item(b, 5, 2, 52))
            self.change_slot(b.ContainerID.INVENTORY, 8, item(b, 306, 2, 53))

        self.reply_to_request([(GRID, 28, 1, 24), (PLAYER, 8, 4, 51)], after=later_updates)
        result = await self.recipes.craft_grid(19, 8)
        self.assertEqual(result.result.name, "SUCCESS")
        grid = self.recipes._grid_slots[28]
        output = self.inventory.slots[8]
        self.assertEqual((grid.id, grid.stack_size, grid.net_id_variant), (5, 2, 52))
        self.assertEqual((output.id, output.stack_size, output.net_id_variant), (306, 2, 53))
