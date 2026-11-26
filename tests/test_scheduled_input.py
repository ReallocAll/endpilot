import asyncio
from contextlib import ExitStack
import math
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from endpilot import Bot, ForwardInput
from endpilot.blocks import BlockUpdate, HASHED_AIR_RUNTIME_ID
from endpilot.compression import PacketConnection
from endpilot.login import JoinSession
from endpilot.protocol import read_batch, write_batch


class Wire:
    def __init__(self, protocol):
        self.protocol = protocol
        self.incoming = asyncio.Queue()
        self.sent = []
        self.before_send = None
        self.closed = False

    async def send(self, batch):
        if self.before_send is not None:
            await self.before_send()
        self.sent.extend(self.protocol.decode(raw) for raw in read_batch(batch))

    async def recv(self):
        data = await self.incoming.get()
        if isinstance(data, BaseException):
            raise data
        return data

    async def close(self):
        self.closed = True

    def push(self, packet):
        self.incoming.put_nowait(write_batch([self.protocol.encode(packet)]))


class ManualClock:
    def __init__(self):
        self.now_ns = 0
        self.sleepers = asyncio.Queue()

    def patch_scheduler(self):
        scope = ExitStack()
        scope.enter_context(patch("endpilot.client_clock.perf_counter_ns", lambda: self.now_ns))
        scope.enter_context(patch("endpilot.movement.perf_counter_ns", lambda: self.now_ns))
        scope.enter_context(patch("endpilot.client_clock.sleep", self.sleep))
        return scope

    async def sleep(self, seconds):
        gate = asyncio.get_running_loop().create_future()
        self.sleepers.put_nowait((self.now_ns + round(seconds * 1_000_000_000), gate))
        await gate

    async def wake_at(self, milliseconds, before_wake=None):
        deadline, gate = await asyncio.wait_for(self.sleepers.get(), 1)
        now = milliseconds * 1_000_000
        assert now >= deadline and now >= self.now_ns, (now, deadline, self.now_ns)
        self.now_ns = now
        if before_wake is not None:
            before_wake()
        gate.set_result(None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)


