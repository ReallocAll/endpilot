import asyncio
import unittest

from endpilot.movement import Movement
from endpilot.protocol import Protocol


class PacketSink:
    def __init__(self, protocol):
        self.protocol = protocol
        self.observer = None
        self.sent = []
        self.on_send = None

    def subscribe_raw(self, callback):
        self.observer = callback
        return lambda: None

    async def send(self, packet):
        self.sent.append(packet)
        if self.on_send is not None:
            self.on_send()

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

    async def test_forward_frames_release_input_and_advance_ticks(self):
        protocol = Protocol(2193)
        b = protocol.binding
        stream = PacketSink(protocol)
        movement = Movement(stream, 1)
        movement.start()
        # Do not submit placeholder input before the server reports a position.
        await asyncio.sleep(0)
        self.assertEqual(stream.sent, [])
        teleport = b.MovePlayerPacket()
        teleport.player_id = 1
        teleport.pos = b.Vec3(x=0.5, y=101.62, z=0.5)
        teleport.rot = b.Vec2(x=0.0, y=0.0)
        teleport.y_head_rot = 0.0
        teleport.riding_id = 0
        teleport.reset_position = 2
        teleport.teleport_data = None
        teleport.tick = 0
        stream.observer(protocol.encode(teleport))
        self.assertEqual(await movement.walk_forward(2), 2)
        self.assertEqual([p.client_tick for p in stream.sent], [1, 2, 3, 4])
        self.assertEqual([list(p.input_data) for p in stream.sent], [
            [b.PlayerAuthInputPacket.InputData.UP,
             b.PlayerAuthInputPacket.InputData.HANDLED_TELEPORT],
            [b.PlayerAuthInputPacket.InputData.UP], [], [],
        ])
        self.assertAlmostEqual(stream.sent[1].pos.z, 0.68, places=5)
        self.assertAlmostEqual(stream.sent[-1].pos.z, 0.68, places=5)
        # Submission must not overwrite the last server-observed position.
        self.assertAlmostEqual(movement.position[0], 0.5)
        self.assertAlmostEqual(movement.position[1], 101.62, places=4)
        self.assertAlmostEqual(movement.position[2], 0.5)
        movement.close()

    async def test_teleport_while_sending_rebases_next_frame(self):
        protocol = Protocol(2193)
        b = protocol.binding
        stream = PacketSink(protocol)
        movement = Movement(stream, 1)

        teleport = b.MovePlayerPacket()
        teleport.player_id = 1
        teleport.pos = b.Vec3(x=0.5, y=100.0, z=0.5)
        teleport.rot = b.Vec2(x=0.0, y=0.0)
        teleport.y_head_rot = 0.0
        teleport.riding_id = 0
        teleport.reset_position = 2
        teleport.teleport_data = None
        teleport.tick = 0
        movement._on_packet(protocol.encode(teleport))

        def correct_during_send():
            if len(stream.sent) == 1:
                teleport.pos = b.Vec3(x=5.5, y=100.0, z=0.5)
                movement._on_packet(protocol.encode(teleport))

        stream.on_send = correct_during_send
        await movement._send_input(True)
        await movement._send_input(True)

        self.assertEqual(movement.position, (5.5, 100.0, 0.5))
        self.assertAlmostEqual(stream.sent[1].pos.x, 5.5)
        self.assertAlmostEqual(stream.sent[1].pos.z, 0.59, places=5)
        self.assertIn(
            b.PlayerAuthInputPacket.InputData.HANDLED_TELEPORT,
            stream.sent[1].input_data,
        )

    async def test_look_at_sets_packet_rotation_camera_and_forward_direction(self):
        protocol = Protocol(2193)
        b = protocol.binding
        stream = PacketSink(protocol)
        movement = Movement(stream, 1)

        teleport = b.MovePlayerPacket()
        teleport.player_id = 1
        teleport.pos = b.Vec3(x=0.5, y=101.62, z=0.5)
        teleport.rot = b.Vec2(x=0.0, y=0.0)
        teleport.y_head_rot = 0.0
        teleport.riding_id = 0
        teleport.teleport_data = None
        teleport.tick = 0
        movement._on_packet(protocol.encode(teleport))

        eye = movement.position
        pitch, yaw = await movement.look_at((eye[0] - 10, eye[1], eye[2]))
        self.assertAlmostEqual(pitch, 0.0)
        self.assertAlmostEqual(yaw, 90.0)
        await movement._send_input(True)
        west = stream.sent[-1]
        self.assertAlmostEqual(west.rot.y, 90.0)
        self.assertAlmostEqual(west.y_head_rot, 90.0)
        self.assertAlmostEqual(west.interact_rotation.y, 90.0)
        self.assertAlmostEqual(west.camera_orientation.x, -1.0)
        self.assertAlmostEqual(west.camera_orientation.y, 0.0)
        self.assertAlmostEqual(west.camera_orientation.z, 0.0, places=5)
        self.assertAlmostEqual(west.pos.x, 0.41, places=5)
        self.assertAlmostEqual(west.pos.z, 0.5, places=5)
        self.assertAlmostEqual(west.pos_delta.x, -0.09, places=5)
        self.assertEqual(list(west.input_data), [b.PlayerAuthInputPacket.InputData.UP])
        self.assertEqual(movement.position, eye)

        await movement.look_at((0.41, 91.62, 0.5))
        await movement._send_input(False)
        down = stream.sent[-1]
        self.assertAlmostEqual(down.rot.x, 90.0, places=5)
        self.assertAlmostEqual(down.camera_orientation.y, -1.0, places=5)
        self.assertEqual(list(down.input_data), [])
    async def test_follow_entity_retargets_and_releases(self):
        from endpilot.entities import Entities, EntityState

        protocol = Protocol(2193)
        b = protocol.binding
        stream = PacketSink(protocol)
        movement = Movement(stream, 1)
        teleport = b.MovePlayerPacket()
        teleport.player_id = 1
        teleport.pos = b.Vec3(x=0.5, y=101.62, z=0.5)
        teleport.rot = b.Vec2(x=0.0, y=0.0)
        teleport.y_head_rot = 0.0
        teleport.riding_id = 0
        teleport.teleport_data = None
        teleport.tick = 0
        movement._on_packet(protocol.encode(teleport))
        entities = Entities(stream)
        entities._record(EntityState(4, 7, "minecraft:cow", (4.5, 100.0, 0.5)))

        def move_target():
            if len(stream.sent) == 2:
                entities._record(EntityState(4, 7, "minecraft:cow", (-4.5, 100.0, 0.5)))

        stream.on_send = move_target
        self.assertEqual(await movement.follow_entity(entities, 7, ticks=5), 5)
        self.assertAlmostEqual(stream.sent[0].rot.y, -90.0)
        self.assertTrue(any(abs(p.rot.y - 90.0) < 1e-6 for p in stream.sent[2:5]))
        self.assertTrue(all(b.PlayerAuthInputPacket.InputData.UP in p.input_data
                            for p in stream.sent[:5]))
        self.assertEqual([list(p.input_data) for p in stream.sent[-2:]], [[], []])
        self.assertEqual([p.client_tick for p in stream.sent], list(range(1, 8)))

        # Cancelled tracking must release input and allow an immediate new target.
        entities._record(EntityState(5, 8, "minecraft:cow", (4.5, 100.0, 0.5)))
        task = asyncio.create_task(movement.follow_entity(entities, 7, ticks=100))
        await asyncio.sleep(0.08)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([list(p.input_data) for p in stream.sent[-2:]], [[], []])
        before = len(stream.sent)
        self.assertEqual(await movement.follow_entity(entities, 8, ticks=2), 2)
        self.assertAlmostEqual(stream.sent[before].rot.y, -90.0)
        self.assertEqual([list(p.input_data) for p in stream.sent[-2:]], [[], []])

    async def test_follow_entity_unobserved_target_stops_without_moving(self):
        from endpilot.entities import Entities

        protocol = Protocol(2193)
        b = protocol.binding
        stream = PacketSink(protocol)
        movement = Movement(stream, 1)
        packet = b.MovePlayerPacket()
        packet.player_id = 1
        packet.pos = b.Vec3(x=0.5, y=101.62, z=0.5)
        packet.rot = b.Vec2(x=0.0, y=0.0)
        packet.y_head_rot = 0.0
        packet.riding_id = 0
        packet.teleport_data = None
        packet.tick = 0
        movement._on_packet(protocol.encode(packet))
        self.assertEqual(await movement.follow_entity(Entities(stream), 123, ticks=10), 0)
        self.assertEqual([list(p.input_data) for p in stream.sent], [[], []])

    async def test_explicit_stop_releases_follow_then_allows_another_walk(self):
        from endpilot.entities import Entities, EntityState

        protocol = Protocol(2193)
        b = protocol.binding
        stream = PacketSink(protocol)
        movement = Movement(stream, 1)
        packet = b.MovePlayerPacket()
        packet.player_id = 1
        packet.pos = b.Vec3(x=0.5, y=101.62, z=0.5)
        packet.rot = b.Vec2(x=0.0, y=0.0)
        packet.y_head_rot = 0.0
        packet.riding_id = 0
        packet.teleport_data = None
        packet.tick = 0
        movement._on_packet(protocol.encode(packet))
        entities = Entities(stream)
        entities._record(EntityState(4, 7, "minecraft:cow", (-6.0, 100.0, 0.5)))

        follow = asyncio.create_task(movement.follow_entity(entities, 7, ticks=100))
        await asyncio.sleep(0.13)
        await movement.stop_moving()
        submitted = await follow
        self.assertGreater(submitted, 0)
        self.assertLess(submitted, 100)
        self.assertEqual(
            sum(b.PlayerAuthInputPacket.InputData.UP in p.input_data for p in stream.sent),
            submitted,
        )
        self.assertEqual([list(p.input_data) for p in stream.sent[-2:]], [[], []])

        before = len(stream.sent)
        walk = asyncio.create_task(movement.walk_forward(100))
        await asyncio.sleep(0.13)
        await movement.stop_moving()
        walked = await walk
        self.assertGreater(walked, 0)
        self.assertLess(walked, 100)
        self.assertEqual(
            sum(b.PlayerAuthInputPacket.InputData.UP in p.input_data for p in stream.sent[before:]),
            walked,
        )
        self.assertEqual([list(p.input_data) for p in stream.sent[-2:]], [[], []])
        self.assertEqual([p.client_tick for p in stream.sent], list(range(1, len(stream.sent) + 1)))
