"""Public mining-face geometry and queued START contracts."""

import asyncio
from dataclasses import FrozenInstanceError
from math import cos, dist, radians, sin
import unittest

from endpilot import Bot, MiningAborted, MiningFaceResult, MiningFaceUnavailable
from endpilot.block_raycast import trace_blocks
from endpilot.blocks import Blocks, BlockUpdate, HASHED_AIR_RUNTIME_ID, HASHED_UNKNOWN_RUNTIME_ID, MiningTarget
from endpilot.harvest import HarvestTools
from endpilot.inventory import Inventory
from endpilot.movement import Movement

from test_blocks import PacketStream
from test_harvest import DIAMOND_ORE


TARGET = (8, 100, 8)
WALL = (6, 100, 8)
HELD = (5, 100, 9)
STONE = 0x80310E21
BOTTOM_OAK_SLAB = 2747170404
EDGE_EYE = (3.679967880249023, 100.62001037597656, 9.80350399017334)
EDGE_POINT = (8, 100.25, 8.75)
EDGE_SCREEN = (5, 100, 9)


class MiningFaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("MiningFace", protocol=2225)
        self.stream = PacketStream(self.bot.protocol)
        self.b = self.bot.protocol.binding
        self.movement = Movement(self.stream, 7)
        self.inventory = Inventory(self.stream, 7)
        self.blocks = Blocks(self.stream, self.movement, self.inventory, hashed_runtime_ids=True)
        self.bot._movement = self.movement
        self.bot._blocks = self.blocks
        self.bot._harvest_tools = HarvestTools(self.blocks, self.inventory, None, {1201: "minecraft:iron_pickaxe"})
        self.addCleanup(self.inventory.close)
        self.addCleanup(self.blocks.close)
        self.addCleanup(self.stream.subscribe_raw(self.movement._on_packet))
        self.blocks.start()

    def emit(self, packet):
        self.stream.emit(self.bot.protocol.encode(packet))

    def block(self, position, runtime_id=DIAMOND_ORE):
        x, y, z = position
        self.emit(self.b.UpdateBlockPacket(pos=self.b.BlockPos(x=x, y=y, z=z), runtime_id=runtime_id, layer=0))

    def eye(self, x=4.5, y=100.5, z=8.5):
        self.emit(self.b.MovePlayerPacket(player_id=7, pos=self.b.Vec3(x=x, y=y, z=z), on_ground=True))

    def air(self, column=0):
        b = self.b
        self.emit(b.SubChunkPacket(
            dimension_type=0, center_pos=b.SubChunkPos(x=column, y=6, z=0),
            sub_chunk_data=[b.SubChunkPacket.SubChunkPacketData(
                sub_chunk_pos_offset=b.SubChunkPacket.SubChunkPosOffset(x=0, y=0, z=0),
                result=b.SubChunkPacket.SubChunkRequestResult.SUCCESS_ALL_AIR,
            )],
        ))

    def scene(self, *, x=4.5, y=100.5, z=8.5):
        self.eye(x=x, y=y, z=z)
        self.air()
        self.block(TARGET)

    def provide_iron_pickaxe(self):
        b = self.b
        self.emit(b.InventorySlotPacket(
            inventory_id=b.ContainerID.INVENTORY, slot=0,
            item=b.SerializedNetworkItemStackDescriptor(id=1201, stack_size=1, net_id_variant=10),
        ))

    def confirm_break_on_start(self):
        send = self.stream.send

        async def accept(packet):
            await send(packet)
            for action in packet.player_block_actions or ():
                if action.player_action_type.name == "START_DESTROY_BLOCK":
                    self.block((action.pos.x, action.pos.y, action.pos.z), HASHED_AIR_RUNTIME_ID)

        self.stream.send = accept

    async def test_automatic_mining_after_a_sprint_jump_uses_the_settled_input_origin(self):
        self.scene()
        await self.bot.set_sprinting(True)
        await self.bot.jump_forward(1)
        for _ in range(12):
            await self.movement._send_input(False)
        self.assertNotEqual(self.stream.sent[-2].pos.z, self.stream.sent[-1].pos.z)
        with self.assertRaisesRegex(MiningFaceUnavailable, "unknown"):
            await self.bot.mine_block(TARGET, face=None)
        for _ in range(32):
            await self.movement._send_input(False)
        self.assertEqual(self.stream.sent[-2].pos, self.stream.sent[-1].pos)
        self.confirm_break_on_start()
        update = await self.bot.mine_block(TARGET, face=None)
        self.assertEqual(update, BlockUpdate(TARGET, HASHED_AIR_RUNTIME_ID, 0))

    async def test_read_only_geometry_and_auto_start_with_empty_hand_and_iron_pickaxe(self):
        self.scene()
        self.confirm_break_on_start()
        expected = MiningFaceResult("hit", 0, TARGET, 4, (0.0, 0.5, 0.5), 3.5)
        for with_iron in (False, True):
            with self.subTest(iron_pickaxe=with_iron):
                self.block(TARGET)
                if with_iron:
                    self.provide_iron_pickaxe()
                tool = self.bot.best_harvest_tool(TARGET)
                self.assertEqual(tool.item_name if tool else None, "minecraft:iron_pickaxe" if with_iron else None)
                await self.bot.set_rotation(13, 28)
                sent = len(self.stream.sent)
                face = self.bot.mining_face(TARGET)
                self.assertEqual(face, expected)
                self.assertEqual((self.bot.rotation, len(self.stream.sent)), ((13, 28), sent))
                with self.assertRaises(FrozenInstanceError):
                    face.face = 5

                self.assertEqual(await self.bot.mine_block(TARGET, face=None),
                                 BlockUpdate(TARGET, HASHED_AIR_RUNTIME_ID, 0))
                start, stop = self.stream.sent[sent:]
                action, = start.player_block_actions
                self.assertEqual((action.player_action_type.name, action.facing), ("START_DESTROY_BLOCK", 4))
                self.assertEqual((action.pos.x, action.pos.y, action.pos.z), TARGET)
                self.assertEqual((start.pos.x, start.pos.y, start.pos.z), (4.5, 100.5, 8.5))
                self.assertEqual((start.move.x, start.move.y, start.rot.y), (0, 0, -90))
                abort, release = stop.player_block_actions
                self.assertEqual((abort.player_action_type.name, release.player_action_type.name),
                                 ("ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK"))
                self.assertEqual((abort.pos.x, abort.pos.y, abort.pos.z), TARGET)

    async def test_replacement_samples_a_face_without_aiming_before_the_old_release(self):
        self.scene()
        self.block(HELD, STONE)
        await self.bot.set_rotation(13, 28)
        started, released, finish_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_send = self.stream.send

        async def send(packet):
            await original_send(packet)
            if len(self.stream.sent) == 1:
                started.set()
            elif len(self.stream.sent) == 2:
                released.set()
                await finish_release.wait()
            elif len(self.stream.sent) == 3:
                self.block(TARGET, HASHED_AIR_RUNTIME_ID)

        self.stream.send = send
        first = asyncio.create_task(self.bot.mine_block(HELD, face=4))
        replacing = None
        async with asyncio.timeout(3):
            try:
                await started.wait()
                self.block(WALL, STONE)
                with self.assertRaises(MiningFaceUnavailable):
                    await self.bot.mine_block(TARGET, face=None, replace_current=True)
                self.assertEqual(self.bot.mining_target, MiningTarget(HELD, 4))
                self.assertEqual((len(self.stream.sent), self.bot.rotation), (1, (13, 28)))
                self.block(WALL, HASHED_AIR_RUNTIME_ID)
                replacing = asyncio.create_task(self.bot.mine_block(TARGET, face=None, replace_current=True))
                await released.wait()
                self.assertEqual((len(self.stream.sent), self.bot.rotation), (2, (13, 28)))
                finish_release.set()
                with self.assertRaises(MiningAborted):
                    await first
                self.assertEqual(await replacing, BlockUpdate(TARGET, HASHED_AIR_RUNTIME_ID, 0))
                start = self.stream.sent[2]
                action, = start.player_block_actions
                self.assertEqual((action.player_action_type.name, action.facing), ("START_DESTROY_BLOCK", 4))
                self.assertEqual((action.pos.x, action.pos.y, action.pos.z), TARGET)
                self.assertEqual((start.rot.x, start.rot.y), (0, -90))
            finally:
                finish_release.set()
                tasks = (first,) if replacing is None else (first, replacing)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_half_slab_covers_five_samples_but_leaves_an_unsampled_gap(self):
        self.scene(y=100.15)
        self.block(WALL, BOTTOM_OAK_SLAB)
        sample = self.bot.mining_face(TARGET)
        self.assertEqual(sample, MiningFaceResult("no_sample", 0, TARGET))
        actual = self.bot.raycast_block_towards((8, 100.99, 8.5), max_distance=4.5)
        self.assertEqual((actual.status, actual.position, actual.face), ("hit", TARGET, 4))
        with self.assertRaises(MiningFaceUnavailable) as rejected:
            await self.bot.mine_block(TARGET, face=None)
        self.assertEqual(rejected.exception.status, "no_sample")
        self.assertEqual(self.stream.sent, [])

        self.block(WALL, STONE)
        self.assertEqual(self.bot.mining_face(TARGET), MiningFaceResult("excluded", 0, TARGET))

    async def test_unknown_air_and_whole_box_distance(self):
        self.eye()
        self.assertEqual(self.bot.mining_face(TARGET), MiningFaceResult("unknown", 0, TARGET))
        self.scene(x=3.5)
        self.assertEqual(self.bot.mining_face(TARGET).distance, 4.5)
        self.eye(x=3.25)
        self.assertEqual(self.bot.mining_face(TARGET), MiningFaceResult("excluded", 0, TARGET))
        self.block(TARGET, HASHED_AIR_RUNTIME_ID)
        self.assertEqual(self.bot.mining_face(TARGET), MiningFaceResult("excluded", 0, TARGET))
        self.eye(x=14.5)
        other = (17, 100, 8)
        self.block(other)
        self.assertEqual(self.bot.mining_face(other), MiningFaceResult("unknown", 0, other))
        self.assertEqual(self.stream.sent, [])

    async def test_replacement_while_queued_does_not_release_another_target(self):
        self.scene()
        self.block(HELD, STONE)
        held = MiningTarget(HELD, 4)
        await self.bot.start_mining(HELD, face=4)
        for lock in (self.blocks._mining_lock, self.movement._action_lock):
            with self.subTest(lock=lock):
                self.block(TARGET)
                frames = len(self.stream.sent)
                async with lock:
                    waiting = asyncio.create_task(self.bot.mine_block(TARGET, face=None))
                    await asyncio.sleep(0)
                    self.assertFalse(waiting.done())
                    self.block(TARGET, STONE)
                with self.assertRaises(MiningFaceUnavailable) as rejected:
                    await waiting
                self.assertEqual(rejected.exception.status, "target_changed")
                self.assertEqual((self.bot.mining_target, len(self.stream.sent)), (held, frames))

    async def test_occlusion_before_start_and_cancellation_preserve_aim_and_holder(self):
        self.scene()
        self.block(HELD, STONE)
        await self.bot.start_mining(HELD, face=4)
        held = self.bot.mining_target
        await self.bot.set_rotation(13, 28)
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                self.block(WALL, HASHED_AIR_RUNTIME_ID)
                frames = len(self.stream.sent)
                async with self.movement._action_lock:
                    waiting = asyncio.create_task(self.bot.mine_block(TARGET, face=None))
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    self.assertFalse(waiting.done())
                    if cancel:
                        waiting.cancel()
                    else:
                        self.block(WALL, STONE)
                if cancel:
                    with self.assertRaises(asyncio.CancelledError):
                        await waiting
                else:
                    with self.assertRaises(MiningFaceUnavailable) as rejected:
                        await waiting
                    self.assertEqual(rejected.exception.status, "no_sample")
                self.assertEqual((self.bot.rotation, len(self.stream.sent), self.bot.mining_target),
                                 ((13, 28), frames, held))

    async def test_default_and_explicit_mining_faces_do_not_aim_automatically(self):
        self.scene()
        self.confirm_break_on_start()
        await self.bot.set_rotation(10, 15)
        for options, facing in (({}, 1), ({"face": 3}, 3)):
            self.block(TARGET)
            sent = len(self.stream.sent)
            await self.bot.mine_block(TARGET, **options)
            action, = self.stream.sent[sent].player_block_actions
            self.assertEqual((action.facing, self.bot.rotation), (facing, (10, 15)))

    async def test_phased_mining_sends_a_reachable_edge_with_the_callers_aim(self):
        x, y, z = EDGE_EYE
        self.scene(x=x, y=y, z=z)
        self.assertGreater(dist(EDGE_EYE, (8, 100.5, 8.5)), 4.5)
        self.assertLess(dist(EDGE_EYE, EDGE_POINT), 4.5)
        aim = await self.bot.look_at(EDGE_POINT)
        codec = self.bot.protocol

        for begin in (self.bot.start_mining, self.bot.continue_mining):
            with self.subTest(begin=begin.__name__):
                sent = len(self.stream.sent)
                start_tick = await begin(TARGET, face=4)
                continue_tick = await self.bot.continue_mining(TARGET, face=4)
                start, continued = self.stream.sent[sent:]
                for packet, expected_action, tick in ((start, "START_DESTROY_BLOCK", start_tick),
                                                      (continued, "CONTINUE_DESTROY_BLOCK", continue_tick)):
                    frame = codec.decode(codec.encode(packet))
                    action, = frame.player_block_actions
                    self.assertEqual((frame.client_tick, action.player_action_type.name, action.facing),
                                     (tick, expected_action, 4))
                    self.assertEqual((action.pos.x, action.pos.y, action.pos.z), TARGET)
                    self.assertEqual((frame.move.x, frame.move.y), (0, 0))
                    hit = trace_blocks((frame.pos.x, frame.pos.y, frame.pos.z), (frame.rot.x, frame.rot.y),
                                       dimension_id=0, max_distance=4.5, outline_at=self.blocks._outline_at)
                    self.assertEqual((hit.status, hit.position, hit.face), ("hit", TARGET, 4))
                    for actual, expected in zip(hit.click_pos, (0, 0.25, 0.75), strict=True):
                        self.assertAlmostEqual(actual, expected, places=6)
                self.assertEqual((self.bot.rotation, self.bot.mining_target), (aim, MiningTarget(TARGET, 4)))
                await self.bot.stop_mining()

    async def test_phased_mining_rejects_unproven_edges(self):
        _, y, z = EDGE_EYE
        for label, x, face, screen, target, expected in (
            ("far", 3.5, 4, HASHED_AIR_RUNTIME_ID, DIAMOND_ORE, "miss"),
            ("other face", EDGE_EYE[0], 2, HASHED_AIR_RUNTIME_ID, DIAMOND_ORE, "hit"),
            ("blocked", EDGE_EYE[0], 4, STONE, DIAMOND_ORE, "hit"),
            ("unknown sightline", EDGE_EYE[0], 4, HASHED_UNKNOWN_RUNTIME_ID, DIAMOND_ORE, "unknown"),
            ("unknown target", EDGE_EYE[0], 4, HASHED_AIR_RUNTIME_ID, HASHED_UNKNOWN_RUNTIME_ID, "unknown"),
            ("removed target", EDGE_EYE[0], 4, HASHED_AIR_RUNTIME_ID, HASHED_AIR_RUNTIME_ID, "miss"),
        ):
            with self.subTest(case=label):
                self.scene(x=x, y=y, z=z)
                self.block(EDGE_SCREEN, screen)
                self.block(TARGET, target)
                aim = await self.bot.look_at(EDGE_POINT)
                hit = self.bot.raycast_block(max_distance=4.5)
                self.assertEqual(hit.status, expected)
                self.assertNotEqual((hit.position, hit.face), (TARGET, face))
                reason = "outside ordinary reach" if target == DIAMOND_ORE else "observed non-air"
                for action in (self.bot.start_mining, self.bot.continue_mining):
                    with self.assertRaisesRegex(ValueError, reason):
                        await action(TARGET, face=face)
                self.assertEqual((self.bot.rotation, self.bot.mining_target, self.stream.sent), (aim, None, []))

    async def test_phased_mining_rechecks_a_held_edge_after_waiting_for_movement(self):
        x, y, z = EDGE_EYE
        self.scene(x=x, y=y, z=z)
        # Establish a holder using the existing center-distance path.
        self.eye(x=4, y=y, z=z)
        await self.bot.start_mining(TARGET, face=4)
        self.eye(x=x, y=y, z=z)
        held = MiningTarget(TARGET, 4)
        frames = len(self.stream.sent)
        aim = await self.bot.look_at(EDGE_POINT)

        for action in (self.bot.start_mining, self.bot.continue_mining):
            with self.subTest(action=action.__name__):
                self.block(EDGE_SCREEN, HASHED_AIR_RUNTIME_ID)
                self.assertEqual(self.bot.raycast_block(max_distance=4.5).position, TARGET)
                async with self.movement._action_lock:
                    waiting = asyncio.create_task(action(TARGET, face=4))
                    await asyncio.sleep(0)
                    self.assertFalse(waiting.done())
                    self.block(EDGE_SCREEN, STONE)
                with self.assertRaisesRegex(ValueError, "mining target is outside ordinary reach"):
                    await waiting
                self.assertEqual((self.bot.rotation, self.bot.mining_target, len(self.stream.sent)),
                                 (aim, held, frames))

    async def test_auto_start_uses_an_alternate_sample_when_center_rounds_out_of_reach(self):
        self.scene(x=10.5, y=103.5, z=11.5)
        self.confirm_break_on_start()
        center = self.bot.mining_face(TARGET)
        self.assertEqual((center.face, center.click_pos, center.distance), (5, (1, 0.5, 0.5), 4.5))
        self.assertEqual(await self.bot.mine_block(TARGET, face=None), BlockUpdate(TARGET, HASHED_AIR_RUNTIME_ID, 0))
        codec = self.bot.protocol
        start = codec.decode(codec.encode(self.stream.sent[0]))
        action, = start.player_block_actions
        self.block(TARGET)
        self.assertEqual((start.pos.x, start.pos.y, start.pos.z), (10.5, 103.5, 11.5))
        self.assertEqual((start.move.x, start.move.y), (0, 0))
        wire_hit = trace_blocks(
            (start.pos.x, start.pos.y, start.pos.z), (start.rot.x, start.rot.y),
            dimension_id=0, max_distance=4.5, outline_at=self.blocks._outline_at,
        )
        self.assertEqual((wire_hit.status, wire_hit.position, wire_hit.face), ("hit", TARGET, action.facing))
        self.assertNotEqual(wire_hit.click_pos, center.click_pos)
        eye = (start.pos.x, start.pos.y, start.pos.z)
        pitch, yaw = radians(start.rot.x), radians(start.rot.y)
        direction = (-sin(yaw) * cos(pitch), -sin(pitch), cos(yaw) * cos(pitch))
        axis = (1, 2, 0)[action.facing // 2]
        distance = (TARGET[axis] + action.facing % 2 - eye[axis]) / direction[axis]
        self.assertTrue(0 < distance <= 4.5)
        for cell, origin, slope in zip(TARGET, eye, direction, strict=True):
            self.assertLessEqual(cell, origin + slope * distance)
            self.assertLessEqual(origin + slope * distance, cell + 1)

    async def test_queued_eye_shift_rejects_an_uncodable_sample_before_aim_or_release(self):
        self.scene(x=10.5, y=103.4, z=11.5)
        held = (12, 100, 11)
        self.block(held, STONE)
        await self.bot.start_mining(held)
        before = len(self.stream.sent)
        await self.bot.set_rotation(13, 28)
        async with self.movement._action_lock:
            waiting = asyncio.create_task(self.bot.mine_block(TARGET, face=None, timeout=2))
            await asyncio.sleep(0)
            self.assertFalse(waiting.done())
            self.eye(x=10.5, y=103.5, z=11.5)
        with self.assertRaises(MiningFaceUnavailable) as rejected:
            await waiting
        self.assertEqual(rejected.exception.status, "no_sample")
        self.assertEqual((len(self.stream.sent), self.bot.rotation, self.bot.mining_target),
                         (before, (13, 28), MiningTarget(held, 1)))

    async def test_column_unload_while_queued_reports_unknown_and_preserves_a_holder(self):
        self.scene(x=14.5)
        hidden, held = (17, 100, 8), (13, 100, 9)
        self.air(1)
        self.block(hidden)
        self.block(held, STONE)
        await self.bot.start_mining(held, face=5)
        before = len(self.stream.sent)
        await self.bot.set_rotation(13, 28)
        for unload in (False, True):
            with self.subTest(unload=unload):
                self.block(hidden)
                async with self.movement._action_lock:
                    waiting = asyncio.create_task(self.bot.mine_block(hidden, face=None, timeout=2))
                    await asyncio.sleep(0)
                    self.assertFalse(waiting.done())
                    if unload:
                        b = self.b
                        self.emit(b.NetworkChunkPublisherUpdatePacket(
                            position=b.BlockPos(x=0, y=100, z=0), radius=15,
                        ))
                        self.assertIsNone(self.blocks.observed_block(hidden))
                    else:
                        self.block(hidden, HASHED_UNKNOWN_RUNTIME_ID)
                        self.assertIsNone(self.blocks.observed_block(hidden).is_air)
                    self.assertIsNotNone(self.blocks.observed_block(held))
                with self.assertRaises(MiningFaceUnavailable) as rejected:
                    await waiting
                self.assertEqual(rejected.exception.status, "unknown")
                self.assertEqual((len(self.stream.sent), self.bot.rotation, self.bot.mining_target),
                                 (before, (13, 28), MiningTarget(held, 5)))
