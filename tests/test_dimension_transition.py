import asyncio
import unittest
from unittest.mock import patch

from endpilot import Bot, DimensionTransition
from endpilot.protocol import write_batch
from endpilot.qa import PacketTimeline
from test_scheduled_input import ManualClock, Wire


class DimensionTransitionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Bot("DimensionTraveler")
        self.b = self.bot.protocol.binding
        self.clock = ManualClock()
        self.enterContext(self.clock.patch_scheduler())
        self.addAsyncCleanup(self.bot.close)

    async def join(self, *early, timeline=None, dimension=0):
        b = self.b
        self.wire = Wire(self.bot.protocol)
        self.wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.push(b.StartGamePacket(runtime_id=7, entity_id=4, block_network_ids_are_hashes=True,
                                   settings=b.LevelSettings(spawn_settings=b.SpawnSettings(dimension=dimension))),
                  *early, b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeline=timeline)

    def push(self, *packets):
        self.wire.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in packets))

    def change(self, dimension, screen_id, *, y=70, respawn=False):
        return self.b.ChangeDimensionPacket(dimension_id=dimension, pos=self.b.Vec3(x=10, y=y, z=8),
                                            loading_screen_id=screen_id, respawn=respawn)

    def credits(self, player_id=7, *, finished=False):
        state = self.b.ShowCreditsPacket.CreditsState
        return self.b.ShowCreditsPacket(player_id=player_id, credits_state=state.FINISHED if finished else state.START)

    def ack(self, runtime_id=7):
        return self.b.PlayerActionPacket(runtime_id=runtime_id, action=self.b.PlayerActionType.CHANGE_DIMENSION_ACK,
                                         pos=self.b.BlockPos(), result_pos=self.b.BlockPos(), face=0)

    def moved(self, x, *, reset_position=1):
        return self.b.MovePlayerPacket(player_id=7, pos=self.b.Vec3(x=x, y=72, z=8),
                                      reset_position=reset_position, on_ground=True)

    def post_move(self, x):
        return self.b.ServerPlayerPostMovePositionPacket(pos=self.b.Vec3(x=x, y=72, z=8))

    async def observe(self, packet):
        waiting = asyncio.create_task(self.bot.packets.wait_for_packet(type(packet).ID, timeout=1))
        await asyncio.sleep(0)
        self.push(packet)
        await waiting

    def screens(self):
        return [(packet.serverbound_loading_screen_packet_type.name, packet.loading_screen_id)
                for packet in self.wire.sent if isinstance(packet, self.b.ServerboundLoadingScreenPacket)]

    def inputs(self):
        return [packet for packet in self.wire.sent if isinstance(packet, self.b.PlayerAuthInputPacket)]

    async def test_own_ack_completes_loading_and_input_uses_the_new_position(self):
        await self.join()
        movement = self.bot._movement
        self.assertIsNone(movement.observed_feet_position)
        self.assertFalse(self.bot.is_changing_dimension)
        self.assertEqual(self.screens(), [("START_LOADING_SCREEN", None), ("END_LOADING_SCREEN", None)])
        await self.observe(self.moved(1, reset_position=2))
        self.assertFalse(self.bot.is_changing_dimension)
        feet = (1, 72 - 1.62, 8)
        self.assertEqual(movement.observed_feet_position, feet)
        await self.bot.set_move_vector(0, 1)
        await self.clock.wake_at(50)
        self.assertEqual(len(self.inputs()), 1)
        self.assertGreater(self.bot.movement_state.feet_position[2], feet[2])
        self.assertEqual(movement.observed_feet_position, feet)
        wait = asyncio.create_task(self.bot.wait_for_dimension_change(timeout=1))
        await asyncio.sleep(0)
        await self.observe(self.change(1, 0))
        opening = DimensionTransition(1, (10, 70, 8), False, 0)
        self.assertEqual(self.bot.active_dimension_change, opening)
        self.assertTrue(self.bot.is_changing_dimension)
        self.assertEqual(self.bot.position, (10, 70, 8))
        self.assertIsNone(movement.observed_feet_position)
        await self.observe(self.b.NetworkChunkPublisherUpdatePacket())
        await self.observe(self.ack(17))
        await self.observe(self.moved(24))
        await self.observe(self.post_move(26))
        self.assertTrue(self.bot.is_changing_dimension)
        self.assertIsNone(movement.observed_feet_position)
        self.assertFalse(wait.done())
        await self.clock.wake_at(100)
        self.assertEqual(self.bot.get_tick(), 2)
        self.assertEqual(len(self.inputs()), 1)
        await self.observe(self.ack())
        self.assertEqual(await wait, opening)
        self.assertFalse(self.bot.is_changing_dimension)
        self.assertEqual(movement.observed_feet_position, (26, 72 - 1.62, 8))
        self.assertEqual(self.screens()[-2:], [("START_LOADING_SCREEN", 0), ("END_LOADING_SCREEN", 0)])
        self.assertEqual(self.wire.sent[-1], self.ack())
        await self.clock.wake_at(150)
        first = self.inputs()[-1]
        self.assertEqual((first.pos.x, first.pos.y, first.pos.z), (26, 72, 8))
        self.assertEqual(len(self.inputs()), 2)

        await self.observe(self.change(0, 1))
        await self.observe(self.ack())
        self.assertFalse(self.bot.is_changing_dimension)
        self.assertEqual(self.bot.position, (10, 70, 8))
        self.assertIsNone(movement.observed_feet_position)

    async def test_explicit_input_is_rejected_until_own_ack(self):
        await self.join()
        await self.observe(self.moved(1))
        await self.observe(self.change(1, 0))
        await self.observe(self.ack(17))
        for action, args in ((self.bot.jump, ()), (self.bot.set_sneaking, (True,))):
            with self.subTest(action=action.__name__):
                with self.assertRaisesRegex(RuntimeError, "dimension transition"):
                    await action(*args)
        self.assertFalse(self.bot.movement_state.sneaking)
        self.assertEqual(self.inputs(), [])
        await self.observe(self.post_move(26))
        await self.observe(self.ack())
        self.assertEqual(self.wire.sent[-1], self.ack())
        tick = await self.bot.jump()
        frame, = self.inputs()
        self.assertEqual((frame.client_tick, frame.pos.x, frame.pos.z), (tick, 26, 8))
        self.assertIn(self.b.PlayerAuthInputPacket.InputData.JUMP_PRESSED_RAW, frame.input_data)

    async def test_first_input_uses_post_move_position_received_before_spawn(self):
        timeline = PacketTimeline()
        await self.join(self.change(1, 0), self.moved(24), self.post_move(26), self.ack(), timeline=timeline)
        self.assertFalse(self.bot.is_changing_dimension)
        self.assertEqual(self.bot.position, (26, 72, 8))
        self.assertEqual(timeline.received_count("ServerPlayerPostMovePositionPacket"), 1)
        await self.clock.wake_at(50)
        frame, = self.inputs()
        self.assertEqual((frame.pos.x, frame.pos.y, frame.pos.z), (26, 72, 8))

    async def test_startup_then_live_dimension_changes_preserve_wire_order(self):
        b = self.b
        timeline = PacketTimeline()
        await self.join(self.change(1, 0), self.moved(20), self.ack(), self.change(0, 1), timeline=timeline)
        self.assertEqual(self.screens(), [
            ("START_LOADING_SCREEN", 0), ("END_LOADING_SCREEN", 0), ("START_LOADING_SCREEN", 1),
            ("START_LOADING_SCREEN", None), ("END_LOADING_SCREEN", None),
        ])
        self.assertEqual(self.bot.active_dimension_change.dimension_id, 0)
        self.assertEqual(self.bot.position, (10, 70, 8))
        wait = asyncio.create_task(self.bot.wait_for_dimension_change(timeout=1))
        await asyncio.sleep(0)
        moved = self.moved(30)
        self.push(moved, self.ack())
        self.assertEqual((await wait).loading_screen_id, 1)
        self.assertEqual(self.bot.position, (30, 72, 8))
        self.assertEqual(timeline.received_count("ChangeDimensionPacket"), 2)
        self.assertEqual(timeline.received_count("PlayerActionPacket"), 2)
        sent_actions = [packet for packet in self.wire.sent if isinstance(packet, b.PlayerActionPacket)]
        self.assertEqual(sent_actions, [self.ack(), self.ack()])

    async def test_no_screen_id_does_not_put_the_loading_position_into_movement(self):
        b = self.b
        await self.join()
        movement = self.bot._movement
        await self.observe(self.moved(1))
        self.assertEqual(movement.observed_feet_position, (1, 72 - 1.62, 8))
        await self.observe(b.RespawnPacket(state=b.PlayerRespawnState.SEARCHING_FOR_SPAWN, runtime_id=0))
        self.assertIsNone(movement.observed_feet_position)
        await self.observe(self.change(0, None, y=32767))
        wait = asyncio.create_task(self.bot.wait_for_dimension_change(timeout=1))
        await asyncio.sleep(0)
        await self.observe(self.ack())
        self.assertEqual(await wait, DimensionTransition(0, (10, 32767, 8), False, None))
        self.assertEqual(self.screens(), [("START_LOADING_SCREEN", None), ("END_LOADING_SCREEN", None)])
        self.assertEqual(self.wire.sent[-1], self.ack())
        await self.observe(self.post_move(10))
        await self.observe(self.moved(12))
        self.assertIsNone(movement.observed_feet_position)
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(0):
                await self.bot.jump()
        await self.clock.wake_at(50)
        self.assertEqual(self.inputs(), [])
        await self.observe(b.RespawnPacket(pos=b.Vec3(x=4, y=72, z=8), state=b.PlayerRespawnState.READY_TO_SPAWN,
                                           runtime_id=0))
        self.assertEqual(movement.observed_feet_position, (4, 72 - 1.62, 8))
        await self.clock.wake_at(100)
        self.assertEqual(self.inputs()[0].pos.x, 4)

    async def test_disconnect_wakes_only_this_sessions_waiters(self):
        await self.join()
        await self.observe(self.change(1, 0))
        wait = asyncio.create_task(self.bot.wait_for_dimension_change(timeout=None))
        await asyncio.sleep(0)
        await self.observe(self.b.DisconnectPacket())
        with self.assertRaises(ConnectionError):
            await wait
        self.assertFalse(self.bot.is_changing_dimension)
        await self.bot.close()
        await self.join()
        self.assertFalse(self.bot.is_changing_dimension)
        await self.observe(self.ack())
        self.assertEqual(self.screens(), [("START_LOADING_SCREEN", None), ("END_LOADING_SCREEN", None)])
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_dimension_change(timeout=0)

    async def test_end_credits_finish_once_and_wait_for_the_real_return_exchange(self):
        b = self.b
        await self.join()
        before = len(self.wire.sent)
        await self.observe(self.credits())
        self.assertEqual(len(self.wire.sent), before)
        await self.observe(self.change(2, 0))
        during_entry = len(self.wire.sent)
        await self.observe(self.credits())
        self.assertEqual(len(self.wire.sent), during_entry)
        await self.observe(self.ack())
        await self.observe(self.moved(24, reset_position=2))

        future = asyncio.create_task(self.bot.wait_for_dimension_change(timeout=1))
        await asyncio.sleep(0)
        before = len(self.wire.sent)
        await self.observe(self.credits())
        await self.observe(self.credits())
        self.assertEqual(self.wire.sent[before:], [self.credits(finished=True)])
        self.assertIsNone(self.bot.active_dimension_change)
        self.assertFalse(future.done())
        self.assertEqual(self.bot.dimension_bounds.dimension_id, 2)
        self.assertEqual(self.bot.position, (24, 72, 8))

        await self.observe(b.RespawnPacket(state=b.PlayerRespawnState.SEARCHING_FOR_SPAWN, runtime_id=0))
        await self.observe(self.change(0, 1, respawn=True))
        await self.observe(self.credits())
        await self.observe(self.ack(17))
        self.assertFalse(future.done())
        await self.observe(self.ack())
        self.assertEqual(await future, DimensionTransition(0, (10, 70, 8), True, 1))
        await self.observe(b.RespawnPacket(pos=b.Vec3(x=8, y=71.62, z=8), state=b.PlayerRespawnState.READY_TO_SPAWN,
                                           runtime_id=0))
        await self.observe(self.moved(8, reset_position=2))
        await self.observe(self.credits())
        sent = self.wire.sent[before:]
        self.assertEqual([type(packet) for packet in sent], [
            b.ShowCreditsPacket, b.ServerboundLoadingScreenPacket,
            b.ServerboundLoadingScreenPacket, b.PlayerActionPacket,
        ])
        self.assertEqual(sent[0], self.credits(finished=True))
        self.assertEqual(self.screens()[-2:], [("START_LOADING_SCREEN", 1), ("END_LOADING_SCREEN", 1)])
        self.assertEqual(sent[-1], self.ack())

    async def test_end_entry_rearms_credits_without_rearming_a_same_dimension_update(self):
        await self.join(dimension=2)
        await self.observe(self.credits())
        await self.observe(self.change(2, None))
        await self.observe(self.ack())
        await self.observe(self.credits())
        self.assertEqual([p for p in self.wire.sent if isinstance(p, self.b.ShowCreditsPacket)],
                         [self.credits(finished=True)])
        await self.observe(self.change(0, None))
        await self.observe(self.ack())
        await self.observe(self.change(2, None))
        await self.observe(self.ack())
        await self.observe(self.credits())
        self.assertEqual([p for p in self.wire.sent if isinstance(p, self.b.ShowCreditsPacket)],
                         [self.credits(finished=True)] * 2)

    async def test_end_credits_ignore_pre_spawn_foreign_finished_and_closed_session_packets(self):
        await self.join(self.credits(), dimension=2)
        self.assertFalse(any(isinstance(p, self.b.ShowCreditsPacket) for p in self.wire.sent))
        before = len(self.wire.sent)
        await self.observe(self.credits(17))
        await self.observe(self.credits(finished=True))
        self.assertEqual(len(self.wire.sent), before)
        await self.observe(self.credits())
        self.assertEqual(self.wire.sent[before:], [self.credits(finished=True)])
        session, original = self.bot._session, self.wire
        await self.observe(self.b.DisconnectPacket())
        await self.bot.close()
        before = len(original.sent)
        await session._receive_dimension_packet(self.bot.protocol.encode(self.credits()))
        self.assertEqual(len(original.sent), before)
        await self.join(dimension=2)
        await self.observe(self.credits())
        self.assertEqual([p for p in self.wire.sent if isinstance(p, self.b.ShowCreditsPacket)],
                         [self.credits(finished=True)])

    async def test_legacy_protocol_keeps_credits_unanswered(self):
        self.bot = Bot("LegacyDimensionTraveler", protocol=2193)
        self.addAsyncCleanup(self.bot.close)
        self.b = self.bot.protocol.binding
        await self.join(dimension=2)
        before = len(self.wire.sent)
        await self.observe(self.credits())
        self.assertEqual(len(self.wire.sent), before)
