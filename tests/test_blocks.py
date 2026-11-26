import asyncio
import unittest
from unittest.mock import patch

from endpilot.blocks import HASHED_AIR_RUNTIME_ID, Blocks, BlockObservation, BlockUpdate, MiningTarget
from endpilot.inventory import Inventory
from endpilot.movement import Movement
from endpilot.protocol import Protocol


class PacketStream:
    def __init__(self, protocol):
        self.protocol = protocol
        self.observers = []
        self.sent = []

    def subscribe_raw(self, observer):
        self.observers.append(observer)

        def unsubscribe():
            self.observers.remove(observer)

        return unsubscribe

    def subscribe_reader_end(self, observer):
        return lambda: None

    def _reader_end_result(self):
        return False, None

    def emit(self, raw):
        for observer in tuple(self.observers):
            observer(raw)

    async def wait_for_packet(self, packet_id, *, timeout, match=None):
        future = asyncio.get_running_loop().create_future()
        packet_ids = (packet_id,) if isinstance(packet_id, int) else packet_id

        def observe(raw):
            if not future.done() and raw.packet_id in packet_ids and (match is None or match(raw)):
                future.set_result(raw)

        unsubscribe = self.subscribe_raw(observe)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            unsubscribe()

    async def send(self, packet):
        self.sent.append(packet)

    def _defer_receiving_until(self, _ready):
        # Tests drive packet delivery directly through emit().
        pass

    def start_receiving(self, *, _before_dispatch=None):
        pass


