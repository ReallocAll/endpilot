import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bedrock_protocol import nbt

from endpilot import Bot
from endpilot.block_data import _catalog
from endpilot.blocks import BlockObservation
from endpilot.containers import Containers, ContainerWindow
from endpilot.inventory import Inventory
from endpilot.protocol import Protocol, RawPacket, write_batch
from test_scheduled_input import ManualClock, Wire


class ContainerTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_uses_selected_item_and_waits_for_server_window(self):
        codec = Protocol(2193)
        b = codec.binding
        callbacks = []
        response = SimpleNamespace(
            container_id=5, type=SimpleNamespace(name="CONTAINER"),
            pos=b.BlockPos(x=1, y=100, z=0), entity_unique_id=-1,
        )
        raw = RawPacket(b.ContainerOpenPacket.ID, b"window")
        protocol = SimpleNamespace(
            number=codec.number, binding=b, encode=codec.encode,
            decode=lambda packet: response if packet.body == b"window" else codec.decode(packet),
        )

        async def wait(packet_id, *, timeout, match):
            self.assertEqual(packet_id, b.ContainerOpenPacket.ID)
            self.assertTrue(match(raw))
            movement.input_position = (0.75, 101.62, 0.5)
            return raw

        stream = SimpleNamespace(
            protocol=protocol,
            subscribe_raw=lambda cb: callbacks.append(cb) or (lambda: callbacks.remove(cb)),
            send=AsyncMock(), wait_for_packet=wait,
        )
        movement = SimpleNamespace(
            wait_for_position_sync=AsyncMock(return_value=(0.5, 101.62, 0.5)),
            input_position=(0.5, 101.62, 0.5),
        )
        inventory = Inventory(stream, 7)
        containers = Containers(stream, movement, inventory)
        blocks = SimpleNamespace(observed_block=lambda pos: BlockObservation(pos, 1234, 0, False))
        try:
            item = b.SerializedNetworkItemStackDescriptor(
                id=42, stack_size=1, aux_value=0, net_id_variant=None,
                block_runtime_id=0, user_data_buffer=b"",
            )
            inventory._set_slot(2, item)
            await inventory.select_hotbar(2)
            stream.send.reset_mock()
            opened = await containers.open((1, 100, 0), blocks, face=4)
            self.assertEqual(opened, ContainerWindow(5, "CONTAINER", (1, 100, 0), -1))
            movement.wait_for_position_sync.assert_awaited_once_with(15)
            self.assertEqual(stream.send.await_count, 1)
            sent = codec.decode(codec.encode(stream.send.await_args.args[0]))
            self.assertIsInstance(sent, b.InventoryTransactionPacket)
            tx = sent.variant_transaction
            self.assertEqual(tx.action_type, b.ItemUseInventoryTransaction.ActionType.PLACE)
            self.assertEqual(tx.trigger_type, b.ItemUseInventoryTransaction.TriggerType.PLAYER_INPUT)
            self.assertEqual(int(tx.face), 4)
            self.assertEqual((tx.click_pos.x, tx.click_pos.y, tx.click_pos.z), (0.0, 0.5, 0.5))
            self.assertEqual((tx.pos.x, tx.pos.y, tx.pos.z), (1, 100, 0))
            self.assertEqual((tx.from_pos.x, tx.from_pos.z), (0.75, 0.5))
            self.assertAlmostEqual(tx.from_pos.y, 101.62, places=4)
            self.assertEqual(int(tx.target_block_id), 1234)
            self.assertEqual(int(tx.slot), 2)
            self.assertEqual(int(tx.item.id), 42)
            self.assertEqual(tx.transaction.actions, [])
            callbacks[1](raw)
            self.assertEqual(containers.window, opened)
            # Server close should clear only the same window, not a different ID.
            close = SimpleNamespace(container_id=5, server_initiated_close=False)
            stream.protocol.decode = lambda packet: close
            callbacks[1](RawPacket(b.ContainerClosePacket.ID, b"close"))
            self.assertIsNone(containers.window)
        finally:
            containers.close()
            inventory.close()
        self.assertEqual(callbacks, [])

    async def test_server_initiated_close_acknowledges_only_active_window(self):
        codec = Protocol(2193)
        b = codec.binding
        callbacks = []
        close_packets = {
            b"other": SimpleNamespace(
                container_id=9, container_type=b.ContainerType.CONTAINER,
                server_initiated_close=True,
            ),
            b"server": SimpleNamespace(
                container_id=5, container_type=b.ContainerType.CONTAINER,
                server_initiated_close=True,
            ),
            b"client": SimpleNamespace(
                container_id=5, container_type=b.ContainerType.CONTAINER,
                server_initiated_close=False,
            ),
        }
        stream = SimpleNamespace(
            protocol=SimpleNamespace(
                binding=b, decode=lambda raw: close_packets[raw.body],
            ),
            subscribe_raw=lambda cb: callbacks.append(cb) or (lambda: callbacks.remove(cb)),
            send=AsyncMock(),
        )
        containers = Containers(stream, None, None)
        opened = ContainerWindow(5, "CONTAINER", (1, 100, 0), -1)
        try:
            containers.window = opened
            callbacks[0](RawPacket(b.ContainerClosePacket.ID, b"other"))
            self.assertEqual(containers.window, opened)
            await asyncio.sleep(0)
            stream.send.assert_not_awaited()

            callbacks[0](RawPacket(b.ContainerClosePacket.ID, b"server"))
            self.assertIsNone(containers.window)
            self.assertIsNone(containers.slots)
            await asyncio.sleep(0)
            self.assertEqual(stream.send.await_count, 1)
            sent = codec.decode(codec.encode(stream.send.await_args.args[0]))
            self.assertIsInstance(sent, b.ContainerClosePacket)
            self.assertEqual(int(sent.container_id), 5)
            self.assertEqual(sent.container_type, b.ContainerType.CONTAINER)
            self.assertTrue(sent.server_initiated_close)

            callbacks[0](RawPacket(b.ContainerClosePacket.ID, b"server"))
            await asyncio.sleep(0)
            self.assertEqual(stream.send.await_count, 1)

            containers.window = opened
            callbacks[0](RawPacket(b.ContainerClosePacket.ID, b"client"))
            self.assertIsNone(containers.window)
            await asyncio.sleep(0)
            self.assertEqual(stream.send.await_count, 1)
        finally:
            containers.close()
        self.assertEqual(callbacks, [])

    async def test_close_waits_for_matching_server_window_and_serializes_fields(self):
        codec = Protocol(2193)
        b = codec.binding
        callbacks = []
        close_raw = RawPacket(b.ContainerClosePacket.ID, b"close")
        different_raw = RawPacket(b.ContainerClosePacket.ID, b"other")
        response = SimpleNamespace(container_id=5, server_initiated_close=False)
        other = SimpleNamespace(container_id=9)
        protocol = SimpleNamespace(
            binding=b, encode=codec.encode,
            decode=lambda packet: response if packet.body == b"close" else other,
        )

        async def wait(packet_id, *, timeout, match):
            self.assertEqual(packet_id, b.ContainerClosePacket.ID)
            self.assertEqual(timeout, 3)
            self.assertFalse(match(different_raw))
            self.assertTrue(match(close_raw))
            callbacks[0](different_raw)
            self.assertEqual(containers.window, opened)
            callbacks[0](close_raw)
            return close_raw

        stream = SimpleNamespace(
            protocol=protocol,
            subscribe_raw=lambda cb: callbacks.append(cb) or (lambda: callbacks.remove(cb)),
            send=AsyncMock(), wait_for_packet=wait,
        )
        containers = Containers(stream, None, None)
        opened = ContainerWindow(5, "CONTAINER", (1, 100, 0), -1)
        containers.window = opened
        try:
            self.assertEqual(await containers.close_window(timeout=3), opened)
            self.assertIsNone(containers.window)
            sent = codec.decode(codec.encode(stream.send.await_args.args[0]))
            self.assertIsInstance(sent, b.ContainerClosePacket)
            self.assertEqual(int(sent.container_id), 5)
            self.assertEqual(sent.container_type, b.ContainerType.CONTAINER)
            self.assertFalse(sent.server_initiated_close)
            stream.send.reset_mock()
            self.assertIsNone(await containers.close_window())
            stream.send.assert_not_awaited()
        finally:
            containers.close()

    async def test_window_slots_are_server_reported_and_scoped_to_window(self):
        codec = Protocol(2193)
        b = codec.binding
        callbacks = []
        first = SimpleNamespace(id=3, stack_size=4)
        updated = SimpleNamespace(id=3, stack_size=2)
        transacted = SimpleNamespace(id=3, stack_size=1)
        messages = {
            b"open": SimpleNamespace(
                container_id=5, type=SimpleNamespace(name="CONTAINER"),
                pos=b.BlockPos(x=1, y=100, z=0), entity_unique_id=-1,
            ),
            b"other": SimpleNamespace(inventory_id=0, slots=[first]),
            b"contents": SimpleNamespace(inventory_id=5, slots=[first]),
            b"slot": SimpleNamespace(inventory_id=5, slot=0, item=updated),
            b"transaction": SimpleNamespace(variant_transaction=SimpleNamespace(
                transaction=SimpleNamespace(actions=[SimpleNamespace(
                    source=SimpleNamespace(
                        type=SimpleNamespace(name="CONTAINER_INVENTORY"), container_id=5,
                    ),
                    slot=0, to_item_descriptor=transacted,
                )]),
            )),
            b"other_transaction": SimpleNamespace(variant_transaction=SimpleNamespace(
                transaction=SimpleNamespace(actions=[SimpleNamespace(
                    source=SimpleNamespace(
                        type=SimpleNamespace(name="CONTAINER_INVENTORY"), container_id=0,
                    ),
                    slot=0, to_item_descriptor=transacted,
                )]),
            )),
            b"close": SimpleNamespace(container_id=5, server_initiated_close=False),
        }
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: messages[raw.body]),
            subscribe_raw=lambda cb: callbacks.append(cb) or (lambda: callbacks.remove(cb)),
        )
        containers = Containers(packets, None, None)

        def inbound(packet_type, body):
            callbacks[0](RawPacket(packet_type.ID, body))

        try:
            inbound(b.ContainerOpenPacket, b"open")
            self.assertIsNone(containers.slots)
            pending = asyncio.create_task(containers.wait_for_slots(timeout=1))
            await asyncio.sleep(0)
            inbound(b.InventoryContentPacket, b"other")
            inbound(b.InventoryTransactionPacket, b"transaction")
            self.assertIsNone(containers.slots)  # Initial contents are not yet complete.
            self.assertFalse(pending.done())
            inbound(b.InventoryContentPacket, b"contents")
            self.assertEqual((await pending)[0].stack_size, 4)

            snapshot = containers.slots
            snapshot.clear()
            self.assertEqual(containers.slots[0].stack_size, 4)
            inbound(b.InventoryTransactionPacket, b"other_transaction")
            self.assertEqual(containers.slots[0].stack_size, 4)
            inbound(b.InventoryTransactionPacket, b"transaction")
            self.assertEqual(containers.slots[0].stack_size, 1)
            inbound(b.InventorySlotPacket, b"slot")
            self.assertEqual(containers.slots[0].stack_size, 2)

            inbound(b.ContainerClosePacket, b"close")
            self.assertIsNone(containers.window)
            self.assertIsNone(containers.slots)
            with self.assertRaises(RuntimeError):
                await containers.wait_for_slots(timeout=1)

            inbound(b.ContainerOpenPacket, b"open")
            waiting = asyncio.create_task(containers.wait_for_slots(timeout=1))
            await asyncio.sleep(0)
            inbound(b.ContainerClosePacket, b"close")
            inbound(b.ContainerOpenPacket, b"open")  # Server reuses numeric window ID.
            inbound(b.InventoryContentPacket, b"contents")
            with self.assertRaises(RuntimeError):
                await waiting
            self.assertEqual(containers.slots[0].stack_size, 4)
        finally:
            containers.close()
        self.assertEqual(callbacks, [])

    async def test_close_timeout_keeps_last_observed_window(self):
        codec = Protocol(2193)
        b = codec.binding
        stream = SimpleNamespace(
            protocol=SimpleNamespace(binding=b),
            subscribe_raw=lambda cb: (lambda: None),
            send=AsyncMock(),
            wait_for_packet=AsyncMock(side_effect=TimeoutError),
        )
        containers = Containers(stream, None, None)
        opened = ContainerWindow(2, "CONTAINER", (1, 100, 0), -1)
        containers.window = opened
        try:
            with self.assertRaises(TimeoutError):
                await containers.close_window()
            self.assertEqual(containers.window, opened)
            self.assertEqual(stream.send.await_count, 1)
        finally:
            containers.close()

    async def test_unknown_target_and_empty_hand_are_valid(self):
        codec = Protocol(2193)
        b = codec.binding
        raw = RawPacket(b.ContainerOpenPacket.ID, b"window")
        response = SimpleNamespace(
            container_id=7, type=SimpleNamespace(name="CONTAINER"),
            pos=b.BlockPos(x=1, y=100, z=0), entity_unique_id=-1,
        )
        stream = SimpleNamespace(
            protocol=SimpleNamespace(
                binding=b, decode=lambda _: response,
            ),
            subscribe_raw=lambda cb: (lambda: None),
            send=AsyncMock(),
            wait_for_packet=AsyncMock(return_value=raw),
        )
        inventory = Inventory(stream, 7)
        containers = Containers(stream, SimpleNamespace(
            wait_for_position_sync=AsyncMock(return_value=(0.5, 101.62, 0.5)),
            input_position=(0.5, 101.62, 0.5),
        ), inventory)
        try:
            await containers.open((1, 100, 0), SimpleNamespace(observed_block=lambda _: None))
            packet = codec.decode(codec.encode(stream.send.await_args.args[0]))
            self.assertEqual(int(packet.variant_transaction.target_block_id), 0)
            self.assertEqual(int(packet.variant_transaction.item.id), 0)
        finally:
            containers.close()
            inventory.close()


    async def test_wait_for_close_observes_server_packet_without_requesting_close(self):
        codec = Protocol(2193)
        b = codec.binding
        callbacks = []
        packets_by_body = {
            b"other": SimpleNamespace(
                container_id=9, container_type=b.ContainerType.CONTAINER,
                server_initiated_close=True,
            ),
            b"server": SimpleNamespace(
                container_id=5, container_type=b.ContainerType.CONTAINER,
                server_initiated_close=True,
            ),
        }
        unrelated = RawPacket(b.ContainerClosePacket.ID, b"other")
        matching = RawPacket(b.ContainerClosePacket.ID, b"server")

        async def wait(packet_id, *, timeout, match):
            self.assertEqual(packet_id, b.ContainerClosePacket.ID)
            self.assertEqual(timeout, 4)
            self.assertFalse(match(unrelated))
            callbacks[0](unrelated)
            self.assertIs(containers.window, opened)
            self.assertTrue(match(matching))
            callbacks[0](matching)
            return matching

        stream = SimpleNamespace(
            protocol=SimpleNamespace(
                binding=b, decode=lambda raw: packets_by_body[raw.body],
            ),
            subscribe_raw=lambda cb: callbacks.append(cb) or (lambda: callbacks.remove(cb)),
            send=AsyncMock(), wait_for_packet=wait,
        )
        containers = Containers(stream, None, None)
        opened = ContainerWindow(5, "CONTAINER", (1, 100, 0), -1)
        containers.window = opened
        try:
            received = await containers.wait_for_close(timeout=4)
            self.assertEqual(int(received.container_id), 5)
            self.assertTrue(received.server_initiated_close)
            self.assertIsNone(containers.window)
            await asyncio.sleep(0)
            # Only the existing server-close acknowledgement was sent.
            self.assertEqual(stream.send.await_count, 1)
            acknowledged = codec.decode(codec.encode(stream.send.await_args.args[0]))
            self.assertTrue(acknowledged.server_initiated_close)
            self.assertEqual(int(acknowledged.container_id), 5)
            with self.assertRaisesRegex(RuntimeError, "no container window"):
                await containers.wait_for_close()
        finally:
            containers.close()
        self.assertEqual(callbacks, [])

    async def test_wait_for_close_timeout_does_not_send_or_clear_window(self):
        b = Protocol(2193).binding
        stream = SimpleNamespace(
            protocol=SimpleNamespace(binding=b),
            subscribe_raw=lambda _: (lambda: None),
            send=AsyncMock(),
            wait_for_packet=AsyncMock(side_effect=TimeoutError),
        )
        containers = Containers(stream, None, None)
        opened = ContainerWindow(5, "TRADE", None, 42)
        containers.window = opened
        try:
            with self.assertRaises(TimeoutError):
                await containers.wait_for_close(timeout=0.01)
            self.assertIs(containers.window, opened)
            stream.send.assert_not_awaited()
        finally:
            containers.close()

class EntityContainerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("CartPlayer", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)
        self.wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        for packet in (b.StartGamePacket(runtime_id=7, entity_id=17, block_network_ids_are_hashes=True),
                       self.cart(31, -201), self.cart(32, -202),
                       b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN)):
            self.wire.push(packet)
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132")

    def cart(self, runtime_id, entity_id):
        b = self.b
        return b.AddActorPacket(runtime_id=runtime_id, entity_id=entity_id,
                                actor_type="minecraft:chest_minecart", pos=b.Vec3(x=1.5, y=100, z=0.5))

    def position(self):
        b = self.b
        return b.MovePlayerPacket(player_id=7, pos=b.Vec3(x=0.5, y=101.62, z=0.5), on_ground=True)

    def opened(self, entity_id, container_type="CONTAINER", *, x=1):
        b = self.b
        return b.ContainerOpenPacket(container_id=2, type=b.ContainerType[container_type],
                                     pos=b.BlockPos(x=x, y=100, z=0), entity_unique_id=entity_id)

    def contents(self):
        b = self.b
        return b.InventoryContentPacket(inventory_id=2, slots=[
            b.SerializedNetworkItemStackDescriptor(id=306, stack_size=5, net_id_variant=31),
        ])

    def block(self, name, *, layer=0):
        b = self.b
        palette, by_hash = _catalog()
        runtime_id = next(key for key, index in by_hash.items()
                          if palette[index] is not None and palette[index].name == name)
        return b.UpdateBlockPacket(pos=b.BlockPos(x=1, y=100, z=0), runtime_id=runtime_id, layer=layer)

    def closed(self, *, server=False):
        b = self.b
        return b.ContainerClosePacket(container_id=2, container_type=b.ContainerType.CONTAINER,
                                      server_initiated_close=server)

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        passed = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(map(self.bot.protocol.encode, (*packets, marker))))
        await passed

    def start(self, operation):
        task = asyncio.create_task(operation)
        self.addAsyncCleanup(self.cancel, task)
        return task

    async def cancel(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def submit_stack(self, operation):
        sent = asyncio.Event()
        unsubscribe = self.bot.packets.subscribe_sent(
            lambda packet_id: sent.set() if packet_id == self.b.ItemStackRequestPacket.ID else None,
        )
        try:
            task = self.start(operation)
            await asyncio.wait_for(sent.wait(), 1)
            request, = self.wire.sent[-1].requests
            return task, request
        finally:
            unsubscribe()

    async def reply_stack(self, request, *corrections):
        b = self.b
        await self.deliver(b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=request.client_request_id, result=b.ItemStackNetResult.SUCCESS,
            containers=[b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName[name]),
                slots=[b.ItemStackResponseSlotInfo(
                    requested_slot=slot, slot=slot, amount=amount,
                    item_stack_net_id=b.ItemStackNetId(raw_id=net),
                )],
            ) for name, slot, amount, net in corrections],
        )]))

    def storage_item(self, amount, net):
        return self.b.SerializedNetworkItemStackDescriptor(id=306, stack_size=amount, net_id_variant=net)

    def storage_content(self, count, *filled):
        empty = self.b.SerializedNetworkItemStackDescriptor()
        slots = [empty] * count
        for index, amount, net in filled:
            slots[index] = self.storage_item(amount, net)
        return self.b.InventoryContentPacket(inventory_id=2, slots=slots)

    def north_chest(self, x):
        b = self.b
        return b.UpdateBlockPacket(pos=b.BlockPos(x=x, y=100, z=0), runtime_id=3162850062, layer=0)

    def chest_pair(self, x, partner, *, lead):
        b = self.b
        tag = nbt.CompoundTag({"id": nbt.StringTag("Chest"), "x": nbt.IntTag(x), "y": nbt.IntTag(100),
                               "z": nbt.IntTag(0), "pairx": nbt.IntTag(partner), "pairz": nbt.IntTag(0),
                               "pairlead": nbt.ByteTag(lead)})
        return b.BlockActorDataPacket(pos=b.BlockPos(x=x, y=100, z=0), data=tag)

    def planned_take(self, index, player_slot, width, mark=None):
        return self.bot._containers._transfer(
            index, player_slot, 1, into_container=False, timeout=2, _expected_slot_count=width,
            _expected_slots=(self.bot.container_slots[index], self.bot.inventory_slots[player_slot]),
            _on_unconfirmed=mark,
        )

    async def test_storage_width_requires_full_contents_and_belongs_to_the_window_instance(self):
        b = self.b
        high_slot = b.InventorySlotPacket(inventory_id=2, slot=53, item=self.storage_item(4, 61))
        await self.deliver(self.opened(-1), high_slot)
        containers = self.bot._containers
        window = self.bot.open_window
        self.assertIsNone(containers._observed_window_slot_count(window))
        await self.deliver(self.storage_content(27, (0, 5, 31)), high_slot,
                           b.InventoryContentPacket(inventory_id=b.ContainerID.INVENTORY,
                                                    slots=[b.SerializedNetworkItemStackDescriptor()]))
        self.assertIn(53, self.bot.container_slots)
        self.assertEqual(containers._observed_window_slot_count(window), 27)
        sent_before = list(self.wire.sent)
        with self.assertRaisesRegex(RuntimeError, "slot count changed"):
            await self.planned_take(53, 0, 54)
        self.assertEqual(self.wire.sent, sent_before)
        await self.deliver(self.closed(server=True), self.opened(-1), self.storage_content(54))
        self.assertIsNone(containers._observed_window_slot_count(window))
        self.assertEqual(containers._observed_window_slot_count(self.bot.open_window), 54)

    async def test_verified_double_slot_blocks_the_matching_single_half_until_fresh_observation(self):
        b = self.b
        empty = b.SerializedNetworkItemStackDescriptor()
        await self.deliver(self.north_chest(1), self.north_chest(2),
                           self.chest_pair(1, 2, lead=1), self.chest_pair(2, 1, lead=0),
                           self.opened(-1, x=1), self.storage_content(54, (0, 7, 30), (27, 5, 31)),
                           b.InventoryContentPacket(inventory_id=b.ContainerID.INVENTORY, slots=[empty, empty, empty]))
        old, old_request = await self.submit_stack(self.planned_take(27, 0, 54))
        self.assertEqual(old_request.actions[0].source.slot, 27)
        await self.deliver(self.closed(server=True), self.north_chest(1), self.north_chest(2),
                           self.opened(-1, x=1), self.storage_content(27, (0, 7, 30)))
        independent, request = await self.submit_stack(self.planned_take(0, 1, 27))
        await self.reply_stack(request, ("LEVEL_ENTITY_CONTAINER", 0, 6, 30),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 1, 1, 41))
        await independent
        self.assertFalse(old.done())

        await self.deliver(self.closed(server=True), self.opened(-1, x=2), self.storage_content(27, (0, 5, 31)))
        sent_before = sum(isinstance(packet, b.ItemStackRequestPacket) for packet in self.wire.sent)
        self.assertIsNone(await self.planned_take(0, 2, 27))
        self.assertEqual(sum(isinstance(packet, b.ItemStackRequestPacket) for packet in self.wire.sent), sent_before)
        await self.reply_stack(old_request, ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 0, 1, 42))
        await old
        self.assertEqual(self.bot.container_slots[0].stack_size, 5)
        self.assertIsNone(await self.planned_take(0, 2, 27))

        await self.deliver(b.InventorySlotPacket(inventory_id=2, slot=0, item=self.storage_item(4, 31)))
        resumed, request = await self.submit_stack(self.planned_take(0, 2, 27))
        await self.reply_stack(request, ("LEVEL_ENTITY_CONTAINER", 0, 3, 31),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 2, 1, 43))
        self.assertEqual((await resumed).result.name, "SUCCESS")

    async def test_unverified_double_slots_use_possible_overlap_only_to_block_cross_window_sends(self):
        b = self.b
        empty = b.SerializedNetworkItemStackDescriptor()
        await self.deliver(self.north_chest(1), self.north_chest(2), self.chest_pair(1, 2, lead=1),
                           self.opened(-1, x=1), self.storage_content(54, (0, 5, 31), (27, 7, 32)),
                           b.InventoryContentPacket(inventory_id=b.ContainerID.INVENTORY, slots=[empty] * 3))
        uncertain = asyncio.Event()
        old, old_request = await self.submit_stack(self.planned_take(0, 0, 54, uncertain.set))
        same_window, request = await self.submit_stack(self.planned_take(27, 1, 54))
        self.assertEqual(request.actions[0].source.slot, 27)
        await self.reply_stack(request, ("LEVEL_ENTITY_CONTAINER", 27, 6, 32),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 1, 1, 41))
        await same_window
        await self.deliver(self.closed(server=True), self.opened(-1, x=2),
                           self.storage_content(54, (0, 5, 31), (27, 6, 32), (28, 9, 33)))
        sent_before = sum(isinstance(packet, b.ItemStackRequestPacket) for packet in self.wire.sent)
        self.assertIsNone(await self.planned_take(27, 2, 54))
        self.assertEqual(sum(isinstance(packet, b.ItemStackRequestPacket) for packet in self.wire.sent), sent_before)

        other_lane, request = await self.submit_stack(self.planned_take(28, 2, 54))
        await self.reply_stack(request, ("LEVEL_ENTITY_CONTAINER", 28, 8, 33),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 2, 1, 42))
        await other_lane
        for amount in (4, 3):
            await self.deliver(b.InventorySlotPacket(inventory_id=2, slot=0, item=self.storage_item(amount, 31)))
        self.assertFalse(uncertain.is_set())
        await self.reply_stack(old_request, ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 0, 1, 43))
        await old
        await self.deliver(b.InventorySlotPacket(inventory_id=2, slot=0, item=self.storage_item(2, 31)))
        self.assertIsNone(await self.planned_take(0, 2, 54))

        await self.deliver(self.closed(server=True), self.north_chest(14), self.opened(-1, x=14),
                           self.storage_content(54, (0, 2, 31)))
        unrelated, request = await self.submit_stack(self.planned_take(0, 2, 54))
        await self.reply_stack(request, ("LEVEL_ENTITY_CONTAINER", 0, 1, 31),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 2, 2, 42))
        self.assertEqual((await unrelated).result.name, "SUCCESS")

    async def test_double_slots_in_other_dimensions_do_not_overlap_or_receive_late_corrections(self):
        b = self.b
        empty = b.SerializedNetworkItemStackDescriptor()
        await self.deliver(self.north_chest(1), self.north_chest(2),
                           self.chest_pair(1, 2, lead=1), self.chest_pair(2, 1, lead=0),
                           self.opened(-1), self.storage_content(54, (27, 5, 31)),
                           b.InventoryContentPacket(inventory_id=b.ContainerID.INVENTORY, slots=[empty, empty]))
        old, old_request = await self.submit_stack(self.planned_take(27, 0, 54))
        await self.deliver(b.ChangeDimensionPacket(dimension_id=1), self.north_chest(1), self.north_chest(2),
                           self.chest_pair(1, 2, lead=1), self.chest_pair(2, 1, lead=0),
                           self.opened(-1), self.storage_content(54, (27, 9, 61)))
        current, current_request = await self.submit_stack(self.planned_take(27, 1, 54))
        self.assertEqual(current_request.actions[0].source.slot, 27)
        await self.reply_stack(old_request, ("LEVEL_ENTITY_CONTAINER", 27, 4, 31),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 0, 1, 41))
        await old
        self.assertEqual(self.bot.container_slots[27].stack_size, 9)
        self.assertEqual(self.bot.inventory_slots[0].stack_size, 1)
        await self.reply_stack(current_request, ("LEVEL_ENTITY_CONTAINER", 27, 8, 61),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 1, 1, 62))
        self.assertEqual((await current).result.name, "SUCCESS")
        self.assertEqual(self.bot.container_slots[27].stack_size, 8)

    async def test_same_window_width_change_separates_layouts_and_guards_late_writeback(self):
        b = self.b
        empty = b.SerializedNetworkItemStackDescriptor()
        double = self.storage_content(54, (27, 5, 31))
        await self.deliver(self.north_chest(1), self.north_chest(2),
                           self.chest_pair(1, 2, lead=1), self.chest_pair(2, 1, lead=0),
                           self.opened(-1, x=2), double,
                           b.InventoryContentPacket(inventory_id=b.ContainerID.INVENTORY, slots=[empty, empty]))
        window = self.bot.open_window
        taking, request = await self.submit_stack(self.planned_take(27, 0, 54))

        await self.deliver(self.storage_content(27, (0, 5, 31)))
        self.assertIs(self.bot.open_window, window)
        before = sum(isinstance(packet, b.ItemStackRequestPacket) for packet in self.wire.sent)
        attempt = self.start(self.planned_take(0, 1, 27))
        await asyncio.sleep(0)
        self.assertTrue(attempt.done(), "the original double slot must block the matching single slot")
        self.assertIsNone(attempt.result())
        self.assertEqual(sum(isinstance(packet, b.ItemStackRequestPacket) for packet in self.wire.sent), before)

        await self.deliver(double)
        self.assertIs(self.bot.open_window, window)
        await self.reply_stack(request, ("LEVEL_ENTITY_CONTAINER", 27, 4, 31),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 0, 1, 41))
        self.assertEqual((await taking).result.name, "SUCCESS")
        self.assertEqual(self.bot.container_slots[27].stack_size, 5)
        self.assertEqual(self.bot.inventory_slots[0].stack_size, 1)

    async def test_definite_observations_resolve_unverified_halves_one_by_one(self):
        b = self.b
        empty = b.SerializedNetworkItemStackDescriptor()
        double = self.storage_content(54, (0, 5, 31))
        await self.deliver(self.north_chest(1), self.opened(-1, x=1), double,
                           b.InventoryContentPacket(inventory_id=b.ContainerID.INVENTORY, slots=[empty, empty]))
        taking, request = await self.submit_stack(self.planned_take(0, 0, 54))
        await self.reply_stack(request, ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 0, 1, 41))
        await taking

        for half in (0, 1, 2):
            await self.deliver(self.closed(server=True), self.opened(-1, x=half),
                               self.storage_content(27, (0, 4, 32)))
            await self.deliver(self.closed(server=True), self.opened(-1, x=1), double)
            if half < 2:
                before = sum(isinstance(packet, b.ItemStackRequestPacket) for packet in self.wire.sent)
                attempt = self.start(self.planned_take(0, 1, 54))
                await asyncio.sleep(0)
                self.assertTrue(attempt.done(), "one definite observation must leave other possible halves blocked")
                self.assertIsNone(attempt.result())
                self.assertEqual(sum(isinstance(packet, b.ItemStackRequestPacket) for packet in self.wire.sent), before)

        resumed, request = await self.submit_stack(self.planned_take(0, 1, 54))
        await self.reply_stack(request, ("LEVEL_ENTITY_CONTAINER", 0, 4, 31),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 1, 1, 42))
        self.assertEqual((await resumed).result.name, "SUCCESS")
        self.assertEqual(self.bot.container_slots[0].stack_size, 4)

    async def start_open(self, runtime_id=31, *, timeout=2):
        b = self.b
        sent = asyncio.Event()
        unsubscribe = self.bot.packets.subscribe_sent(
            lambda packet_id: sent.set() if packet_id == b.InventoryTransactionPacket.ID else None,
        )
        try:
            task = self.start(self.bot.open_entity_container(runtime_id, timeout=timeout))
            await asyncio.wait_for(sent.wait(), 1)
        finally:
            unsubscribe()
        click = self.wire.sent[-1].variant_transaction
        self.assertEqual(int(click.runtime_id), runtime_id)
        self.assertEqual(click.action_type, b.ItemUseOnActorInventoryTransaction.ActionType.INTERACT)
        return task

    async def test_wrong_entity_window_is_observed_but_fails_the_requested_open(self):
        await self.deliver(self.position())
        pending = await self.start_open()
        await self.deliver(self.opened(-202), self.contents())
        with self.assertRaisesRegex(RuntimeError, "different container"):
            await pending
        self.assertEqual(self.bot.open_window.entity_unique_id, -202)
        self.assertEqual(self.bot.container_slots[0].stack_size, 5)
        sent_before = list(self.wire.sent)
        with self.assertRaisesRegex(RuntimeError, "open or opening"):
            await self.bot.open_entity_container(31)
        self.assertEqual(self.wire.sent, sent_before)

    async def test_rebound_entity_during_position_sync_sends_no_interaction(self):
        sent_before = list(self.wire.sent)
        movement = self.bot._movement
        original_wait = movement.wait_for_position_sync
        waiting = asyncio.Event()

        async def wait(*args, **kwargs):
            waiting.set()
            return await original_wait(*args, **kwargs)

        with patch.object(movement, "wait_for_position_sync", side_effect=wait):
            pending = self.start(self.bot.open_entity_container(31, timeout=2))
            await asyncio.wait_for(waiting.wait(), 1)
            await self.deliver(self.cart(31, -203), self.position())
            with self.assertRaisesRegex(RuntimeError, "no longer observed"):
                await pending
        self.assertEqual(self.wire.sent, sent_before)

    async def test_player_inventory_opened_while_syncing_prevents_the_interaction(self):
        sent_before = list(self.wire.sent)
        movement = self.bot._movement
        original_wait = movement.wait_for_position_sync
        waiting = asyncio.Event()

        async def wait(*args, **kwargs):
            waiting.set()
            return await original_wait(*args, **kwargs)

        with patch.object(movement, "wait_for_position_sync", side_effect=wait):
            pending = self.start(self.bot.open_entity_container(31, timeout=2))
            await asyncio.wait_for(waiting.wait(), 1)
            await self.deliver(self.opened(17, "INVENTORY"), self.position())
            with self.assertRaisesRegex(RuntimeError, "opened before the interaction"):
                await pending
        self.assertEqual(self.bot.open_window.container_type, "INVENTORY")
        self.assertEqual(self.wire.sent, sent_before)

    async def test_removed_target_rejects_transfers_and_preserves_server_close_acknowledgement(self):
        await self.deliver(self.position())
        pending = await self.start_open()
        await self.deliver(self.opened(-201), self.contents())
        window = await pending
        self.assertIs(window, self.bot.open_window)
        await self.deliver(self.b.RemoveActorPacket(entity_id=-201), self.cart(31, -201))
        sent_before = list(self.wire.sent)
        self.assertIs(self.bot.open_window, window)
        for transfer in (self.bot.take_from_container, self.bot.put_into_container):
            with self.assertRaisesRegex(RuntimeError, "no longer observed"):
                await transfer(0, 1)
        self.assertEqual(self.wire.sent, sent_before)

        await self.deliver(self.closed(server=True))
        self.assertIsNone(self.bot.open_window)
        acknowledged = self.wire.sent[-1]
        self.assertIsInstance(acknowledged, self.b.ContainerClosePacket)
        self.assertTrue(acknowledged.server_initiated_close)
        self.assertEqual(int(acknowledged.container_id), 2)
        with self.assertRaisesRegex(RuntimeError, "window.*unavailable"):
            await self.bot.take_from_container(0, 1)

    async def test_queued_transfer_rejects_a_reopened_window_with_the_same_numeric_id(self):
        await self.deliver(self.position())
        pending = await self.start_open()
        await self.deliver(self.opened(-201), self.contents())
        original = await pending
        inventory = self.bot._inventory
        request_item_stack = inventory._request_item_stack
        reached, release = asyncio.Event(), asyncio.Event()

        async def hold(*args, **kwargs):
            reached.set()
            await release.wait()
            return await request_item_stack(*args, **kwargs)

        with patch.object(inventory, "_request_item_stack", side_effect=hold):
            taking = self.start(self.bot.take_from_container(0, 1))
            await asyncio.wait_for(reached.wait(), 1)
            await self.deliver(self.closed())
            reopening = await self.start_open()
            await self.deliver(self.opened(-201), self.contents())
            replacement = await reopening
            self.assertEqual(replacement, original)
            self.assertIsNot(replacement, original)
            sent_before = list(self.wire.sent)
            release.set()
            with self.assertRaisesRegex(RuntimeError, "closed or changed"):
                await taking
        self.assertIs(self.bot.open_window, replacement)
        self.assertEqual(self.wire.sent, sent_before)

    async def test_runtime_reuse_back_to_the_original_unique_id_cannot_revive_its_window(self):
        await self.deliver(self.position())
        pending = await self.start_open()
        await self.deliver(self.opened(-201), self.contents())
        window = await pending
        await self.deliver(self.cart(31, -203), self.cart(31, -201))
        self.assertEqual(next(actor.entity_id for actor in self.bot.observed_entities if actor.runtime_id == 31), -201)
        self.assertIs(self.bot.open_window, window)
        sent_before = list(self.wire.sent)
        with self.assertRaisesRegex(RuntimeError, "no longer observed"):
            await self.bot.take_from_container(0, 1, timeout=0.5)
        self.assertEqual(self.wire.sent, sent_before)
        self.assertEqual(self.bot.container_slots[0].stack_size, 5)

    async def test_late_cart_open_after_timeout_with_unobserved_target_stays_readable(self):
        b = self.b
        await self.deliver(self.position())
        pending = await self.start_open(timeout=0.5)
        with self.assertRaises(TimeoutError):
            await pending
        await self.deliver(b.RemoveActorPacket(entity_id=-201), self.opened(-201), self.contents())
        window = self.bot.open_window
        self.assertEqual(window.entity_unique_id, -201)
        self.assertEqual((await self.bot.wait_for_container_slots())[0].stack_size, 5)
        sent_before = list(self.wire.sent)
        for transfer in (self.bot.take_from_container, self.bot.put_into_container):
            with self.assertRaisesRegex(RuntimeError, "unbound entity container"):
                await transfer(0, 1, timeout=0.5)
        self.assertEqual(self.wire.sent, sent_before)

        sent = asyncio.Event()
        unsubscribe = self.bot.packets.subscribe_sent(
            lambda packet_id: sent.set() if packet_id == b.ContainerClosePacket.ID else None,
        )
        try:
            closing = self.start(self.bot.close_container(timeout=2))
            await asyncio.wait_for(sent.wait(), 1)
            await self.deliver(self.closed())
            self.assertIs(await closing, window)
            self.assertIsNone(self.bot.open_window)
        finally:
            unsubscribe()

    async def test_ordinary_entity_interaction_opens_a_cart_for_normal_transfers(self):
        barrel = self.block("minecraft:barrel")
        await self.deliver(self.position(), barrel)
        await self.bot.interact_entity(31)
        await self.deliver(self.opened(-201), self.contents())
        window = self.bot.open_window
        self.assertEqual(window.entity_unique_id, -201)
        self.assertEqual(self.bot.observed_block(window.position).runtime_id, barrel.runtime_id)

        for operation, side, chest_amount, player_amount in (
            (self.bot.take_from_container(0, 1, 2), "source", 3, 2),
            (self.bot.put_into_container(1, 0, 1), "destination", 4, 1),
        ):
            task, request = await self.submit_stack(operation)
            endpoint = getattr(request.actions[0], side)
            self.assertEqual(int(endpoint.full_container_name.name), 7)
            await self.reply_stack(request, ("LEVEL_ENTITY_CONTAINER", 0, chest_amount, 31),
                                   ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 1, player_amount, 32))
            self.assertEqual((await task).result.name, "SUCCESS")
        self.assertIs(self.bot.open_window, window)
        self.assertEqual(self.bot.container_slots[0].stack_size, 4)
        self.assertEqual(self.bot.inventory_slots[1].stack_size, 1)

    async def test_barrel_address_stays_with_the_open_window_and_its_late_receipt(self):
        await self.deliver(self.block("minecraft:barrel"), self.opened(-1), self.contents())
        barrel = self.bot.open_window
        await self.deliver(self.block("minecraft:chest"))
        taking, request = await self.submit_stack(self.bot.take_from_container(0, 3))
        self.assertEqual(int(request.actions[0].source.full_container_name.name), 58)

        await self.deliver(self.closed(server=True), self.opened(-1), self.contents())
        self.assertIsNot(self.bot.open_window, barrel)
        self.assertEqual(self.bot.open_window.container_id, barrel.container_id)
        await self.reply_stack(request, ("BARREL_CONTAINER", 0, 4, 31),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 3, 1, 61))
        self.assertEqual((await taking).result.name, "SUCCESS")
        self.assertEqual((self.bot.container_slots[0].stack_size, self.bot.inventory_slots[3].stack_size), (5, 1))

        putting, request = await self.submit_stack(self.bot.put_into_container(3, 0))
        self.assertEqual(int(request.actions[0].destination.full_container_name.name), 7)
        await self.reply_stack(request, ("LEVEL_ENTITY_CONTAINER", 0, 6, 31),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 3, 0, 0))
        self.assertEqual((await putting).result.name, "SUCCESS")
        self.assertEqual((self.bot.container_slots[0].stack_size, self.bot.inventory_slots[3].stack_size), (6, 0))

    async def test_barrel_address_uses_the_current_dimensions_primary_layer(self):
        await self.deliver(self.block("minecraft:barrel"), self.opened(-1), self.contents())
        await self.deliver(self.b.ChangeDimensionPacket(dimension_id=1))
        self.assertIsNone(self.bot.open_window)
        await self.deliver(self.block("minecraft:barrel", layer=1), self.opened(-1), self.contents())
        self.assertIsNone(self.bot.observed_block(self.bot.open_window.position))
        taking, request = await self.submit_stack(self.bot.take_from_container(0, 3))
        self.assertEqual(int(request.actions[0].source.full_container_name.name), 7)
        await self.reply_stack(request, ("LEVEL_ENTITY_CONTAINER", 0, 4, 31),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 3, 1, 61))
        self.assertEqual((await taking).result.name, "SUCCESS")

        await self.deliver(self.block("minecraft:barrel"))
        putting, request = await self.submit_stack(self.bot.put_into_container(3, 0))
        self.assertEqual(int(request.actions[0].destination.full_container_name.name), 58)
        await self.reply_stack(request, ("BARREL_CONTAINER", 0, 5, 31),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 3, 0, 0))
        self.assertEqual((await putting).result.name, "SUCCESS")
        self.assertEqual((self.bot.container_slots[0].stack_size, self.bot.inventory_slots[3].stack_size), (5, 0))

    async def test_block_range_and_reopened_cart_share_only_the_player_slot(self):
        b = self.b
        empty = b.SerializedNetworkItemStackDescriptor(id=0, stack_size=0)
        await self.deliver(self.opened(-1), self.contents(),
                           b.InventoryContentPacket(inventory_id=b.ContainerID.INVENTORY, slots=[empty, empty]))
        original_window = self.bot.open_window
        unconfirmed = asyncio.Event()
        emitted = asyncio.Event()
        unsubscribe = self.bot.packets.subscribe_sent(
            lambda packet_id: emitted.set() if packet_id == b.ItemStackRequestPacket.ID else None,
        )
        self.addCleanup(unsubscribe)

        async def submit(operation):
            emitted.clear()
            task = self.start(operation)
            await asyncio.wait_for(emitted.wait(), 1)
            request, = self.wire.sent[-1].requests
            return task, request.client_request_id

        async def verdict(request_id, chest_amount, chest_net, player_slot, player_net):
            await self.deliver(b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
                client_request_id=request_id, result=b.ItemStackNetResult.SUCCESS,
                containers=[b.ItemStackResponseContainerInfo(
                    full_container_name=b.FullContainerName(name=b.ContainerEnumName[name]),
                    slots=[b.ItemStackResponseSlotInfo(
                        requested_slot=slot, slot=slot, amount=amount,
                        item_stack_net_id=b.ItemStackNetId(raw_id=net),
                    )],
                ) for name, slot, amount, net in (
                    ("LEVEL_ENTITY_CONTAINER", 0, chest_amount, chest_net),
                    ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", player_slot, 1, player_net),
                )],
            )]))

        block, block_id = await submit(self.bot._containers._transfer(
            0, 0, 1, into_container=False, timeout=2,
            _expected_slots=(self.bot.container_slots[0], self.bot.inventory_slots[0]),
            _on_unconfirmed=unconfirmed.set,
        ))
        await self.deliver(self.closed(server=True), self.opened(-201), b.InventoryContentPacket(
            inventory_id=2, slots=[b.SerializedNetworkItemStackDescriptor(id=306, stack_size=7, net_id_variant=61)],
        ))
        cart = self.bot.open_window
        self.assertIsNot(cart, original_window)
        self.assertEqual((cart.container_id, cart.position), (original_window.container_id, original_window.position))
        independent, independent_id = await submit(self.bot.take_from_container(0, 1))
        await verdict(independent_id, 6, 61, 1, 62)
        self.assertEqual((await independent).result.name, "SUCCESS")
        self.assertFalse(block.done())
        self.assertEqual(self.bot.container_slots[0].stack_size, 6)

        held_change = self.start(self.bot.wait_for_held_item_change(timeout=2))
        dependent = self.start(self.bot.put_into_container(0, 0))
        emitted.clear()
        await asyncio.sleep(0)
        self.assertFalse(dependent.done())
        await self.deliver(b.RemoveActorPacket(entity_id=-201), self.cart(31, -201))
        self.assertFalse(emitted.is_set())
        await verdict(block_id, 4, 31, 0, 32)
        self.assertEqual((await block).result.name, "SUCCESS")
        self.assertFalse(unconfirmed.is_set())
        held = await held_change
        self.assertEqual((held.slot, held.observed_item.count, held.observed_item.net_id), (0, 1, 32))
        with self.assertRaisesRegex(RuntimeError, "chest minecart is no longer observed"):
            await dependent
        self.assertFalse(emitted.is_set())
        self.assertIs(self.bot.open_window, cart)
        self.assertEqual(self.bot.container_slots[0].stack_size, 6)

    async def test_recreated_cart_before_open_waiter_registration_cannot_receive_old_click(self):
        b = self.b
        await self.deliver(self.position())
        sent_before = list(self.wire.sent)
        order = []
        wait_for_packet = self.bot.packets.wait_for_packet

        async def record_registration(packet_ids, **kwargs):
            order.append("registered")
            return await wait_for_packet(packet_ids, **kwargs)

        unsubscribe = self.bot.packets.subscribe_raw(
            lambda raw: order.append(raw.packet_id)
            if raw.packet_id in (b.RemoveActorPacket.ID, b.AddActorPacket.ID) else None,
        )
        try:
            with patch.object(self.bot.packets, "wait_for_packet", side_effect=record_registration):
                opening = self.start(self.bot.open_entity_container(31, timeout=0.5))
                # The open task is already runnable. The reader will handle this batch
                # before the waiter child scheduled at the open task's first yield.
                self.wire.incoming.put_nowait(write_batch(map(self.bot.protocol.encode, (
                    b.RemoveActorPacket(entity_id=-201), self.cart(31, -201),
                ))))
                with self.assertRaisesRegex(RuntimeError, "no longer observed"):
                    await opening
        finally:
            unsubscribe()
        self.assertEqual(order, [b.RemoveActorPacket.ID, b.AddActorPacket.ID, "registered"])
        self.assertEqual(self.wire.sent, sent_before)

    async def test_interaction_uses_the_carts_latest_position_after_waiting_for_the_player(self):
        b = self.b
        movement = self.bot._movement
        original_wait = movement.wait_for_position_sync
        waiting = asyncio.Event()
        sent = asyncio.Event()

        async def wait(*args, **kwargs):
            waiting.set()
            return await original_wait(*args, **kwargs)

        unsubscribe = self.bot.packets.subscribe_sent(
            lambda packet_id: sent.set() if packet_id == b.InventoryTransactionPacket.ID else None,
        )
        try:
            with patch.object(movement, "wait_for_position_sync", side_effect=wait):
                opening = self.start(self.bot.open_entity_container(31, timeout=2))
                await asyncio.wait_for(waiting.wait(), 1)
                await self.deliver(b.MoveActorAbsolutePacket(move_data=b.MoveActorAbsoluteData(
                    runtime_id=31, pos=b.Vec3(x=3.5, y=100, z=0.5),
                )), self.position())
                await asyncio.wait_for(sent.wait(), 1)
            transaction = self.wire.sent[-1].variant_transaction
            self.assertEqual((int(transaction.runtime_id), transaction.hit_pos.x), (31, 3.5))
            await self.deliver(self.opened(-201))
            self.assertIs(await opening, self.bot.open_window)
        finally:
            unsubscribe()
