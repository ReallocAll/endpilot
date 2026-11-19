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
