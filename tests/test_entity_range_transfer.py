"""Exercise transfers through native minecart packets."""

import asyncio
import unittest
from unittest.mock import patch

from endpilot import Bot, RangeTransferResult, transfer_item, transfer_range
from endpilot.protocol import write_batch

from test_scheduled_input import ManualClock, Wire


CART = "LEVEL_ENTITY_CONTAINER"
PLAYER = "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"
PAPER = 306


class EntityRangeTransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("CartRange", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)
        self.wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        for packet in (b.StartGamePacket(runtime_id=7, entity_id=17),
                       b.ItemRegistryPacket(items=[b.ItemData(id=PAPER, name="minecraft:paper")]),
                       self.actor(31, -201), self.actor(32, -202),
                       b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN)):
            self.wire.push(packet)
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132")
        await self.deliver(b.MovePlayerPacket(player_id=7, pos=b.Vec3(x=0.5, y=101.62, z=0.5)))

    def actor(self, runtime, unique, kind="minecraft:chest_minecart"):
        return self.b.AddActorPacket(runtime_id=runtime, entity_id=unique, actor_type=kind,
                                     pos=self.b.Vec3(x=1.5, y=100, z=0.5))

    def opened(self, unique, window_type="CONTAINER"):
        b = self.b
        return b.ContainerOpenPacket(container_id=2, entity_unique_id=unique, type=b.ContainerType[window_type],
                                     pos=b.BlockPos(x=1, y=100, z=0))

    def closed(self, window_type="CONTAINER", *, server_initiated=True):
        b = self.b
        return b.ContainerClosePacket(container_id=2, container_type=b.ContainerType[window_type],
                                      server_initiated_close=server_initiated)

    def stack(self, amount=0, net=None):
        return self.b.SerializedNetworkItemStackDescriptor(
            id=PAPER if amount else 0, stack_size=amount, net_id_variant=net,
        )

    def contents(self, *filled, count=27):
        slots = [self.stack() for _ in range(count)]
        for index, amount, net in filled:
            slots[index] = self.stack(amount, net)
        return self.b.InventoryContentPacket(inventory_id=2, slots=slots)

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        passed = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(map(self.bot.protocol.encode, (*packets, marker))))
        await passed

    async def player_slots(self, *filled):
        b = self.b
        await self.deliver(*(b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=index,
                                                   item=self.stack(amount, net)) for index, amount, net in filled))

    def cart_slot(self, index, amount, net, *, user_data=b""):
        stack = self.stack(amount, net)
        stack.user_data_buffer = user_data
        return self.b.InventorySlotPacket(inventory_id=2, slot=index, item=stack)

    def start(self, operation):
        task = asyncio.create_task(operation)
        self.addAsyncCleanup(self.drain, task)
        return task

    async def drain(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def submit(self, packet_id, operation):
        event = asyncio.Event()
        unsubscribe = self.bot.packets.subscribe_sent(lambda sent: event.set() if sent == packet_id else None)
        try:
            task = None if operation is None else self.start(operation)
            await asyncio.wait_for(event.wait(), 1)
            return task, self.wire.sent[-1]
        finally:
            unsubscribe()

    async def open_cart(self, runtime, unique, *filled):
        pending, sent = await self.submit(self.b.InventoryTransactionPacket.ID,
                                          self.bot.open_entity_container(runtime, timeout=2))
        self.assertEqual(int(sent.variant_transaction.runtime_id), runtime)
        await self.deliver(self.opened(unique), self.contents(*filled))
        self.assertIs(await pending, self.bot.open_window)

    def transfer(self, count, *, direction="take", cart=range(2), player=range(9, 11)):
        source, destination = (cart, player) if direction == "take" else (player, cart)
        return transfer_range(self.bot, direction=direction, item="minecraft:paper", count=count,
                              source_slots=source, destination_slots=destination, timeout=2)

    async def request(self, operation=None):
        task, sent = await self.submit(self.b.ItemStackRequestPacket.ID, operation)
        request, = sent.requests
        return task, request

    async def verdict(self, request, *slots, following_request=False):
        b = self.b
        packet = b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=request.client_request_id, result=b.ItemStackNetResult.SUCCESS,
            containers=[b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName[side], dynamic_id=None),
                slots=[b.ItemStackResponseSlotInfo(
                    requested_slot=index, slot=index, amount=amount,
                    item_stack_net_id=None if net is None else b.ItemStackNetId(raw_id=net),
                )],
            ) for side, index, amount, net in slots],
        )])
        if not following_request:
            await self.deliver(packet)
            return
        async def push():
            await self.deliver(packet)
        task = self.start(push())
        _, following = await self.request()
        await task
        return following

    def assert_action(self, request, quantity, source, destination, *, taking=True):
        action, = request.actions
        name = "TakeActionData" if taking else "PlaceActionData"
        self.assertIsInstance(action, getattr(self.b.ItemStackRequestCereal, name))
        self.assertEqual(action.amount, quantity)
        for address, (side, slot, net) in zip((action.source, action.destination), (source, destination), strict=True):
            self.assertEqual((address.full_container_name.name.name, address.full_container_name.dynamic_id,
                              address.slot, int(address.net_id_variant)), (side, None, slot, net))

    def requests(self):
        return [packet for packet in self.wire.sent if isinstance(packet, self.b.ItemStackRequestPacket)]

    async def test_named_and_ordinary_hopper_carts_transfer_the_fifth_slot(self):
        for named in (True, False):
            with self.subTest(named=named):
                await self.deliver(self.actor(33, -203, "minecraft:hopper_minecart"))
                operation = (self.bot.open_entity_container(33, timeout=2) if named
                             else self.bot.interact_entity(33))
                pending, sent = await self.submit(self.b.InventoryTransactionPacket.ID, operation)
                self.assertEqual(int(sent.variant_transaction.runtime_id), 33)
                await self.deliver(self.opened(-203, "HOPPER"), self.contents((4, 5, 31), count=5))
                opened = await pending
                window = self.bot.open_window
                if named:
                    self.assertIs(opened, window)
                self.assertEqual((window.container_type, window.entity_unique_id), ("HOPPER", -203))
                self.assertEqual(set(await self.bot.wait_for_container_slots()), set(range(5)))
                await self.player_slots((9, 0, None))

                taking, request = await self.request(self.bot.take_from_container(4, 9, 2))
                self.assert_action(request, 2, (CART, 4, 31), (PLAYER, 9, 0))
                await self.verdict(request, (CART, 4, 3, 31), (PLAYER, 9, 2, 41))
                self.assertEqual((await taking).result.name, "SUCCESS")
                putting, request = await self.request(self.bot.put_into_container(9, 4, 1))
                self.assert_action(request, 1, (PLAYER, 9, 41), (CART, 4, 31), taking=False)
                await self.verdict(request, (CART, 4, 4, 31), (PLAYER, 9, 1, 41))
                self.assertEqual((await putting).result.name, "SUCCESS")
                self.assertEqual(self.bot.container_slots[4].stack_size, 4)
                self.assertEqual(self.bot.inventory_slots[9].stack_size, 1)
                await self.deliver(self.closed("HOPPER"))

    async def test_hopper_range_and_whole_window_use_the_same_exact_slots(self):
        await self.deliver(self.actor(33, -203, "minecraft:hopper_minecart"), self.opened(-203, "HOPPER"))
        self.assertEqual(await self.transfer(1), RangeTransferResult(1, (), "unobserved"))
        await self.player_slots((9, 0, None))
        taking = self.start(transfer_item(self.bot, direction="take", item="minecraft:paper", count=3))
        await asyncio.sleep(0)
        self.assertFalse(taking.done())
        self.assertEqual(self.requests(), [])
        sent = self.start(self.request())
        await asyncio.sleep(0)
        await self.deliver(self.contents((4, 5, 31), count=5))
        _, request = await sent
        self.assert_action(request, 3, (CART, 4, 31), (PLAYER, 9, 0))
        await self.verdict(request, (CART, 4, 2, 31), (PLAYER, 9, 3, 41))
        result = await taking
        self.assertEqual((result.confirmed_count, result.stop_reason, len(result.steps)), (3, "complete", 1))

        putting, request = await self.request(self.transfer(1, direction="put", cart=range(4, 5)))
        self.assert_action(request, 1, (PLAYER, 9, 41), (CART, 4, 31), taking=False)
        await self.verdict(request, (CART, 4, 3, 31), (PLAYER, 9, 2, 41))
        result = await putting
        self.assertEqual((result.confirmed_count, result.stop_reason, len(result.steps)), (1, "complete", 1))
        self.assertEqual(self.bot.container_slots[4].stack_size, 3)
        self.assertEqual(self.bot.inventory_slots[9].stack_size, 2)

    async def test_hopper_transfers_require_the_five_slot_observation_and_a_slot_within_it(self):
        await self.deliver(self.actor(33, -203, "minecraft:hopper_minecart"), self.opened(-203, "HOPPER"),
                           self.contents((4, 5, 31), count=6))
        await self.player_slots((9, 1, 41))
        with self.assertRaisesRegex(RuntimeError, "unsupported slot count"):
            await self.bot.take_from_container(4, 9)
        with self.assertRaisesRegex(RuntimeError, "unsupported slot count"):
            await self.bot.put_into_container(9, 4)
        with self.assertRaisesRegex(RuntimeError, "unsupported slot count"):
            await self.transfer(1, cart=range(4, 5))
        with self.assertRaisesRegex(RuntimeError, "unsupported slot count"):
            await transfer_item(self.bot, direction="take", item="minecraft:paper", count=1)

        await self.deliver(self.contents((4, 5, 31), count=5), self.cart_slot(5, 1, 32))
        with self.assertRaisesRegex(ValueError, r"range\(5\)"):
            await self.bot.take_from_container(5, 9)
        with self.assertRaisesRegex(ValueError, r"range\(5\)"):
            await self.bot.put_into_container(9, 5)
        with self.assertRaisesRegex(ValueError, r"0\.\.5"):
            await self.transfer(1, cart=range(5, 6))
        self.assertEqual(self.requests(), [])

    async def test_wrong_hopper_windows_are_readable_and_closeable(self):
        await self.deliver(self.actor(33, -203, "minecraft:hopper_minecart"))
        await self.player_slots((9, 1, 41))
        cases = ((True, -203, "CONTAINER"), (True, -201, "HOPPER"), (False, -203, "CONTAINER"))
        for named, unique, window_type in cases:
            with self.subTest(named=named, unique=unique, window_type=window_type):
                operation = (self.bot.open_entity_container(33, timeout=2) if named
                             else self.bot.interact_entity(33))
                pending, _ = await self.submit(self.b.InventoryTransactionPacket.ID, operation)
                await self.deliver(self.opened(unique, window_type), self.contents((4, 5, 31), count=5))
                if named:
                    with self.assertRaisesRegex(RuntimeError, "different container"):
                        await pending
                else:
                    await pending
                window = self.bot.open_window
                self.assertEqual((window.container_type, window.entity_unique_id), (window_type, unique))
                self.assertEqual((await self.bot.wait_for_container_slots())[4].stack_size, 5)
                with self.assertRaisesRegex(RuntimeError, "unbound entity container"):
                    await self.bot.take_from_container(4, 9, timeout=0.25)
                with self.assertRaisesRegex(RuntimeError, "unbound entity container"):
                    await self.bot.put_into_container(9, 4, timeout=0.25)
                with self.assertRaisesRegex(RuntimeError, "bound"):
                    await self.transfer(1, cart=range(4, 5))
                closing, _ = await self.submit(self.b.ContainerClosePacket.ID, self.bot.close_container(timeout=2))
                await self.deliver(self.closed(window_type, server_initiated=False))
                self.assertIs(await closing, window)
                self.assertIsNone(self.bot.open_window)
        self.assertEqual(self.requests(), [])

    async def test_block_hopper_remains_available_for_exact_transfer(self):
        await self.deliver(self.opened(-1, "HOPPER"), self.contents((4, 5, 31), count=5))
        await self.player_slots((9, 0, None))
        with self.assertRaisesRegex(RuntimeError, "bound to a hopper minecart"):
            await self.transfer(1, cart=range(4, 5))
        taking, request = await self.request(self.bot.take_from_container(4, 9, 2))
        self.assert_action(request, 2, (CART, 4, 31), (PLAYER, 9, 0))
        await self.verdict(request, (CART, 4, 3, 31), (PLAYER, 9, 2, 41))
        self.assertEqual((await taking).result.name, "SUCCESS")

    async def test_hopper_instance_reuse_before_wire_write_rejects_exact_and_range_transfers(self):
        b = self.b
        send = self.bot.packets.send
        for exact in (True, False):
            with self.subTest(exact=exact):
                await self.deliver(self.actor(33, -203, "minecraft:hopper_minecart"), self.opened(-203, "HOPPER"),
                                   self.contents((4, 5, 31), count=5))
                await self.player_slots((9, 0, None))
                entered, released = asyncio.Event(), asyncio.Event()

                async def held(packet, **kwargs):
                    if isinstance(packet, b.ItemStackRequestPacket):
                        entered.set()
                        await released.wait()
                    await send(packet, **kwargs)

                with patch.object(self.bot.packets, "send", side_effect=held):
                    operation = (self.bot.take_from_container(4, 9) if exact else
                                 self.transfer(1, cart=range(4, 5)))
                    moving = self.start(operation)
                    await asyncio.wait_for(entered.wait(), 1)
                    await self.deliver(b.RemoveActorPacket(entity_id=-203),
                                       self.actor(33, -203, "minecraft:hopper_minecart"))
                    released.set()
                    if exact:
                        with self.assertRaisesRegex(RuntimeError, "hopper minecart is no longer observed"):
                            await moving
                    else:
                        self.assertEqual(await moving, RangeTransferResult(1, (), "window_changed"))
                self.assertEqual(self.requests(), [])
                self.assertEqual(self.bot.container_slots[4].stack_size, 5)
                await self.deliver(self.closed("HOPPER"))

    async def test_hopper_layout_round_trip_prevents_late_exact_slot_writeback(self):
        await self.deliver(self.actor(33, -203, "minecraft:hopper_minecart"), self.opened(-203, "HOPPER"),
                           self.contents((4, 5, 31), count=5))
        await self.player_slots((9, 0, None))
        taking, request = await self.request(self.bot.take_from_container(4, 9, 2))
        self.assert_action(request, 2, (CART, 4, 31), (PLAYER, 9, 0))
        await self.deliver(self.contents(count=6), self.contents((4, 8, 31), count=5))
        await self.verdict(request, (CART, 4, 3, 31), (PLAYER, 9, 2, 41))
        self.assertEqual((await taking).result.name, "SUCCESS")
        self.assertEqual((self.bot.container_slots[4].stack_size, self.bot.container_slots[4].net_id_variant), (8, 31))
        self.assertEqual(self.bot.inventory_slots[9].stack_size, 2)

    async def test_take_and_put_use_observed_cart_addresses_and_count_actual_moves(self):
        b = self.b
        await self.open_cart(31, -201, (0, 7, 31), (1, 12, 32), (2, 63, 41))
        await self.player_slots((9, 62, 12), (10, 0, None))
        taking, first = await self.request(self.transfer(8))
        self.assert_action(first, 2, (CART, 0, 31), (PLAYER, 9, 12))
        await self.deliver(b.MoveActorAbsolutePacket(move_data=b.MoveActorAbsoluteData(
            runtime_id=31, pos=b.Vec3(x=5.5, y=100, z=0.5),
        )))
        await self.deliver(self.cart_slot(0, 5, 31))
        await self.player_slots((9, 64, 12))
        second = await self.verdict(first, (CART, 0, 5, 31), (PLAYER, 9, 64, 12), following_request=True)
        self.assert_action(second, 5, (CART, 0, 31), (PLAYER, 10, 0))
        third = await self.verdict(second, (CART, 0, 0, None), (PLAYER, 10, 5, 52), following_request=True)
        self.assert_action(third, 1, (CART, 1, 32), (PLAYER, 10, 52))
        await self.verdict(third, (CART, 1, 11, 32), (PLAYER, 10, 6, 52))
        result = await taking
        self.assertEqual((result.confirmed_count, result.stop_reason), (8, "complete"))
        self.assertEqual([step.confirmed for step in result.steps], [2, 5, 1])

        putting, first = await self.request(self.transfer(4, direction="put", cart=range(2, 4)))
        self.assert_action(first, 1, (PLAYER, 9, 12), (CART, 2, 41), taking=False)
        second = await self.verdict(first, (PLAYER, 9, 63, 12), (CART, 2, 64, 41), following_request=True)
        self.assert_action(second, 3, (PLAYER, 9, 12), (CART, 3, 0), taking=False)
        third = await self.verdict(second, (PLAYER, 9, 61, 12), (CART, 3, 2, 72), following_request=True)
        self.assert_action(third, 1, (PLAYER, 9, 12), (CART, 3, 72), taking=False)
        await self.verdict(third, (PLAYER, 9, 60, 12), (CART, 3, 3, 72))
        result = await putting
        self.assertEqual((result.confirmed_count, result.stop_reason), (4, "complete"))
        self.assertEqual([step.confirmed for step in result.steps], [1, 2, 1])

    async def test_late_reply_writes_only_its_player_slot_after_same_window_id_opens_other_cart(self):
        await self.open_cart(31, -201, (0, 1, 31), (1, 5, 32))
        await self.player_slots((9, 0, None), (10, 0, None))
        moving, old = await self.request(self.transfer(3, player=range(9, 10)))
        self.assertEqual(await self.transfer(1, cart=range(1), player=range(10, 11)),
                         RangeTransferResult(1, (), "unconfirmed"))
        await self.deliver(self.closed())
        await self.open_cart(32, -202, (0, 7, 61))
        independent, fresh = await self.request(self.transfer(1, cart=range(1), player=range(10, 11)))
        await self.verdict(fresh, (CART, 0, 6, 61), (PLAYER, 10, 1, 62))
        self.assertEqual((await independent).confirmed_count, 1)
        await self.verdict(old, (CART, 0, 0, None), (PLAYER, 9, 1, 41))
        result = await moving
        self.assertEqual((result.confirmed_count, result.stop_reason, len(result.steps)), (1, "window_changed", 1))
        self.assertEqual(self.bot.open_window.entity_unique_id, -202)
        self.assertEqual((self.bot.container_slots[0].stack_size, self.bot.container_slots[0].net_id_variant), (6, 61))
        self.assertEqual((self.bot.inventory_slots[9].net_id_variant, self.bot.inventory_slots[10].net_id_variant),
                         (41, 62))
        self.assertEqual(len(self.requests()), 2)

    async def test_runtime_reused_before_first_wire_write_sends_nothing(self):
        b = self.b
        await self.open_cart(31, -201, (0, 2, 31))
        await self.player_slots((9, 0, None))
        send = self.bot.packets.send
        entered, released = asyncio.Event(), asyncio.Event()

        async def held(packet, **kwargs):
            if isinstance(packet, b.ItemStackRequestPacket):
                entered.set()
                await released.wait()
            await send(packet, **kwargs)

        with patch.object(self.bot.packets, "send", side_effect=held):
            task = self.start(self.transfer(2, player=range(9, 10)))
            await asyncio.wait_for(entered.wait(), 1)
            await self.deliver(b.RemoveActorPacket(entity_id=-201), self.actor(31, -201))
            released.set()
            self.assertEqual(await task, RangeTransferResult(2, (), "window_changed"))
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.bot.container_slots[0].stack_size, 2)

    async def test_removed_actor_cannot_receive_a_late_write_through_its_still_open_window(self):
        b = self.b
        await self.open_cart(31, -201, (0, 1, 31), (1, 5, 32))
        await self.player_slots((9, 0, None))
        pending, request = await self.request(self.transfer(3, player=range(9, 10)))
        original_window = self.bot.open_window
        await self.deliver(b.RemoveActorPacket(entity_id=-201), self.actor(31, -201))
        self.assertIs(self.bot.open_window, original_window)
        self.assertEqual(await self.transfer(1), RangeTransferResult(1, (), "window_changed"))
        await self.verdict(request, (CART, 0, 0, None), (PLAYER, 9, 1, 41))
        result = await pending
        self.assertEqual((result.confirmed_count, result.stop_reason, len(result.steps)), (1, "window_changed", 1))
        self.assertEqual(self.bot.container_slots[0].stack_size, 1)
        self.assertEqual(self.bot.inventory_slots[9].stack_size, 1)
        self.assertEqual(len(self.requests()), 1)

    async def test_new_metadata_and_aba_cart_slot_observations_cannot_be_overwritten_or_counted(self):
        for aba in (False, True):
            with self.subTest(aba=aba):
                await self.open_cart(31, -201, (0, 7, 31))
                await self.player_slots((9, 0, None))
                pending, request = await self.request(self.transfer(2))
                self.assert_action(request, 2, (CART, 0, 31), (PLAYER, 9, 0))
                if aba:
                    await self.deliver(self.cart_slot(0, 6, 31), self.cart_slot(0, 7, 31))
                else:
                    await self.deliver(self.cart_slot(0, 8, 31, user_data=b"new metadata"))
                await self.verdict(request, (CART, 0, 5, 31), (PLAYER, 9, 2, 41))
                result = await pending
                self.assertEqual((result.confirmed_count, result.stop_reason, len(result.steps)),
                                 (0, "unconfirmed", 1))
                self.assertIsNone(result.steps[0].confirmed)
                self.assertEqual(result.steps[0].verdict, "SUCCESS")
                observed = self.bot.container_slots[0]
                self.assertEqual((observed.stack_size, observed.user_data_buffer),
                                 (7, b"") if aba else (8, b"new metadata"))
                self.assertEqual(self.bot.inventory_slots[9].stack_size, 2)
                await self.deliver(self.closed())
        self.assertEqual(len(self.requests()), 2)

    async def test_write_gate_rechecks_the_cart_stack_and_player_stack_before_sending(self):
        b = self.b
        for cart_changes in (False, True):
            with self.subTest(cart_changes=cart_changes):
                await self.open_cart(31, -201, (0, 7, 31))
                await self.player_slots((9, 62, 12))
                send = self.bot.packets.send
                entered, released = asyncio.Event(), asyncio.Event()

                async def held(packet, **kwargs):
                    if isinstance(packet, b.ItemStackRequestPacket):
                        entered.set()
                        await released.wait()
                    await send(packet, **kwargs)

                with patch.object(self.bot.packets, "send", side_effect=held):
                    pending = self.start(self.transfer(2))
                    await asyncio.wait_for(entered.wait(), 1)
                    if cart_changes:
                        await self.deliver(self.cart_slot(0, 7, 33))
                    else:
                        await self.player_slots((9, 62, 14))
                    released.set()
                    with self.assertRaisesRegex(RuntimeError, "slots changed before submission") as stopped:
                        await pending
                    self.assertEqual(stopped.exception.transfer_progress.confirmed_count, 0)
                self.assertEqual(len(self.requests()), 0)
                await self.deliver(self.closed())

    async def test_layout_round_trip_or_dimension_change_invalidates_late_cart_writeback(self):
        b = self.b
        for dimension_change in (False, True):
            with self.subTest(dimension_change=dimension_change):
                await self.open_cart(31, -201, (0, 1, 31), (1, 5, 32))
                await self.player_slots((9, 0, None))
                pending, request = await self.request(self.transfer(3, player=range(9, 10)))
                if dimension_change:
                    await self.deliver(b.ChangeDimensionPacket(dimension_id=1))
                else:
                    await self.deliver(self.contents(count=28), self.contents((0, 9, 61)))
                await self.verdict(request, (CART, 0, 0, None), (PLAYER, 9, 1, 41))
                result = await pending
                self.assertEqual((result.confirmed_count, result.stop_reason, len(result.steps)), (1, "window_changed", 1))
                self.assertEqual(self.bot.inventory_slots[9].stack_size, 1)
                if dimension_change:
                    self.assertIsNone(self.bot.open_window)
                else:
                    self.assertEqual(self.bot.container_slots[0].net_id_variant, 61)
                    await self.deliver(self.closed())
        self.assertEqual(len(self.requests()), 2)

    async def test_only_bound_chest_minecarts_with_observed_27_slots_are_eligible(self):
        await self.deliver(self.opened(-201))
        self.assertEqual(await self.transfer(1), RangeTransferResult(1, (), "unobserved"))
        with self.assertRaisesRegex(ValueError, r"0\.\.27"):
            await self.transfer(1, cart=range(27, 28))
        await self.deliver(self.contents(count=28))
        with self.assertRaisesRegex(RuntimeError, "unsupported slot count"):
            await self.transfer(1)
        await self.deliver(self.actor(33, -203, "minecraft:hopper_minecart"), self.opened(-203), self.contents())
        with self.assertRaisesRegex(RuntimeError, "chest minecart"):
            await self.transfer(1)
        await self.deliver(self.opened(-204), self.contents())
        with self.assertRaisesRegex(RuntimeError, "chest minecart"):
            await self.transfer(1)
        self.assertEqual(self.requests(), [])
