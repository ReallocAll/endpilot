import unittest

from endpilot.movement import Movement
from endpilot.protocol import Protocol


class PacketSink:
    def __init__(self, protocol):
        self.protocol = protocol
        self.observer = None

    def subscribe_raw(self, callback):
        self.observer = callback
        return lambda: None

    def start_receiving(self):
        pass


class MovementTests(unittest.IsolatedAsyncioTestCase):
    async def test_teleport_updates_only_local_player_position(self):
        protocol = Protocol(2193)
        b = protocol.binding
        stream = PacketSink(protocol)
        movement = Movement(stream, 1)
        movement.start()
        packet = b.MovePlayerPacket()
        packet.player_id = 2
        packet.pos = b.Vec3(x=8.5, y=81.0, z=9.5)
        packet.rot = b.Vec2(x=0.0, y=0.0)
        packet.y_head_rot = 0.0
        packet.riding_id = 0
        packet.teleport_data = None
        packet.tick = 0
        stream.observer(protocol.encode(packet))
        self.assertIsNone(movement.position)
        packet.player_id = 1
        stream.observer(protocol.encode(packet))
        self.assertEqual(await movement.wait_for_position_sync(timeout=0.1), (8.5, 81.0, 9.5))
        movement.close()
