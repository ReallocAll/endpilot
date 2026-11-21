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

    async def test_sneak_input_transition_is_held_then_released(self):
        protocol = Protocol(2193)
        b = protocol.binding
        stream = PacketSink(protocol)
        movement = Movement(stream, 1)
        movement._input_position = (0.5, 101.62, 0.5)
        movement.position = movement._input_position
        flags = b.PlayerAuthInputPacket.InputData

        self.assertEqual(await movement.set_sneaking(True), 1)
        self.assertEqual(set(stream.sent[-1].input_data), {
            flags.SNEAKING, flags.SNEAK_DOWN, flags.SNEAK_CURRENT_RAW,
            flags.WANT_DOWN, flags.START_SNEAKING, flags.SNEAK_PRESSED_RAW,
        })
        self.assertEqual(await movement.set_sneaking(True), 2)
        self.assertEqual(set(stream.sent[-1].input_data), {
            flags.SNEAKING, flags.SNEAK_DOWN, flags.SNEAK_CURRENT_RAW, flags.WANT_DOWN,
        })
        await movement._send_input(True)
        self.assertIn(flags.UP, stream.sent[-1].input_data)
        self.assertAlmostEqual(stream.sent[-1].move.y, 0.3)
        self.assertAlmostEqual(stream.sent[-1].pos.z, 0.527, places=5)

        self.assertEqual(await movement.set_sneaking(False), 4)
        self.assertEqual(set(stream.sent[-1].input_data), {
            flags.STOP_SNEAKING, flags.SNEAK_RELEASED_RAW,
        })
        await movement._send_input(False)
        self.assertEqual(list(stream.sent[-1].input_data), [])
        self.assertEqual(movement.position, (0.5, 101.62, 0.5))
        movement.close()


    async def test_jump_has_client_flags_vertical_prediction_and_landing(self):
        protocol = Protocol(2193)
        b = protocol.binding
        stream = PacketSink(protocol)
        movement = Movement(stream, 1)
        movement.position = (0.5, 101.62, 0.5)
        movement._input_position = movement.position
        flags = b.PlayerAuthInputPacket.InputData

        self.assertEqual(await movement.jump(), 1)
        first = stream.sent[-1]
        self.assertEqual(set(first.input_data), {
            flags.JUMPING, flags.JUMP_DOWN, flags.JUMP_CURRENT_RAW,
            flags.WANT_UP, flags.JUMP_PRESSED_RAW, flags.START_JUMPING,
        })
        self.assertAlmostEqual(first.pos_delta.y, 0.42, places=5)
        self.assertAlmostEqual(first.pos.y, 102.04, places=5)

        for _ in range(14):
            await movement._send_input(False)
        self.assertEqual(list(stream.sent[1].input_data), [flags.JUMP_RELEASED_RAW])
        self.assertTrue(all(not p.input_data for p in stream.sent[2:]))
        self.assertGreater(max(p.pos.y for p in stream.sent), 102.6)
        self.assertAlmostEqual(stream.sent[-1].pos.y, 101.62, places=5)
        self.assertEqual([p.client_tick for p in stream.sent], list(range(1, 16)))
        self.assertEqual(movement.position, (0.5, 101.62, 0.5))
        movement.close()

    async def test_jump_forward_keeps_forward_input_through_jump_and_releases(self):
        protocol = Protocol(2193)
        b = protocol.binding
        flags = b.PlayerAuthInputPacket.InputData
        sink = PacketSink(protocol)
        movement = Movement(sink, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)

        self.assertEqual(await movement.jump_forward(4), 4)
        self.assertEqual([p.client_tick for p in sink.sent], list(range(1, 7)))
        self.assertTrue(all(flags.UP in p.input_data for p in sink.sent[:4]))
        self.assertTrue(all(p.move.y == 1.0 for p in sink.sent[:4]))
        self.assertTrue(all(p.pos_delta.z > 0 for p in sink.sent[:4]))
        self.assertIn(flags.START_JUMPING, sink.sent[0].input_data)
        self.assertIn(flags.JUMPING, sink.sent[0].input_data)
        self.assertIn(flags.JUMP_RELEASED_RAW, sink.sent[1].input_data)
        self.assertTrue(all(flags.JUMPING not in p.input_data for p in sink.sent[1:]))
        self.assertTrue(all(flags.UP not in p.input_data for p in sink.sent[4:]))
        self.assertEqual([p.move.y for p in sink.sent[4:]], [0.0, 0.0])
        self.assertGreater(max(p.pos.y for p in sink.sent), 102.0)
        self.assertEqual(movement.position, (0.5, 101.62, 0.5))
        movement.close()

    async def test_server_teleport_interrupts_jump_prediction(self):
        protocol = Protocol(2193)
        b = protocol.binding
        stream = PacketSink(protocol)
        movement = Movement(stream, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        await movement.jump()

        teleport = b.MovePlayerPacket()
        teleport.player_id = 1
        teleport.pos = b.Vec3(x=4.5, y=80.0, z=4.5)
        teleport.rot = b.Vec2(x=0.0, y=0.0)
        teleport.y_head_rot = 0.0
        teleport.riding_id = 0
        teleport.reset_position = 2
        teleport.teleport_data = None
        teleport.tick = 0
        movement._on_packet(protocol.encode(teleport))
        await movement._send_input(False)
        self.assertEqual(
            set(stream.sent[-1].input_data),
            {b.PlayerAuthInputPacket.InputData.HANDLED_TELEPORT,
             b.PlayerAuthInputPacket.InputData.JUMP_RELEASED_RAW},
        )
        self.assertAlmostEqual(stream.sent[-1].pos.y, 80.0)
        self.assertAlmostEqual(stream.sent[-1].pos_delta.y, 0.0)
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


    async def test_switch_active_follow_without_releasing_input(self):
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
        entities._record(EntityState(5, 8, "minecraft:cow", (6.0, 100.0, 0.5)))

        with self.assertRaisesRegex(RuntimeError, "no active entity follow"):
            movement.switch_follow_target(8)
        task = asyncio.create_task(movement.follow_entity(entities, 7, ticks=100))
        await asyncio.sleep(0.14)
        before = len(stream.sent)
        self.assertGreaterEqual(before, 2)
        self.assertTrue(all(abs(p.rot.y - 90.0) < 1e-6 for p in stream.sent))
        movement.switch_follow_target(8)
        await asyncio.sleep(0.14)
        after = len(stream.sent)
        self.assertGreater(after, before)
        self.assertTrue(all(abs(p.rot.y + 90.0) < 1e-6
                            for p in stream.sent[before:after]))
        self.assertTrue(all(b.PlayerAuthInputPacket.InputData.UP in p.input_data
                            for p in stream.sent[:after]))
        await movement.stop_moving()
        self.assertLess(await task, 100)
        self.assertEqual([list(p.input_data) for p in stream.sent[-2:]], [[], []])
        self.assertEqual([p.client_tick for p in stream.sent],
                         list(range(1, len(stream.sent) + 1)))
        with self.assertRaisesRegex(RuntimeError, "no active entity follow"):
            movement.switch_follow_target(7)

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


        # A queued new action must not extend stop_moving()'s wait for
        # the action that was active when stop was requested.
        following = asyncio.create_task(movement.follow_entity(entities, 7, ticks=100))
        await asyncio.sleep(0.13)
        queued_walk = asyncio.create_task(movement.walk_forward(100))
        await asyncio.sleep(0)
        await asyncio.wait_for(movement.stop_moving(), timeout=0.8)
        self.assertLess(await following, 100)
        self.assertFalse(queued_walk.done())
        queued_walk.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued_walk
        self.assertEqual([list(p.input_data) for p in stream.sent[-2:]], [[], []])

    async def test_strafe_frames_match_client_vector_and_release(self):
        protocol = Protocol(2193)
        b = protocol.binding
        sink = PacketSink(protocol)
        movement = Movement(sink, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        flags = b.PlayerAuthInputPacket.InputData

        self.assertEqual(await movement.strafe_left(2), 2)
        self.assertEqual(await movement.strafe_right(2), 2)
        self.assertEqual([p.client_tick for p in sink.sent], list(range(1, 9)))
        self.assertEqual([list(p.input_data) for p in sink.sent], [
            [flags.LEFT], [flags.LEFT], [], [],
            [flags.RIGHT], [flags.RIGHT], [], [],
        ])
        self.assertEqual([p.move.x for p in sink.sent],
                         [1.0, 1.0, 0.0, 0.0, -1.0, -1.0, 0.0, 0.0])
        self.assertTrue(all(p.move.y == 0 and p.rot.y == 0 for p in sink.sent))
        self.assertAlmostEqual(sink.sent[1].pos.x, 0.68, places=5)
        self.assertAlmostEqual(sink.sent[5].pos.x, 0.5, places=5)
        self.assertAlmostEqual(sink.sent[0].pos_delta.x, 0.09, places=5)
        self.assertAlmostEqual(sink.sent[4].pos_delta.x, -0.09, places=5)
        self.assertTrue(all(p.analog_move_vector.x == p.move.x
                            and p.raw_move_vector.x == p.move.x for p in sink.sent))
        self.assertEqual(movement.position, (0.5, 101.62, 0.5))

        await movement.look_at((-10, 101.62, 0.5))
        await movement._send_input(False, strafe=1)
        self.assertAlmostEqual(sink.sent[-1].rot.y, 90.0, places=5)
        self.assertAlmostEqual(sink.sent[-1].pos_delta.z, 0.09, places=5)
        self.assertIn(flags.LEFT, sink.sent[-1].input_data)
        movement.close()

    async def test_forward_diagonal_frames_keep_raw_keys_and_normalize_prediction(self):
        protocol = Protocol(2193)
        b = protocol.binding
        flags = b.PlayerAuthInputPacket.InputData
        sink = PacketSink(protocol)
        movement = Movement(sink, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)

        self.assertEqual(await movement.walk_forward_left(2), 2)
        self.assertEqual(await movement.walk_forward_right(2), 2)
        self.assertEqual([p.client_tick for p in sink.sent], list(range(1, 9)))
        self.assertEqual([list(p.input_data) for p in sink.sent], [
            [flags.UP, flags.LEFT], [flags.UP, flags.LEFT], [], [],
            [flags.UP, flags.RIGHT], [flags.UP, flags.RIGHT], [], [],
        ])
        self.assertEqual([(p.move.x, p.move.y) for p in sink.sent], [
            (1.0, 1.0), (1.0, 1.0), (0.0, 0.0), (0.0, 0.0),
            (-1.0, 1.0), (-1.0, 1.0), (0.0, 0.0), (0.0, 0.0),
        ])
        self.assertAlmostEqual(sink.sent[0].pos_delta.x, 0.09 / 2**0.5)
        self.assertAlmostEqual(sink.sent[0].pos_delta.z, 0.09 / 2**0.5)
        self.assertAlmostEqual(sink.sent[1].pos.x, 0.5 + 0.18 / 2**0.5)
        self.assertAlmostEqual(sink.sent[5].pos.x, 0.5)
        self.assertAlmostEqual(sink.sent[5].pos.z, 0.5 + 0.36 / 2**0.5)
        self.assertTrue(all(p.analog_move_vector.x == p.move.x
                            and p.raw_move_vector.y == p.move.y for p in sink.sent))
        self.assertTrue(all(p.rot.y == 0.0 for p in sink.sent))
        self.assertEqual(movement.position, (0.5, 101.62, 0.5))
        movement.close()

    async def test_backward_diagonal_frames_normalize_and_release(self):
        protocol = Protocol(2193)
        b = protocol.binding
        flags = b.PlayerAuthInputPacket.InputData
        sink = PacketSink(protocol)
        movement = Movement(sink, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)

        self.assertEqual(await movement.walk_backward_left(2), 2)
        self.assertEqual(await movement.walk_backward_right(2), 2)
        self.assertEqual([p.client_tick for p in sink.sent], list(range(1, 9)))
        self.assertEqual([list(p.input_data) for p in sink.sent], [
            [flags.DOWN, flags.LEFT], [flags.DOWN, flags.LEFT], [], [],
            [flags.DOWN, flags.RIGHT], [flags.DOWN, flags.RIGHT], [], [],
        ])
        self.assertEqual([(p.move.x, p.move.y) for p in sink.sent], [
            (1.0, -1.0), (1.0, -1.0), (0.0, 0.0), (0.0, 0.0),
            (-1.0, -1.0), (-1.0, -1.0), (0.0, 0.0), (0.0, 0.0),
        ])
        step = 0.09 / 2**0.5
        self.assertAlmostEqual(sink.sent[0].pos_delta.x, step)
        self.assertAlmostEqual(sink.sent[0].pos_delta.z, -step)
        self.assertAlmostEqual(sink.sent[1].pos.x, 0.5 + 2 * step)
        self.assertAlmostEqual(sink.sent[1].pos.z, 0.5 - 2 * step)
        self.assertAlmostEqual(sink.sent[5].pos.x, 0.5)
        self.assertAlmostEqual(sink.sent[5].pos.z, 0.5 - 4 * step)
        self.assertTrue(all(p.analog_move_vector.x == p.move.x
                            and p.raw_move_vector.y == p.move.y for p in sink.sent))
        self.assertTrue(all(p.rot.y == 0.0 for p in sink.sent))
        self.assertEqual(movement.position, (0.5, 101.62, 0.5))
        movement.close()

    async def test_strafe_does_not_start_held_sprint(self):
        protocol = Protocol(2193)
        flags = protocol.binding.PlayerAuthInputPacket.InputData
        sink = PacketSink(protocol)
        movement = Movement(sink, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        await movement.set_sprinting(True)
        self.assertEqual(await movement.strafe_left(1), 1)
        self.assertEqual(set(sink.sent[1].input_data),
                         {flags.LEFT, flags.SPRINT_DOWN})
        self.assertNotIn(flags.START_SPRINTING, sink.sent[1].input_data)
        self.assertNotIn(flags.SPRINTING, sink.sent[1].input_data)
        self.assertEqual(list(sink.sent[-1].input_data), [flags.SPRINT_DOWN])
        movement.close()

    async def test_backward_frames_keep_facing_and_release_input(self):
        protocol = Protocol(2193)
        b = protocol.binding
        sink = PacketSink(protocol)
        movement = Movement(sink, 1)
        movement._input_position = (0.5, 101.62, 0.5)
        movement.position = movement._input_position
        flags = b.PlayerAuthInputPacket.InputData

        self.assertEqual(await movement.walk_backward(2), 2)
        self.assertEqual([p.client_tick for p in sink.sent], [1, 2, 3, 4])
        self.assertEqual([list(p.input_data) for p in sink.sent],
                         [[flags.DOWN], [flags.DOWN], [], []])
        self.assertEqual([p.move.y for p in sink.sent], [-1.0, -1.0, 0.0, 0.0])
        self.assertTrue(all(p.rot.y == 0.0 for p in sink.sent))
        self.assertAlmostEqual(sink.sent[1].pos.z, 0.32, places=5)
        self.assertAlmostEqual(sink.sent[1].pos_delta.z, -0.09, places=5)
        self.assertEqual(movement.position, (0.5, 101.62, 0.5))

        await movement.look_at((-10, 101.62, 0.32))
        await movement._send_input(False, backward=True)
        self.assertAlmostEqual(sink.sent[-1].rot.y, 90.0, places=5)
        self.assertAlmostEqual(sink.sent[-1].pos_delta.x, 0.09, places=5)
        self.assertIn(flags.DOWN, sink.sent[-1].input_data)
        movement.close()

    async def test_backward_input_does_not_engage_held_sprint(self):
        protocol = Protocol(2193)
        flags = protocol.binding.PlayerAuthInputPacket.InputData
        sink = PacketSink(protocol)
        movement = Movement(sink, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        await movement.set_sprinting(True)
        self.assertEqual(await movement.walk_backward(1), 1)
        self.assertEqual(set(sink.sent[1].input_data),
                         {flags.DOWN, flags.SPRINT_DOWN})
        self.assertNotIn(flags.SPRINTING, sink.sent[1].input_data)
        self.assertEqual(list(sink.sent[-1].input_data), [flags.SPRINT_DOWN])
        movement.close()

    async def test_held_sprint_starts_only_forward_and_stops_on_release_or_sneak(self):
        protocol = Protocol(2193)
        b = protocol.binding
        flags = b.PlayerAuthInputPacket.InputData
        sink = PacketSink(protocol)
        movement = Movement(sink, 1)
        movement._input_position = (0.5, 101.62, 0.5)
        movement.position = movement._input_position

        self.assertEqual(await movement.set_sprinting(True), 1)
        self.assertEqual(set(sink.sent[-1].input_data), {flags.SPRINT_DOWN})
        await movement._send_input(True)
        self.assertEqual(set(sink.sent[-1].input_data), {
            flags.UP, flags.SPRINTING, flags.SPRINT_DOWN, flags.START_SPRINTING,
        })
        self.assertAlmostEqual(sink.sent[-1].pos.z, 0.617, places=5)
        await movement._send_input(True)
        self.assertEqual(set(sink.sent[-1].input_data), {
            flags.UP, flags.SPRINTING, flags.SPRINT_DOWN,
        })
        await movement._send_input(False)
        self.assertEqual(set(sink.sent[-1].input_data), {
            flags.SPRINT_DOWN, flags.STOP_SPRINTING,
        })

        await movement.set_sneaking(True)
        await movement._send_input(True)
        self.assertNotIn(flags.START_SPRINTING, sink.sent[-1].input_data)
        self.assertNotIn(flags.SPRINTING, sink.sent[-1].input_data)
        await movement.set_sneaking(False)
        await movement._send_input(True)
        self.assertIn(flags.START_SPRINTING, sink.sent[-1].input_data)
        await movement._send_input(False)
        await movement.set_sprinting(False)
        self.assertEqual(list(sink.sent[-1].input_data), [])
        movement.close()