class BlocksTests(unittest.IsolatedAsyncioTestCase):
    def mining_fixture(self):
        protocol = Protocol(2225)
        b = protocol.binding
        stream = PacketStream(protocol)
        movement = Movement(stream, 7)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        inventory = Inventory(stream, 7)
        blocks = Blocks(stream, movement, inventory, hashed_runtime_ids=True)
        blocks.start()
        self.addCleanup(inventory.close)
        self.addCleanup(blocks.close)

        def observe(position, runtime_id=0x80310E21):
            x, y, z = position
            stream.emit(protocol.encode(b.UpdateBlockPacket(
                pos=b.BlockPos(x=x, y=y, z=z), runtime_id=runtime_id, layer=0,
            )))

        return protocol, stream, blocks, observe

    async def test_phased_start_switch_and_release_share_one_target(self):
        protocol, stream, blocks, observe = self.mining_fixture()
        a, b = (1, 100, 0), (2, 100, 0)
        observe(a)
        observe(b)
        self.assertEqual(await blocks.start_mining(a, face=4), 1)
        self.assertEqual(blocks.mining_target, MiningTarget(a, 4))
        self.assertEqual(await blocks.continue_mining(a, face=4), 2)
        self.assertEqual(await blocks.continue_mining(b, face=4), 3)
        self.assertEqual(blocks.mining_target, MiningTarget(b, 4))
        sent = [protocol.decode(protocol.encode(frame)) for frame in stream.sent]
        actions = [[action.player_action_type.name for action in frame.player_block_actions] for frame in sent]
        self.assertEqual(actions, [
            ["START_DESTROY_BLOCK"], ["CONTINUE_DESTROY_BLOCK"],
            ["ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK", "START_DESTROY_BLOCK"],
        ])
        self.assertEqual(sent[-1].player_block_actions[0].pos.x, a[0])
        self.assertEqual(sent[-1].player_block_actions[-1].pos.x, b[0])
        observe(b, HASHED_AIR_RUNTIME_ID)
        self.assertIsNone(blocks.mining_target)
        self.assertFalse(blocks.observed_block(a).is_air)
        observe(b)
        self.assertIsNone(blocks.mining_target)
        self.assertEqual(await blocks.stop_mining(), 4)

    async def test_whole_mine_cancellation_cannot_stop_the_next_phased_target(self):
        _, stream, blocks, observe = self.mining_fixture()
        a, b = (1, 100, 0), (2, 100, 0)
        observe(a)
        observe(b)
        started = asyncio.Event()
        original = stream.send

        async def send(packet):
            await original(packet)
            started.set()

        stream.send = send
        whole = asyncio.create_task(blocks.mine_block(a, face=4))
        await started.wait()
        later = asyncio.create_task(blocks.start_mining(b, face=4))
        whole.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await whole
        self.assertEqual(await later, 3)
        self.assertEqual([a.player_action_type.name for a in stream.sent[1].player_block_actions],
                         ["ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK"])
        self.assertEqual([a.player_action_type.name for a in stream.sent[-1].player_block_actions],
                         ["START_DESTROY_BLOCK"])
        self.assertEqual(blocks.mining_target, MiningTarget(b, 4))

    async def test_dimension_change_rejects_a_start_waiting_for_the_movement_lock(self):
        protocol, stream, blocks, observe = self.mining_fixture()
        target = (1, 100, 0)
        observe(target)
        async with blocks.movement._action_lock:
            pending = asyncio.create_task(blocks.start_mining(target))
            await asyncio.sleep(0)
            stream.emit(protocol.encode(protocol.binding.ChangeDimensionPacket(dimension_id=1)))
        with self.assertRaisesRegex(RuntimeError, "world changed"):
            await pending
        self.assertIsNone(blocks.mining_target)
        self.assertEqual(stream.sent, [])

    async def test_dimension_change_rejects_a_start_queued_behind_a_mining_action(self):
        protocol, stream, blocks, observe = self.mining_fixture()
        target = (1, 100, 0)
        observe(target)
        async with blocks._mining_lock:
            pending = asyncio.create_task(blocks.start_mining(target))
            await asyncio.sleep(0)
            stream.emit(protocol.encode(protocol.binding.ChangeDimensionPacket(dimension_id=1)))
            observe(target)
        with self.assertRaisesRegex(RuntimeError, "world changed"):
            await pending
        self.assertIsNone(blocks.mining_target)
        self.assertEqual(stream.sent, [])

    async def test_creative_finish_requires_the_observed_mode_and_preserves_block_observation(self):
        protocol, stream, blocks, observe = self.mining_fixture()
        b = protocol.binding
        target = (1, 100, 0)
        observe(target)
        with self.assertRaisesRegex(RuntimeError, "creative"):
            await blocks.finish_mining(target, face=4)
        self.assertEqual(stream.sent, [])
        stream.emit(protocol.encode(b.SetPlayerGameTypePacket(player_game_type=b.GameType.CREATIVE)))
        self.assertEqual(await blocks.finish_mining(target, face=4), 1)
        frame = protocol.decode(protocol.encode(stream.sent[-1]))
        self.assertEqual(frame.item_use_transaction.transaction.action_type,
                         b.ItemUseInventoryTransaction.ActionType.DESTROY)
        self.assertIsNone(blocks.mining_target)
        self.assertFalse(blocks.observed_block(target).is_air)

    async def test_ignores_other_positions_and_layers_then_returns_server_update(self):
        protocol = Protocol(2193)
        binding = protocol.binding
        stream = PacketStream(protocol)
        blocks = Blocks(stream, None, None)

        def emit(x, layer, runtime):
            packet = binding.UpdateBlockPacket()
            packet.pos = binding.BlockPos(x=x, y=100, z=0)
            packet.runtime_id = runtime
            packet.update_flags = 0
            packet.layer = layer
            stream.emit(protocol.encode(packet))

        pending = asyncio.create_task(blocks.wait_for_update((2, 100, 0), timeout=1))
        await asyncio.sleep(0)
        emit(3, 0, 17)
        emit(2, 1, 18)
        self.assertFalse(pending.done())
        emit(2, 0, 19)
        self.assertEqual(await pending, BlockUpdate((2, 100, 0), 19, 0))
        self.assertEqual(stream.observers, [])

    async def test_timeout_unsubscribes(self):
        stream = PacketStream(Protocol(2193))
        with self.assertRaises(TimeoutError):
            await Blocks(stream, None, None).wait_for_update((2, 100, 0), timeout=0.01)
        self.assertEqual(stream.observers, [])

    async def test_placement_submits_server_item_and_block_runtime_with_shared_tick(self):
        protocol = Protocol(2193)
        b = protocol.binding
        stream = PacketStream(protocol)
        movement = Movement(stream, 7)
        inventory = Inventory(stream, 7)
        blocks = Blocks(stream, movement, inventory)
        movement._input_position = (0.5, 101.62, 0.5)
        movement.position = movement._input_position
        movement._updated.set()
        item = b.SerializedNetworkItemStackDescriptor(
            id=1, stack_size=4, aux_value=0, net_id_variant=9,
            block_runtime_id=123, user_data_buffer=b"\x00" * 10,
        )
        packet = b.InventorySlotPacket(
            inventory_id=b.ContainerID.INVENTORY,
            slot=0,
            full_container_name=None,
            storage_item=None,
            item=item,
        )
        stream.emit(protocol.encode(packet))
        self.assertEqual(await blocks.place_against(BlockUpdate((2, 99, 0), 77, 0)), 1)
        self.assertEqual(len(stream.sent), 2)
        equipment, input_frame = stream.sent
        self.assertIsInstance(equipment, b.MobEquipmentPacket)
        self.assertEqual(int(equipment.runtime_id), 7)
        self.assertEqual(int(equipment.item.id), 1)
        self.assertEqual(int(equipment.selected_slot), 0)
        self.assertIn(b.PlayerAuthInputPacket.InputData.PERFORM_ITEM_INTERACTION, input_frame.input_data)
        self.assertEqual(int(input_frame.item_use_transaction.transaction.target_block_id), 77)
        self.assertEqual(int(input_frame.item_use_transaction.transaction.item.net_id_variant), 9)
        self.assertEqual(int(input_frame.item_use_transaction.transaction.pos.y), 99)
        decoded = protocol.decode(protocol.encode(input_frame))
        self.assertEqual(int(decoded.item_use_transaction.transaction.target_block_id), 77)
        self.assertEqual(decoded.client_tick, 1)
        self.assertEqual(int(decoded.item_use_transaction.transaction.face), 1)
        self.assertEqual(float(decoded.item_use_transaction.transaction.click_pos.y), 1.0)

        # A placement without an explicit hit point uses the clicked face center.
        face_centers = (
            (0.5, 0.0, 0.5), (0.5, 1.0, 0.5),
            (0.5, 0.5, 0.0), (0.5, 0.5, 1.0),
            (0.0, 0.5, 0.5), (1.0, 0.5, 0.5),
        )
        observed = BlockObservation((2, 99, 0), 77, 0, False)
        for face, expected in enumerate(face_centers):
            tick = await blocks.place_against(observed, face=face)
            frame = protocol.decode(protocol.encode(stream.sent[-1]))
            transaction = frame.item_use_transaction.transaction
            self.assertEqual(frame.client_tick, tick)
            self.assertEqual(int(transaction.face), face)
            self.assertEqual(int(transaction.target_block_id), 77)
            self.assertEqual(
                (float(transaction.click_pos.x), float(transaction.click_pos.y),
                 float(transaction.click_pos.z)), expected,
            )

        # An explicit hit point must survive the public transaction encoding.
        # An observed palette-air support without a runtime ID is sent as ID 0.
        tick = await blocks.place_against(
            BlockObservation((2, 99, 0), None, 0, True),
            face=4, click_pos=(0.25, 0.75, 0.125),
        )
        frame = protocol.decode(protocol.encode(stream.sent[-1]))
        transaction = frame.item_use_transaction.transaction
        self.assertEqual(frame.client_tick, tick)
        self.assertEqual(int(transaction.target_block_id), 0)
        self.assertEqual(
            (transaction.click_pos.x, transaction.click_pos.y, transaction.click_pos.z),
            (0.25, 0.75, 0.125),
        )
        inventory.close()

    async def test_mine_block_uses_auth_input_actions_and_waits_for_server_air(self):
        protocol = Protocol(2193)
        b = protocol.binding
        stream = PacketStream(protocol)
        movement = Movement(stream, 7)
        movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        blocks = Blocks(stream, movement, None, hashed_runtime_ids=True)
        original_send = stream.send

        async def send(packet):
            await original_send(packet)
            if not isinstance(packet, b.PlayerAuthInputPacket) or not packet.player_block_actions:
                return
            if packet.player_block_actions[0].player_action_type != b.PlayerActionType.START_DESTROY_BLOCK:
                return
            # An unrelated change is not completion; only server-reported air is.
            for x, runtime_id in ((2, 123), (1, 0xDBF44120)):
                update = b.UpdateBlockPacket()
                update.pos = b.BlockPos(x=x, y=100, z=0)
                update.runtime_id = runtime_id
                update.update_flags = 0
                update.layer = 0
                stream.emit(protocol.encode(update))

        stream.send = send
        observed = await blocks.mine_block((1, 100, 0), face=4, timeout=1)
        self.assertEqual(observed, BlockUpdate((1, 100, 0), 0xDBF44120, 0))
        self.assertEqual([packet.client_tick for packet in stream.sent], [1, 2])
        start, stop = [protocol.decode(protocol.encode(p)) for p in stream.sent]
        self.assertIn(b.PlayerAuthInputPacket.InputData.PERFORM_BLOCK_ACTIONS, start.input_data)
        self.assertEqual(start.player_block_actions[0].player_action_type, b.PlayerActionType.START_DESTROY_BLOCK)
        self.assertEqual(tuple((start.player_block_actions[0].pos.x,
                                start.player_block_actions[0].pos.y,
                                start.player_block_actions[0].pos.z)), (1, 100, 0))
        self.assertEqual(start.player_block_actions[0].facing, 4)
        self.assertEqual(stop.player_block_actions[0].player_action_type, b.PlayerActionType.STOP_DESTROY_BLOCK)
        self.assertEqual(stream.observers, [])

    async def test_mining_timeout_aborts_the_target(self):
        protocol = Protocol(2193)
        b = protocol.binding
        stream = PacketStream(protocol)
        movement = Movement(stream, 7)
        movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        blocks = Blocks(stream, movement, None, hashed_runtime_ids=True)
        target = (1, 100, 0)
        send = stream.send
        timeout_at = asyncio.timeout_at
        observation_timeout = None

        def capture_timeout(deadline):
            nonlocal observation_timeout
            observation_timeout = timeout_at(deadline)
            return observation_timeout

        async def send_on_wire(packet):
            frame = protocol.decode(protocol.encode(packet))
            await send(frame)
            if len(stream.sent) == 1:
                self.assertIsInstance(frame, b.PlayerAuthInputPacket)
                self.assertIn(b.PlayerAuthInputPacket.InputData.PERFORM_BLOCK_ACTIONS, frame.input_data)
                start, = frame.player_block_actions
                self.assertEqual(start.player_action_type, b.PlayerActionType.START_DESTROY_BLOCK)
                self.assertEqual((start.pos.x, start.pos.y, start.pos.z, start.facing), (*target, 4))
                self.assertIsNotNone(observation_timeout)
                # START is on the wire. Expire the wait on the next event loop turn.
                observation_timeout.reschedule(asyncio.get_running_loop().time())

        stream.send = send_on_wire
        with patch.object(asyncio, "timeout_at", capture_timeout), self.assertRaises(TimeoutError):
            await blocks.mine_block(target, face=4)

        self.assertEqual([frame.client_tick for frame in stream.sent], [1, 2])
        self.assertTrue(observation_timeout.expired())
        abort, stop = stream.sent[1].player_block_actions
        self.assertEqual(abort.player_action_type, b.PlayerActionType.ABORT_DESTROY_BLOCK)
        self.assertEqual((abort.pos.x, abort.pos.y, abort.pos.z), target)
        self.assertEqual(stop.player_action_type, b.PlayerActionType.STOP_DESTROY_BLOCK)
        self.assertIsNone(blocks.mining_target)
        self.assertEqual(stream.observers, [])

    async def test_mining_cancel_before_start_unsubscribes_waiter(self):
        protocol = Protocol(2193)
        stream = PacketStream(protocol)
        movement = Movement(stream, 7)
        movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        blocks = Blocks(stream, movement, None, hashed_runtime_ids=True)
        subscribe = stream.subscribe_raw
        task = None

        def cancel_on_subscribe(observer):
            unsubscribe = subscribe(observer)
            task.cancel()  # Cancel while the newly registered waiter is pending.
            return unsubscribe

        stream.subscribe_raw = cancel_on_subscribe
        task = asyncio.create_task(blocks.mine_block((1, 100, 0), timeout=1))
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        self.assertEqual(stream.observers, [])
        self.assertEqual(stream.sent, [])

    async def test_mining_disconnect_preserves_original_error_when_abort_fails(self):
        protocol = Protocol(2193)
        stream = PacketStream(protocol)
        movement = Movement(stream, 7)
        movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        blocks = Blocks(stream, movement, None, hashed_runtime_ids=True)
        original_send = stream.send

        async def send(packet):
            if stream.sent:
                raise OSError("connection already closed")
            await original_send(packet)

        async def disconnected(*args, **kwargs):
            raise ConnectionError("packet stream lost")

        stream.send = send
        stream.wait_for_packet = disconnected
        with self.assertRaisesRegex(ConnectionError, "packet stream lost") as caught:
            await blocks.mine_block((1, 100, 0), timeout=1)
        self.assertIn("mining abort failed", " ".join(caught.exception.__notes__))
        self.assertEqual(len(stream.sent), 1)
