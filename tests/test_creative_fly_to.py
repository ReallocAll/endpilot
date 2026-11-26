import asyncio
from dataclasses import FrozenInstanceError
import math
import unittest

from endpilot import Bot, ForwardInput
from endpilot.protocol import write_batch
import test_flight
from test_scheduled_input import ManualClock, Wire


class CreativeFlightTests(unittest.IsolatedAsyncioTestCase):
    async def join(self, *, flying=True, x=3.5, y=121.4, yaw=37, air=True, input_source=None):
        self.clock = ManualClock()
        self.scheduler = self.enterContext(self.clock.patch_scheduler())
        self.bot = Bot("Creative flight", input_source=input_source)
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)
        self.flags = self.b.PlayerAuthInputPacket.InputData
        self.may_fly = 1 << int(b.AbilitiesIndex.MAY_FLY)
        self.flying = 1 << int(b.AbilitiesIndex.FLYING)
        self.feet = (x, y, 3.5)
        start = b.StartGamePacket(
            runtime_id=13, entity_id=-27, entity_game_type=b.GameType.CREATIVE,
            block_network_ids_are_hashes=True,
        )
        await test_flight.FlightTests.join(
            self, self.bot, self.wire, start=start,
            after_start=(self.abilities(self.may_fly | (self.flying if flying else 0)),),
        )
        position = b.MovePlayerPacket(
            player_id=13, pos=b.Vec3(x=x, y=y + 1.62, z=3.5), rot=b.Vec2(y=yaw),
            reset_position=2, on_ground=not flying,
        )
        await self.deliver(position, *((self.air_section(),) if air else ()))
        await self.bot.wait_for_position_sync()
        self.start = len(self.wire.sent)

    def abilities(self, values):
        data = self.b.SerializedAbilitiesData
        return self.b.UpdateAbilitiesPacket(data=data(
            target_player=-27, layers=[data.SerializedLayer(
                serialized_layer=data.SerializedAbilitiesLayer.BASE,
                abilities_set=self.may_fly | self.flying, ability_values=values,
            )],
        ))

    def stone(self, x, y, z):
        return self.b.UpdateBlockPacket(pos=self.b.BlockPos(x=x, y=y, z=z), runtime_id=0x80310E21)

    def air_section(self):
        b = self.b
        return b.SubChunkPacket(
            dimension_type=0, center_pos=b.SubChunkPos(x=0, y=7, z=0),
            sub_chunk_data=[b.SubChunkPacket.SubChunkPacketData(
                sub_chunk_pos_offset=b.SubChunkPacket.SubChunkPosOffset(),
                result=b.SubChunkPacket.SubChunkRequestResult.SUCCESS_ALL_AIR,
            )],
        )

    def correction(self, tick, feet, velocity=(0, 0, 0), *, on_ground=False):
        b = self.b
        return b.CorrectPlayerMovePredictionPacket(
            tick=tick, on_ground=on_ground,
            pos=b.Vec3(x=feet[0], y=feet[1] + 1.62, z=feet[2]),
            pos_delta=b.Vec3(x=velocity[0], y=velocity[1], z=velocity[2]),
        )

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        seen = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(self.bot.protocol.encode(p) for p in (*packets, marker)))
        await seen

    @property
    def frames(self):
        return [p for p in self.wire.sent[self.start:] if isinstance(p, self.b.PlayerAuthInputPacket)]

    async def advance(self):
        await self.clock.wake_at(self.clock.now_ns // 1_000_000 + 50)

    async def run_to(self, target, *, ticks=160, after_step=None, **tolerances):
        action = asyncio.create_task(self.bot.fly_to(target, ticks=ticks, **tolerances))
        try:
            await asyncio.sleep(0)
            for _ in range(ticks + 2):
                if action.done():
                    return await action
                if after_step is not None:
                    await after_step()
                await self.advance()
            return await asyncio.wait_for(action, 1)
        finally:
            if not action.done():
                action.cancel()
                await asyncio.gather(action, return_exceptions=True)

    def assert_hover(self, feet, target):
        self.assertLessEqual(math.hypot(feet[0] - target[0], feet[2] - target[2]), 0.35)
        self.assertLessEqual(abs(feet[1] - target[1]), 0.35)

    def neutral(self, frame):
        return (frame.move.x == 0 and frame.move.y == 0
                and self.flags.WANT_UP not in frame.input_data and self.flags.WANT_DOWN not in frame.input_data)

    async def test_grounded_takeoff_then_combined_ascent_and_descent_hover(self):
        await self.join(flying=False, y=120)
        await self.deliver(self.stone(3, 119, 3), self.correction(0, self.feet, on_ground=True))
        f = self.flags
        simulated = [3.5, 121.28525, 3.5]
        velocity = [0.0, 0.0, 0.0]
        processed = 0
        switch_tick = None
        sent_takeoff = False
        corrections = []

        async def peer():
            nonlocal processed, switch_tick, sent_takeoff
            for frame in self.frames[processed:]:
                if f.START_FLYING in frame.input_data:
                    switch_tick = frame.client_tick
                if switch_tick is None:
                    continue
                if not sent_takeoff:
                    if frame.client_tick == switch_tick:
                        # An airborne correction from before the switch must not arm travel.
                        await self.deliver(self.correction(switch_tick - 1, (3.5, 120.4, 3.5)))
                    if frame.client_tick == switch_tick + 12:
                        await self.deliver(self.correction(frame.client_tick, simulated))
                        sent_takeoff = True
                    self.assertEqual((frame.move.x, frame.move.y), (0, 0))
                    continue

                # Simulate consumed controls without reading the submitted coordinates.
                # The server's velocity is the post-travel velocity.
                movement = complex(frame.move.y, -frame.move.x)
                heading = complex(math.cos(math.radians(frame.rot.y)), math.sin(math.radians(frame.rot.y)))
                direction = movement * heading
                velocity[0] -= direction.imag * 0.049
                velocity[2] += direction.real * 0.049
                if f.ASCEND in frame.input_data:
                    velocity[1] += 0.15
                    self.assertIn(f.WANT_UP, frame.input_data)
                elif f.DESCEND in frame.input_data:
                    velocity[1] -= 0.22
                    self.assertIn(f.WANT_DOWN, frame.input_data)
                elif not movement:
                    velocity[1] *= 0.375
                for axis in range(3):
                    simulated[axis] += velocity[axis]
                velocity[1] *= 0.6
                for axis in (0, 2):
                    velocity[axis] *= 0.91 if movement else 0.34125
                if frame.client_tick % 6 == 0:
                    corrections.append(self.correction(frame.client_tick, simulated, velocity))
                    if len(corrections) > 1:
                        await self.deliver(corrections.pop(0))
            processed = len(self.frames)

        for target, vertical in (((5.5, 122.6, 6.5), f.ASCEND), ((4.5, 121.0, 4.5), f.DESCEND)):
            begin = len(self.frames)
            result = await self.run_to(target, after_step=peer)
            self.assertEqual(result.status, "predicted_arrived")
            self.assert_hover(result.feet_position, target)
            self.assert_hover(simulated, target)
            self.assertLess(math.hypot(*velocity), 0.004)
            phase = self.frames[begin:]
            self.assertTrue(any(vertical in frame.input_data and frame.move.x and frame.move.y for frame in phase))
            self.assertTrue(all(self.neutral(frame) for frame in phase[-8:]))
            self.assertIs(self.bot.abilities.flying, False)
            with self.assertRaises(FrozenInstanceError):
                result.status = "blocked"
        self.assertEqual(sum(f.START_FLYING in frame.input_data for frame in self.frames), 1)
        self.assertFalse(any(f.STOP_FLYING in frame.input_data for frame in self.frames))
        before = len(self.frames)
        await self.bot.set_flying(True)
        self.assertEqual(len(self.frames), before)

    async def test_short_offset_reduces_directional_input(self):
        await self.join(yaw=0)
        result = await self.run_to((3.5, 121.4, 3.57), horizontal_tolerance=0.015)
        self.assertEqual(result.status, "predicted_arrived")
        self.assertLessEqual(abs(result.feet_position[2] - 3.57), 0.015)
        movement = [p.move.y for p in self.frames if p.move.y]
        self.assertTrue(movement and max(map(abs, movement)) < 0.95)

    async def test_hover_waits_for_a_full_half_second_after_coasting(self):
        for target in ((3.5, 121.4, 4.75), (3.5, 122.6, 3.5)):
            with self.subTest(target=target):
                await self.join(yaw=0)
                y, z = self.feet[1:]
                vy = vz = 0.0
                sampled = []

                async def consume_input():
                    nonlocal y, z, vy, vz
                    for frame in self.frames[len(sampled):]:
                        forward = frame.move.y
                        up = self.flags.WANT_UP in frame.input_data
                        down = self.flags.WANT_DOWN in frame.input_data
                        if up:
                            vy += 0.15
                        elif down:
                            vy -= 0.22
                        elif not forward:
                            vy *= 0.375
                        vz += forward * 0.049
                        y, z = y + vy, z + vz
                        sampled.append((y, z))
                        vy *= 0.6
                        vz *= 0.91 if forward else 0.34125

                result = await self.run_to(target, after_step=consume_input)
                self.assertEqual(result.status, "predicted_arrived")
                self.assert_hover((3.5, y, z), target)
                self.assertGreaterEqual(len(sampled), 11)
                half_second = sampled[-11:]
                self.assertLessEqual(max(math.dist(a, b) for a in half_second for b in half_second), 0.001)
                self.assertTrue(all(self.neutral(frame) for frame in self.frames[-12:]))
                await self.bot.close()
                self.scheduler.close()

    async def test_stationary_target_still_requires_a_full_hover_window(self):
        await self.join()
        result = await self.run_to(self.feet, ticks=10)
        self.assertEqual(result.status, "budget")
        self.assertTrue(all(self.neutral(frame) for frame in self.frames))

    async def test_reverse_descent_keeps_jump_presses_apart(self):
        await self.join(y=121.35, yaw=0)
        target = (3.5, 119.75, 7.526)
        result = await self.run_to(target)
        self.assertEqual(result.status, "predicted_arrived")
        self.assert_hover(result.feet_position, target)
        self.assertTrue(any(self.flags.DESCEND in frame.input_data for frame in self.frames))
        self.assertTrue(any(self.flags.ASCEND in frame.input_data for frame in self.frames))
        self.assert_no_flight_switch_or_quick_jump()

    async def test_jump_release_interval_survives_stopping_and_retrying(self):
        await self.join()
        target = (3.5, 123.85, 3.5)
        action = asyncio.create_task(self.bot.fly_to(target))
        await asyncio.sleep(0)
        self.assertIn(self.flags.JUMP_PRESSED_RAW, self.frames[-1].input_data)
        stopper = asyncio.create_task(self.bot.stop_moving())
        await asyncio.sleep(0)
        await self.advance()
        self.assertEqual((await asyncio.wait_for(action, 1)).status, "stopped")
        await asyncio.wait_for(stopper, 1)
        result = await self.run_to(target)
        self.assertEqual(result.status, "predicted_arrived")
        self.assertGreaterEqual(sum(self.flags.JUMP_PRESSED_RAW in p.input_data for p in self.frames), 2)
        self.assert_no_flight_switch_or_quick_jump()

    def assert_no_flight_switch_or_quick_jump(self):
        released = None
        for frame in self.frames:
            flags = set(frame.input_data)
            self.assertTrue(flags.isdisjoint((self.flags.START_FLYING, self.flags.STOP_FLYING)))
            if self.flags.JUMP_RELEASED_RAW in flags:
                released = frame.client_tick
            if released is not None and self.flags.JUMP_PRESSED_RAW in flags:
                self.assertGreaterEqual(frame.client_tick - released, 10)

    async def test_wall_contact_from_the_body_edge(self):
        for start_x, expected in ((3.68, "predicted_arrived"), (3.76, "blocked")):
            with self.subTest(start_x=start_x):
                await self.join(x=start_x, yaw=0)
                await self.deliver(self.stone(4, 122, 5))
                target = (start_x, 121.4, 6.5)
                result = await self.run_to(target)
                self.assertEqual(result.status, expected)
                if expected == "blocked":
                    self.assertTrue(all(frame.pos.z < 4.7 for frame in self.frames))
                else:
                    self.assert_hover(result.feet_position, target)
                self.assertTrue(self.neutral(self.frames[-1]))
                await self.bot.close()
                self.scheduler.close()

    async def test_new_obstruction_clips_the_predicted_release_path(self):
        await self.join(yaw=0)

        async def add_wall_before_coasting():
            if len(self.frames) == 7:
                await self.deliver(self.stone(3, 122, 5))

        result = await self.run_to((3.5, 121.4, 10.5), after_step=add_wall_before_coasting)
        self.assertEqual(result.status, "blocked")
        moving = [p for p in self.frames if p.move.y]
        self.assertEqual(len(moving), 7)
        release = self.frames[7]
        self.assertTrue(self.neutral(release))
        self.assertLessEqual(release.pos.z, 4.7001)
        self.assertEqual(release.pos.z, moving[-1].pos.z)

    async def test_unknown_terrain_and_takeoff_roof_emit_no_input(self):
        await self.join(flying=False, y=120, air=False)
        target = (4.5, 122.8, 3.5)
        self.assertEqual((await self.bot.fly_to(target)).status, "unknown")
        self.assertEqual(len(self.frames), 0)
        await self.deliver(self.air_section(), self.stone(3, 123, 3))
        self.assertEqual((await self.bot.fly_to(target)).status, "blocked")
        self.assertEqual(len(self.frames), 0)

    async def test_takeoff_rechecks_roof_between_jump_presses(self):
        await self.join(flying=False, y=120, yaw=0)
        await self.deliver(self.stone(3, 119, 3), self.correction(0, self.feet, on_ground=True))

        async def add_roof_after_release():
            if len(self.frames) == 2:
                await self.deliver(self.stone(3, 123, 3))

        result = await self.run_to((3.5, 122.5, 7.5), after_step=add_roof_after_release)
        self.assertEqual(result.status, "blocked")
        self.assertIn(self.flags.JUMP_PRESSED_RAW, self.frames[0].input_data)
        self.assertIn(self.flags.JUMP_RELEASED_RAW, self.frames[1].input_data)
        self.assertFalse(any(self.flags.START_FLYING in frame.input_data for frame in self.frames))
        self.assertTrue(all(self.neutral(frame) for frame in self.frames[1:]))

    async def test_takeoff_wait_checks_neutral_frames_for_a_new_roof(self):
        await self.join(flying=False, y=120, yaw=0)
        await self.deliver(self.stone(3, 119, 3), self.correction(0, self.feet, on_ground=True))

        async def add_roof_after_switch():
            if len(self.frames) == 3:
                self.assertIn(self.flags.START_FLYING, self.frames[-1].input_data)
                await self.deliver(self.stone(3, 123, 3))

        result = await self.run_to((3.5, 122.5, 7.5), ticks=25, after_step=add_roof_after_switch)
        self.assertEqual(result.status, "blocked")
        self.assertTrue(self.neutral(self.frames[-1]))
        self.assertTrue(any(self.flags.JUMP_RELEASED_RAW in p.input_data for p in self.frames[3:]))
        self.assertTrue(all(p.pos.y - 1.62 + 1.8 < 123.0001 for p in self.frames))

    async def test_takeoff_requires_an_observed_airborne_settle(self):
        await self.join(flying=False, y=120, yaw=0)
        await self.deliver(self.stone(3, 119, 3), self.correction(0, self.feet, on_ground=True))
        sent_correction = False

        async def report_falling_once():
            nonlocal sent_correction
            switches = [p for p in self.frames if self.flags.START_FLYING in p.input_data]
            if switches and self.frames[-1].client_tick == switches[0].client_tick + 12:
                sent_correction = True
                await self.deliver(self.correction(self.frames[-1].client_tick, (3.5, 121.05, 3.5),
                                                   velocity=(0, -0.3, 0)))

        result = await self.run_to((3.5, 121.05, 7.5), ticks=30, after_step=report_falling_once)
        self.assertEqual(result.status, "budget")
        self.assertTrue(sent_correction)
        self.assertIs(self.bot.abilities.flying, False)
        self.assertTrue(all(frame.move.x == 0 and frame.move.y == 0 for frame in self.frames))

    async def test_unknown_neighbor_stops_before_the_body_crosses_the_chunk_edge(self):
        await self.join(x=15, yaw=0)
        result = await self.run_to((16.9, 121.4, 3.5))
        self.assertEqual(result.status, "unknown")
        self.assertTrue(any(self.flags.LEFT in frame.input_data for frame in self.frames))
        self.assertTrue(all(frame.pos.x <= 15.701 for frame in self.frames))
        self.assertTrue(self.neutral(self.frames[-1]))

    async def test_vertical_corrections_can_prevent_a_predicted_arrival(self):
        await self.join()

        async def keep_current_altitude():
            await self.deliver(self.correction(self.frames[-1].client_tick, self.feet))

        result = await self.run_to((3.5, 122.55, 3.5), ticks=18, after_step=keep_current_altitude)
        self.assertEqual(result.status, "budget")
        self.assertAlmostEqual(result.feet_position[1], self.feet[1], places=4)
        self.assertTrue(any(self.flags.ASCEND in frame.input_data for frame in self.frames))
        self.assertTrue(self.neutral(self.frames[-1]))
        self.assertIn(self.flags.JUMP_RELEASED_RAW, self.frames[-1].input_data)

    async def test_budget_releases_descent(self):
        await self.join()
        result = await self.run_to((7.5, 117.4, 7.5), ticks=2)
        self.assertEqual(result.status, "budget")
        self.assertTrue(any(self.flags.WANT_DOWN in frame.input_data for frame in self.frames))
        self.assertTrue(self.neutral(self.frames[-1]))
        self.assertIn(self.flags.SNEAK_RELEASED_RAW, self.frames[-1].input_data)

    async def test_late_correction_is_replayed_before_stopping_and_releasing(self):
        for outcome, ticks in (("stopped", 160), ("budget", 3)):
            with self.subTest(outcome=outcome):
                await self.join(yaw=0)
                stopper = None
                corrected = False

                async def late_correction():
                    nonlocal stopper, corrected
                    if len(self.frames) != 3 or corrected:
                        return
                    corrected = True
                    self.assertEqual([frame.move.y for frame in self.frames], [1, 1, 1])
                    packet = self.correction(self.frames[0].client_tick, (3.5, 121.4, 3.52),
                                             velocity=(0, 0, 0.03))
                    await self.deliver(packet)
                    if outcome == "stopped":
                        stopper = asyncio.create_task(self.bot.stop_moving())
                        await asyncio.sleep(0)

                result = await self.run_to((3.5, 121.4, 10.5), ticks=ticks, after_step=late_correction)
                if stopper is not None:
                    await asyncio.wait_for(stopper, 1)
                self.assertEqual(result.status, outcome)
                self.assertTrue(corrected)
                release = self.frames[3]
                self.assertTrue(self.neutral(release))
                # After the corrected first frame, independently integrate two
                # more full inputs, then the first neutral frame.
                second = 0.03 + 0.049
                third = second * 0.91 + 0.049
                expected = 3.52 + second + third + third * 0.91
                self.assertAlmostEqual(release.pos.z, expected, places=5)
                self.assertAlmostEqual(result.feet_position[2], expected, places=5)
                await self.bot.close()
                self.scheduler.close()

    async def test_later_server_pose_in_same_batch_supersedes_the_correction(self):
        for source in ("move", "post_move", "teleport"):
            with self.subTest(source=source):
                await self.join(yaw=0)
                replaced = False

                async def late_correction_then_new_pose():
                    nonlocal replaced
                    if len(self.frames) != 3 or replaced:
                        return
                    replaced = True
                    vec = self.b.Vec3(x=3.5, y=123.02, z=8.5)
                    if source == "post_move":
                        newer = self.b.ServerPlayerPostMovePositionPacket(pos=vec)
                    else:
                        newer = self.b.MovePlayerPacket(
                            player_id=13, pos=vec, reset_position=2 if source == "teleport" else 0,
                            tick=self.frames[2].client_tick,
                        )
                    old = self.correction(self.frames[0].client_tick, (3.5, 121.4, 3.6), velocity=(0, 0, 0.03))
                    await self.deliver(old, newer)
                    self.assertEqual(self.bot.position[2], 8.5)

                target = (3.5, 121.4, 12)
                result = await self.run_to(target, after_step=late_correction_then_new_pose)
                self.assertTrue(replaced)
                if source == "teleport":
                    self.assertEqual(result.status, "stopped")
                    self.assertTrue(self.neutral(self.frames[3]))
                    self.assertEqual(result.feet_position[2], 8.5)
                else:
                    self.assertEqual(result.status, "predicted_arrived")
                    self.assert_hover(result.feet_position, target)
                    self.assertAlmostEqual(self.frames[3].pos.z, 8.579, places=5)
                await self.bot.close()
                self.scheduler.close()

    async def test_stop_permission_dimension_cancellation_and_disconnect(self):
        for interruption in ("stop", "permission", "dimension", "cancel", "disconnect"):
            with self.subTest(interruption=interruption):
                await self.join()
                action = asyncio.create_task(self.bot.fly_to((7.5, 117.4, 7.5)))
                await asyncio.sleep(0)
                self.assertIn(self.flags.WANT_DOWN, self.frames[-1].input_data)
                stopper = None
                if interruption == "stop":
                    stopper = asyncio.create_task(self.bot.stop_moving())
                    await asyncio.sleep(0)
                elif interruption == "permission":
                    await self.deliver(self.abilities(0), self.abilities(self.may_fly))
                elif interruption == "dimension":
                    await self.deliver(self.b.ChangeDimensionPacket(
                        dimension_id=1, pos=self.b.Vec3(x=3.5, y=123, z=3.5), loading_screen_id=17,
                    ))
                elif interruption == "cancel":
                    action.cancel()
                else:
                    self.wire.incoming.put_nowait(ConnectionError("peer closed"))

                if interruption in ("stop", "permission", "dimension"):
                    await self.advance()
                    result = await asyncio.wait_for(action, 1)
                    self.assertEqual(result.status, "stopped")
                else:
                    with self.assertRaises(asyncio.CancelledError if interruption == "cancel" else ConnectionError):
                        await asyncio.wait_for(action, 1)
                if stopper is not None:
                    await asyncio.wait_for(stopper, 1)
                if interruption in ("dimension", "disconnect"):
                    self.assertEqual(len(self.frames), 1)
                else:
                    self.assertTrue(self.neutral(self.frames[-1]))
                    self.assertTrue(any(self.flags.SNEAK_RELEASED_RAW in frame.input_data for frame in self.frames))
                if interruption == "disconnect":
                    with self.assertRaisesRegex(ConnectionError, "peer closed"):
                        await self.bot.close()
                else:
                    await self.bot.close()
                self.scheduler.close()

    async def test_external_input_source_keeps_ownership(self):
        await self.join(input_source=lambda: ForwardInput(20, 0.2))
        with self.assertRaisesRegex(RuntimeError, "input source owns movement"):
            await self.bot.fly_to((7.5, 122.4, 7.5))
        self.assertEqual(len(self.frames), 0)
        await self.advance()
        self.assertEqual(len(self.frames), 1)
        self.assertAlmostEqual(self.frames[0].rot.y, 20)

    async def test_cancel_is_preserved_if_the_release_fails(self):
        await self.join()
        action = asyncio.create_task(self.bot.fly_to((7.5, 117.4, 7.5)))
        await asyncio.sleep(0)
        self.assertIn(self.flags.WANT_DOWN, self.frames[-1].input_data)

        async def fail_release():
            raise ConnectionError("release failed")

        self.wire.before_send = fail_release
        try:
            action.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await action
        finally:
            self.wire.before_send = None
        await self.advance()
        self.assertTrue(self.neutral(self.frames[-1]))
        self.assertFalse(self.bot.movement_state.sneaking)
