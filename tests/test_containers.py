import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bedrock_protocol import nbt

from endpilot import Bot, transfer_item, transfer_range
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
        sent = asyncio.Event()

        async def wait(packet_id, *, timeout, match):
            self.assertIn(b.ContainerClosePacket.ID, packet_id)
            self.assertEqual(timeout, 3)
            await sent.wait()
            callbacks[0](different_raw)
            self.assertFalse(match(different_raw))
            self.assertEqual(containers.window, opened)
            callbacks[0](close_raw)
            self.assertTrue(match(close_raw))
            return close_raw

        async def send(packet, *, _before_send):
            _before_send()
            sent.set()

        stream = SimpleNamespace(
            protocol=protocol,
            subscribe_raw=lambda cb: callbacks.append(cb) or (lambda: callbacks.remove(cb)),
            send=AsyncMock(side_effect=send), wait_for_packet=wait,
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
            self.assertIn(b.ContainerClosePacket.ID, packet_id)
            self.assertEqual(timeout, 4)
            callbacks[0](unrelated)
            self.assertFalse(match(unrelated))
            self.assertIs(containers.window, opened)
            callbacks[0](matching)
            self.assertTrue(match(matching))
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
    SADDLE_ID = 1234

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

    def block(self, name, *, layer=0, variant=0):
        b = self.b
        palette, by_hash = _catalog()
        runtime_id = [key for key, index in by_hash.items()
                      if palette[index] is not None and palette[index].name == name][variant]
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

    def boat(self, unique=-204):
        b = self.b
        return b.AddActorPacket(runtime_id=34, entity_id=unique, actor_type="minecraft:chest_boat",
                                pos=b.Vec3(x=1.5, y=100, z=0.5))

    def horse(self, unique=-205, *, runtime_id=35):
        b = self.b
        return b.AddActorPacket(runtime_id=runtime_id, entity_id=unique, actor_type="minecraft:horse",
                                pos=b.Vec3(x=1.5, y=100, z=0.5))

    def horse_equipment(self, unique=-205, *, window_id=2):
        b = self.b
        return b.UpdateEquipPacket(
            container_id=window_id, type=b.ContainerType.HORSE, size=0, entity_unique_id=unique,
            data=nbt.CompoundTag({"slots": nbt.ListTag([nbt.CompoundTag({
                "slotNumber": nbt.IntTag(1),
                "item": nbt.CompoundTag({"Name": nbt.StringTag("minecraft:horsearmoriron")}),
            })])}),
        )

    async def prepare_donkey(self, held_item=None):
        b = self.b
        donkey = b.AddActorPacket(runtime_id=36, entity_id=-206, actor_type="minecraft:donkey",
                                  pos=b.Vec3(x=1.5, y=100, z=0.5))
        await self.deliver(donkey, self.position(), self.main_slot(item=held_item), self.main_slot(1, self.saddle()),
                           self.sneak_pose(False))
        await self.bot.set_sneaking(True)
        await self.deliver(self.sneak_pose(True))

    def donkey_equipment(self):
        b = self.b
        return b.UpdateEquipPacket(
            container_id=2, type=b.ContainerType.HORSE, size=0, entity_unique_id=-206,
            data=nbt.CompoundTag({"slots": nbt.ListTag([nbt.CompoundTag({
                "slotNumber": nbt.IntTag(0),
                "item": nbt.CompoundTag({"Name": nbt.StringTag("minecraft:saddle")}),
            })])}),
        )

    async def test_donkey_window_reads_only_the_current_full_layout_and_rejects_transfers(self):
        b = self.b
        await self.prepare_donkey()
        empty = b.SerializedNetworkItemStackDescriptor()
        marker = b.SerializedNetworkItemStackDescriptor(id=523, stack_size=7, net_id_variant=13)
        chested = b.InventoryContentPacket(inventory_id=2, slots=[empty] * 15 + [marker])
        plain = b.InventoryContentPacket(inventory_id=2, slots=[empty])
        terminal = b.InventorySlotPacket(inventory_id=2, slot=15, item=marker)

        opening = await self.start_open(36)
        click, = self.clicks()
        self.assertEqual((int(click.slot), int(click.item.id), click.item.stack_size), (0, 0, 0))
        await self.deliver(chested)
        await self.deliver(self.donkey_equipment())
        window = await opening
        self.assertEqual(window, ContainerWindow(2, "HORSE", None, -206))
        self.assertIs(self.bot.open_window, window)
        waiting = self.start(self.bot.wait_for_container_slots())
        await self.deliver(b.InventoryContentPacket(inventory_id=3, slots=chested.slots),
                           self.horse_content(), terminal)
        self.assertIsNone(self.bot.container_slots)
        self.assertFalse(waiting.done())
        await self.deliver(chested)
        slots = await waiting
        self.assertEqual(set(slots), set(range(16)))
        self.assertEqual((slots[15].id, slots[15].stack_size), (523, 7))

        sent = list(self.wire.sent)
        for operation in (self.bot.take_from_container(15, 0), self.bot.take_from_container(0, 0),
                          self.bot.put_into_container(1, 0), self.bot.put_into_container(1, 14)):
            with self.assertRaisesRegex(RuntimeError, "read-only"):
                await operation
        with self.assertRaisesRegex(RuntimeError, "supported storage container"):
            await transfer_range(self.bot, direction="take", item="minecraft:emerald", count=1,
                                 source_slots=range(15, 16), destination_slots=range(1))
        with self.assertRaisesRegex(RuntimeError, "supported storage container"):
            await transfer_item(self.bot, direction="put", item="minecraft:saddle", count=1)
        self.assertEqual(self.wire.sent, sent)

        closing = await self.start_close()
        await self.deliver(self.horse_closed(3))
        self.assertFalse(closing.done())
        await self.deliver(self.horse_closed(), chested)
        self.assertIs(await closing, window)
        self.assertIsNone(self.bot.container_slots)

        opening = await self.start_open(36)
        await self.deliver(self.donkey_equipment(), terminal)
        reopened = await opening
        self.assertEqual(reopened, window)
        self.assertIsNot(reopened, window)
        self.assertIsNone(self.bot.container_slots)
        await self.deliver(plain, terminal)
        self.assertEqual([(i, item.id) for i, item in (await self.bot.wait_for_container_slots()).items()], [(0, 0)])

    async def test_donkey_open_requires_empty_hand_and_a_matching_equipment_window(self):
        await self.prepare_donkey(held_item=self.saddle())
        with self.assertRaisesRegex(RuntimeError, "donkey requires an observed empty main hand"):
            await self.bot.open_entity_container(36)
        self.assertFalse(self.clicks())

        await self.deliver(self.main_slot())
        opening = await self.start_open(36)
        await self.deliver(self.horse_equipment())
        with self.assertRaisesRegex(RuntimeError, "different container"):
            await opening
        self.assertEqual(self.bot.open_window.entity_unique_id, -205)

    def saddle(self, *, count=1, net=70, item_id=SADDLE_ID):
        return self.b.SerializedNetworkItemStackDescriptor(id=item_id, stack_size=count, net_id_variant=net)

    def horse_content(self, item=None, *, count=2):
        slots = [self.b.SerializedNetworkItemStackDescriptor() for _ in range(count)]
        if item is not None:
            slots[0] = item
        return self.b.InventoryContentPacket(inventory_id=2, slots=slots)

    async def open_horse(self, inventory, *, runtime_id=35, unique=-205):
        opening = await self.start_open(runtime_id)
        await self.deliver(self.horse_equipment(unique), inventory)
        return await opening

    async def prepare_horse(self, *, confirmed=True):
        b = self.b
        registry = b.ItemRegistryPacket(items=[b.ItemData(id=self.SADDLE_ID, name="minecraft:saddle")])
        await self.deliver(registry, self.horse(), self.position(), self.main_slot(), self.main_slot(1),
                           self.sneak_pose(False))
        await self.bot.set_sneaking(True)
        if confirmed:
            await self.deliver(self.sneak_pose(True))

    def horse_closed(self, window_id=2):
        b = self.b
        return b.ContainerClosePacket(container_id=window_id, container_type=b.ContainerType.NONE,
                                      server_initiated_close=False)

    async def start_close(self, *, timeout=2):
        b = self.b
        sent = asyncio.Event()
        unsubscribe = self.bot.packets.subscribe_sent(
            lambda packet_id: sent.set() if packet_id == b.ContainerClosePacket.ID else None,
        )
        try:
            closing = self.start(self.bot.close_container(timeout=timeout))
            await asyncio.wait_for(sent.wait(), 1)
            return closing
        finally:
            unsubscribe()

    async def test_horse_window_reads_matching_inventory_and_closes_by_server_receipt(self):
        b = self.b
        await self.prepare_horse()
        opening = await self.start_open(35)
        click, = self.clicks()
        self.assertEqual((int(click.slot), int(click.item.id), click.item.stack_size), (0, 0, 0))
        contents = self.horse_content(self.saddle())
        await self.deliver(contents)
        self.assertIsNone(self.bot.open_window)
        self.assertFalse(opening.done())
        await self.deliver(self.horse_equipment())
        window = await opening
        self.assertEqual(window, ContainerWindow(2, "HORSE", None, -205))
        self.assertIs(self.bot.open_window, window)
        self.assertIsNone(self.bot.container_slots)

        changes = []
        self.bot.subscribe_container_slot_changes(changes.append)
        waiting = self.start(self.bot.wait_for_container_slots())
        await self.deliver(b.InventoryContentPacket(inventory_id=3, slots=contents.slots))
        self.assertFalse(waiting.done())
        await self.deliver(contents)
        self.assertEqual([(i, item.id, item.stack_size) for i, item in (await waiting).items()],
                         [(0, self.SADDLE_ID, 1), (1, 0, 0)])
        self.assertEqual([change.slot for change in changes], [0, 1])
        armor = b.SerializedNetworkItemStackDescriptor(id=386, stack_size=1)
        await self.deliver(b.InventorySlotPacket(inventory_id=3, slot=1, item=armor))
        self.assertEqual(self.bot.container_slots[1].id, 0)
        await self.deliver(b.InventorySlotPacket(inventory_id=2, slot=1, item=armor))
        self.assertEqual(self.bot.container_slots[1].id, 386)
        self.assertEqual(changes[-1].after.item_id, 386)

        def address(part):
            group = part.full_container_name
            return (group.name.name, group.dynamic_id, int(part.slot), int(part.net_id_variant))

        horse_domain, player_domain = "HORSE_EQUIP_CONTAINER", "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"
        take, request = await self.submit_stack(self.bot.take_from_container(0, 1))
        action, = request.actions
        self.assertIsInstance(action, b.ItemStackRequestCereal.TakeActionData)
        self.assertEqual((action.amount, address(action.source), address(action.destination)),
                         (1, (horse_domain, None, 0, 70), (player_domain, None, 1, 0)))
        await self.deliver(b.InventorySlotPacket(inventory_id=2, slot=0, item=contents.slots[1]))
        await self.reply_stack(request, (horse_domain, 0, 0, 0), (player_domain, 1, 1, 71))
        self.assertEqual((await take).result.name, "SUCCESS")
        self.assertEqual((self.bot.container_slots[0].id, self.bot.inventory_slots[1].id,
                          self.bot.inventory_slots[1].net_id_variant), (0, self.SADDLE_ID, 71))

        put, request = await self.submit_stack(self.bot.put_into_container(1, 0))
        action, = request.actions
        self.assertIsInstance(action, b.ItemStackRequestCereal.PlaceActionData)
        self.assertEqual((action.amount, address(action.source), address(action.destination)),
                         (1, (player_domain, None, 1, 71), (horse_domain, None, 0, 0)))
        await self.deliver(b.InventorySlotPacket(inventory_id=2, slot=0, item=self.saddle(net=72)))
        await self.reply_stack(request, (horse_domain, 0, 1, 72), (player_domain, 1, 0, 0))
        self.assertEqual((await put).result.name, "SUCCESS")
        self.assertEqual((self.bot.container_slots[0].id, self.bot.container_slots[0].net_id_variant,
                          self.bot.container_slots[1].id, self.bot.inventory_slots[1].id),
                         (self.SADDLE_ID, 72, 386, 0))

        requests_before = list(self.wire.sent)
        for operation in (
            transfer_range(self.bot, direction="take", item="minecraft:saddle", count=1,
                           source_slots=range(1), destination_slots=range(1)),
            transfer_item(self.bot, direction="put", item="minecraft:saddle", count=1),
        ):
            with self.assertRaisesRegex(RuntimeError, "supported storage container"):
                await operation
        self.assertEqual(self.wire.sent, requests_before)

        closing = await self.start_close()
        request = self.wire.sent[-1]
        self.assertEqual((int(request.container_id), request.container_type, request.server_initiated_close),
                         (2, b.ContainerType.HORSE, False))
        await self.deliver(self.horse_closed(3), self.closed())
        self.assertIs(self.bot.open_window, window)
        self.assertFalse(closing.done())
        await self.deliver(self.horse_closed())
        self.assertIs(await closing, window)
        self.assertIsNone(self.bot.open_window)
        self.assertIsNone(self.bot.container_slots)
        await self.deliver(contents)
        self.assertIsNone(self.bot.container_slots)

    async def test_horse_take_rejects_unsupported_slots_and_unconfirmed_stacks_without_sending(self):
        await self.prepare_horse()
        await self.open_horse(self.horse_content(self.saddle()))
        requests_before = list(self.wire.sent)
        for slots in ((1, 1, 1), (0, 1, 2), (0, 36, 1)):
            with self.assertRaisesRegex(ValueError, "one saddle in slot 0"):
                await self.bot.take_from_container(*slots)
        with self.assertRaisesRegex(RuntimeError, "observed horse equipment and main inventory"):
            await self.bot.take_from_container(0, 2)
        for item in (self.saddle(item_id=374), self.saddle(count=2), self.saddle(net=None)):
            await self.deliver(self.horse_content(item))
            with self.assertRaisesRegex(ValueError, "one server-registered saddle"):
                await self.bot.take_from_container(0, 1)
        await self.deliver(self.horse_content(self.saddle(), count=1))
        with self.assertRaisesRegex(RuntimeError, "unsupported slot count"):
            await self.bot.take_from_container(0, 1)
        await self.deliver(self.horse_content(self.saddle()), self.main_slot(1, self.saddle(net=81)))
        with self.assertRaisesRegex(ValueError, "an observed empty slot"):
            await self.bot.take_from_container(0, 1)
        with self.assertRaisesRegex(ValueError, "an observed empty slot"):
            await self.bot.put_into_container(1, 0)
        self.assertEqual(self.wire.sent, requests_before)

    async def test_horse_native_replacement_does_not_inherit_transfer_ownership_or_late_verdict(self):
        await self.prepare_horse()
        original = await self.open_horse(self.horse_content(self.saddle()))
        take, request = await self.submit_stack(self.bot.take_from_container(0, 1))
        await self.deliver(self.horse_equipment(), self.horse_content(self.saddle(net=81)))
        self.assertEqual(self.bot.open_window, original)
        self.assertIsNot(self.bot.open_window, original)
        await self.reply_stack(request, ("HORSE_EQUIP_CONTAINER", 0, 0, 0),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 1, 1, 72))
        self.assertEqual((await take).result.name, "SUCCESS")
        self.assertEqual((self.bot.container_slots[0].net_id_variant, self.bot.inventory_slots[1].id), (81, 0))
        requests_before = list(self.wire.sent)
        with self.assertRaisesRegex(RuntimeError, "read-only"):
            await self.bot.take_from_container(0, 1)
        self.assertEqual(self.wire.sent, requests_before)

    async def test_late_horse_take_requires_fresh_player_empty_slot_before_another_take(self):
        await self.assert_late_horse_take_requires_fresh_player_slot(("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 1, 1, 71))

    async def test_late_horse_take_without_player_correction_requires_fresh_player_slot(self):
        await self.assert_late_horse_take_requires_fresh_player_slot()

    async def test_late_horse_take_success_after_cancellation_still_requires_fresh_player_slot(self):
        await self.assert_late_horse_take_requires_fresh_player_slot(cancel_wait=True)

    async def assert_late_horse_take_requires_fresh_player_slot(self, *player_corrections, cancel_wait=False):
        await self.prepare_horse()
        original = await self.open_horse(self.horse_content(self.saddle()))
        packet_send = self.bot.packets.send

        async def refresh_before_write(packet, **kwargs):
            if isinstance(packet, self.b.ItemStackRequestPacket):
                await self.deliver(self.main_slot(1))
            await packet_send(packet, **kwargs)

        with patch.object(self.bot.packets, "send", refresh_before_write):
            take, request = await self.submit_stack(self.bot.take_from_container(0, 1))
        if cancel_wait:
            take.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await take
        await self.deliver(self.horse_closed(), self.horse(-206, runtime_id=36))
        replacement = await self.open_horse(self.horse_content(self.saddle(net=81)), runtime_id=36, unique=-206)
        self.assertEqual(replacement.container_id, original.container_id)
        await self.reply_stack(request, ("HORSE_EQUIP_CONTAINER", 0, 0, 0), *player_corrections)
        if not cancel_wait:
            self.assertEqual((await take).result.name, "SUCCESS")
        self.assertEqual((self.bot.container_slots[0].net_id_variant, self.bot.inventory_slots[1].id), (81, 0))
        before = list(self.wire.sent)
        with self.assertRaisesRegex(RuntimeError, "fresh server observation"):
            await self.bot.take_from_container(0, 1, timeout=.1)
        self.assertEqual(self.wire.sent, before)

        await self.deliver(self.main_slot(1))
        resumed, request = await self.submit_stack(self.bot.take_from_container(0, 1))
        action, = request.actions
        self.assertEqual((action.source.net_id_variant, action.destination.net_id_variant), (81, 0))
        await self.reply_stack(request, ("HORSE_EQUIP_CONTAINER", 0, 0, 0),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 1, 1, 82))
        self.assertEqual((await resumed).result.name, "SUCCESS")

    async def test_late_horse_put_requires_fresh_player_saddle_before_another_put(self):
        await self.assert_late_horse_put_requires_fresh_player_slot(("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 1, 0, 0))

    async def test_late_horse_put_without_player_correction_requires_fresh_player_slot(self):
        await self.assert_late_horse_put_requires_fresh_player_slot()

    async def assert_late_horse_put_requires_fresh_player_slot(self, *player_corrections):
        await self.prepare_horse()
        await self.deliver(self.main_slot(1, self.saddle(net=71)))
        await self.open_horse(self.horse_content())
        put, request = await self.submit_stack(self.bot.put_into_container(1, 0))
        await self.deliver(self.horse_closed(), self.horse(-206, runtime_id=36))
        await self.open_horse(self.horse_content(), runtime_id=36, unique=-206)
        await self.reply_stack(request, ("HORSE_EQUIP_CONTAINER", 0, 1, 71), *player_corrections)
        self.assertEqual((await put).result.name, "SUCCESS")
        self.assertEqual((self.bot.container_slots[0].id, self.bot.inventory_slots[1].net_id_variant), (0, 71))
        before = list(self.wire.sent)
        with self.assertRaisesRegex(RuntimeError, "fresh server observation"):
            await self.bot.put_into_container(1, 0, timeout=.1)
        self.assertEqual(self.wire.sent, before)

        await self.deliver(self.main_slot(1, self.saddle(net=72)))
        resumed, request = await self.submit_stack(self.bot.put_into_container(1, 0))
        action, = request.actions
        self.assertEqual((action.source.net_id_variant, action.destination.net_id_variant), (72, 0))
        await self.reply_stack(request, ("HORSE_EQUIP_CONTAINER", 0, 1, 72),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 1, 0, 0))
        self.assertEqual((await resumed).result.name, "SUCCESS")

    async def test_late_horse_take_keeps_new_player_observation_of_the_same_empty_slot(self):
        await self.prepare_horse()
        await self.open_horse(self.horse_content(self.saddle()))
        take, request = await self.submit_stack(self.bot.take_from_container(0, 1))
        await self.deliver(self.horse_closed(), self.horse(-206, runtime_id=36))
        await self.open_horse(self.horse_content(self.saddle(net=81)), runtime_id=36, unique=-206)
        await self.deliver(self.main_slot(1, self.saddle(net=71)), self.main_slot(1))
        await self.reply_stack(request, ("HORSE_EQUIP_CONTAINER", 0, 0, 0))
        self.assertEqual((await take).result.name, "SUCCESS")
        self.assertEqual((self.bot.container_slots[0].net_id_variant, self.bot.inventory_slots[1].id), (81, 0))
        resumed, request = await self.submit_stack(self.bot.take_from_container(0, 1))
        action, = request.actions
        self.assertEqual((action.source.net_id_variant, action.destination.net_id_variant), (81, 0))
        await self.reply_stack(request, ("HORSE_EQUIP_CONTAINER", 0, 0, 0),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 1, 1, 82))
        self.assertEqual((await resumed).result.name, "SUCCESS")

    async def test_late_horse_rejection_keeps_player_slot_confirmed(self):
        b = self.b
        await self.prepare_horse()
        await self.open_horse(self.horse_content(self.saddle()))
        take, request = await self.submit_stack(self.bot.take_from_container(0, 1))
        await self.deliver(self.horse_closed(), self.horse(-206, runtime_id=36))
        await self.open_horse(self.horse_content(self.saddle(net=81)), runtime_id=36, unique=-206)
        await self.deliver(b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=request.client_request_id, result=b.ItemStackNetResult.ERROR,
        )]))
        self.assertEqual((await take).result.name, "ERROR")
        self.assertEqual((self.bot.container_slots[0].net_id_variant, self.bot.inventory_slots[1].id), (81, 0))
        resumed, request = await self.submit_stack(self.bot.take_from_container(0, 1))
        await self.reply_stack(request, ("HORSE_EQUIP_CONTAINER", 0, 0, 0),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 1, 1, 82))
        self.assertEqual((await resumed).result.name, "SUCCESS")

    async def test_horse_take_cancelled_before_write_leaves_player_slot_confirmed(self):
        await self.prepare_horse()
        await self.open_horse(self.horse_content(self.saddle()))
        original_send = self.bot.packets.send
        writing = asyncio.Event()

        async def pause_stack_request(packet, **kwargs):
            if isinstance(packet, self.b.ItemStackRequestPacket):
                writing.set()
                await asyncio.Future()
            await original_send(packet, **kwargs)

        before = list(self.wire.sent)
        with patch.object(self.bot.packets, "send", pause_stack_request):
            take = self.start(self.bot.take_from_container(0, 1))
            await asyncio.wait_for(writing.wait(), 1)
            await self.cancel(take)
        self.assertTrue(take.cancelled())
        self.assertEqual(self.wire.sent, before)
        resumed, request = await self.submit_stack(self.bot.take_from_container(0, 1))
        await self.reply_stack(request, ("HORSE_EQUIP_CONTAINER", 0, 0, 0),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 1, 1, 82))
        self.assertEqual((await resumed).result.name, "SUCCESS")

    async def test_horse_success_with_wrong_receipt_address_requires_fresh_observations(self):
        b = self.b
        await self.prepare_horse()
        await self.open_horse(self.horse_content(self.saddle()))
        take, request = await self.submit_stack(self.bot.take_from_container(0, 1))
        await self.reply_stack(request, ("HORSE_EQUIP_CONTAINER", 1, 0, 0),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 1, 1, 71))
        self.assertEqual((await take).result.name, "SUCCESS")
        self.assertEqual((self.bot.container_slots[0].id, self.bot.inventory_slots[1].id), (self.SADDLE_ID, 0))
        requests_before = list(self.wire.sent)
        with self.assertRaisesRegex(RuntimeError, "fresh server observation"):
            await self.bot.take_from_container(0, 1)
        self.assertEqual(self.wire.sent, requests_before)
        await self.deliver(self.horse_content(self.saddle()), self.main_slot(1))
        take, request = await self.submit_stack(self.bot.take_from_container(0, 1))
        await self.deliver(b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=request.client_request_id, result=b.ItemStackNetResult.ERROR,
        )]))
        self.assertEqual((await take).result.name, "ERROR")
        self.assertEqual((self.bot.container_slots[0].id, self.bot.inventory_slots[1].id), (self.SADDLE_ID, 0))

    async def test_horse_success_does_not_replace_new_main_hand_equipment(self):
        b = self.b
        await self.prepare_horse()
        await self.open_horse(self.horse_content(self.saddle()))
        take, request = await self.submit_stack(self.bot.take_from_container(0, 1))
        await self.deliver(b.MobEquipmentPacket(runtime_id=7, container_id=b.ContainerID.INVENTORY,
                                                slot=1, selected_slot=1, item=self.storage_item(1, 80)))
        await self.reply_stack(request, ("HORSE_EQUIP_CONTAINER", 0, 0, 0),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 1, 1, 71))
        self.assertEqual((await take).result.name, "SUCCESS")
        self.assertEqual((self.bot.container_slots[0].id, self.bot.inventory_slots[1].id), (self.SADDLE_ID, 0))

    async def test_horse_take_rechecks_window_actor_and_both_slots_at_the_write(self):
        b = self.b
        await self.prepare_horse()
        changes = (
            (self.main_slot(1, self.saddle(net=82)),),
            (b.InventoryContentPacket(inventory_id=b.ContainerID.INVENTORY, slots=[self.main_slot().item]),),
            (self.horse_content(self.saddle(net=83)),),
            (self.horse_equipment(-206), self.horse_content(self.saddle())),
            (self.horse_closed(), self.horse_equipment(), self.horse_content(self.saddle())),
            (b.RemoveActorPacket(entity_id=-205), self.horse()),
            (b.ItemRegistryPacket(items=[b.ItemData(id=self.SADDLE_ID, name="minecraft:stone")]),),
        )
        original_send = self.bot.packets.send
        for packets in changes:
            with self.subTest(changed=type(packets[0]).__name__):
                if self.bot.open_window is not None:
                    await self.deliver(self.horse_closed())
                await self.prepare_horse()
                await self.open_horse(self.horse_content(self.saddle()))

                async def send(packet, **kwargs):
                    if isinstance(packet, b.ItemStackRequestPacket):
                        await self.deliver(*packets)
                    await original_send(packet, **kwargs)

                requests_before = list(self.wire.sent)
                with patch.object(self.bot.packets, "send", send):
                    with self.assertRaisesRegex(RuntimeError, "changed before submission|no longer observed"):
                        await self.bot.take_from_container(0, 1)
                self.assertEqual(self.wire.sent, requests_before)

    async def test_horse_open_rejects_wrong_window_and_tracks_actual_replacement(self):
        await self.prepare_horse()
        opening = await self.start_open(35)
        await self.deliver(self.horse_equipment(-206))
        with self.assertRaisesRegex(RuntimeError, "different container"):
            await opening
        self.assertEqual(self.bot.open_window, ContainerWindow(2, "HORSE", None, -206))
        await self.deliver(self.contents(), self.horse_equipment(window_id=4))
        self.assertEqual(self.bot.open_window, ContainerWindow(4, "HORSE", None, -205))
        self.assertIsNone(self.bot.container_slots)

        await self.deliver(self.horse_closed(4))
        opening = await self.start_open(35)
        await self.deliver(self.opened(-205, "HORSE"))
        with self.assertRaisesRegex(RuntimeError, "different container"):
            await opening

    async def test_horse_open_cannot_use_stale_announcement_or_replaced_actor(self):
        b = self.b
        await self.prepare_horse(confirmed=False)
        with self.assertRaises(TimeoutError):
            await self.bot.open_entity_container(35, timeout=.03)
        self.assertEqual(self.clicks(), [])

        waiting = asyncio.Event()
        wait_for_packet = self.bot.packets.wait_for_packet

        async def observe_wait(packet_ids, **kwargs):
            if packet_ids == b.SetActorDataPacket.ID:
                waiting.set()
            return await wait_for_packet(packet_ids, **kwargs)

        with patch.object(self.bot.packets, "wait_for_packet", side_effect=observe_wait):
            opening = self.start(self.bot.open_entity_container(35))
            await asyncio.wait_for(waiting.wait(), 1)
            await self.deliver(self.horse_equipment(), self.sneak_pose(True))
            with self.assertRaisesRegex(RuntimeError, "opened before the interaction"):
                await opening
        self.assertEqual(self.clicks(), [])

        await self.deliver(self.horse_closed())
        opening = await self.start_open(35)
        await self.deliver(b.RemoveActorPacket(entity_id=-205), self.horse(), self.horse_equipment())
        with self.assertRaisesRegex(RuntimeError, "horse is no longer observed"):
            await opening

    async def test_horse_open_does_not_return_a_window_already_closed_or_replaced(self):
        await self.prepare_horse()
        for end in (self.horse_closed(), self.horse_equipment(window_id=4)):
            with self.subTest(packet=type(end).__name__):
                opening = await self.start_open(35)
                await self.deliver(self.horse_equipment(), end)
                with self.assertRaisesRegex(RuntimeError, "closed or changed during open"):
                    await opening
                if self.bot.open_window is not None:
                    await self.deliver(self.horse_closed(4))

    async def test_horse_close_cannot_return_a_receipt_from_a_reused_window_id(self):
        await self.prepare_horse()
        await self.deliver(self.horse_equipment())
        closing = await self.start_close()
        await self.deliver(self.horse_equipment(-206), self.horse_closed())
        with self.assertRaisesRegex(RuntimeError, "window changed before its close"):
            await closing

    async def test_cart_close_cannot_return_a_horse_receipt_with_the_same_window_id(self):
        await self.deliver(self.opened(-201))
        closing = await self.start_close()
        await self.deliver(self.horse_equipment(), self.horse_closed())
        with self.assertRaisesRegex(RuntimeError, "window changed before its close"):
            await closing
        self.assertIsNone(self.bot.open_window)

    async def test_disconnect_ends_close_waits_while_reader_is_still_open(self):
        await self.deliver(self.horse_equipment(window_id=3))
        reader = self.bot.packets._reader_task
        observing = self.start(self.bot.wait_for_container_close(timeout=.3))
        closing = await self.start_close(timeout=.3)
        await self.deliver(self.b.DisconnectPacket())
        self.assertIsNone(self.bot.open_window)
        self.assertFalse(reader.done())
        for waiter in (observing, closing):
            with self.assertRaises(ConnectionError):
                await waiter

    async def test_passive_close_wait_rejects_replacement_by_any_window_announcement(self):
        b = self.b
        for replacement in (self.opened(-202), self.horse_equipment(),
                            b.UpdateTradePacket(container_id=2, entity_unique_id=-203),
                            b.ChangeDimensionPacket(dimension_id=1)):
            with self.subTest(packet=type(replacement).__name__):
                await self.deliver(self.opened(-201))
                before = sum(isinstance(packet, b.ContainerClosePacket) for packet in self.wire.sent)
                observing = self.start(self.bot.wait_for_container_close(timeout=.3))
                await asyncio.sleep(0)
                await self.deliver(replacement, self.horse_closed())
                with self.assertRaisesRegex(RuntimeError, "window changed before its close"):
                    await observing
                self.assertEqual(sum(isinstance(packet, b.ContainerClosePacket) for packet in self.wire.sent), before)

    async def test_trade_refresh_keeps_passive_close_bound_for_matching_none_receipt(self):
        b = self.b
        quote = b.UpdateTradePacket(container_id=2, entity_unique_id=-203)
        await self.deliver(self.opened(-203, "TRADE"))
        window = self.bot.open_window
        self.wire.sent.clear()
        observing = self.start(self.bot.wait_for_container_close(timeout=1))
        await asyncio.sleep(0)
        await self.deliver(quote, b.UpdateEquipPacket(container_id=2, type=b.ContainerType.CONTAINER))
        self.assertIs(self.bot.open_window, window)
        self.assertFalse(observing.done())
        await self.deliver(self.horse_closed())
        reply = await observing
        self.assertEqual((int(reply.container_id), reply.container_type), (2, b.ContainerType.NONE))
        self.assertIsNone(self.bot.open_window)
        self.assertFalse(any(isinstance(packet, b.ContainerClosePacket) for packet in self.wire.sent))

    async def test_close_rechecks_starting_window_at_packet_write(self):
        b = self.b
        await self.deliver(self.opened(-201))
        blocked, release = asyncio.Event(), asyncio.Event()
        send = self.bot.packets.send

        async def paused_send(packet, **kwargs):
            if isinstance(packet, b.ContainerClosePacket):
                blocked.set()
                await release.wait()
            return await send(packet, **kwargs)

        with patch.object(self.bot.packets, "send", side_effect=paused_send):
            closing = self.start(self.bot.close_container(timeout=.3))
            await asyncio.wait_for(blocked.wait(), 1)
            await self.deliver(self.horse_equipment())
            replacement = self.bot.open_window
            release.set()
            with self.assertRaisesRegex(RuntimeError, "window changed before its close"):
                await closing
        self.assertEqual([packet for packet in self.wire.sent if isinstance(packet, b.ContainerClosePacket)], [])
        self.assertIs(self.bot.open_window, replacement)

    def sneak_pose(self, sneaking, *, runtime_id=7):
        b = self.b
        flags = 1 << int(b.ActorFlags.SNEAKING) if sneaking else 0
        return b.SetActorDataPacket(id=runtime_id, packed_items=b.SynchedActorData.CopyableDataList(data=[
            b.DataItemEntry(id=0, payload=b.DataItemInt64Payload(value=flags)),
        ]))

    def main_slot(self, slot=0, item=None):
        b = self.b
        return b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=slot,
                                     item=b.SerializedNetworkItemStackDescriptor() if item is None else item)

    def clicks(self):
        return [packet.variant_transaction for packet in self.wire.sent
                if isinstance(packet, self.b.InventoryTransactionPacket)]

    async def test_chest_boat_waits_for_own_sneak_and_requires_a_known_empty_hand(self):
        b = self.b
        await self.deliver(self.boat(), self.position(), self.sneak_pose(False))
        with self.assertRaisesRegex(RuntimeError, "hold sneak"):
            await self.bot.open_entity_container(34)
        await self.bot.set_sneaking(True)
        with self.assertRaisesRegex(RuntimeError, "observed empty main hand"):
            await self.bot.open_entity_container(34)
        await self.deliver(self.main_slot(item=self.storage_item(1, 32)))
        with self.assertRaisesRegex(RuntimeError, "observed empty main hand"):
            await self.bot.open_entity_container(34)
        await self.deliver(self.main_slot())

        waiting = asyncio.Event()
        original_wait = self.bot.packets.wait_for_packet

        async def wait(packet_id, **kwargs):
            if packet_id == b.SetActorDataPacket.ID:
                waiting.set()
            return await original_wait(packet_id, **kwargs)

        with patch.object(self.bot.packets, "wait_for_packet", side_effect=wait):
            opening = self.start(self.start_open(34))
            await asyncio.wait_for(waiting.wait(), 1)
            await self.deliver(self.sneak_pose(True, runtime_id=31))
            self.assertEqual(self.clicks(), [])
            await self.deliver(self.sneak_pose(True))
            pending = await opening
        click, = self.clicks()
        self.assertEqual((click.slot, int(click.item.id), int(click.item.stack_size)), (0, 0, 0))
        await self.deliver(self.opened(-204))
        self.assertIs(await pending, self.bot.open_window)

    async def test_cancelled_sneak_input_cannot_open_chest_boat(self):
        b = self.b
        await self.deliver(self.boat(), self.position(), self.main_slot(), self.sneak_pose(False))
        started = asyncio.Event()
        original_send = self.bot.packets.send

        async def send(packet, **kwargs):
            if isinstance(packet, b.PlayerAuthInputPacket):
                started.set()
                await asyncio.Future()
            await original_send(packet, **kwargs)

        with patch.object(self.bot.packets, "send", side_effect=send):
            sneaking = self.start(self.bot.set_sneaking(True))
            await asyncio.wait_for(started.wait(), 1)
            await self.cancel(sneaking)
            with self.assertRaises(TimeoutError):
                await self.bot.open_entity_container(34, timeout=0.03)
        self.assertEqual(self.clicks(), [])

    async def test_chest_boat_rechecks_pose_hand_and_actor_at_the_click_write(self):
        b = self.b
        await self.deliver(self.boat(), self.position(), self.main_slot(), self.main_slot(1), self.sneak_pose(True))
        await self.bot.set_sneaking(True)
        original_send = self.bot.packets.send

        async def send(packet, **kwargs):
            if isinstance(packet, b.InventoryTransactionPacket):
                await change()
            await original_send(packet, **kwargs)

        async def select_other_empty_slot():
            await self.deliver(b.PlayerHotbarPacket(selected_slot=1, container_id=b.ContainerID.INVENTORY,
                                                    should_select_slot=True))

        async def stand():
            await self.bot.set_sneaking(False)

        async def recreate():
            await self.deliver(b.RemoveActorPacket(entity_id=-204), self.boat())

        async def respawn():
            await self.deliver(b.RespawnPacket(runtime_id=7, state=b.PlayerRespawnState.SEARCHING_FOR_SPAWN))

        with patch.object(self.bot.packets, "send", side_effect=send):
            for change, error in ((select_other_empty_slot, "selected main hand changed"),
                                  (stand, "hold sneak"), (recreate, "chest boat is no longer observed"),
                                  (respawn, "pose changed generation")):
                with self.subTest(change=change.__name__):
                    await self.bot.set_sneaking(True)
                    with self.assertRaisesRegex(RuntimeError, error):
                        await self.bot.open_entity_container(34, timeout=1)
        self.assertEqual(self.clicks(), [])

    async def test_chest_boat_rechecks_new_equipment_then_accepts_a_fresh_empty_slot(self):
        b = self.b
        await self.deliver(self.boat(), self.position(), self.main_slot(), self.sneak_pose(True))
        await self.bot.set_sneaking(True)
        original_send = self.bot.packets.send

        async def send(packet, **kwargs):
            if isinstance(packet, b.InventoryTransactionPacket):
                await self.deliver(b.MobEquipmentPacket(runtime_id=7, container_id=b.ContainerID.INVENTORY,
                                                        selected_slot=0, slot=0, item=self.storage_item(1, 31)))
                self.assertEqual(self.bot.held_item.observed_item.item_id, 0)
            await original_send(packet, **kwargs)
            if isinstance(packet, b.InventoryTransactionPacket):
                await self.deliver(self.opened(-204))

        with patch.object(self.bot.packets, "send", side_effect=send):
            with self.assertRaisesRegex(RuntimeError, "observed empty main hand"):
                await self.bot.open_entity_container(34, timeout=1)
        with self.assertRaisesRegex(RuntimeError, "observed empty main hand"):
            await self.bot.open_entity_container(34, timeout=1)
        self.assertEqual(self.clicks(), [])

        await self.deliver(self.main_slot())
        opening = await self.start_open(34)
        self.assertEqual(len(self.clicks()), 1)
        await self.deliver(self.opened(-204))
        self.assertIs(await opening, self.bot.open_window)

    async def test_chest_boat_exact_and_range_require_the_matching_complete_layout(self):
        b = self.b
        await self.deliver(self.boat(), self.position(), self.main_slot(), self.sneak_pose(True))
        await self.bot.set_sneaking(True)
        opening = await self.start_open(34)
        await self.deliver(self.opened(-204, "HOPPER"), self.storage_content(27, (26, 3, 31)))
        with self.assertRaisesRegex(RuntimeError, "different container"):
            await opening
        with self.assertRaisesRegex(RuntimeError, "unsupported container type"):
            await self.bot.take_from_container(26, 1)

        await self.deliver(self.closed(), self.opened(-204), self.storage_content(28, (26, 3, 31)))
        for operation in (self.bot.take_from_container(26, 1), self.bot.put_into_container(1, 26),
                          transfer_range(self.bot, direction="take", item="minecraft:diamond", count=1,
                                         source_slots=range(26, 27), destination_slots=range(1, 2))):
            with self.assertRaisesRegex(RuntimeError, "unsupported slot count"):
                await operation
        await self.deliver(self.storage_content(27, (26, 3, 31)))
        with self.assertRaisesRegex(ValueError, r"range\(27\)"):
            await self.bot.take_from_container(27, 1)
        self.assertFalse(any(isinstance(packet, b.ItemStackRequestPacket) for packet in self.wire.sent))
        taking, request = await self.submit_stack(self.bot.take_from_container(26, 1))
        self.assertEqual(int(request.actions[0].source.slot), 26)
        await self.reply_stack(request, ("LEVEL_ENTITY_CONTAINER", 26, 2, 31),
                               ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 1, 1, 32))
        self.assertEqual((await taking).result.name, "SUCCESS")
        self.assertEqual(self.bot.container_slots[26].stack_size, 2)
        await self.deliver(b.RemoveActorPacket(entity_id=-204), self.boat())
        with self.assertRaisesRegex(RuntimeError, "chest boat is no longer observed"):
            await self.bot.put_into_container(1, 26)

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

    async def test_vanilla_shulker_exact_transfers_use_their_slot_domain(self):
        b = self.b
        for color in (
            "black", "blue", "brown", "cyan", "gray", "green", "light_blue", "light_gray",
            "lime", "magenta", "orange", "pink", "purple", "red", "undyed", "white", "yellow",
        ):
            with self.subTest(color=color):
                await self.deliver(
                    self.closed(), self.block(f"minecraft:{color}_shulker_box"), self.opened(-1),
                    self.storage_content(27),
                    b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=3,
                                          item=self.storage_item(5, 61)),
                )
                putting, request = await self.submit_stack(self.bot.put_into_container(3, 26, 3))
                action, = request.actions
                self.assertEqual((int(action.destination.full_container_name.name), action.destination.slot), (30, 26))
                await self.reply_stack(request, ("SHULKER_BOX_CONTAINER", 26, 3, 31),
                                       ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 3, 2, 61))
                self.assertEqual((await putting).result.name, "SUCCESS")
                self.assertEqual((self.bot.container_slots[26].stack_size, self.bot.inventory_slots[3].stack_size),
                                 (3, 2))

                taking, request = await self.submit_stack(self.bot.take_from_container(26, 3, 1))
                action, = request.actions
                self.assertEqual((int(action.source.full_container_name.name), action.source.slot,
                                  action.source.net_id_variant), (30, 26, 31))
                await self.reply_stack(request, ("SHULKER_BOX_CONTAINER", 26, 2, 31),
                                       ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 3, 3, 61))
                self.assertEqual((await taking).result.name, "SUCCESS")
                self.assertEqual((self.bot.container_slots[26].stack_size, self.bot.inventory_slots[3].stack_size),
                                 (2, 3))

    async def test_only_exact_vanilla_shulker_names_select_the_shulker_domain(self):
        for name in ("custom:red_shulker_box", "minecraft:future_shulker_box", "minecraft:shulker_box"):
            with self.subTest(name=name):
                state = SimpleNamespace(name=name)
                with patch.object(self.bot._blocks, "_vanilla_state", return_value=state):
                    await self.deliver(self.closed(), self.block("minecraft:chest"), self.opened(-1),
                                       self.storage_content(27, (26, 5, 31)))
                    taking, request = await self.submit_stack(self.bot.take_from_container(26, 3))
                    self.assertEqual(int(request.actions[0].source.full_container_name.name), 7)
                    await self.reply_stack(request, ("LEVEL_ENTITY_CONTAINER", 26, 4, 31),
                                           ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 3, 1, 61))
                    self.assertEqual((await taking).result.name, "SUCCESS")

    async def test_shulker_requires_complete_matching_window_before_exact_transfer(self):
        b = self.b
        for opened, replaced in (("undyed_shulker_box", "chest"), ("red_shulker_box", "blue_shulker_box")):
            with self.subTest(opened=opened, replaced=replaced):
                await self.deliver(self.closed(), self.block(f"minecraft:{opened}"), self.opened(-1),
                                   self.storage_content(1),
                                   b.InventorySlotPacket(inventory_id=2, slot=26, item=self.storage_item(5, 31)))
                sent_before = list(self.wire.sent)
                with self.assertRaisesRegex(RuntimeError, "unsupported slot count"):
                    await self.bot.take_from_container(26, 3)
                await self.deliver(self.storage_content(27, (26, 5, 31)))
                with self.assertRaisesRegex(ValueError, "range"):
                    await self.bot.take_from_container(27, 3)
                await self.deliver(self.block(f"minecraft:{replaced}"))
                with self.assertRaisesRegex(RuntimeError, "no longer matches"):
                    await self.bot.take_from_container(26, 3)
                self.assertEqual(self.wire.sent, sent_before)

    def hold_stack_writes(self):
        entered = asyncio.Event()
        released = asyncio.Event()
        original_send = self.bot.packets.send

        async def hold_stack_send(packet, **options):
            if isinstance(packet, self.b.ItemStackRequestPacket):
                entered.set()
                await released.wait()
            await original_send(packet, **options)

        return patch.object(self.bot.packets, "send", new=hold_stack_send), entered, released

    def transfer_observers(self):
        return len(self.bot._blocks._block_observers), len(self.bot.packets._subscribers)

    async def test_exact_block_transfer_rejects_observed_replacement_at_transport_write(self):
        shulker = "minecraft:undyed_shulker_box"
        red_shulker, blue_shulker = "minecraft:red_shulker_box", "minecraft:blue_shulker_box"
        b = self.b
        air_section = b.SubChunkPacket(
            dimension_type=0, center_pos=b.SubChunkPos(x=0, y=6, z=0),
            sub_chunk_data=[b.SubChunkPacket.SubChunkPacketData(
                sub_chunk_pos_offset=b.SubChunkPacket.SubChunkPosOffset(x=0, y=0, z=0),
                result=b.SubChunkPacket.SubChunkRequestResult.SUCCESS_ALL_AIR,
            )],
        )
        for initial, changes in (
            (None, (self.block(red_shulker),)),
            (shulker, (self.block("minecraft:chest"),)),
            (shulker, (self.block("minecraft:air"), self.block(shulker))),
            (shulker, (air_section, self.block(shulker))),
            ("minecraft:chest", (self.block(shulker),)),
            (red_shulker, (self.block(blue_shulker),)),
            (red_shulker, (self.block(blue_shulker), self.block(red_shulker))),
        ):
            with self.subTest(initial=initial, changes=[type(packet).__name__ for packet in changes]):
                if initial is not None:
                    await self.deliver(self.block(initial))
                await self.deliver(self.opened(-1), self.storage_content(27, (26, 5, 31)))
                observers = self.transfer_observers()
                hold, entered, released = self.hold_stack_writes()
                with hold:
                    taking = self.start(self.bot.take_from_container(26, 3))
                    await asyncio.wait_for(entered.wait(), 1)
                    self.assertEqual(self.transfer_observers(), tuple(count + 1 for count in observers))
                    await self.deliver(*changes)
                    sent_before = list(self.wire.sent)
                    released.set()
                    with self.assertRaisesRegex(RuntimeError, "no longer matches"):
                        await taking
                self.assertEqual(self.wire.sent, sent_before)
                self.assertEqual(self.transfer_observers(), observers)

    async def test_exact_block_transfer_keeps_same_type_and_ignores_additional_layer(self):
        shulker, chest = "minecraft:undyed_shulker_box", "minecraft:chest"
        for name, changes, domain in (
            (None, (self.block(shulker, layer=1),), "LEVEL_ENTITY_CONTAINER"),
            (chest, (self.block(chest, variant=1), self.block("minecraft:air", layer=1)), "LEVEL_ENTITY_CONTAINER"),
            (shulker, (self.block(shulker), self.block("minecraft:air", layer=1)), "SHULKER_BOX_CONTAINER"),
        ):
            with self.subTest(name=name):
                if name is not None:
                    await self.deliver(self.block(name))
                await self.deliver(self.opened(-1), self.storage_content(27, (26, 5, 31)))
                observers = self.transfer_observers()
                hold, entered, released = self.hold_stack_writes()
                with hold:
                    submission = self.start(self.submit_stack(self.bot.take_from_container(26, 3)))
                    await asyncio.wait_for(entered.wait(), 1)
                    await self.deliver(*changes)
                    released.set()
                    taking, request = await submission
                    self.assertEqual(self.transfer_observers(), observers)
                    self.assertEqual(request.actions[0].source.full_container_name.name.name, domain)
                    await self.reply_stack(request, (domain, 26, 4, 31),
                                           ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 3, 1, 61))
                    self.assertEqual((await taking).result.name, "SUCCESS")

    async def test_exact_block_transfer_unregisters_watch_on_close_timeout_or_cancellation(self):
        for stop in ("close", "timeout", "cancel"):
            with self.subTest(stop=stop):
                closed_observers = self.transfer_observers()
                await self.deliver(self.block("minecraft:undyed_shulker_box"), self.opened(-1),
                                   self.storage_content(27, (26, 5, 31)))
                observers = self.transfer_observers()
                sent_before = list(self.wire.sent)
                hold, entered, released = self.hold_stack_writes()
                with hold:
                    taking = self.start(self.bot.take_from_container(26, 3, timeout=0.15 if stop == "timeout" else 2))
                    await asyncio.wait_for(entered.wait(), 1)
                    self.assertEqual(self.transfer_observers(), tuple(count + 1 for count in observers))
                    if stop == "close":
                        await self.deliver(self.closed())
                        self.assertEqual(self.transfer_observers(), closed_observers)
                        released.set()
                        with self.assertRaisesRegex(RuntimeError, "endpoint changed"):
                            await taking
                    elif stop == "timeout":
                        with self.assertRaises(TimeoutError):
                            await taking
                    else:
                        taking.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await taking
                self.assertEqual(self.transfer_observers(), closed_observers if stop == "close" else observers)
                self.assertEqual(self.wire.sent, sent_before)
                await self.deliver(self.closed())
                self.assertEqual(self.transfer_observers(), closed_observers)

    async def test_late_shulker_reply_cannot_change_a_reused_window(self):
        b = self.b
        for opened, replaced, domain, domain_id in (
            ("undyed_shulker_box", "chest", "LEVEL_ENTITY_CONTAINER", 7),
            ("undyed_shulker_box", "ender_chest", "LEVEL_ENTITY_CONTAINER", 7),
            ("red_shulker_box", "blue_shulker_box", "SHULKER_BOX_CONTAINER", 30),
        ):
            with self.subTest(opened=opened, replaced=replaced):
                await self.deliver(self.closed(), self.block(f"minecraft:{opened}"), self.opened(-1),
                                   self.storage_content(27, (26, 5, 31)),
                                   b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=3,
                                                         item=b.SerializedNetworkItemStackDescriptor()))
                taking, request = await self.submit_stack(self.bot.take_from_container(26, 3))
                original = self.bot.open_window
                # The replacement can reuse both the window ID and the stack descriptor.
                await self.deliver(self.closed(), self.block(f"minecraft:{replaced}"), self.opened(-1),
                                   self.storage_content(27, (26, 5, 31)))
                self.assertEqual(self.bot.open_window.container_id, original.container_id)
                self.assertIsNot(self.bot.open_window, original)
                await self.reply_stack(request, ("SHULKER_BOX_CONTAINER", 26, 4, 31),
                                       ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 3, 1, 61))
                self.assertEqual((await taking).result.name, "SUCCESS")
                self.assertEqual((self.bot.container_slots[26].stack_size, self.bot.inventory_slots[3].stack_size),
                                 (5, 1))

                taking, request = await self.submit_stack(self.bot.take_from_container(26, 3))
                self.assertEqual(int(request.actions[0].source.full_container_name.name), domain_id)
                await self.reply_stack(request, (domain, 26, 4, 31),
                                       ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 3, 2, 61))
                self.assertEqual((await taking).result.name, "SUCCESS")

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
