import asyncio
import unittest

from endpilot.blocks import Blocks, BlockUpdate
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

    def emit(self, raw):
        for observer in tuple(self.observers):
            observer(raw)

    async def wait_for_packet(self, packet_id, *, timeout, match=None):
        future = asyncio.get_running_loop().create_future()

        def observe(raw):
            if not future.done() and raw.packet_id == packet_id and (match is None or match(raw)):
                future.set_result(raw)

        unsubscribe = self.subscribe_raw(observe)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            unsubscribe()

    async def send(self, packet):
        self.sent.append(packet)

    def start_receiving(self):
        pass


class BlocksTests(unittest.IsolatedAsyncioTestCase):
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
        with self.assertRaises(TimeoutError):
            await blocks.mine_block((1, 100, 0), face=4, timeout=0.02)
        self.assertEqual([p.client_tick for p in stream.sent], [1, 2])
        self.assertEqual([action.player_action_type for action in stream.sent[1].player_block_actions], [
            b.PlayerActionType.ABORT_DESTROY_BLOCK,
            b.PlayerActionType.STOP_DESTROY_BLOCK,
        ])
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
