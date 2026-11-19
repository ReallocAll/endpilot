import asyncio
import unittest

from endpilot.movement import Movement
from endpilot.protocol import Protocol


class PacketSink:
    def __init__(self, protocol):
        self.protocol = protocol
        self.sent = []

    async def send(self, packet):
        self.sent.append(self.protocol.decode(self.protocol.encode(packet)))


class MovementTests(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_idle_walk_and_stop_input(self):
        protocol = Protocol(2193)
        b = protocol.binding
        stream = PacketSink(protocol)
        movement = Movement(stream, type("Start", (), {"pos": b.Vec3(x=1.5, y=80.0, z=2.5)})())
        movement.start()
        try:
            await asyncio.sleep(0)
            self.assertEqual(await movement.walk_forward(2), 2)
            await asyncio.sleep(0.06)
        finally:
            await movement.close()
        self.assertEqual(stream.sent[0].input_data, [])
        active = [p for p in stream.sent if b.PlayerAuthInputPacket.InputData.UP in p.input_data]
        self.assertEqual(len(active), 2)
        self.assertIn(b.PlayerAuthInputPacket.InputData.START_SPRINTING, active[0].input_data)
        self.assertAlmostEqual(active[-1].pos.z, 2.68, places=5)
        self.assertIn(b.PlayerAuthInputPacket.InputData.STOP_SPRINTING, stream.sent[-1].input_data)
        self.assertEqual(stream.sent[-1].pos_delta.z, 0.0)
