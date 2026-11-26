import asyncio
import unittest

from endpilot.bot import Bot
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


    async def test_missed_swing_sends_attack_animation_before_one_input_flag(self):
        protocol = Protocol(2193)
        b = protocol.binding
        sink = PacketSink(protocol)
        movement = Movement(sink, 17)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)

        self.assertEqual(await movement.missed_swing(), 1)
        self.assertEqual(len(sink.sent), 2)
        animation, input_packet = [
            protocol.decode(protocol.encode(packet)) for packet in sink.sent
        ]
        self.assertIsInstance(animation, b.AnimatePacket)
        self.assertEqual(animation.action, b.AnimatePacket.Action.SWING)
        self.assertEqual(int(animation.runtime_id), 17)
        self.assertEqual(animation.swing_source, b.ActorSwingSource.ATTACK)
        self.assertEqual(animation.data, 0.0)
        self.assertIsInstance(input_packet, b.PlayerAuthInputPacket)
        self.assertEqual(input_packet.client_tick, 1)
        self.assertEqual(list(input_packet.input_data), [
            b.PlayerAuthInputPacket.InputData.MISSED_SWING
        ])
        self.assertEqual((input_packet.move.x, input_packet.move.y), (0.0, 0.0))
        self.assertEqual(movement.position, (0.5, 101.62, 0.5))

        await movement._send_input(False)
        self.assertEqual(sink.sent[-1].client_tick, 2)
        self.assertNotIn(b.PlayerAuthInputPacket.InputData.MISSED_SWING,
                         sink.sent[-1].input_data)
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

    async def test_sprint_jump_preserves_held_keys_and_horizontal_launch_impulse(self):
        protocol = Protocol(2193)
        flags = protocol.binding.PlayerAuthInputPacket.InputData
        sink = PacketSink(protocol)
        movement = Movement(sink, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)

        key_tick = await movement.set_sprinting(True)
        self.assertEqual(key_tick, 1)
        self.assertIn(flags.SPRINT_DOWN, sink.sent[0].input_data)
        self.assertNotIn(flags.SPRINTING, sink.sent[0].input_data)
        self.assertEqual(await movement.jump_forward(4), 4)
        first, second = sink.sent[1:3]
        self.assertTrue({
            flags.UP, flags.SPRINT_DOWN, flags.SPRINTING, flags.START_SPRINTING,
            flags.JUMPING, flags.START_JUMPING,
        }.issubset(set(first.input_data)))
        self.assertAlmostEqual(first.pos_delta.y, 0.42, places=5)
        self.assertAlmostEqual(first.pos_delta.z, 0.117 + 0.2, places=5)
        self.assertAlmostEqual(second.pos_delta.z, 0.117 + 0.2 * 0.546, places=5)
        self.assertIn(flags.JUMP_RELEASED_RAW, second.input_data)
        self.assertNotIn(flags.JUMPING, second.input_data)
        self.assertIn(flags.STOP_SPRINTING, sink.sent[5].input_data)
        self.assertNotIn(flags.SPRINTING, sink.sent[5].input_data)
        self.assertGreater(sink.sent[5].pos_delta.z, 0)
        release_tick = await movement.set_sprinting(False)
        self.assertEqual(release_tick, 8)
        self.assertNotIn(flags.SPRINT_DOWN, sink.sent[-1].input_data)
        movement.close()

    async def test_forward_hops_repeat_after_predicted_landing_and_release(self):
        protocol = Protocol(2193)
        b = protocol.binding
        flags = b.PlayerAuthInputPacket.InputData
        sink = PacketSink(protocol)
        movement = Movement(sink, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)

        self.assertEqual(await movement.hop_forward(28), 28)
        self.assertEqual(len(sink.sent), 30)
        launches = [
            i for i, packet in enumerate(sink.sent[:28])
            if flags.START_JUMPING in packet.input_data
        ]
        self.assertGreaterEqual(len(launches), 2)
        self.assertEqual(launches[0], 0)
        for i in launches:
            self.assertIn(flags.JUMP_PRESSED_RAW, sink.sent[i].input_data)
            self.assertIn(flags.JUMPING, sink.sent[i].input_data)
            self.assertGreater(sink.sent[i].pos_delta.y, 0)
        for previous, following in zip(launches, launches[1:]):
            self.assertGreater(following - previous, 5)
            self.assertTrue(any(
                flags.JUMP_RELEASED_RAW in sink.sent[i].input_data
                for i in range(previous + 1, following)
            ))
        self.assertTrue(all(flags.UP in p.input_data for p in sink.sent[:28]))
        self.assertTrue(all(flags.UP not in p.input_data for p in sink.sent[28:]))
        self.assertEqual([p.client_tick for p in sink.sent], list(range(1, 31)))
        self.assertEqual(movement.position, (0.5, 101.62, 0.5))
        movement.close()

    async def test_backward_hops_repeat_and_release(self):
        protocol = Protocol(2193)
        flags = protocol.binding.PlayerAuthInputPacket.InputData
        sink = PacketSink(protocol)
        movement = Movement(sink, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)

        self.assertEqual(await movement.hop_backward(28), 28)
        self.assertEqual(len(sink.sent), 30)
        launches = [
            i for i, packet in enumerate(sink.sent[:28])
            if flags.START_JUMPING in packet.input_data
        ]
        self.assertGreaterEqual(len(launches), 2)
        self.assertEqual(launches[0], 0)
        for i in launches:
            self.assertIn(flags.JUMP_PRESSED_RAW, sink.sent[i].input_data)
            self.assertGreater(sink.sent[i].pos_delta.y, 0)
        for previous, following in zip(launches, launches[1:]):
            self.assertTrue(any(
                flags.JUMP_RELEASED_RAW in sink.sent[i].input_data
                for i in range(previous + 1, following)
            ))
        for packet in sink.sent[:28]:
            self.assertIn(flags.DOWN, packet.input_data)
            self.assertNotIn(flags.UP, packet.input_data)
            self.assertEqual(packet.move.y, -1.0)
            self.assertLess(packet.pos_delta.z, 0)
        self.assertTrue(all(not packet.input_data for packet in sink.sent[28:]))
        self.assertEqual([p.client_tick for p in sink.sent], list(range(1, 31)))
        movement.close()

    async def test_lateral_hops_repeat_jump_with_strafe_and_release(self):
        protocol = Protocol(2193)
        flags = protocol.binding.PlayerAuthInputPacket.InputData
        for direction, sign, flag in (
            ("left", 1, flags.LEFT), ("right", -1, flags.RIGHT),
        ):
            with self.subTest(direction=direction):
                sink = PacketSink(protocol)
                movement = Movement(sink, 1)
                movement.position = movement._input_position = (0.5, 101.62, 0.5)

                self.assertEqual(await getattr(movement, f"hop_{direction}")(28), 28)
                self.assertEqual(len(sink.sent), 30)
                launches = [
                    i for i, packet in enumerate(sink.sent[:28])
                    if flags.START_JUMPING in packet.input_data
                ]
                self.assertGreaterEqual(len(launches), 2)
                self.assertEqual(launches[0], 0)
                for i in launches:
                    self.assertIn(flags.JUMP_PRESSED_RAW, sink.sent[i].input_data)
                    self.assertIn(flags.JUMPING, sink.sent[i].input_data)
                    self.assertGreater(sink.sent[i].pos_delta.y, 0)
                for prev, later in zip(launches, launches[1:]):
                    self.assertGreater(later - prev, 5)
                    self.assertTrue(any(
                        flags.JUMP_RELEASED_RAW in sink.sent[i].input_data
                        for i in range(prev + 1, later)
                    ))
                for packet in sink.sent[:28]:
                    self.assertIn(flag, packet.input_data)
                    self.assertNotIn(flags.UP, packet.input_data)
                    self.assertNotIn(flags.DOWN, packet.input_data)
                    self.assertEqual((packet.move.x, packet.move.y), (sign, 0.0))
                    self.assertEqual(packet.raw_move_vector.x, sign)
                    self.assertEqual(packet.analog_move_vector.x, sign)
                    self.assertAlmostEqual(packet.pos_delta.x, sign * 0.09)
                    self.assertAlmostEqual(packet.pos_delta.z, 0.0)
                self.assertTrue(all(not p.input_data for p in sink.sent[28:]))
                self.assertEqual([p.client_tick for p in sink.sent], list(range(1, 31)))
                self.assertEqual(movement.position, (0.5, 101.62, 0.5))
                movement.close()

    async def test_forward_diagonal_hops_repeat_and_release(self):
        protocol = Protocol(2193)
        flags = protocol.binding.PlayerAuthInputPacket.InputData
        for direction, sign, lateral_flag in (
            ("left", 1, flags.LEFT), ("right", -1, flags.RIGHT),
        ):
            with self.subTest(direction=direction):
                sink = PacketSink(protocol)
                movement = Movement(sink, 1)
                movement.position = movement._input_position = (0.5, 101.62, 0.5)

                self.assertEqual(await getattr(movement, f"hop_forward_{direction}")(28), 28)
                self.assertEqual(len(sink.sent), 30)
                launches = [
                    i for i, packet in enumerate(sink.sent[:28])
                    if flags.START_JUMPING in packet.input_data
                ]
                self.assertGreaterEqual(len(launches), 2)
                self.assertEqual(launches[0], 0)
                for i in launches:
                    self.assertIn(flags.JUMP_PRESSED_RAW, sink.sent[i].input_data)
                    self.assertGreater(sink.sent[i].pos_delta.y, 0)
                for earlier, later in zip(launches, launches[1:]):
                    self.assertGreater(later - earlier, 5)
                    self.assertTrue(any(
                        flags.JUMP_RELEASED_RAW in sink.sent[i].input_data
                        for i in range(earlier + 1, later)
                    ))
                for packet in sink.sent[:28]:
                    self.assertIn(flags.UP, packet.input_data)
                    self.assertIn(lateral_flag, packet.input_data)
                    self.assertNotIn(flags.DOWN, packet.input_data)
                    self.assertEqual((packet.move.x, packet.move.y), (sign, 1.0))
                    self.assertEqual((packet.raw_move_vector.x, packet.raw_move_vector.y), (sign, 1.0))
                    self.assertEqual((packet.analog_move_vector.x, packet.analog_move_vector.y), (sign, 1.0))
                    self.assertAlmostEqual(packet.pos_delta.x, sign * 0.09 / 2**0.5)
                    self.assertAlmostEqual(packet.pos_delta.z, 0.09 / 2**0.5)
                self.assertTrue(all(not p.input_data for p in sink.sent[28:]))
                self.assertEqual([p.client_tick for p in sink.sent], list(range(1, 31)))
                self.assertEqual(movement.position, (0.5, 101.62, 0.5))
                movement.close()

    async def test_backward_diagonal_hops_repeat_and_release(self):
        protocol = Protocol(2193)
        flags = protocol.binding.PlayerAuthInputPacket.InputData
        for direction, sign, lateral_flag in (
            ("left", 1, flags.LEFT), ("right", -1, flags.RIGHT),
        ):
            with self.subTest(direction=direction):
                sink = PacketSink(protocol)
                movement = Movement(sink, 1)
                movement.position = movement._input_position = (0.5, 101.62, 0.5)

                self.assertEqual(await getattr(movement, f"hop_backward_{direction}")(28), 28)
                self.assertEqual(len(sink.sent), 30)
                launches = [
                    i for i, packet in enumerate(sink.sent[:28])
                    if flags.START_JUMPING in packet.input_data
                ]
                self.assertGreaterEqual(len(launches), 2)
                self.assertEqual(launches[0], 0)
                for i in launches:
                    self.assertIn(flags.JUMP_PRESSED_RAW, sink.sent[i].input_data)
                    self.assertGreater(sink.sent[i].pos_delta.y, 0)
                for earlier, later in zip(launches, launches[1:]):
                    self.assertGreater(later - earlier, 5)
                    self.assertTrue(any(
                        flags.JUMP_RELEASED_RAW in sink.sent[i].input_data
                        for i in range(earlier + 1, later)
                    ))
                for packet in sink.sent[:28]:
                    self.assertIn(flags.DOWN, packet.input_data)
                    self.assertIn(lateral_flag, packet.input_data)
                    self.assertNotIn(flags.UP, packet.input_data)
                    self.assertEqual((packet.move.x, packet.move.y), (sign, -1.0))
                    self.assertEqual((packet.raw_move_vector.x, packet.raw_move_vector.y), (sign, -1.0))
                    self.assertEqual((packet.analog_move_vector.x, packet.analog_move_vector.y), (sign, -1.0))
                    self.assertAlmostEqual(packet.pos_delta.x, sign * 0.09 / 2**0.5)
                    self.assertAlmostEqual(packet.pos_delta.z, -0.09 / 2**0.5)
                self.assertTrue(all(not p.input_data for p in sink.sent[28:]))
                self.assertEqual([p.client_tick for p in sink.sent], list(range(1, 31)))
                self.assertEqual(movement.position, (0.5, 101.62, 0.5))
                movement.close()

    async def test_jump_backward_keeps_backward_input_through_jump_and_releases(self):
        protocol = Protocol(2193)
        b = protocol.binding
        flags = b.PlayerAuthInputPacket.InputData
        sink = PacketSink(protocol)
        movement = Movement(sink, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)

        self.assertEqual(await movement.jump_backward(4), 4)
        self.assertEqual([p.client_tick for p in sink.sent], list(range(1, 7)))
        self.assertTrue(all(flags.DOWN in p.input_data for p in sink.sent[:4]))
        self.assertTrue(all(p.move.y == -1.0 for p in sink.sent[:4]))
        self.assertTrue(all(p.pos_delta.z < 0 for p in sink.sent[:4]))
        self.assertIn(flags.START_JUMPING, sink.sent[0].input_data)
        self.assertIn(flags.JUMPING, sink.sent[0].input_data)
        self.assertIn(flags.JUMP_RELEASED_RAW, sink.sent[1].input_data)
        self.assertTrue(all(flags.JUMPING not in p.input_data for p in sink.sent[1:]))
        self.assertTrue(all(flags.DOWN not in p.input_data for p in sink.sent[4:]))
        self.assertEqual([p.move.y for p in sink.sent[4:]], [0.0, 0.0])
        self.assertGreater(max(p.pos.y for p in sink.sent), 102.0)
        self.assertEqual(movement.position, (0.5, 101.62, 0.5))
        movement.close()

    async def test_lateral_jump_combines_strafe_and_jump_then_releases_both(self):
        protocol = Protocol(2193)
        flags = protocol.binding.PlayerAuthInputPacket.InputData

        for direction, sign, lateral_flag in (
            ("left", 1, flags.LEFT), ("right", -1, flags.RIGHT),
        ):
            with self.subTest(direction=direction):
                sink = PacketSink(protocol)
                movement = Movement(sink, 1)
                movement.position = movement._input_position = (0.5, 101.62, 0.5)

                self.assertEqual(await getattr(movement, f"jump_{direction}")(4), 4)
                self.assertEqual([p.client_tick for p in sink.sent], list(range(1, 7)))
                for packet in sink.sent[:4]:
                    self.assertIn(lateral_flag, packet.input_data)
                    self.assertEqual(packet.move.x, sign)
                    self.assertEqual(packet.move.y, 0.0)
                    self.assertAlmostEqual(packet.pos_delta.x, sign * 0.09, places=5)
                    self.assertAlmostEqual(packet.pos_delta.z, 0.0, places=5)
                    self.assertEqual(packet.raw_move_vector.x, sign)
                    self.assertEqual(packet.analog_move_vector.x, sign)
                self.assertIn(flags.START_JUMPING, sink.sent[0].input_data)
                self.assertIn(flags.JUMPING, sink.sent[0].input_data)
                self.assertIn(flags.JUMP_RELEASED_RAW, sink.sent[1].input_data)
                self.assertTrue(all(flags.JUMPING not in p.input_data
                                    for p in sink.sent[1:]))
                self.assertTrue(all(lateral_flag not in p.input_data
                                    for p in sink.sent[4:]))
                self.assertEqual([p.move.x for p in sink.sent[4:]], [0.0, 0.0])
                self.assertGreater(max(p.pos.y for p in sink.sent), 102.0)
                self.assertEqual(movement.position, (0.5, 101.62, 0.5))
                movement.close()

    async def test_forward_diagonal_jump_combines_input_predicts_and_releases(self):
        protocol = Protocol(2193)
        flags = protocol.binding.PlayerAuthInputPacket.InputData
        for direction, sign, lateral_flag in (
            ("left", 1, flags.LEFT), ("right", -1, flags.RIGHT),
        ):
            with self.subTest(direction=direction):
                sink = PacketSink(protocol)
                movement = Movement(sink, 1)
                movement.position = movement._input_position = (0.5, 101.62, 0.5)

                self.assertEqual(await getattr(movement, f"jump_forward_{direction}")(4), 4)
                self.assertEqual([p.client_tick for p in sink.sent], list(range(1, 7)))
                for packet in sink.sent[:4]:
                    self.assertIn(flags.UP, packet.input_data)
                    self.assertIn(lateral_flag, packet.input_data)
                    self.assertEqual((packet.move.x, packet.move.y), (sign, 1.0))
                    self.assertEqual((packet.raw_move_vector.x, packet.raw_move_vector.y), (sign, 1.0))
                    self.assertEqual((packet.analog_move_vector.x, packet.analog_move_vector.y), (sign, 1.0))
                    self.assertAlmostEqual(packet.pos_delta.x, sign * 0.09 / 2**0.5)
                    self.assertAlmostEqual(packet.pos_delta.z, 0.09 / 2**0.5)
                self.assertIn(flags.JUMPING, sink.sent[0].input_data)
                self.assertIn(flags.START_JUMPING, sink.sent[0].input_data)
                self.assertAlmostEqual(sink.sent[0].pos_delta.y, 0.42)
                self.assertIn(flags.JUMP_RELEASED_RAW, sink.sent[1].input_data)
                self.assertTrue(all(flags.JUMPING not in p.input_data for p in sink.sent[1:]))
                self.assertTrue(all(not p.input_data for p in sink.sent[4:]))
                self.assertTrue(all(p.move.x == 0.0 and p.move.y == 0.0
                                    for p in sink.sent[4:]))
                self.assertGreater(max(p.pos.y for p in sink.sent), 102.0)
                self.assertEqual(movement.position, (0.5, 101.62, 0.5))
                movement.close()

    async def test_backward_diagonal_jump_combines_input_predicts_and_releases(self):
        protocol = Protocol(2193)
        flags = protocol.binding.PlayerAuthInputPacket.InputData
        for direction, sign, lateral_flag in (
            ("left", 1, flags.LEFT), ("right", -1, flags.RIGHT),
        ):
            with self.subTest(direction=direction):
                sink = PacketSink(protocol)
                movement = Movement(sink, 1)
                movement.position = movement._input_position = (0.5, 101.62, 0.5)

                self.assertEqual(await getattr(movement, f"jump_backward_{direction}")(4), 4)
                self.assertEqual([p.client_tick for p in sink.sent], list(range(1, 7)))
                for packet in sink.sent[:4]:
                    self.assertIn(flags.DOWN, packet.input_data)
                    self.assertIn(lateral_flag, packet.input_data)
                    self.assertEqual((packet.move.x, packet.move.y), (sign, -1.0))
                    self.assertEqual((packet.raw_move_vector.x, packet.raw_move_vector.y), (sign, -1.0))
                    self.assertEqual((packet.analog_move_vector.x, packet.analog_move_vector.y), (sign, -1.0))
                    self.assertAlmostEqual(packet.pos_delta.x, sign * 0.09 / 2**0.5)
                    self.assertAlmostEqual(packet.pos_delta.z, -0.09 / 2**0.5)
                self.assertIn(flags.JUMPING, sink.sent[0].input_data)
                self.assertIn(flags.START_JUMPING, sink.sent[0].input_data)
                self.assertAlmostEqual(sink.sent[0].pos_delta.y, 0.42)
                self.assertIn(flags.JUMP_RELEASED_RAW, sink.sent[1].input_data)
                self.assertTrue(all(flags.JUMPING not in p.input_data for p in sink.sent[1:]))
                self.assertTrue(all(not p.input_data for p in sink.sent[4:]))
                self.assertTrue(all(p.move.x == 0.0 and p.move.y == 0.0
                                    for p in sink.sent[4:]))
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


    async def test_public_walk_task_cancellation_releases_input_and_reuses_bot(self):
        protocol = Protocol(2193)
        sink = PacketSink(protocol)
        movement = Movement(sink, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        movement.start()
        ticker = movement._ticker
        bot = Bot("CancelWalker")
        bot._movement = movement
        up = protocol.binding.PlayerAuthInputPacket.InputData.UP
        moving = asyncio.Event()

        def on_send():
            if up in sink.sent[-1].input_data:
                moving.set()

        sink.on_send = on_send
        try:
            task = asyncio.create_task(bot.walk_forward(160))
            await asyncio.wait_for(moving.wait(), 2)
            await asyncio.sleep(0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            self.assertIsNone(movement._active_done)
            self.assertEqual([list(p.input_data) for p in sink.sent[-2:]], [[], []])
            moving_frames = sum(up in p.input_data for p in sink.sent)
            self.assertGreaterEqual(moving_frames, 1)
            await asyncio.sleep(0.15)
            self.assertEqual(sum(up in p.input_data for p in sink.sent), moving_frames)
            self.assertFalse(ticker.done())

            self.assertEqual(await bot.walk_forward(2), 2)
            self.assertIsNone(movement._active_done)
            self.assertEqual([list(p.input_data) for p in sink.sent[-2:]], [[], []])
        finally:
            await bot.close()
        self.assertTrue(ticker.done())
        sent = len(sink.sent)
        await asyncio.sleep(0.1)
        self.assertEqual(len(sink.sent), sent)

    async def test_public_follow_task_cancellation_releases_input_and_reuses_bot(self):
        from endpilot.entities import Entities, EntityState

        protocol = Protocol(2193)
        sink = PacketSink(protocol)
        movement = Movement(sink, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        movement.start()
        ticker = movement._ticker
        entities = Entities(sink)
        entities._record(EntityState(4, 7, "minecraft:cow", (-6.0, 100.0, 0.5)))
        bot = Bot("CancelFollower")
        bot._movement = movement
        bot._entities = entities
        up = protocol.binding.PlayerAuthInputPacket.InputData.UP
        moving = asyncio.Event()

        def on_send():
            if up in sink.sent[-1].input_data:
                moving.set()

        sink.on_send = on_send
        try:
            follow = asyncio.create_task(bot.follow_entity(7, ticks=160))
            await asyncio.wait_for(moving.wait(), 2)
            follow.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(follow, 2)
            self.assertIsNone(movement._active_done)
            self.assertIsNone(movement._follow_target_id)
            self.assertEqual([list(p.input_data) for p in sink.sent[-2:]], [[], []])
            moving_frames = sum(up in p.input_data for p in sink.sent)
            await asyncio.sleep(0.15)
            self.assertEqual(sum(up in p.input_data for p in sink.sent), moving_frames)
            self.assertEqual(await bot.walk_forward(2), 2)
            self.assertIsNone(movement._active_done)
            self.assertEqual([list(p.input_data) for p in sink.sent[-2:]], [[], []])
        finally:
            await bot.close()
        self.assertTrue(ticker.done())
        sent = len(sink.sent)
        await asyncio.sleep(0.1)
        self.assertEqual(len(sink.sent), sent)

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


    async def test_server_kick_interrupts_active_follow_and_drains_ticker(self):
        from endpilot.entities import Entities, EntityState
        from endpilot.protocol import RawPacket

        protocol = Protocol(2193)
        sink = PacketSink(protocol)
        movement = Movement(sink, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        movement.start()
        ticker = movement._ticker
        entities = Entities(sink)
        entities._record(EntityState(4, 7, "minecraft:cow", (-6.0, 100.0, 0.5)))
        moving = asyncio.Event()
        up = protocol.binding.PlayerAuthInputPacket.InputData.UP

        def on_send():
            if up in sink.sent[-1].input_data:
                moving.set()

        sink.on_send = on_send
        follow = asyncio.create_task(movement.follow_entity(entities, 7, ticks=160))
        await asyncio.wait_for(moving.wait(), 2)
        sink.observer(RawPacket(protocol.binding.DisconnectPacket.ID, b""))
        with self.assertRaisesRegex(ConnectionError, "server disconnected"):
            await asyncio.wait_for(follow, 2)
        self.assertIsNone(movement._active_done)
        self.assertIsNone(movement._follow_target_id)
        await movement.aclose()
        self.assertTrue(ticker.done())
        sent = len(sink.sent)
        await asyncio.sleep(0.1)
        self.assertEqual(len(sink.sent), sent)
        with self.assertRaisesRegex(ConnectionError, "movement is closed"):
            await movement.follow_entity(entities, 7, ticks=1)


class BotShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_close_drains_walk_and_rejects_queued_input(self):
        bot = Bot("ClosingPlayer")
        sink = PacketSink(bot.protocol)
        movement = Movement(sink, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        movement.start()
        ticker = movement._ticker
        bot._movement = movement

        class Session:
            closed = False

            async def close(self):
                self.closed = True

        session = Session()
        bot._session = session
        moving = asyncio.Event()
        up = sink.protocol.binding.PlayerAuthInputPacket.InputData.UP

        def on_send():
            if up in sink.sent[-1].input_data:
                moving.set()

        sink.on_send = on_send
        walk = asyncio.create_task(bot.walk_forward(200))
        await asyncio.wait_for(moving.wait(), 2)
        # This call is queued behind the active walk, before the Bot shuts down.
        queued_jump = asyncio.create_task(bot.jump())
        await asyncio.sleep(0)
        await asyncio.wait_for(bot.close(), 2)

        frames = await asyncio.wait_for(walk, 2)
        self.assertGreater(frames, 0)
        self.assertLess(frames, 200)
        with self.assertRaisesRegex(ConnectionError, "movement is closed"):
            await queued_jump
        self.assertTrue(session.closed)
        self.assertTrue(ticker.done())
        sent_at_close = len(sink.sent)
        await asyncio.sleep(0.1)
        self.assertEqual(len(sink.sent), sent_at_close)
        with self.assertRaises(RuntimeError):
            await bot.walk_forward(1)

    async def test_bot_close_drains_active_follow_and_queued_input(self):
        bot = Bot("ClosingFollower")
        sink = PacketSink(bot.protocol)
        movement = Movement(sink, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        movement.start()
        ticker = movement._ticker
        bot._movement = movement

        class Actor:
            position = (-10.0, 101.62, 0.5)

        class EntityView(dict):
            def close(self):
                pass

        bot._entities = EntityView({7: Actor()})

        class Session:
            closed = False

            async def close(self):
                self.closed = True

        session = Session()
        bot._session = session
        moving = asyncio.Event()
        up = sink.protocol.binding.PlayerAuthInputPacket.InputData.UP

        def on_send():
            if up in sink.sent[-1].input_data:
                moving.set()

        sink.on_send = on_send
        follow = asyncio.create_task(bot.follow_entity(7, ticks=200))
        await asyncio.wait_for(moving.wait(), 2)
        queued_jump = asyncio.create_task(bot.jump())
        await asyncio.sleep(0)
        await asyncio.wait_for(bot.close(), 2)

        frames = await asyncio.wait_for(follow, 2)
        self.assertGreater(frames, 0)
        self.assertLess(frames, 200)
        with self.assertRaisesRegex(ConnectionError, "movement is closed"):
            await queued_jump
        self.assertTrue(session.closed)
        self.assertTrue(ticker.done())
        self.assertIsNone(movement._active_done)
        self.assertIsNone(movement._follow_target_id)
        sent_at_close = len(sink.sent)
        await asyncio.sleep(0.1)
        self.assertEqual(len(sink.sent), sent_at_close)
        with self.assertRaises(RuntimeError):
            await bot.follow_entity(7, ticks=1)

    async def test_close_unblocks_walk_waiting_for_first_position(self):
        sink = PacketSink(Protocol(2193))
        movement = Movement(sink, 1)
        movement.start()
        ticker = movement._ticker
        walking = asyncio.create_task(movement.walk_forward(24))
        await asyncio.sleep(0)
        await asyncio.wait_for(movement.aclose(), 2)
        with self.assertRaisesRegex(ConnectionError, "movement is closed"):
            await walking
        self.assertEqual(sink.sent, [])
        self.assertTrue(ticker.done())