class ScheduledInputTests(unittest.IsolatedAsyncioTestCase):
    async def join(self, bot, *, position=None, on_ground=True):
        wire = Wire(bot.protocol)
        settings = SimpleNamespace(compression_algorithm=65535, compression_threshold=0)
        session = JoinSession(PacketConnection(wire, bot.protocol, settings), bot.identity, "loopback")
        session.player_id = session.player_unique_id = 1
        if position is not None:
            self.teleport(wire, position, on_ground=on_ground)
        with patch.object(JoinSession, "connect_nethernet", AsyncMock(return_value=session)):
            await bot.join_nethernet("http://127.0.0.1:19192")
        return wire

    def teleport(self, wire, pos, *, on_ground=True):
        b = wire.protocol.binding
        wire.push(b.MovePlayerPacket(player_id=1, pos=b.Vec3(x=pos[0], y=pos[1], z=pos[2]),
                                     rot=b.Vec2(x=0.0, y=0.0), reset_position=2, y_head_rot=0.0,
                                     on_ground=on_ground, riding_id=0, tick=0, teleport_data=None))

    async def observe(self, bot, wire, packet):
        waiting = asyncio.create_task(bot.packets.wait_for_packet(type(packet).ID, timeout=1))
        await asyncio.sleep(0)
        wire.push(packet)
        await waiting

    async def test_public_rotation_uses_bedrock_angles_on_the_next_scheduled_input(self):
        clock = ManualClock()
        bot = Bot("LookAngles")
        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.62, 0.5))
            try:
                await bot.wait_for_position_sync()
                self.assertIsNone(await bot.set_rotation(90, -37))
                self.assertEqual(bot.rotation, (90, -37))
                self.assertEqual(wire.sent, [])
                await clock.wake_at(50)
                frame = wire.sent[-1]
                self.assertEqual((frame.rot.x, frame.rot.y, frame.y_head_rot), (90, -37, -37))
                self.assertEqual((frame.interact_rotation.x, frame.interact_rotation.y), (90, -37))
                with self.assertRaises(ValueError):
                    await bot.set_rotation(math.inf, 0)
                self.assertEqual(bot.rotation, (90, -37))
            finally:
                await bot.close()

    async def test_teleport_rotation_reaches_next_input_only_for_the_local_player(self):
        clock = ManualClock()
        bot = Bot("TeleportAngles")
        b = bot.protocol.binding
        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.62, 0.5))
            try:
                await bot.wait_for_position_sync()
                await bot.set_rotation(12, 35)
                packet = b.MovePlayerPacket(
                    player_id=2, pos=b.Vec3(x=8.5, y=101.62, z=9.5),
                    rot=b.Vec2(x=-25, y=110), y_head_rot=0, reset_position=2,
                )
                await self.observe(bot, wire, packet)
                await clock.wake_at(50)
                frame = wire.sent[-1]
                self.assertEqual((frame.rot.x, frame.rot.y, frame.y_head_rot), (12, 35, 35))
                self.assertEqual((frame.pos.x, frame.pos.z), (0.5, 0.5))

                await bot.move_to((6.5, 100.0, 0.5))
                packet.player_id = 1
                await self.observe(bot, wire, packet)
                self.assertEqual(bot.rotation, (-25, 110))
                await clock.wake_at(100)
                frame = wire.sent[-1]
                self.assertEqual((frame.rot.x, frame.rot.y, frame.y_head_rot), (-25, 110, 110))
                self.assertEqual((frame.interact_rotation.x, frame.interact_rotation.y), (-25, 110))
                self.assertEqual((frame.pos.x, frame.pos.z), (8.5, 9.5))
                self.assertIn(b.PlayerAuthInputPacket.InputData.HANDLED_TELEPORT, frame.input_data)
                self.assertFalse(bot.movement_state.moving)
            finally:
                await bot.close()

    async def test_sprinting_manual_directions_survive_single_frame_actions(self):
        clock = ManualClock()
        bot = Bot("CombinedInput")
        b = bot.protocol.binding
        flags = b.PlayerAuthInputPacket.InputData
        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.62, 0.5))
            try:
                await self.observe(bot, wire, b.InventorySlotPacket(
                    inventory_id=b.ContainerID.INVENTORY, slot=0,
                    item=b.SerializedNetworkItemStackDescriptor(id=1, stack_size=4),
                ))
                await bot.set_move_vector(0, 1)
                start_tick = await bot.set_sprinting(True)
                self.assertIn(flags.START_SPRINTING, wire.sent[-1].input_data)
                jump_tick = await bot.jump()
                swing_tick = await bot.missed_swing()
                await bot.use_item()
                self.assertIsInstance(wire.sent[-1], b.InventoryTransactionPacket)
                self.assertTrue(bot.movement_state.sprinting)
                position = (0, 99, 1)
                place_tick = await bot.place_against(BlockUpdate(position, 77, 0))

                def report_broken(packet_id):
                    if packet_id != b.PlayerAuthInputPacket.ID:
                        return
                    actions = wire.sent[-1].player_block_actions or ()
                    if any(a.player_action_type == b.PlayerActionType.START_DESTROY_BLOCK for a in actions):
                        wire.push(b.UpdateBlockPacket(
                            pos=b.BlockPos(x=0, y=99, z=1), runtime_id=HASHED_AIR_RUNTIME_ID,
                            layer=0, update_flags=0,
                        ))

                unsubscribe = bot.packets.subscribe_sent(report_broken)
                try:
                    mined = await bot.mine_block(position, timeout=1)
                    self.assertEqual(mined.position, position)
                finally:
                    unsubscribe()
                frames = {p.client_tick: p for p in wire.sent if isinstance(p, b.PlayerAuthInputPacket)}
                self.assertIn(flags.JUMP_PRESSED_RAW, frames[jump_tick].input_data)
                self.assertIn(flags.MISSED_SWING, frames[swing_tick].input_data)
                self.assertIn(flags.PERFORM_ITEM_INTERACTION, frames[place_tick].input_data)
                self.assertEqual(len([p for p in frames.values() if flags.PERFORM_BLOCK_ACTIONS in p.input_data]), 2)
                self.assertEqual(len(frames), 6)
                for tick, packet in frames.items():
                    self.assertTrue({flags.UP, flags.SPRINTING, flags.SPRINT_DOWN} <= set(packet.input_data))
                    self.assertNotIn(flags.STOP_SPRINTING, packet.input_data)
                    if tick != start_tick:
                        self.assertNotIn(flags.START_SPRINTING, packet.input_data)
            finally:
                await bot.close()

    async def test_received_stale_corrections_count_without_replacing_current_motion(self):
        clock = ManualClock()
        bot = Bot("CorrectionOrder")
        b = bot.protocol.binding
        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.62, 0.5))
            try:
                await clock.wake_at(50)
                await clock.wake_at(100)
                await self.observe(bot, wire, b.CorrectPlayerMovePredictionPacket(
                    prediction_type=0, tick=2, on_ground=True, pos=b.Vec3(x=2, y=101.62, z=3),
                    pos_delta=b.Vec3(x=0.125, y=0, z=0),
                ))
                corrected = bot.movement_state
                self.assertEqual(corrected.correction_count, 1)
                self.assertTrue(corrected.on_ground)
                stale = b.CorrectPlayerMovePredictionPacket(
                    prediction_type=0, tick=1, on_ground=False, pos=b.Vec3(x=9, y=50, z=8),
                    pos_delta=b.Vec3(x=0, y=-0.5, z=0),
                )
                await self.observe(bot, wire, stale)
                current = bot.movement_state
                self.assertEqual(current.correction_count, 2)
                self.assertEqual(current.feet_position, corrected.feet_position)
                self.assertEqual(current.velocity, corrected.velocity)
                self.assertTrue(current.on_ground)
                self.assertEqual((bot.position[0], bot.position[2]), (2, 3))
                self.teleport(wire, (4, 101.62, 5))
                await self.observe(bot, wire, stale)
                self.assertEqual(bot.movement_state.correction_count, 3)
                self.assertEqual((bot.position[0], bot.position[2]), (4, 5))
                self.assertEqual(bot.movement_state.velocity, (0.0, 0.0, 0.0))
            finally:
                await bot.close()

    async def test_pre_spawn_movement_speed_reaches_the_first_ordinary_input(self):
        clock = ManualClock()
        bot = Bot("EarlySpeed")
        wire = Wire(bot.protocol)
        b = bot.protocol.binding

        def attributes(runtime_id, speed):
            return b.UpdateAttributesPacket(runtime_id=runtime_id, attribute_data=[
                b.AttributeData(name="minecraft:movement", current_value=speed),
            ])

        wire.push(b.NetworkSettingsPacket(
            compression_algorithm=b.PacketCompressionAlgorithm.NONE, compression_threshold=0,
        ))
        wire.push(attributes(1, 0.24))
        wire.push(b.StartGamePacket(runtime_id=1, entity_id=1, block_network_ids_are_hashes=True))
        wire.push(b.UpdateAttributesPacket(runtime_id=1, attribute_data=[
            b.AttributeData(name="minecraft:movement", current_value=0.24),
            b.AttributeData(name="minecraft:health", current_value=14),
        ]))
        wire.push(attributes(2, 0.75))
        wire.push(b.PlayStatusPacket(status=3))
        self.teleport(wire, (0.5, 101.62, 0.5))
        with clock.patch_scheduler(), patch("endpilot.transport.nethernet.connect", return_value=wire):
            await bot.join_nethernet("http://127.0.0.1:19192")
            try:
                self.assertEqual(bot.health, 14.0)
                await bot.set_move_vector(0, 1)
                await clock.wake_at(50)
                frame, = [p for p in wire.sent if isinstance(p, b.PlayerAuthInputPacket)]
                self.assertAlmostEqual(frame.pos_delta.z, 0.24, places=5)
            finally:
                await bot.close()

    async def test_pose_setters_return_the_submitted_tick(self):
        clock = ManualClock()
        bot = Bot("PoseResult")
        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.62, 0.5))
            try:
                self.assertEqual(await bot.set_sneaking(True), wire.sent[-1].client_tick)
                self.assertEqual(await bot.set_sneaking(False), wire.sent[-1].client_tick)
                self.assertEqual(await bot.set_sprinting(True), wire.sent[-1].client_tick)
                self.assertEqual(await bot.set_sprinting(False), wire.sent[-1].client_tick)
                self.assertEqual([p.client_tick for p in wire.sent], [1, 2, 3, 4])
            finally:
                await bot.close()

    async def test_jump_returns_the_start_tick(self):
        clock = ManualClock()
        bot = Bot("JumpResult")
        flags = bot.protocol.binding.PlayerAuthInputPacket.InputData
        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.62, 0.5))
            try:
                self.assertEqual(await bot.jump(), 1)
                frame, = wire.sent
                self.assertEqual(frame.client_tick, 1)
                self.assertIn(flags.START_JUMPING, frame.input_data)
                self.assertAlmostEqual(frame.pos_delta.y, 0.42)
            finally:
                await bot.close()

    async def test_jump_tap_after_flight_has_its_own_press_and_release(self):
        clock = ManualClock()
        bot = Bot("Flight")
        self.addAsyncCleanup(bot.close)
        flags = bot.protocol.binding.PlayerAuthInputPacket.InputData

        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.62, 0.5))
            await bot.wait_for_position_sync()
            switch = asyncio.create_task(bot._movement.set_flying(True, bot._player_state, require_permission=False))
            await asyncio.sleep(0)
            await clock.wake_at(50)
            await clock.wake_at(100)
            self.assertIsNone(await asyncio.wait_for(switch, 1))
            self.assertIn(flags.START_FLYING, wire.sent[-1].input_data)
            before = len(wire.sent)
            self.assertIsNone(bot._movement._queue_qa_jump_presses([True]))
            self.assertEqual(len(wire.sent), before)

            for slot in range(150, 400, 50):
                await clock.wake_at(slot)
            release, press, final_release, *idle = wire.sent[before:]
            self.assertIn(flags.JUMP_RELEASED_RAW, release.input_data)
            self.assertIn(flags.JUMP_PRESSED_RAW, press.input_data)
            self.assertIn(flags.JUMP_RELEASED_RAW, final_release.input_data)
            self.assertEqual([packet.client_tick for packet in (release, press, final_release)],
                             list(range(release.client_tick, release.client_tick + 3)))
            for packet in wire.sent[before:]:
                self.assertNotIn(flags.START_FLYING, packet.input_data)
                self.assertNotIn(flags.STOP_FLYING, packet.input_data)
            for packet in idle:
                self.assertNotIn(flags.JUMP_DOWN, packet.input_data)
                self.assertNotIn(flags.JUMP_PRESSED_RAW, packet.input_data)
                self.assertNotIn(flags.JUMP_RELEASED_RAW, packet.input_data)

    async def test_jump_levels_use_one_frame_each_and_close_discards_pending_input(self):
        clock = ManualClock()
        bot = Bot("Flight")
        self.addAsyncCleanup(bot.close)
        flags = bot.protocol.binding.PlayerAuthInputPacket.InputData

        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.62, 0.5))
            await bot.wait_for_position_sync()
            before = len(wire.sent)
            presses = [True, True, False]
            self.assertIsNone(bot._movement._queue_qa_jump_presses(presses))
            presses.clear()
            self.assertEqual(len(wire.sent), before)
            await clock.wake_at(50)
            await clock.wake_at(100)
            await clock.wake_at(150)
            first, held, release = wire.sent[before:]
            self.assertIn(flags.JUMP_PRESSED_RAW, first.input_data)
            self.assertIn(flags.JUMP_CURRENT_RAW, held.input_data)
            self.assertNotIn(flags.JUMP_PRESSED_RAW, held.input_data)
            self.assertIn(flags.JUMP_RELEASED_RAW, release.input_data)

            self.assertIsNone(bot._movement._queue_qa_jump_presses([True]))
            movement = bot._movement
            await bot.close()
            self.assertTrue(wire.closed)
            self.assertEqual(len(wire.sent), before + 3)
            self.assertFalse(movement._qa_jump_presses)

    async def test_jump_taps_reject_closed_and_failed_readers(self):
        for read_failure in (None, OSError("jump reader failed")):
            with self.subTest(read_failure=read_failure):
                clock = ManualClock()
                bot = Bot("Flight")

                with clock.patch_scheduler():
                    wire = await self.join(bot, position=(0.5, 101.62, 0.5))
                    try:
                        await bot.wait_for_position_sync()
                        movement = bot._movement
                        reader = bot.packets._reader_task
                        before = len(wire.sent)
                        self.assertIsNone(bot._movement._queue_qa_jump_presses([True]))
                        if read_failure is None:
                            reader.cancel()
                        else:
                            wire.incoming.put_nowait(read_failure)
                        await asyncio.wait((reader,))
                        end = await bot.wait_for_session_end(timeout=1)
                        self.assertEqual(end.kind, "reader_end")
                        self.assertEqual(end.error_type, None if read_failure is None else "OSError")

                        with self.assertRaises(OSError) as rejected:
                            bot._movement._queue_qa_jump_presses([True])
                        if read_failure is None:
                            self.assertIsInstance(rejected.exception, ConnectionError)
                        else:
                            self.assertIs(rejected.exception, read_failure)
                        self.assertFalse(movement._qa_jump_presses)
                        self.assertEqual(len(wire.sent), before)
                    finally:
                        try:
                            await bot.close()
                        except OSError:
                            if read_failure is None:
                                raise
                self.assertTrue(wire.closed)

    async def test_reader_cancellation_on_first_tick_drains_after_dimension_ack(self):
        clock = ManualClock()
        bot = Bot("DimensionClose")
        b = bot.protocol.binding
        self.addAsyncCleanup(bot.close)

        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.62, 0.5))
            await bot.wait_for_position_sync()
            await self.observe(bot, wire, b.ChangeDimensionPacket(
                dimension_id=1, pos=b.Vec3(x=40, y=101.62, z=0), loading_screen_id=0,
            ))
            self.assertTrue(bot.is_changing_dimension)
            await self.observe(bot, wire, b.PlayerActionPacket(
                runtime_id=1, action=b.PlayerActionType.CHANGE_DIMENSION_ACK,
            ))
            self.assertFalse(bot.is_changing_dimension)
            self.assertEqual(wire.sent[-1].action, b.PlayerActionType.CHANGE_DIMENSION_ACK)
            before = list(wire.sent)

            packets = bot.packets
            reader = packets.start_receiving()
            loop = asyncio.get_running_loop()
            # Run the clock before the reader finishes, then the ticker before the reader callbacks.
            await clock.wake_at(50, before_wake=lambda: loop.call_soon(reader.cancel))
            self.assertTrue(reader.cancelled())
            with self.assertRaisesRegex(ConnectionError, "packet stream closed"):
                await packets.send(b.SetTimePacket())
            await bot.close()

        self.assertTrue(wire.closed)
        self.assertEqual(wire.sent, before)
        end = await bot.wait_for_session_end(timeout=0)
        self.assertEqual(end.kind, "reader_end")
        self.assertIsNone(end.error_type)
        self.assertIs(bot.session_end, end)

    async def test_jump_taps_reject_a_stopped_clock_and_clear_pending_input(self):
        clock = ManualClock()
        bot = Bot("Flight")

        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.62, 0.5))
            try:
                await bot.wait_for_position_sync()
                movement = bot._movement
                before = len(wire.sent)
                self.assertIsNone(bot._movement._queue_qa_jump_presses([True]))
                bot._clock.stop()
                with self.assertRaisesRegex(ConnectionError, "client clock closed"):
                    await bot.wait_for_input_end()
                with self.assertRaisesRegex(ConnectionError, "client clock closed"):
                    bot._movement._queue_qa_jump_presses([True])
                self.assertFalse(movement._qa_jump_presses)
                self.assertEqual(len(wire.sent), before)
            finally:
                with self.assertRaisesRegex(ConnectionError, "client clock closed"):
                    await bot.close()
        self.assertTrue(wire.closed)

    async def test_jump_taps_reject_after_send_failure_with_original_cause(self):
        clock = ManualClock()
        bot = Bot("Flight")
        failure = OSError("jump send failed")

        async def fail():
            raise failure

        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.62, 0.5))
            try:
                await bot.wait_for_position_sync()
                before = len(wire.sent)
                movement = bot._movement
                self.assertIsNone(bot._movement._queue_qa_jump_presses([True, False]))
                wire.before_send = fail
                await clock.wake_at(50)
                with self.assertRaises(OSError) as stopped:
                    await bot.wait_for_input_end()
                self.assertIs(stopped.exception, failure)
                self.assertIsNone(bot.session_end)
                with self.assertRaises(OSError) as rejected:
                    bot._movement._queue_qa_jump_presses([True])
                self.assertIs(rejected.exception, failure)
                self.assertFalse(movement._qa_jump_presses)
                self.assertEqual(len(wire.sent), before)
            finally:
                with self.assertRaises(OSError) as close_error:
                    await bot.close()
                self.assertIs(close_error.exception, failure)
        self.assertTrue(wire.closed)

    async def test_idle_rebases_only_when_grounding_matches_the_active_jump(self):
        clock = ManualClock()
        bot = Bot("JumpGrounding")
        b = bot.protocol.binding
        flags = b.PlayerAuthInputPacket.InputData
        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.62, 0.5))
            try:
                await clock.wake_at(50)
                jump_tick = await bot.jump()
                await self.observe(bot, wire, b.CorrectPlayerMovePredictionPacket(
                    pos=b.Vec3(x=0.5, y=101.62, z=0.5), on_ground=True, tick=jump_tick - 1,
                ))
                await clock.wake_at(100)
                flying = wire.sent[-1]
                self.assertGreater(flying.pos.y, 101.9)
                await self.observe(bot, wire, b.CorrectPlayerMovePredictionPacket(
                    pos=b.Vec3(x=1.5, y=102.62, z=0.5), on_ground=True, tick=flying.client_tick,
                ))
                await clock.wake_at(150)
                await clock.wake_at(200)
                for frame in wire.sent[-2:]:
                    self.assertAlmostEqual(frame.pos.y, 102.62, places=4)
                    self.assertEqual(frame.pos_delta.y, 0)
                self.assertEqual(sum(flags.START_JUMPING in frame.input_data for frame in wire.sent), 1)
            finally:
                await bot.close()

    async def test_held_directions_and_sprint_key_share_the_scheduled_frames(self):
        clock = ManualClock()
        bot = Bot("ManualPlayer")
        flags = bot.protocol.binding.PlayerAuthInputPacket.InputData
        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.62, 0.5))
            try:
                await bot.set_move_vector(0.5, 1.0)
                self.assertEqual(wire.sent, [])
                await clock.wake_at(50)
                self.assertEqual((wire.sent[-1].move.x, wire.sent[-1].move.y), (0.5, 1.0))
                self.assertTrue({flags.LEFT, flags.UP} <= set(wire.sent[-1].input_data))
                self.assertAlmostEqual(bot.movement_state.feet_position[1], 100, places=4)
                self.assertTrue(bot.movement_state.on_ground)
                await bot.set_sprinting(True)
                held = set(wire.sent[-1].input_data)
                self.assertTrue({flags.LEFT, flags.UP, flags.SPRINT_DOWN, flags.START_SPRINTING} <= held)
                self.assertTrue(bot.movement_state.sprint_key_down)
                self.assertTrue(bot.movement_state.sprinting)
                await bot.stop_moving()
                await clock.wake_at(100)
                stopped = set(wire.sent[-1].input_data)
                self.assertIn(flags.STOP_SPRINTING, stopped)
                self.assertIn(flags.SPRINT_DOWN, stopped)
                self.assertTrue({flags.LEFT, flags.UP, flags.SPRINTING}.isdisjoint(stopped))
                self.assertTrue(bot.movement_state.sprint_key_down)
                self.assertFalse(bot.movement_state.sprinting)
            finally:
                await bot.close()

    async def test_fixed_walk_takes_over_without_restoring_held_directions(self):
        clock = ManualClock()
        bot = Bot("TakeoverPlayer")
        flags = bot.protocol.binding.PlayerAuthInputPacket.InputData
        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.62, 0.5))
            try:
                await bot.set_move_vector(0, 1)
                await clock.wake_at(50)
                self.assertIn(flags.UP, wire.sent[-1].input_data)
                self.assertEqual(await bot.strafe_right(1), 1)
                self.assertIn(flags.RIGHT, wire.sent[1].input_data)
                await clock.wake_at(100)
                self.assertEqual([p.client_tick for p in wire.sent], list(range(1, 6)))
                self.assertEqual((wire.sent[-1].move.x, wire.sent[-1].move.y), (0.0, 0.0))
                self.assertFalse(bot.movement_state.moving)
            finally:
                await bot.close()

    async def test_move_to_brakes_and_rests_at_six_block_feet_target(self):
        clock = ManualClock()
        bot = Bot("DestinationPlayer")
        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.62, 0.5), on_ground=False)
            try:
                self.assertIsNone(bot.movement_state.on_ground)
                await bot.move_to((6.5, 100.0, 0.5))
                self.assertTrue(bot.movement_state.moving)
                for milliseconds in range(50, 4050, 50):
                    await clock.wake_at(milliseconds)
                    if not bot.movement_state.moving:
                        break
                state = bot.movement_state
                self.assertFalse(state.moving)
                self.assertEqual(state.velocity, (0.0, 0.0, 0.0))
                self.assertAlmostEqual(state.feet_position[0], 6.5, delta=0.01)
                self.assertAlmostEqual(state.feet_position[1], 100.0, places=4)
                self.assertAlmostEqual(state.feet_position[2], 0.5)
                positions = [packet.pos.x for packet in wire.sent]
                self.assertEqual(positions, sorted(positions))
                self.assertGreater(wire.sent[1].pos_delta.x, wire.sent[0].pos_delta.x)
                self.assertEqual(wire.sent[-1].move.y, 0.0)
                self.assertEqual(bot._movement.manual_feet_position, state.feet_position)
                await bot.strafe_left(1)
                self.assertIsNone(bot._movement.manual_feet_position)
                x, y, z = bot._movement.observed_feet_position
                self.assertEqual((x, z), (0.5, 0.5))
                self.assertAlmostEqual(y, 100, places=4)
                self.assertGreater(bot.movement_state.feet_position[0], 6.4)
            finally:
                await bot.close()

    async def test_move_to_steers_in_the_current_facing_until_retargeted(self):
        clock = ManualClock()
        bot = Bot("TargetFacing")
        b = bot.protocol.binding
        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.62, 0.5))
            try:
                target = (6.5, 100.0, 0.5)
                await bot.move_to(target)
                await clock.wake_at(50)
                self.assertEqual(wire.sent[-1].rot.y, -90)
                self.assertGreater(wire.sent[-1].move.y, 0.9)

                await self.observe(bot, wire, b.MovePlayerPacket(
                    player_id=1, pos=b.Vec3(x=0.5, y=101.62, z=0.5),
                    rot=b.Vec2(x=17, y=0), reset_position=2,
                ))
                self.assertTrue(bot.movement_state.moving)
                await bot.move_to(target)
                await clock.wake_at(100)
                frame = wire.sent[-1]
                self.assertEqual((frame.rot.x, frame.rot.y), (17, 0))
                self.assertGreater(frame.move.x, 0.9)
                self.assertGreater(frame.pos_delta.x, 0)
                self.assertAlmostEqual(frame.pos_delta.z, 0)

                x, _, z = bot.movement_state.feet_position
                await bot.move_to((x, 100, z - 6))
                await clock.wake_at(150)
                self.assertAlmostEqual(abs(wire.sent[-1].rot.y), 180)
                self.assertLess(wire.sent[-1].pos_delta.z, 0)

                await bot.stop_moving()
                await clock.wake_at(200)
                self.assertEqual((wire.sent[-1].move.x, wire.sent[-1].move.y), (0, 0))
                self.assertAlmostEqual(abs(wire.sent[-1].rot.y), 180)
            finally:
                await bot.close()

    async def test_absolute_grid_survives_late_wakes_and_overrun(self):
        sent = []
        delays_ms = (5, 5, 125, 5, 5, 5, 5)
        clock = ManualClock()

        async def send_delay():
            clock.now_ns += delays_ms[len(sent)] * 1_000_000

        bot = Bot("DeadlinePlayer", input_source=lambda: ForwardInput(0, 0.2158),
                  on_input_sent=lambda tick, ns: sent.append((tick, ns)))
        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.0, 0.5))
            wire.before_send = send_delay
            try:
                for wake in (50, 101, 151, 276, 301, 351, 401, 451, 501):
                    await clock.wake_at(wake)
                self.assertEqual(await bot.wait_ticks(0), 8)
            finally:
                await bot.close()
        self.assertEqual([p.client_tick for p in wire.sent], list(range(1, 8)))
        # Slot 200 expired during the long send. The next eligible send is
        # slot 350, the first grid deadline at least 50 ms after completion.
        expected_ms = (55, 106, 276, 356, 406, 456, 506)
        self.assertEqual(sent, [(tick, ms * 1_000_000) for tick, ms in enumerate(expected_ms, 1)])

    async def test_long_action_lock_does_not_send_an_awaiting_frame_on_release(self):
        await self.check_action_lock_deadline((151, 201), 225, (251, 301), (50, 251, 301))

    async def test_action_lock_release_just_before_next_slot_skips_that_slot(self):
        await self.check_action_lock_deadline((), 149, (151, 201), (50, 149, 201))

    async def test_first_position_enables_the_first_valid_grid_slot(self):
        clock = ManualClock()
        sent = []
        bot = Bot("GridPlayer", on_input_sent=lambda tick, ns: sent.append((tick, ns)))
        with clock.patch_scheduler():
            wire = await self.join(bot)
            try:
                await clock.wake_at(170)
                self.assertEqual(bot.get_tick(), 0)
                await clock.wake_at(200)
                self.assertEqual(bot.get_tick(), 1)
                self.assertEqual(wire.sent, [])
                # Deliver the position on the same event-loop turn as the slot.
                await clock.wake_at(250, before_wake=lambda: self.teleport(wire, (0.5, 101.0, 0.5)))
                self.assertEqual(sent, [(1, 250_000_000)])
                packet, = wire.sent
                self.assertIn(wire.protocol.binding.PlayerAuthInputPacket.InputData.HANDLED_TELEPORT,
                              packet.input_data)
            finally:
                await bot.close()

    async def check_action_lock_deadline(self, while_locked, release_ms, after_release, expected_ms):
        sent = []
        clock = ManualClock()
        bot = Bot("ActionLockPlayer", input_source=lambda: ForwardInput(0, 0.2158),
                  on_input_sent=lambda tick, ns: sent.append((tick, ns)))
        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.0, 0.5))
            try:
                await clock.wake_at(50)
                async with bot._movement._action_lock:
                    await clock.wake_at(101)
                    for wake in while_locked:
                        await clock.wake_at(wake)
                    self.assertEqual(await bot.wait_ticks(0), 2 + len(while_locked))
                    self.assertEqual(sent, [(1, 50_000_000)])
                    clock.now_ns = release_ms * 1_000_000
                await asyncio.sleep(0)
                for wake in after_release:
                    await clock.wake_at(wake)
            finally:
                await bot.close()
        self.assertEqual([p.client_tick for p in wire.sent], [1, 2, 3])
        self.assertEqual(sent, [(tick, ms * 1_000_000) for tick, ms in enumerate(expected_ms, 1)])

    async def test_scheduled_input_rebases_and_never_bursts_or_sends_idle(self):
        sent = []
        clock = ManualClock()
        first_in_flight, finish_first = asyncio.Event(), asyncio.Event()
        angle = 0.0

        def next_frame():
            nonlocal angle
            angle += 0.01
            return ForwardInput(math.degrees(angle), 0.2158)

        bot = Bot("ScheduledPlayer", input_source=next_frame,
                  on_input_sent=lambda tick, ns: sent.append((tick, ns)))
        self.addAsyncCleanup(bot.close)
        b = bot.protocol.binding

        async def delay_first():
            if not wire.sent:
                first_in_flight.set()
                await finish_first.wait()

        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.0, 0.5))
            wire.before_send = delay_first
            try:
                self.assertEqual(wire.sent, [])
                await clock.wake_at(50)
                await asyncio.wait_for(first_in_flight.wait(), 1)
                correction = b.CorrectPlayerMovePredictionPacket(
                    prediction_type=0, pos=b.Vec3(x=9.0, y=81.0, z=11.0),
                    pos_delta=b.Vec3(x=0.0, y=0.0, z=0.0), on_ground=True, tick=1,
                )
                received = asyncio.create_task(bot.packets.wait_for_packet(correction.ID, timeout=1))
                await asyncio.sleep(0)
                wire.push(correction)
                await received
                for wake in (101, 151, 201):
                    await clock.wake_at(wake)
                self.assertEqual(await bot.wait_ticks(0), 4)
                self.assertEqual(sent, [])
                self.assertEqual(bot.position, (9.0, 81.0, 11.0))
                state = bot.movement_state
                self.assertEqual(state.correction_count, 1)
                self.assertTrue(state.on_ground)
                self.assertEqual(state.feet_position, (9.0, 81.0 - 1.62, 11.0))

                finish_first.set()
                await asyncio.sleep(0)
                for wake in (251, 301, 351, 401, 451, 501):
                    await clock.wake_at(wake)
            finally:
                await bot.close()
        self.assertTrue(wire.closed)
        flags = b.PlayerAuthInputPacket.InputData
        self.assertEqual([p.client_tick for p in wire.sent], list(range(1, 7)))
        self.assertEqual(sent, [(tick, ns * 1_000_000)
                                for tick, ns in enumerate((201, 301, 351, 401, 451, 501), 1)])
        self.assertTrue(all(flags.UP in p.input_data and p.move.y == 1 for p in wire.sent))
        self.assertTrue(all(flags.VERTICAL_COLLISION in p.input_data for p in wire.sent))
        self.assertIn(flags.HANDLED_TELEPORT, wire.sent[0].input_data)
        self.assertNotIn(flags.HANDLED_TELEPORT, wire.sent[1].input_data)
        self.assertAlmostEqual(wire.sent[1].pos.x, 9.0 - math.sin(0.02) * 0.2158, places=5)
        self.assertEqual(wire.sent[1].pos.y, 81.0)
        self.assertAlmostEqual(wire.sent[1].pos.z, 11.0 + math.cos(0.02) * 0.2158, places=5)

    async def test_failed_send_does_not_notify_and_close_still_closes_transport(self):
        sent = []
        clock = ManualClock()
        failure = ConnectionError("input transport failed")
        bot = Bot("FailingPlayer", input_source=lambda: ForwardInput(0, 0.2158),
                  on_input_sent=lambda tick, _ns: sent.append(tick))
        self.addAsyncCleanup(bot.close)

        async def fail_second():
            if wire.sent:
                raise failure

        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.0, 0.5))
            wire.before_send = fail_second
            waiting = asyncio.create_task(bot.wait_for_input_end())
            await asyncio.sleep(0)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting

            ticks = asyncio.create_task(bot.wait_ticks(3))
            await asyncio.sleep(0)
            await clock.wake_at(50)
            self.assertEqual(sent, [1])
            self.assertFalse(ticks.done())
            await clock.wake_at(100)
            with self.assertRaises(ConnectionError) as tick_error:
                await asyncio.wait_for(ticks, 1)
            self.assertIs(tick_error.exception, failure)
            with self.assertRaises(ConnectionError) as input_error:
                await asyncio.wait_for(bot.wait_for_input_end(), 1)
            self.assertIs(input_error.exception, failure)
            with self.assertRaises(ConnectionError) as clock_error:
                bot.get_tick()
            self.assertIs(clock_error.exception, failure)
            self.assertEqual(sent, [1])
            self.assertEqual(len(wire.sent), 1)
            with self.assertRaises(ConnectionError) as close_error:
                await bot.close()
            self.assertIs(close_error.exception, failure)
        self.assertTrue(wire.closed)

    async def test_inflight_send_failure_survives_concurrent_reader_cancellation(self):
        clock = ManualClock()
        bot = Bot("InFlightFailure")
        failure = ConnectionError("packet stream closed")
        send_started = asyncio.Event()
        finish_send = asyncio.Event()
        self.addAsyncCleanup(bot.close)

        async def fail():
            send_started.set()
            await finish_send.wait()
            raise failure

        with clock.patch_scheduler():
            wire = await self.join(bot, position=(0.5, 101.62, 0.5))
            await bot.wait_for_position_sync()
            before = list(wire.sent)
            wire.before_send = fail
            await clock.wake_at(50)
            await asyncio.wait_for(send_started.wait(), 1)

            reader = bot.packets.start_receiving()
            reader.cancel()
            finish_send.set()
            with self.assertRaises(ConnectionError) as input_error:
                await asyncio.wait_for(bot.wait_for_input_end(), 1)
            self.assertIs(input_error.exception, failure)
            with self.assertRaises(ConnectionError) as close_error:
                await bot.close()
            self.assertIs(close_error.exception, failure)

        self.assertTrue(wire.closed)
        self.assertEqual(wire.sent, before)
        self.assertEqual(bot.session_end.kind, "reader_end")
