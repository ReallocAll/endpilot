import asyncio
import struct
import unittest
from unittest.mock import patch

from endpilot import Bot
from endpilot.blocks import HASHED_AIR_RUNTIME_ID
from endpilot.compression import PacketConnection
from endpilot.login import JoinSession
from test_scheduled_input import ManualClock, Wire


class MovementPoseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = ManualClock()
        self.addCleanup(self.clock.patch_scheduler().close)
        self.bot = Bot("PosePlayer")
        self.b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)
        stream = PacketConnection(self.wire, self.bot.protocol, self.b.NetworkSettingsPacket(
            compression_algorithm=self.b.PacketCompressionAlgorithm.NONE,
        ))
        session = JoinSession(stream, self.bot.identity, "loopback")
        session.player_id = session.player_unique_id = 1
        session.item_names = {7: "minecraft:elytra", 8: "minecraft:iron_chestplate"}
        session.block_network_ids_are_hashes = True
        self.bot._start_session(session)
        self.addAsyncCleanup(self.bot.close)
        await self.deliver(self.b.MovePlayerPacket(
            player_id=1, pos=self.b.Vec3(x=0.5, y=101.62, z=0.5), on_ground=True,
        ))

    async def deliver(self, *packets):
        for packet in packets:
            self.wire.push(packet)
        await asyncio.sleep(0)

    def armor(self, item_id, *, slot=1, data=b""):
        b = self.b
        return b.InventorySlotPacket(
            inventory_id=b.ContainerID.ARMOR, slot=slot,
            item=b.SerializedNetworkItemStackDescriptor(id=item_id, stack_size=1, user_data_buffer=data),
        )

    def flags(self, *, actor=1, low=None, high=None):
        b = self.b
        return b.SetActorDataPacket(id=actor, packed_items=b.SynchedActorData.CopyableDataList(data=[
            b.DataItemEntry(id=index, payload=b.DataItemInt64Payload(value=value))
            for index, value in ((0, low), (92, high)) if value is not None
        ]))

    def correction(self, *, y=103.0, dy=-0.2, on_ground=False):
        b = self.b
        return b.CorrectPlayerMovePredictionPacket(
            prediction_type=0, pos=b.Vec3(x=0.5, y=y, z=0.5), pos_delta=b.Vec3(x=0, y=dy, z=0),
            on_ground=on_ground, tick=self.bot._movement._tick - 1,
        )

    def block(self, y, runtime_id):
        b = self.b
        return b.UpdateBlockPacket(pos=b.BlockPos(x=0, y=y, z=0), runtime_id=runtime_id, layer=0)

    def air_section(self):
        b = self.b
        return b.SubChunkPacket(
            cache_enabled=False, dimension_type=0, center_pos=b.SubChunkPos(x=0, y=6, z=0),
            sub_chunk_data=[b.SubChunkPacket.SubChunkPacketData(
                sub_chunk_pos_offset=b.SubChunkPacket.SubChunkPosOffset(x=0, y=0, z=0),
                result=b.SubChunkPacket.SubChunkRequestResult.SUCCESS_ALL_AIR,
                height_map_data=b.SubChunkPacket.HeightmapData(),
            )],
        )

    def respawn(self, state, *, actor=1, y=111.62):
        b = self.b
        return b.RespawnPacket(runtime_id=actor, state=state, pos=b.Vec3(x=0.5, y=y, z=0.5))

    def teleport(self, y):
        b = self.b
        return b.MovePlayerPacket(player_id=1, pos=b.Vec3(x=0.5, y=y, z=0.5),
                                  on_ground=True, reset_position=2)

    def dimension_ack(self):
        b = self.b
        return b.PlayerActionPacket(runtime_id=1, action=b.PlayerActionType.CHANGE_DIMENSION_ACK)

    async def test_startup_replays_only_current_dimension_pose_metadata(self):
        b = self.b
        bot = Bot("JoiningPosePlayer")
        wire = Wire(bot.protocol)
        self.addAsyncCleanup(bot.close)
        for packet in (
            b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
            b.StartGamePacket(runtime_id=1, entity_id=1),
            self.flags(low=(1 << 32) | (1 << 57)),
            b.ChangeDimensionPacket(dimension_id=1, loading_screen_id=0),
            self.dimension_ack(),
            self.flags(actor=2, low=1 << 32),
            self.flags(high=1 << 50),
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        ):
            wire.push(packet)
        with patch("endpilot.transport.nethernet.connect", return_value=wire):
            await bot.join_nethernet("http://127.0.0.1:19132")
        self.assertIsNone(bot.movement_state.gliding)
        self.assertIsNone(bot.is_swimming)
        self.assertTrue(bot.movement_state.crawling)

    async def test_glide_uses_equipped_chest_and_fall_while_pose_waits_on_server(self):
        flags = self.b.PlayerAuthInputPacket.InputData
        await self.deliver(self.correction(), self.flags(low=0), self.armor(7, slot=0))
        await self.deliver(self.armor(8))
        self.assertIsNone(await self.bot.set_gliding(True))
        broken = b"\xff\xff\x01\x0a\0\0\x03\x06\0Damage" + struct.pack("<i", 431) + b"\0"
        await self.deliver(self.armor(7, data=broken))
        self.assertIsNone(await self.bot.set_gliding(True))
        self.assertEqual(self.wire.sent, [])
        await self.deliver(self.armor(7))

        tick = await self.bot.set_gliding(True)
        frame, = self.wire.sent
        self.assertEqual(tick, frame.client_tick)
        self.assertTrue({flags.JUMP_PRESSED_RAW, flags.JUMP_DOWN, flags.WANT_UP, flags.START_GLIDING}
                        .issubset(set(frame.input_data)))
        self.assertNotIn(flags.START_JUMPING, frame.input_data)
        self.assertAlmostEqual(frame.pos_delta.y, -0.2)
        self.assertFalse(self.bot.movement_state.gliding)
        self.assertIsNone(await self.bot.set_gliding(True))
        self.assertEqual(len(self.wire.sent), 1)
        await self.deliver(self.flags(actor=2, low=1 << 32))
        self.assertFalse(self.bot.movement_state.gliding)
        await self.deliver(self.flags(low=1 << 32))
        self.assertTrue(self.bot.movement_state.gliding)
        await self.deliver(self.armor(8))
        self.assertTrue(self.bot.movement_state.gliding)
        await self.bot.set_gliding(False)
        self.assertIn(flags.STOP_GLIDING, self.wire.sent[-1].input_data)
        self.assertTrue(self.bot.movement_state.gliding)
        await self.deliver(self.correction(on_ground=True), self.flags(low=0))
        self.assertFalse(self.bot.movement_state.gliding)

    async def test_glide_waits_for_fall_and_dimension_invalidates_pending_start(self):
        await self.deliver(self.flags(low=0, high=1 << 50), self.armor(7))
        pending = asyncio.create_task(self.bot.set_gliding(True))
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        self.assertEqual(self.wire.sent, [])
        await self.deliver(self.b.ChangeDimensionPacket(dimension_id=1))
        self.assertIsNone(await asyncio.wait_for(pending, 1))
        self.assertIsNone(self.bot.movement_state.gliding)
        self.assertIsNone(self.bot.movement_state.crawling)
        self.assertEqual(self.wire.sent, [])

    async def test_transport_loss_forgets_observed_poses(self):
        await self.deliver(self.flags(low=1 << 32, high=1 << 50))
        self.assertTrue(self.bot.movement_state.gliding)
        self.assertTrue(self.bot.movement_state.crawling)
        self.wire.incoming.put_nowait(ConnectionError("loopback dropped"))
        with self.assertRaisesRegex(ConnectionError, "loopback dropped"):
            await self.bot.packets._reader_task
        self.assertIsNone(self.bot.movement_state.gliding)
        self.assertIsNone(self.bot.movement_state.crawling)
        with self.assertRaisesRegex(ConnectionError, "loopback dropped"):
            await self.bot.close()
        self.assertTrue(self.wire.closed)

    async def test_glide_reverses_before_confirmation_and_ignores_old_intents(self):
        flags = self.b.PlayerAuthInputPacket.InputData
        await self.deliver(self.armor(7), self.correction(), self.flags(low=0))
        self.assertIsNotNone(await self.bot.set_gliding(True))
        self.assertFalse(self.bot.movement_state.gliding)
        self.assertIsNotNone(await self.bot.set_gliding(False))
        self.assertIn(flags.STOP_GLIDING, self.wire.sent[-1].input_data)
        self.assertIn(flags.JUMP_RELEASED_RAW, self.wire.sent[-1].input_data)

        # A stale copy of the initial flags provides no evidence of either request.
        await self.deliver(self.flags(low=0), self.flags(low=1 << 32))
        self.assertTrue(self.bot.movement_state.gliding)
        self.assertIsNone(await self.bot.set_gliding(False))
        self.assertEqual(len(self.wire.sent), 2)
        await self.deliver(self.flags(low=0))
        self.assertFalse(self.bot.movement_state.gliding)
        self.assertIsNotNone(await self.bot.set_gliding(True))
        await self.clock.wake_at(50)
        self.assertIn(flags.JUMP_RELEASED_RAW, self.wire.sent[-1].input_data)
        await self.deliver(self.flags(low=1 << 32), self.flags(low=0))
        await self.deliver(self.correction())
        # A confirmed request can be submitted again after a server-driven stop.
        self.assertIsNotNone(await self.bot.set_gliding(True))

    async def test_glide_stop_waits_for_a_start_in_flight(self):
        flags = self.b.PlayerAuthInputPacket.InputData
        await self.deliver(self.armor(7), self.correction(), self.flags(low=0))
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_start():
            entered.set()
            await release.wait()

        self.wire.before_send = hold_start
        start = asyncio.create_task(self.bot.set_gliding(True))
        await asyncio.wait_for(entered.wait(), 1)
        stop = asyncio.create_task(self.bot.set_gliding(False))
        await asyncio.sleep(0)
        release.set()
        self.assertIsNotNone(await asyncio.wait_for(start, 1))
        self.assertIsNotNone(await asyncio.wait_for(stop, 1))
        start_frame, stop_frame = self.wire.sent
        self.assertIn(flags.START_GLIDING, start_frame.input_data)
        self.assertIn(flags.STOP_GLIDING, stop_frame.input_data)

    async def test_failed_glide_frames_leave_the_latest_successful_intent(self):
        flags = self.b.PlayerAuthInputPacket.InputData
        await self.deliver(self.armor(7), self.correction(), self.flags(low=0))

        async def refused():
            raise ConnectionError("test send failure")

        self.wire.before_send = refused
        with self.assertRaisesRegex(ConnectionError, "test send failure"):
            await self.bot.set_gliding(True)
        self.assertEqual(self.wire.sent, [])
        self.assertIsNone(await self.bot.set_gliding(False))
        self.wire.before_send = None
        self.assertIsNotNone(await self.bot.set_gliding(True))
        self.assertAlmostEqual(self.wire.sent[-1].pos.y, 102.8, places=4)
        self.wire.before_send = refused
        with self.assertRaisesRegex(ConnectionError, "test send failure"):
            await self.bot.set_gliding(False)
        self.wire.before_send = None
        self.assertIsNotNone(await self.bot.set_gliding(False))
        self.assertIn(flags.STOP_GLIDING, self.wire.sent[-1].input_data)
        self.assertIn(flags.JUMP_RELEASED_RAW, self.wire.sent[-1].input_data)

    async def test_only_a_successful_glide_start_ends_an_existing_jump_prediction(self):
        await self.bot.jump()
        await self.clock.wake_at(50)
        await self.deliver(self.armor(7), self.correction(), self.flags(low=0))

        async def refuse():
            raise ConnectionError("test send failure")

        self.wire.before_send = refuse
        with self.assertRaisesRegex(ConnectionError, "test send failure"):
            await self.bot.set_gliding(True)
        self.wire.before_send = None
        await self.clock.wake_at(100)
        self.assertGreater(self.wire.sent[-1].pos_delta.y, 0)
        self.assertIsNotNone(await self.bot.set_gliding(True))
        await self.clock.wake_at(150)
        self.assertEqual(self.wire.sent[-1].pos_delta.y, 0)

    async def test_glide_send_keeps_a_correction_received_during_send(self):
        await self.deliver(self.armor(7), self.correction(), self.flags(low=0))
        for fails, corrected_y in ((True, 105.0), (False, 106.0)):
            with self.subTest(fails=fails):
                before = self.bot.movement_state

                async def correct_during_send():
                    self.assertEqual(self.bot.movement_state, before)
                    await self.deliver(self.correction(y=corrected_y, dy=-0.4))
                    if fails:
                        raise ConnectionError("test send failure")

                self.wire.before_send = correct_during_send
                if fails:
                    with self.assertRaisesRegex(ConnectionError, "test send failure"):
                        await self.bot.set_gliding(True)
                else:
                    self.assertIsNotNone(await self.bot.set_gliding(True))
                self.assertAlmostEqual(self.bot.movement_state.feet_position[1], corrected_y - 1.62, places=4)
                self.assertAlmostEqual(self.bot.movement_state.velocity[1], -0.4)

    async def test_glide_send_keeps_position_only_update_and_commits_velocity(self):
        b = self.b
        correction = self.correction()
        correction.pos_delta = b.Vec3(x=0.15, y=-0.2, z=0.0)
        await self.deliver(self.armor(7), correction, self.flags(low=0))

        async def move_during_send():
            await self.deliver(b.MovePlayerPacket(
                player_id=1, pos=b.Vec3(x=0.5, y=105.0, z=0.5), on_ground=False,
            ))

        self.wire.before_send = move_during_send
        self.assertIsNotNone(await self.bot.set_gliding(True))
        self.assertAlmostEqual(self.bot.movement_state.feet_position[1], 105.0 - 1.62, places=4)
        self.assertEqual(self.bot.movement_state.velocity[0], 0.0)
        self.assertAlmostEqual(self.bot.movement_state.velocity[1], -0.2)

    async def test_glide_confirmation_then_stop_during_start_allows_restart(self):
        flags = self.b.PlayerAuthInputPacket.InputData
        await self.deliver(self.armor(7), self.correction(), self.flags(low=0))

        async def confirm_and_stop():
            await self.deliver(self.flags(low=1 << 32), self.flags(low=0))

        self.wire.before_send = confirm_and_stop
        self.assertIsNotNone(await self.bot.set_gliding(True))
        self.assertFalse(self.bot.movement_state.gliding)
        self.wire.before_send = None
        await self.clock.wake_at(50)
        self.assertIn(flags.JUMP_RELEASED_RAW, self.wire.sent[-1].input_data)
        self.assertIsNotNone(await self.bot.set_gliding(True))

    async def test_glide_send_cannot_commit_into_a_new_position_generation(self):
        state = self.b.PlayerRespawnState
        await self.deliver(self.armor(7), self.correction(), self.flags(low=0))

        async def respawn_during_send():
            await self.deliver(self.respawn(state.SEARCHING_FOR_SPAWN), self.respawn(state.READY_TO_SPAWN),
                               self.flags(low=0))

        self.wire.before_send = respawn_during_send
        self.assertIsNotNone(await self.bot.set_gliding(True))
        self.assertAlmostEqual(self.bot.movement_state.feet_position[1], 110.0, places=4)
        self.assertEqual(self.bot.movement_state.velocity, (0.0, 0.0, 0.0))
        self.wire.before_send = None
        await self.clock.wake_at(50)
        await self.deliver(self.correction(y=112.0))
        self.assertIsNotNone(await self.bot.set_gliding(True))
        self.assertAlmostEqual(self.wire.sent[-1].pos.y, 111.8, places=4)

    async def test_teleport_ack_is_retried_and_does_not_consume_a_new_teleport(self):
        ack = self.b.PlayerAuthInputPacket.InputData.HANDLED_TELEPORT
        await self.deliver(self.teleport(103.0))

        async def refuse():
            raise ConnectionError("test send failure")

        self.wire.before_send = refuse
        with self.assertRaisesRegex(ConnectionError, "test send failure"):
            await self.bot.set_sneaking(False)

        async def replace_teleport():
            await self.deliver(self.teleport(106.0))

        self.wire.before_send = replace_teleport
        await self.bot.set_sneaking(False)
        self.assertIn(ack, self.wire.sent[-1].input_data)
        self.assertAlmostEqual(self.wire.sent[-1].pos.y, 103.0, places=4)
        self.assertAlmostEqual(self.bot.movement_state.feet_position[1], 106.0 - 1.62, places=4)
        self.wire.before_send = None
        await self.bot.set_sneaking(False)
        self.assertIn(ack, self.wire.sent[-1].input_data)
        self.assertAlmostEqual(self.wire.sent[-1].pos.y, 106.0, places=4)
        await self.bot.set_sneaking(False)
        self.assertNotIn(ack, self.wire.sent[-1].input_data)

    async def test_failed_glide_stop_keeps_observations_of_the_previous_start(self):
        flags = self.b.PlayerAuthInputPacket.InputData
        await self.deliver(self.armor(7), self.correction(), self.flags(low=0))
        self.assertIsNotNone(await self.bot.set_gliding(True))

        async def confirm_and_stop_then_fail():
            await self.deliver(self.flags(low=1 << 32), self.flags(low=0))
            raise ConnectionError("test send failure")

        self.wire.before_send = confirm_and_stop_then_fail
        with self.assertRaisesRegex(ConnectionError, "test send failure"):
            await self.bot.set_gliding(False)
        self.wire.before_send = None
        self.assertFalse(self.bot.movement_state.gliding)
        self.assertIsNone(await self.bot.set_gliding(False))
        await self.clock.wake_at(50)
        self.assertIn(flags.JUMP_RELEASED_RAW, self.wire.sent[-1].input_data)
        self.assertIsNotNone(await self.bot.set_gliding(True))

    async def test_crawl_reverses_before_confirmation_when_headroom_changes(self):
        flags = self.b.PlayerAuthInputPacket.InputData
        stone = 0x80310E21
        await self.deliver(self.air_section(), self.block(99, stone), self.block(101, stone), self.flags(high=0))
        self.assertIsNotNone(await self.bot.set_crawling(True))
        await self.deliver(self.block(101, HASHED_AIR_RUNTIME_ID))
        self.assertIsNotNone(await self.bot.set_crawling(False))
        self.assertIn(flags.STOP_CRAWLING, self.wire.sent[-1].input_data)
        await self.deliver(self.flags(high=0), self.flags(high=1 << 50))
        self.assertTrue(self.bot.movement_state.crawling)
        self.assertIsNone(await self.bot.set_crawling(False))
        await self.deliver(self.flags(high=0))
        self.assertFalse(self.bot.movement_state.crawling)
        self.assertEqual(len(self.wire.sent), 2)

    async def test_foreign_ready_does_not_interrupt_a_pending_glide(self):
        await self.deliver(self.armor(7), self.flags(low=0))
        pending = asyncio.create_task(self.bot.set_gliding(True))
        await asyncio.sleep(0)
        await self.deliver(self.respawn(self.b.PlayerRespawnState.READY_TO_SPAWN, actor=2))
        await self.deliver(self.correction())
        self.assertIsNotNone(await asyncio.wait_for(pending, 1))
        self.assertAlmostEqual(self.wire.sent[-1].pos.y, 102.8, places=4)

    async def test_local_respawn_invalidates_old_motion_until_own_ready(self):
        state = self.b.PlayerRespawnState
        await self.deliver(self.armor(7), self.correction(), self.flags(low=0))
        last_position = self.bot.position
        await self.deliver(self.respawn(state.SEARCHING_FOR_SPAWN, actor=0))
        self.assertEqual(self.bot.position, last_position)
        self.assertIsNone(self.bot.movement_state.feet_position)
        self.assertEqual(self.bot.movement_state.velocity, (0.0, 0.0, 0.0))
        await self.deliver(self.respawn(state.READY_TO_SPAWN, actor=2), self.correction())
        self.assertIsNone(self.bot.movement_state.feet_position)
        self.assertIsNone(await self.bot.set_gliding(True))
        self.assertEqual(self.wire.sent, [])
        await self.deliver(self.respawn(state.READY_TO_SPAWN, actor=0))
        self.assertAlmostEqual(self.bot.movement_state.feet_position[1], 110.0, places=4)

    async def test_respawn_during_swing_blocks_the_following_movement_frame(self):
        async def search_before_send_completes():
            await self.deliver(self.respawn(self.b.PlayerRespawnState.SEARCHING_FOR_SPAWN))

        self.wire.before_send = search_before_send_completes
        with self.assertRaisesRegex(ConnectionError, "waiting for the player to respawn"):
            await self.bot.missed_swing()
        animation, = self.wire.sent
        self.assertIsInstance(animation, self.b.AnimatePacket)

    async def test_respawn_during_walk_release_preserves_submitted_count(self):
        async def search_during_first_release():
            if len(self.wire.sent) == 1:
                await self.deliver(self.respawn(self.b.PlayerRespawnState.SEARCHING_FOR_SPAWN))

        self.wire.before_send = search_during_first_release
        self.assertEqual(await asyncio.wait_for(self.bot.walk_forward(ticks=1), 1), 1)
        move, release = self.wire.sent
        self.assertEqual(move.move.y, 1)
        self.assertEqual(release.move.y, 0)

    async def test_queued_pose_cannot_send_in_another_position_generation(self):
        b = self.b
        for transition in ((b.ChangeDimensionPacket(dimension_id=1), self.dimension_ack()), (
            self.respawn(b.PlayerRespawnState.SEARCHING_FOR_SPAWN),
            self.respawn(b.PlayerRespawnState.READY_TO_SPAWN),
        )):
            with self.subTest(packet=type(transition[0]).__name__):
                await self.deliver(self.flags(low=1 << 32))
                async with self.bot._movement._action_lock:
                    pending = asyncio.create_task(self.bot.set_gliding(False))
                    await asyncio.sleep(0)
                    await self.deliver(*transition, b.MovePlayerPacket(
                        player_id=1, pos=b.Vec3(x=2.5, y=111.62, z=0.5), on_ground=True,
                    ), self.flags(low=1 << 32))
                self.assertIsNone(await asyncio.wait_for(pending, 1))
                self.assertFalse(any(isinstance(frame, b.PlayerAuthInputPacket) for frame in self.wire.sent))
        self.assertIsNotNone(await self.bot.set_gliding(False))
        self.assertAlmostEqual(self.wire.sent[-1].pos.y, 111.62, places=4)

    async def test_crawl_observes_extended_bit_and_restores_standing_with_clearance(self):
        b = self.b
        flags = b.PlayerAuthInputPacket.InputData
        # An all-air section supplies both layers, and the updates place floor and roof.
        stone = 0x80310E21
        torch = 0xB9301CF8
        await self.deliver(self.air_section(), self.block(99, stone), self.flags(low=0, high=0))
        self.assertIsNone(await self.bot.set_crawling(True))
        await self.deliver(self.block(101, torch))
        self.assertIsNone(await self.bot.set_crawling(True))
        self.assertEqual(self.wire.sent, [])
        await self.deliver(self.block(101, stone))
        start_tick = await self.bot.set_crawling(True)
        self.assertEqual(start_tick, self.wire.sent[-1].client_tick)
        self.assertIn(flags.START_CRAWLING, self.wire.sent[-1].input_data)
        self.assertFalse(self.bot.movement_state.crawling)
        await self.deliver(self.flags(actor=2, high=1 << 50))
        self.assertFalse(self.bot.movement_state.crawling)
        await self.deliver(self.flags(high=1 << 50), self.flags(low=0))
        self.assertTrue(self.bot.movement_state.crawling)
        self.assertIsNone(await self.bot.set_crawling(False))
        self.assertEqual(len(self.wire.sent), 1)
        await self.deliver(self.block(101, HASHED_AIR_RUNTIME_ID))
        stop_tick = await self.bot.set_crawling(False)
        self.assertEqual(stop_tick, self.wire.sent[-1].client_tick)
        self.assertIn(flags.STOP_CRAWLING, self.wire.sent[-1].input_data)
        self.assertTrue(self.bot.movement_state.crawling)
        await self.deliver(self.flags(high=0))
        self.assertFalse(self.bot.movement_state.crawling)
        await self.deliver(b.DisconnectPacket())
        self.assertIsNone(self.bot.movement_state.crawling)
