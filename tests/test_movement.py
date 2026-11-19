import unittest

from endpilot.movement import Movement
from endpilot.protocol import Protocol


class PacketSink:
    def __init__(self, protocol):
        self.protocol = protocol
        self.sent = []

    async def send(self, packet):
        # Exercise the installed upstream nanobind serializer, not a fake packet type.
        raw = self.protocol.encode(packet)
        self.sent.append(self.protocol.decode(raw))


class MovementTests(unittest.IsolatedAsyncioTestCase):
    async def test_forward_frames_are_serializable_and_stop(self):
        protocol = Protocol(2193)
        b = protocol.binding
        stream = PacketSink(protocol)
        movement = Movement(stream, type("Start", (), {"pos": b.Vec3(x=1.5, y=80.0, z=2.5)})())
        self.assertEqual(await movement.walk_forward(2, interval=0), 2)
        self.assertEqual(len(stream.sent), 3)
        self.assertEqual([p.client_tick for p in stream.sent], [1, 2, 3])
        self.assertAlmostEqual(stream.sent[1].pos.z, 2.68, places=5)
        self.assertIn(b.PlayerAuthInputPacket.InputData.UP, stream.sent[0].input_data)
        self.assertEqual(stream.sent[-1].input_data, [])
        self.assertEqual(float(stream.sent[-1].pos_delta.z), 0.0)
