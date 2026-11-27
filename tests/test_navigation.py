"""Known-flat-ground route selection and input release behavior."""
import asyncio
from dataclasses import FrozenInstanceError
from fractions import Fraction
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

from bedrock_protocol import nbt

from endpilot import (
    AllGoal, AnyGoal, BlockApproach, BlockFaceGoal, BlockFaceMatch, BlockInteractionGoal, BlockInteractionMatch,
    BlockPlacementGoal, BlockPlacementMatch, Bot, DimensionTransition, EntityNearGoal, EntityVisibilityGoal,
    FootGoal, NavigationCosts, NavigationResult, NavigationRules, NearGoal, NearXZGoal, XZGoal, YGoal,
)
from endpilot.blocks import Blocks
from endpilot.compression import PacketConnection
from endpilot.containers import Containers
from endpilot.dimension_transition import _DimensionTransitions
from endpilot.entities import Entities
from endpilot.inventory import Inventory
from endpilot.movement import Movement
from endpilot.player_state import PlayerState
from endpilot.protocol import Protocol, RawPacket
from endpilot.trading import Trading
from test_scheduled_input import ManualClock


class BlocksSnapshot:
    def __init__(self, blocked=(), unknown=()):
        self.blocked = set(blocked)
        self.unknown = set(unknown)

    def observed_block(self, pos):
        if pos in self.unknown:
            return None
        x, y, z = pos
        if y == 99:
            air = False
        elif y in (100, 101):
            air = pos not in self.blocked
        else:
            return None
        return SimpleNamespace(is_air=air)

    def close(self):
        pass


class Sink:
    _subscribe = staticmethod(PacketConnection._subscribe)
    _notify = staticmethod(PacketConnection._notify)

    def __init__(self, protocol=2193):
        self.protocol = Protocol(protocol)
        self.sent = []
        self.observer = None
        self.on_send = None

    def subscribe_raw(self, callback):
        self.observer = callback
        return lambda: None

    def start_receiving(self):
        pass

    async def send(self, packet, *, _before_send=None):
        if _before_send is not None:
            _before_send()
        self.sent.append(packet)
        if self.on_send is not None:
            self.on_send()


class AxisGoalValidationTests(unittest.TestCase):
    def test_axis_goals_snapshot_integer_coordinates_and_validate_radius(self):
        column = [-2, 3]
        exact, near = XZGoal(column), NearXZGoal(column, 5)
        column[0] = 1
        self.assertEqual(exact.position, (-2, 3))
        self.assertEqual((near.position, near.radius), ((-2, 3), 5.0))
        with self.assertRaises(FrozenInstanceError):
            exact.position = (0, 0)
        with self.assertRaises(FrozenInstanceError):
            near.radius = 0
        height = YGoal(-12)
        with self.assertRaises(FrozenInstanceError):
            height.y = 0
        for position in ((0,), (0, 1, 2), (0.0, 1)):
            with self.subTest(position=position):
                with self.assertRaisesRegex(ValueError, "two integer"):
                    XZGoal(position)
                with self.assertRaisesRegex(ValueError, "two integer"):
                    NearXZGoal(position, 0)
        for radius in (-1, math.inf, math.nan):
            with self.subTest(radius=radius), self.assertRaisesRegex(ValueError, "finite and nonnegative"):
                NearXZGoal((0, 0), radius)
        with self.assertRaisesRegex(ValueError, "integer feet block height"):
            YGoal(100.0)
        with self.assertRaisesRegex(TypeError, "navigation goal"):
            AllGoal((XZGoal((0, 0)), (0, 100, 0)))


class EntityVisibilityGoalValidationTests(unittest.TestCase):
    def test_aim_offset_is_immutable_and_configuration_is_validated(self):
        offset = [0, 1.5, -0.25]
        goal = EntityVisibilityGoal(-21, aim_offset=offset, max_distance=0)
        offset[0] = 1
        self.assertEqual((goal.entity_id, goal.aim_offset, goal.max_distance), (-21, (0.0, 1.5, -0.25), 0.0))
        with self.assertRaises(FrozenInstanceError):
            goal.max_distance = 1
        with self.assertRaises(TypeError):
            EntityVisibilityGoal(-21, offset, 2)
        for entity_id in (0, 2.5):
            with self.subTest(entity_id=entity_id), self.assertRaisesRegex(ValueError, "actor unique ID"):
                EntityVisibilityGoal(entity_id, aim_offset=offset, max_distance=2)
        for invalid in ((1, 2), (1, 2, 3, 4), (0, math.inf, 0), (math.nan, 0, 0)):
            with self.subTest(offset=invalid), self.assertRaisesRegex(ValueError, "three finite"):
                EntityVisibilityGoal(-21, aim_offset=invalid, max_distance=2)
        for distance in (-1, math.inf, math.nan):
            with self.subTest(distance=distance), self.assertRaisesRegex(ValueError, "finite and nonnegative"):
                EntityVisibilityGoal(-21, aim_offset=offset, max_distance=distance)


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_detours_around_observed_obstacle_and_releases_input(self):
        stream = Sink()
        movement = Movement(stream, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        blocks = BlocksSnapshot(blocked={(1, 100, 0), (1, 101, 0)})
        count = await movement.navigate_to(blocks, (3, 100, 0), ticks=100)
        self.assertGreater(count, 35)
        self.assertLess(count, 100)
        b = stream.protocol.binding
        moving = stream.sent[:count]
        self.assertTrue(all(b.PlayerAuthInputPacket.InputData.UP in p.input_data
                            for p in moving))
        self.assertEqual([list(p.input_data) for p in stream.sent[-2:]], [[], []])
        self.assertEqual([p.client_tick for p in stream.sent],
                         list(range(1, len(stream.sent) + 1)))
        self.assertGreater(max(p.pos.z for p in moving), 1.3)
        self.assertTrue(all(not (1.0 <= p.pos.x < 2.0 and p.pos.z < 1.0)
                            for p in moving))
        self.assertAlmostEqual(stream.sent[-1].pos.x, 3.5, delta=0.15)
        self.assertAlmostEqual(stream.sent[-1].pos.z, 0.5, delta=0.15)
        self.assertEqual(movement.position, (0.5, 101.62, 0.5))

    async def test_unknown_terrain_never_becomes_air_or_input(self):
        stream = Sink()
        movement = Movement(stream, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        blocks = BlocksSnapshot(unknown={(1, 100, 0), (0, 100, 1),
                                         (0, 100, -1), (-1, 100, 0)})
        with self.assertRaisesRegex(RuntimeError, "no route"):
            await movement.navigate_to(blocks, (2, 100, 0), search_radius=2)
        self.assertEqual(stream.sent, [])

    async def test_explicit_stop_releases_navigation_and_allows_new_walk(self):
        stream = Sink()
        movement = Movement(stream, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        task = asyncio.create_task(movement.navigate_to(
            BlocksSnapshot(), (5, 100, 0), ticks=160
        ))
        await asyncio.sleep(0.15)
        await movement.stop_moving()
        count = await task
        self.assertGreater(count, 0)
        self.assertLess(count, 160)
        self.assertEqual([list(p.input_data) for p in stream.sent[-2:]], [[], []])
        self.assertEqual(await movement.walk_forward(1), 1)

    async def test_bot_close_drains_active_navigation_and_queued_input(self):
        bot = Bot("ClosingNavigator")
        stream = Sink()
        movement = Movement(stream, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        movement.start()
        ticker = movement._ticker
        bot._movement = movement
        bot._blocks = BlocksSnapshot()

        class Session:
            closed = False

            async def close(self):
                self.closed = True

        session = Session()
        bot._session = session
        moving = asyncio.Event()
        up = stream.protocol.binding.PlayerAuthInputPacket.InputData.UP

        def on_send():
            if up in stream.sent[-1].input_data:
                moving.set()

        stream.on_send = on_send
        navigation = asyncio.create_task(bot.navigate_to((7, 100, 0), ticks=180))
        await asyncio.wait_for(moving.wait(), 2)
        queued_jump = asyncio.create_task(bot.jump())
        await asyncio.sleep(0)
        await asyncio.wait_for(bot.close(), 2)

        frames = await asyncio.wait_for(navigation, 2)
        self.assertGreater(frames, 0)
        self.assertLess(frames, 180)
        with self.assertRaisesRegex(ConnectionError, "movement is closed"):
            await queued_jump
        self.assertTrue(session.closed)
        self.assertTrue(ticker.done())
        self.assertIsNone(movement._active_done)
        sent_at_close = len(stream.sent)
        await asyncio.sleep(0.1)
        self.assertEqual(len(stream.sent), sent_at_close)
        with self.assertRaises(RuntimeError):
            await bot.navigate_to((1, 100, 0))

    async def test_cancelling_public_navigation_releases_input_for_next_action(self):
        bot = Bot("CancelNavigator")
        stream = Sink()
        movement = Movement(stream, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        movement.start()
        ticker = movement._ticker
        bot._movement = movement
        bot._blocks = BlocksSnapshot()
        moving = asyncio.Event()
        up = stream.protocol.binding.PlayerAuthInputPacket.InputData.UP

        def on_send():
            if up in stream.sent[-1].input_data:
                moving.set()

        stream.on_send = on_send
        try:
            task = asyncio.create_task(bot.navigate_to((7, 100, 0), ticks=180))
            await asyncio.wait_for(moving.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            self.assertIsNone(movement._active_done)
            self.assertEqual([list(p.input_data) for p in stream.sent[-2:]], [[], []])
            moving_frames = sum(up in p.input_data for p in stream.sent)
            await asyncio.sleep(0.15)
            self.assertEqual(sum(up in p.input_data for p in stream.sent), moving_frames)

            self.assertEqual(await bot.walk_forward(2), 2)
            self.assertIsNone(movement._active_done)
            self.assertEqual([list(p.input_data) for p in stream.sent[-2:]], [[], []])
        finally:
            await bot.close()
        self.assertTrue(ticker.done())
        sent = len(stream.sent)
        await asyncio.sleep(0.1)
        self.assertEqual(len(stream.sent), sent)

    async def test_server_kick_interrupts_navigation_and_drains_ticker(self):
        stream = Sink()
        movement = Movement(stream, 1)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        movement.start()
        ticker = movement._ticker
        moving = asyncio.Event()
        up = stream.protocol.binding.PlayerAuthInputPacket.InputData.UP

        def on_send():
            if up in stream.sent[-1].input_data:
                moving.set()

        stream.on_send = on_send
        task = asyncio.create_task(movement.navigate_to(
            BlocksSnapshot(), (7, 100, 0), ticks=180
        ))
        await asyncio.wait_for(moving.wait(), 2)
        stream.observer(RawPacket(stream.protocol.binding.DisconnectPacket.ID, b""))
        with self.assertRaisesRegex(ConnectionError, "server disconnected"):
            await asyncio.wait_for(task, 2)
        self.assertIsNone(movement._active_done)
        await movement.aclose()
        self.assertTrue(ticker.done())
        sent = len(stream.sent)
        await asyncio.sleep(0.1)
        self.assertEqual(len(stream.sent), sent)
        with self.assertRaisesRegex(ConnectionError, "movement is closed"):
            await movement.navigate_to(BlocksSnapshot(), (1, 100, 0))


class BlockApproachTests(unittest.IsolatedAsyncioTestCase):
    AIR = 0xDBF44120
    STONE = 0x80310E21
    DANDELION = 0x8E0BE318
    TARGET = (4, 100, 0)
    LADDER_DESCENT_FIXTURES = {
        2: ((0, 0), 0, 1, 0, 0x547A1F1A),
        3: ((-16, -14), 0, -1, 180, 0x50D01713),
        4: ((13, -17), 1, 0, -90, 0x4D260F0C),
        5: ((-14, 17), -1, 0, 90, 0x497C0705),
    }

    async def asyncSetUp(self):
        self.bot = Bot("BlockApproach")
        self.stream = Sink(2225)
        self.binding = self.stream.protocol.binding
        self.up = self.binding.PlayerAuthInputPacket.InputData.UP
        self.movement = Movement(self.stream, 1)
        self.blocks = Blocks(self.stream, self.movement, None, hashed_runtime_ids=True)
        self.bot._movement = self.movement
        self.bot._blocks = self.blocks
        self.set_position(0.5, 0.5)

    async def asyncTearDown(self):
        await self.bot.close()

    def set_position(self, x, z, *, y=101.62, teleport=False, on_ground=False):
        packet = self.binding.MovePlayerPacket()
        packet.player_id = 1
        packet.pos = self.binding.Vec3(x=x, y=y, z=z)
        packet.rot = self.binding.Vec2(x=0.0, y=0.0)
        packet.y_head_rot = 0.0
        packet.riding_id = 0
        packet.on_ground = on_ground
        packet.reset_position = 2 if teleport else 0
        packet.teleport_data = None
        packet.tick = 0
        self.movement._on_packet(self.stream.protocol.encode(packet))

    def correct_position(self, x, y, z, *, on_ground, tick, delta=(0.0, 0.0, 0.0)):
        b = self.binding
        packet = b.CorrectPlayerMovePredictionPacket()
        packet.pos = b.Vec3(x=x, y=y, z=z)
        packet.pos_delta = b.Vec3(x=delta[0], y=delta[1], z=delta[2])
        packet.on_ground = on_ground
        packet.tick = tick
        self.movement._on_packet(self.stream.protocol.encode(packet))

    def set_block(self, position, runtime_id, *, layer=0):
        x, y, z = position
        packet = self.binding.UpdateBlockPacket()
        packet.pos = self.binding.BlockPos(x=x, y=y, z=z)
        packet.runtime_id = runtime_id
        packet.update_flags = 0
        packet.layer = layer
        self.blocks._on_packet(self.stream.protocol.encode(packet))

    def prepare_terrain(self, *, blocked=(), unknown=()):
        for x in range(7):
            for z in range(-3, 4):
                for y in (99, 100, 101):
                    pos = (x, y, z)
                    if y >= 100:
                        self.set_block(pos, self.AIR, layer=1)
                    if pos not in unknown:
                        solid = y == 99 or pos == self.TARGET or pos in blocked
                        self.set_block(pos, self.STONE if solid else self.AIR)

    def prepare_vegetation_corridor(self, *, hashed):
        self.blocks.hashed_runtime_ids = hashed
        palette = {
            "air": (self.AIR, 18894),
            "stone": (self.STONE, 3317),
            "dandelion": (self.DANDELION, 23959),
            "short_grass": (0xA88E6325, 18720),
            "water": (0x483B8B2A, 14122),
            "unknown": (0xFFFFFFFE, 11244),
        }
        ids = {name: pair[0 if hashed else 1] for name, pair in palette.items()}
        for x in range(-1, 4):
            for z in range(-1, 2):
                for y in (99, 100, 101):
                    pos = (x, y, z)
                    solid = y == 99 or z != 0 or x in (-1, 3)
                    self.set_block(pos, ids["stone" if solid else "air"])
                    self.set_block(pos, ids["air"], layer=1)
        return ids

    async def test_flat_navigation_plans_and_walks_through_passable_vegetation(self):
        path = ((0, 100, 0), (1, 100, 0), (2, 100, 0))
        goal = FootGoal(path[-1])
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        for hashed in (True, False):
            ids = self.prepare_vegetation_corridor(hashed=hashed)
            for block in ("air", "dandelion", "short_grass"):
                with self.subTest(hashed=hashed, block=block):
                    self.set_position(0.5, 0.5, on_ground=True)
                    self.stream.sent.clear()
                    self.set_block(path[1], ids[block])
                    self.assertEqual(self.bot.observed_block_state(path[1]).name, f"minecraft:{block}")

                    plan = self.bot.plan_navigation(goal, rules=rules)
                    self.assertEqual((plan.status, plan.nodes, plan.cost), ("found", path, Fraction(2)))
                    self.assertEqual(self.stream.sent, [])

                    result = await self.bot.navigate(goal, rules=rules)
                    self.assertEqual((result.status, result.stand_position), ("reached", goal.position))
                    self.assertGreater(result.frames, 0)
                    self.assertTrue(any(1.0 <= frame.pos.x < 2.0 for frame in self.moving_input()))
                    for frame in self.stream.sent:
                        self.assertAlmostEqual(frame.pos.y, 101.62, places=4)
                        self.assertAlmostEqual(frame.pos.z, 0.5, places=4)
                    self.assert_released()

    async def test_flat_navigation_can_start_in_passable_vegetation(self):
        start, destination = (1, 100, 0), (2, 100, 0)
        goal = FootGoal(destination)
        rules = NavigationRules(search_radius=1, vertical_radius=0)
        for hashed in (True, False):
            ids = self.prepare_vegetation_corridor(hashed=hashed)
            for block in ("dandelion", "short_grass"):
                with self.subTest(hashed=hashed, block=block):
                    self.set_block(start, ids[block])
                    self.set_position(1.5, 0.5, on_ground=True)
                    self.stream.sent.clear()
                    plan = self.bot.plan_navigation(goal, rules=rules)
                    self.assertEqual((plan.status, plan.nodes), ("found", (start, destination)))
                    self.assertEqual(self.stream.sent, [])

                    result = await self.bot.navigate(goal, rules=rules)
                    self.assertEqual((result.status, result.stand_position), ("reached", destination))
                    self.assertGreater(result.frames, 0)
                    self.assert_released()

                    self.set_position(1.5, 0.5, on_ground=True)
                    self.stream.sent.clear()
                    self.assertGreater(await self.bot.navigate_to(destination, search_radius=1), 0)
                    self.assertGreater(self.moving_input()[-1].pos.x, 2.0)
                    self.assert_released()

    async def test_flat_navigation_requires_clear_layers_and_support_around_vegetation(self):
        plant, head, support = (1, 100, 0), (1, 101, 0), (1, 99, 0)
        goal = FootGoal((2, 100, 0))
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        for hashed in (True, False):
            ids = self.prepare_vegetation_corridor(hashed=hashed)
            self.set_block(plant, ids["dandelion"])
            for cell, layer, replacement, restore, status in (
                (plant, 0, "stone", "dandelion", "unreachable"),
                (plant, 1, "water", "air", "unreachable"),
                (plant, 1, "dandelion", "air", "unreachable"),
                (plant, 1, "unknown", "air", "unknown"),
                (head, 0, "short_grass", "air", "unreachable"),
                (head, 1, "short_grass", "air", "unreachable"),
                (support, 0, "air", "stone", "unreachable"),
            ):
                with self.subTest(hashed=hashed, cell=cell, layer=layer, block=replacement):
                    self.set_block(cell, ids[replacement], layer=layer)
                    plan = self.bot.plan_navigation(goal, rules=rules)
                    self.assertEqual((plan.status, plan.nodes), (status, ()))
                    result = await self.bot.navigate(goal, rules=rules)
                    self.assertEqual((result.status, result.frames), (status, 0))
                    self.assertEqual(self.stream.sent, [])
                    self.set_block(cell, ids[restore], layer=layer)

    async def test_flat_navigation_rechecks_vegetation_before_entry(self):
        ids = self.prepare_vegetation_corridor(hashed=True)
        plant = (1, 100, 0)
        self.set_block(plant, ids["dandelion"])
        goal = FootGoal((2, 100, 0))
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        plans = []

        def flood_after_first_step():
            if self.up in self.stream.sent[-1].input_data:
                self.set_block(plant, ids["water"], layer=1)
                self.stream.on_send = None

        self.stream.on_send = flood_after_first_step
        result = await self.bot.navigate(goal, rules=rules, on_plan=plans.append)

        plan, = plans
        self.assertEqual((plan.status, plan.nodes), ("found", ((0, 100, 0), plant, goal.position)))
        self.assertEqual((result.status, result.frames, result.stand_position), ("unreachable", 1, None))
        self.assertEqual(len(self.moving_input()), 1)
        self.assertTrue(all(frame.pos.x < 1.0 for frame in self.stream.sent))
        self.assert_released()

    async def test_flat_navigation_can_leave_but_not_enter_magma(self):
        self.prepare_terrain()
        self.set_block((1, 99, 0), 0x6680F5C9)  # magma
        with self.assertRaisesRegex(RuntimeError, "no route"):
            await self.bot.navigate_to((1, 100, 0), search_radius=1)
        self.assertFalse(self.stream.sent)
        self.set_block((1, 99, 0), self.STONE)
        self.set_block((0, 99, 0), 0x6680F5C9)
        self.assertGreater(await self.bot.navigate_to((1, 100, 0), search_radius=1), 0)

    async def test_flat_navigation_stops_if_the_next_cell_floods(self):
        self.prepare_terrain()

        def flood():
            if self.up in self.stream.sent[-1].input_data:
                self.set_block((1, 100, 0), 0x483B8B2A, layer=1)  # water
                self.stream.on_send = None

        self.stream.on_send = flood
        self.assertEqual(await self.bot.navigate_to((1, 100, 0), ticks=20), 1)
        self.assertFalse(any(self.up in p.input_data for p in self.stream.sent[1:]))

    def prepare_descent(self, target=(3, 99, 0)):
        solids = {(0, 99, 0), (1, 98, 0), (2, 98, 0), target}
        for x in range(-3, 4):
            for z in range(-3, 4):
                for y in range(98, 103):
                    pos = (x, y, z)
                    self.set_block(pos, self.STONE if pos in solids else self.AIR)
                    self.set_block(pos, self.AIR, layer=1)
        return target

    def prepare_ladder(self, dx=1, dz=0, height=4):
        self.blocks._player_game_type = "SURVIVAL"
        self.movement._gap_jump_allowed = lambda: True
        self.correct_position(0.5, 101.62, 0.5, on_ground=True, tick=0)
        for x in (-1, 0):
            for z in (-1, 0):
                self.blocks._sub_chunks[x, 6, z] = None
        facing = {(0, 1): 0x547A1F1A, (0, -1): 0x50D01713,
                  (1, 0): 0x4D260F0C, (-1, 0): 0x497C0705}[dx, dz]
        for step in range(3):
            self.set_block((step * dx, 99, step * dz), self.STONE)
        for y in range(100, 100 + height):
            self.set_block((2 * dx, y, 2 * dz), facing)
            self.set_block((3 * dx, y, 3 * dz), self.STONE)
        for step in range(4, 8):
            self.set_block((step * dx, 99 + height, step * dz), self.STONE)
        return (3 * dx, 100 + height, 3 * dz), (6 * dx, 100 + height, 6 * dz)

    def prepare_ladder_descent(self, face=2):
        (ox, oz), dx, dz, _, rung = self.LADDER_DESCENT_FIXTURES[face]
        self.blocks._player_game_type = "SURVIVAL"
        self.movement._gap_jump_allowed = lambda: True
        self.blocks._updates.clear()
        self.blocks._sub_chunks.clear()
        for x in range((ox - 7) // 16, (ox + 7) // 16 + 1):
            for z in range((oz - 7) // 16, (oz + 7) // 16 + 1):
                self.blocks._sub_chunks[x, 6, z] = None

        def at(x, y, z):
            return ox + x * dz + z * dx, y, oz - x * dx + z * dz

        def pose(x, y, z):
            wx, y, wz = at(x - 0.5, y, z - 0.5)
            return wx + 0.5, y, wz + 0.5

        x, y, z = pose(0.5, 105.62, 3.75)
        self.set_position(x, z, y=y, teleport=True, on_ground=True)
        for y in range(100, 104):
            self.set_block(at(0, y, 2), rung)
            self.set_block(at(0, y, 3), self.STONE)
        for z in range(-1, 3):
            self.set_block(at(0, 99, z), self.STONE)
        self.set_block(at(0, 103, 4), self.STONE)
        return at, pose

    def prepare_water(self, dx=0, dz=1):
        self.blocks._player_game_type = "SURVIVAL"
        self.movement._gap_jump_allowed = lambda: True
        self.air = 300
        self.movement._navigation_air_supply = lambda: self.air
        self.correct_position(0.5, 101.62, 0.5, on_ground=True, tick=0)
        for x in (-1, 0):
            for z in (-1, 0):
                self.blocks._sub_chunks[x, 6, z] = None

        def at(x, y, z):
            return dx * z + dz * x, y, dz * z - dx * x

        for z in (0, 1, *range(6, 11)):
            for x in (-1, 0, 1):
                self.set_block(at(x, 99, z), self.STONE)
        for z in (1, 6):
            for x in range(-3, 4):
                for y in (97, 98, 99):
                    self.set_block(at(x, y, z), self.STONE)
        for z in range(2, 6):
            for x in range(-3, 4):
                self.set_block(at(x, 96, z), self.STONE)
                for y in (97, 98, 99):
                    self.set_block(at(x, y, z), 0x483B8B2A)
            for side in (-4, 4):
                for y in (97, 98, 99):
                    self.set_block(at(side, y, z), self.STONE)

    def observe_swimming(self, tick, swimming, player=1, *, crawling=None):
        b = self.binding
        entries = []
        if swimming is not None:
            flags = 1 << int(b.ActorFlags.SWIMMING) if swimming else 0
            entries.append(b.DataItemEntry(id=0, payload=b.DataItemInt64Payload(value=flags)))
        if crawling is not None:
            flags = 1 << (int(b.ActorFlags.CRAWLING) % 64) if crawling else 0
            entries.append(b.DataItemEntry(id=92, payload=b.DataItemInt64Payload(value=flags)))
        packet = b.SetActorDataPacket(id=player, tick=tick, packed_items=b.SynchedActorData.CopyableDataList(data=entries))
        self.movement._on_packet(self.stream.protocol.encode(packet))

    async def test_water_plan_is_opt_in_exact_and_preserves_longer_dry_routes(self):
        self.prepare_water()
        goal = FootGoal((0, 100, 6))
        rules = NavigationRules(search_radius=10, vertical_radius=0, allow_swimming=True)
        plan = self.bot.plan_navigation(goal, rules=rules)
        self.assertEqual((plan.status, plan.nodes, plan.cost),
                         ("found", ((0, 100, 0), (0, 100, 6)), Fraction(6)))
        dry = NavigationRules(search_radius=10, vertical_radius=0)
        self.assertEqual(self.bot.plan_navigation(goal, rules=dry).status, "unreachable")
        short = NavigationRules(search_radius=9, vertical_radius=0, allow_swimming=True)
        self.assertEqual(self.bot.plan_navigation(goal, rules=short).status, "unreachable")
        for z in (1, 6):
            self.set_block((4, 99, z), self.STONE)
        detour = self.bot.plan_navigation(goal, rules=rules)
        self.assertEqual((detour.status, detour.cost), ("found", Fraction(14)))
        self.assertIn((4, 100, 4), detour.nodes)
        self.assertEqual(self.stream.sent, [])

    async def test_water_plan_rejects_unknown_or_unsafe_shape_and_insufficient_air(self):
        self.prepare_water()
        goal = FootGoal((0, 100, 6))
        rules = NavigationRules(search_radius=10, vertical_radius=0, allow_swimming=True)
        for cell, layer, replacement, restore, status in (
            ((1, 98, 4), 0, self.AIR, 0x483B8B2A, "unreachable"),
            ((0, 99, 4), 1, self.STONE, self.AIR, "unreachable"),
            ((0, 102, 8), 0, self.STONE, self.AIR, "unreachable"),
            ((0, 99, 10), 0, self.AIR, self.STONE, "unreachable"),
            ((0, 98, 4), 1, 0xFFFFFFFE, self.AIR, "unknown"),
        ):
            with self.subTest(cell=cell):
                self.set_block(cell, replacement, layer=layer)
                self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, status)
                result = await self.bot.navigate(goal, rules=rules, ticks=220)
                self.assertEqual((result.status, result.frames), (status, 0))
                self.set_block(cell, restore, layer=layer)
        self.air = 100
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unreachable")
        self.assertEqual(self.stream.sent, [])

    async def test_water_plan_uses_the_same_orthogonal_contract_in_all_four_directions(self):
        rules = NavigationRules(search_radius=10, vertical_radius=0, allow_swimming=True)
        for dx, dz in ((0, 1), (1, 0), (0, -1), (-1, 0)):
            with self.subTest(dx=dx, dz=dz):
                self.blocks._updates.clear()
                self.prepare_water(dx, dz)
                goal = FootGoal((6 * dx, 100, 6 * dz))
                plan = self.bot.plan_navigation(goal, rules=rules)
                self.assertEqual((plan.status, plan.nodes), ("found", ((0, 100, 0), goal.position)))
        self.assertEqual(self.stream.sent, [])

    async def test_water_enforces_a_single_budget_before_the_first_moving_input(self):
        self.prepare_water()
        goal = FootGoal((0, 100, 6))
        rules = NavigationRules(search_radius=10, vertical_radius=0, allow_swimming=True)
        for ticks, timeout in ((80, 15), (220, 0.2)):
            result = await self.bot.navigate(goal, rules=rules, ticks=ticks, timeout=timeout)
            self.assertEqual((result.status, result.frames), ("budget", 0))
        self.assertEqual(self.stream.sent, [])

    async def replay_water(self, *, stale_start=False, stale_exit=False, swim_again=False):
        self.prepare_water()
        rules = NavigationRules(search_radius=10, vertical_radius=0, allow_swimming=True)
        start_flag = self.binding.PlayerAuthInputPacket.InputData.START_SWIMMING
        entry = submerged = swim = wall = rise = rim = landed = None
        b = self.binding

        def speed(value):
            packet = b.UpdateAttributesPacket(runtime_id=1, attribute_data=[
                b.AttributeData(name="minecraft:movement", current_value=value),
            ])
            self.movement._on_packet(self.stream.protocol.encode(packet))

        def report():
            nonlocal entry, submerged, swim, wall, rise, rim, landed
            frame = self.stream.sent[-1]
            tick = int(frame.client_tick)
            if entry is None and frame.move.y > 0:
                entry = tick
                self.correct_position(0.5, 100.49, 2.7, on_ground=False, tick=tick, delta=(0, -0.2, 0))
            elif submerged is None and not frame.move.y:
                submerged = tick
                self.air = 299
                self.correct_position(0.5, 99.65, 3.2, on_ground=False, tick=tick, delta=(0, -0.15, 0))
            elif swim is None and start_flag in frame.input_data:
                swim = tick
                self.observe_swimming(tick, True)
                if stale_start:
                    self.observe_swimming(tick - 1, False)
                speed(0.13)
            elif swim is not None and wall is None and frame.move.y:
                wall = tick
                self.correct_position(0.5, 99.7, 5.7, on_ground=False, tick=tick)
            elif wall is not None and rise is None and frame.rot.x == -60:
                rise = tick
                self.correct_position(0.5, 100.3, 5.7, on_ground=False, tick=tick, delta=(0, 0.14, 0))
                self.movement.set_rotation(20, 90)
            elif rise is not None and rim is None:
                rim = tick
                self.correct_position(0.5, 102.0, 5.90, on_ground=False, tick=tick, delta=(0, -0.08, 0.05))
            elif rim is not None and landed is None and not frame.move.y:
                landed = tick
                self.air = 300
                self.observe_swimming(swim - 1 if stale_exit else tick, False)
                self.correct_position(0.5, 101.62, 7.5, on_ground=True, tick=tick)
                speed(0.1)
            elif swim_again and landed is not None and tick == landed + 2:
                self.observe_swimming(tick, True)

        self.stream.on_send = report
        goal = FootGoal((0, 100, 8))
        result = await self.bot.navigate(goal, rules=rules, ticks=220, timeout=15)
        return result, swim, rim, landed

    async def test_water_uses_real_swimming_ascent_and_ground_before_continuing(self):
        result, swim, rim, landed = await self.replay_water(stale_start=True)
        self.assertEqual((result.status, result.stand_position), ("reached", (0, 100, 8)))
        self.assertEqual(landed - rim, 7)
        self.assertEqual(next(frame.client_tick for frame in self.stream.sent
                              if frame.client_tick > landed and frame.move.y), landed + 7)
        self.assertTrue(all(frame.pos_delta.y == 0 for frame in self.stream.sent))
        self.assertTrue(all(frame.rot.y == 0 for frame in self.stream.sent
                            if swim <= frame.client_tick < landed and frame.move.y))
        self.assertFalse(self.movement.state.sprint_key_down)

    async def test_water_rejects_a_swim_exit_older_than_its_confirmed_start(self):
        result, _, _, landed = await self.replay_water(stale_exit=True)
        self.assertIsNotNone(landed)
        self.assertEqual((result.status, result.stand_position), ("unknown", None))
        self.assertTrue(all(not frame.move.y for frame in self.stream.sent if frame.client_tick >= landed))

    async def test_water_rechecks_the_latest_swim_state_until_dry_handoff(self):
        result, _, _, landed = await self.replay_water(swim_again=True)
        self.assertIsNotNone(landed)
        self.assertEqual((result.status, result.stand_position), ("unknown", None))
        self.assertTrue(all(not frame.move.y for frame in self.stream.sent if frame.client_tick >= landed))

    async def expire_water_sprint(self):
        self.prepare_water()
        rules = NavigationRules(search_radius=10, vertical_radius=0, allow_swimming=True)
        flags = self.binding.PlayerAuthInputPacket.InputData
        fifth = asyncio.Event()
        send = self.stream.send
        timeout_at = asyncio.timeout_at
        action_timeouts = []

        async def hold_fifth(frame):
            if frame.client_tick == 5:
                fifth.set()
                await asyncio.Future()
            await send(frame)
            if frame.client_tick == 1:
                self.correct_position(0.5, 100.49, 2.7, on_ground=False, tick=1)
            elif frame.client_tick == 2:
                self.air = 299
                self.correct_position(0.5, 99.65, 3.2, on_ground=False, tick=2)
            elif frame.client_tick == 4:
                self.assertIn(flags.START_SWIMMING, frame.input_data)
                self.observe_swimming(4, True)

        def record_timeout(at):
            timeout = timeout_at(at)
            action_timeouts.append(timeout)
            return timeout

        self.stream.send = hold_fifth
        try:
            with patch("endpilot.movement.asyncio.timeout_at", record_timeout):
                action = asyncio.create_task(
                    self.bot.navigate(FootGoal((0, 100, 6)), rules=rules, ticks=220, timeout=15)
                )
                try:
                    await asyncio.wait_for(fifth.wait(), 1)
                    _, deadline = action_timeouts
                    deadline.reschedule(asyncio.get_running_loop().time())
                    result = await asyncio.wait_for(action, 1)
                finally:
                    if not action.done():
                        action.cancel()
                        await asyncio.gather(action, return_exceptions=True)
        finally:
            self.stream.send = send
        self.assertEqual((result.status, result.frames), ("budget", 2))
        self.assertEqual([frame.client_tick for frame in self.stream.sent], [1, 2, 3, 4])
        self.assertIn(flags.SPRINTING, self.stream.sent[-1].input_data)
        self.assertTrue(self.movement.state.sprinting)
        self.assertFalse(self.movement.state.sprint_key_down)

    async def test_water_deadline_rolls_sprint_stop_into_the_next_normal_forward_frame(self):
        await self.expire_water_sprint()
        flags = self.binding.PlayerAuthInputPacket.InputData
        clock = ManualClock()
        with clock.patch_scheduler():
            self.movement.start()
            await self.bot.set_move_vector(0, 1)
            await clock.wake_at(50)
            await clock.wake_at(100)
            stopped, continued = self.stream.sent[4:]
        self.assertEqual((stopped.client_tick, continued.client_tick), (6, 7))
        self.assertEqual((stopped.move.y, continued.move.y), (1.0, 1.0))
        self.assertIn(flags.STOP_SPRINTING, stopped.input_data)
        self.assertNotIn(flags.SPRINTING, stopped.input_data)
        self.assertNotIn(flags.STOP_SPRINTING, continued.input_data)
        self.assertNotIn(flags.SPRINTING, continued.input_data)
        self.assertFalse(self.movement.state.sprinting)

    async def test_water_deadline_preserves_the_next_callers_explicit_sprint(self):
        await self.expire_water_sprint()
        flags = self.binding.PlayerAuthInputPacket.InputData
        await self.bot.set_move_vector(0, 1)
        tick = await self.bot.set_sprinting(True)
        frame = self.stream.sent[-1]
        self.assertEqual((tick, frame.move.y), (6, 1.0))
        self.assertIn(flags.SPRINTING, frame.input_data)
        self.assertIn(flags.SPRINT_DOWN, frame.input_data)
        self.assertNotIn(flags.STOP_SPRINTING, frame.input_data)
        self.assertTrue(self.movement.state.sprint_key_down)

    async def test_water_cancellation_releases_direction_and_sprint_without_assuming_a_dry_stance(self):
        self.prepare_water()
        rules = NavigationRules(search_radius=10, vertical_radius=0, allow_swimming=True)
        flags = self.binding.PlayerAuthInputPacket.InputData
        started = asyncio.Event()
        entered = submerged = False

        def report():
            nonlocal entered, submerged
            frame = self.stream.sent[-1]
            tick = int(frame.client_tick)
            if not entered and frame.move.y:
                entered = True
                self.correct_position(0.5, 100.49, 2.7, on_ground=False, tick=tick)
            elif entered and not submerged and not frame.move.y:
                submerged = True
                self.air = 299
                self.correct_position(0.5, 99.65, 3.2, on_ground=False, tick=tick)
            elif flags.START_SWIMMING in frame.input_data:
                self.observe_swimming(tick, True)
                started.set()

        self.stream.on_send = report
        action = asyncio.create_task(self.bot.navigate(FootGoal((0, 100, 6)), rules=rules, ticks=220, timeout=15))
        await asyncio.wait_for(started.wait(), 1)
        action.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await action
        self.assertFalse(self.movement.state.sprint_key_down)
        self.assertFalse(self.movement.state.on_ground)
        self.assertEqual(self.movement.position[2], self.stream.protocol.binding.Vec3(z=3.2).z)
        self.assertTrue(all(not frame.move.y and not frame.pos_delta.y for frame in self.stream.sent[-2:]))
        self.assertFalse(self.movement._action_lock.locked())

    async def test_water_does_not_tilt_for_another_actor_or_a_stale_swim_confirmation(self):
        self.prepare_water()
        rules = NavigationRules(search_radius=10, vertical_radius=0, allow_swimming=True)
        flags = self.binding.PlayerAuthInputPacket.InputData
        entered = submerged = started = False

        def report():
            nonlocal entered, submerged, started
            frame = self.stream.sent[-1]
            tick = int(frame.client_tick)
            if not entered and frame.move.y:
                entered = True
                self.correct_position(0.5, 100.49, 2.7, on_ground=False, tick=tick)
            elif entered and not submerged and not frame.move.y:
                submerged = True
                self.air = 299
                self.correct_position(0.5, 99.65, 3.2, on_ground=False, tick=tick)
            elif flags.START_SWIMMING in frame.input_data:
                started = True
                self.observe_swimming(tick - 1, True)
                self.observe_swimming(tick, True, player=99)
            elif started and frame.move.y:
                self.correct_position(0.5, 99.65, 5.7, on_ground=False, tick=tick)

        self.stream.on_send = report
        result = await self.bot.navigate(FootGoal((0, 100, 6)), rules=rules, ticks=220, timeout=15)
        self.assertTrue(started)
        self.assertEqual(result.status, "unknown")
        self.assertTrue(all(frame.rot.x == 0 for frame in self.stream.sent))
        self.assertFalse(self.movement.state.sprint_key_down)

    async def check_water_stale_swim_with_crawling(self, *, split):
        self.prepare_water()
        rules = NavigationRules(search_radius=10, vertical_radius=0, allow_swimming=True)
        b = self.binding
        flags = b.PlayerAuthInputPacket.InputData
        swim_tick = None

        def report():
            nonlocal swim_tick
            frame = self.stream.sent[-1]
            tick = int(frame.client_tick)
            if tick == 1:
                self.correct_position(0.5, 100.49, 2.7, on_ground=False, tick=tick)
            elif tick == 2:
                self.air = 299
                self.correct_position(0.5, 99.65, 3.2, on_ground=False, tick=tick)
            elif flags.START_SWIMMING in frame.input_data:
                swim_tick = tick
                self.observe_swimming(tick, True)
                if split:
                    self.observe_swimming(tick - 1, False)
                    self.observe_swimming(tick - 1, None, crawling=True)
                else:
                    self.observe_swimming(tick - 1, False, crawling=True)

        self.stream.on_send = report
        result = await self.bot.navigate(FootGoal((0, 100, 6)), rules=rules, ticks=220, timeout=15)
        self.assertEqual(swim_tick, 4)
        self.assertEqual((result.status, result.frames, result.stand_position), ("unreachable", 2, None))
        self.assertIs(self.bot.is_swimming, True)
        self.assertIs(self.movement._actor_flag(b.ActorFlags.SWIMMING), True)
        self.assertIs(self.bot.movement_state.crawling, True)
        self.assertTrue(all(not frame.move.y for frame in self.stream.sent if frame.client_tick > swim_tick))
        self.assertFalse(self.movement.state.sprint_key_down)

    async def test_water_keeps_combined_crawling_metadata_when_swimming_is_stale(self):
        await self.check_water_stale_swim_with_crawling(split=False)

    async def test_water_keeps_separate_crawling_metadata_when_swimming_is_stale(self):
        await self.check_water_stale_swim_with_crawling(split=True)

    async def test_ladder_plan_is_directed_bounded_and_costs_one_up_per_rung(self):
        for dx, dz, height in ((0, 1, 4), (0, -1, 4), (1, 0, 4), (-1, 0, 4), (1, 0, 6), (-1, 0, 6)):
            with self.subTest(dx=dx, dz=dz, height=height):
                self.blocks._updates.clear()
                self.set_position(0.5, 0.5, teleport=True, on_ground=True)
                landing, _ = self.prepare_ladder(dx, dz, height)
                goal = FootGoal(landing)
                costs = NavigationCosts(up=2.5)
                rules = NavigationRules(search_radius=7, vertical_radius=height, costs=costs)
                plan = self.bot.plan_navigation(goal, rules=rules)
                if height == 6 and dx == -1:
                    self.assertEqual(plan.status, "unreachable")
                    continue
                self.assertEqual((plan.status, plan.nodes, plan.cost),
                                 ("found", ((0, 100, 0), landing), Fraction(height * 5, 2)))
                short = NavigationRules(search_radius=6, vertical_radius=height)
                self.assertEqual(self.bot.plan_navigation(goal, rules=short).status, "unreachable")
                reverse = FootGoal((0, 100, 0))
                self.set_position(landing[0] + 0.5, landing[2] + 0.5, y=landing[1] + 1.62,
                                  teleport=True, on_ground=True)
                self.assertEqual(self.bot.plan_navigation(reverse, rules=rules).status, "unreachable")
        self.assertEqual(self.stream.sent, [])

    async def test_ladder_requires_rungs_backing_two_layers_and_the_braking_deck(self):
        landing, _ = self.prepare_ladder()
        goal = FootGoal(landing)
        rules = NavigationRules(search_radius=7, vertical_radius=4)
        for cell, layer, bad, good, status in (
            ((2, 102, 0), 0, 0x497C0705, 0x4D260F0C, "unreachable"),
            ((2, 103, 0), 0, self.AIR, 0x4D260F0C, "unreachable"),
            ((2, 101, 0), 1, self.STONE, self.AIR, "unreachable"),
            ((3, 102, 0), 0, self.AIR, self.STONE, "unreachable"),
            ((3, 106, 0), 1, self.STONE, self.AIR, "unreachable"),
            ((7, 103, 0), 0, self.AIR, self.STONE, "unreachable"),
            ((7, 106, 0), 0, 0xFFFFFFFE, self.AIR, "unknown"),
        ):
            with self.subTest(cell=cell, layer=layer, bad=bad):
                self.set_block(cell, bad, layer=layer)
                self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, status)
                result = await self.bot.navigate(goal, rules=rules, ticks=140, timeout=8)
                self.assertEqual((result.status, result.frames), (status, 0))
                self.assertEqual(self.stream.sent, [])
                self.set_block(cell, good, layer=layer)

    async def test_ladder_needs_time_and_frame_budget_before_entering(self):
        landing, _ = self.prepare_ladder()
        goal = FootGoal(landing)
        for ticks, timeout in ((50, 8), (140, 0.3)):
            with self.subTest(ticks=ticks, timeout=timeout):
                result = await self.bot.navigate(goal, rules=NavigationRules(search_radius=7),
                                                 ticks=ticks, timeout=timeout)
                self.assertEqual((result.status, result.frames), ("budget", 0))
                self.assertEqual(self.stream.sent, [])

    async def test_ladder_uses_ordered_own_ascent_and_braking_then_replans_on_the_top(self):
        _, target = self.prepare_ladder()
        first = brake = landing_tick = None

        def report():
            nonlocal first, brake, landing_tick
            frame = self.stream.sent[-1]
            tick = int(frame.client_tick)
            if first is None and self.up in frame.input_data:
                first = tick
            if first is None:
                return
            if tick == first + 12:
                self.correct_position(2.5125, 102.818, 0.5, on_ground=False, tick=tick - 5, delta=(0, 0.2, 0))
                self.correct_position(2.575, 105.938, 0.5, on_ground=False, tick=tick, delta=(0.03, -0.119, 0))
                self.movement.set_rotation(0, 0)
            if brake is None and self.up not in frame.input_data:
                brake = tick
            if brake is not None and brake + 1 <= tick <= brake + 8:
                # Many equivalent acknowledgments still allow the landing to settle.
                self.correct_position(4.96 + (tick % 2) * 0.002, 105.62, 0.5, on_ground=True, tick=tick)
                if landing_tick is None:
                    landing_tick = tick

        self.stream.on_send = report
        result = await self.bot.navigate(FootGoal(target), rules=NavigationRules(search_radius=7), ticks=140, timeout=8)
        self.assertEqual((result.status, result.stand_position), ("reached", target))
        self.assertEqual(self.jump_input(), [])
        self.assertTrue(all(p.pos_delta.y == 0 for p in self.stream.sent))
        before_brake = [p for p in self.moving_input() if p.client_tick < brake]
        self.assertTrue(all(math.isclose(p.rot.y, -90) for p in before_brake))
        continued = [p for p in self.moving_input() if p.client_tick > brake]
        self.assertGreaterEqual(continued[0].client_tick, landing_tick + 7)
        self.assertAlmostEqual(continued[0].pos.x, 4.862, delta=0.01)

    async def test_ladder_needs_real_crest_and_ground_after_release_with_full_footprint(self):
        landing, _ = self.prepare_ladder()
        goal = FootGoal(landing)
        rules = NavigationRules(search_radius=7)
        for case, expected in (("height_only", "unknown"), ("before_release", "unknown"),
                               ("foot_over_edge", "unreachable"), ("back_on_ladder", "unknown"),
                               ("in_backing", "unreachable")):
            with self.subTest(case=case):
                self.set_position(0.5, 0.5, teleport=True, on_ground=True)
                self.stream.sent.clear()
                first = brake = None

                def report():
                    nonlocal first, brake
                    frame = self.stream.sent[-1]
                    tick = int(frame.client_tick)
                    if first is None:
                        if self.up in frame.input_data:
                            first = tick
                        return
                    if tick == first + 8:
                        self.correct_position(2.5125, 102.818, 0.5, on_ground=False, tick=tick,
                                              delta=(0, 0 if case == "height_only" else 0.2, 0))
                    elif tick == first + 12:
                        self.correct_position(2.575, 105.938, 0.5, on_ground=False, tick=tick,
                                              delta=(0, 0 if case == "height_only" else 0.036, 0))
                    elif case == "before_release" and tick == first + 22:
                        self.correct_position(4.36, 105.62, 0.5, on_ground=True, tick=tick)
                    elif brake is None and self.up not in frame.input_data:
                        brake = tick
                        if case != "before_release":
                            if case in ("back_on_ladder", "in_backing"):
                                x = 2.5125 if case == "back_on_ladder" else 3.10
                                self.correct_position(x, 103.62, 0.5, on_ground=False, tick=tick - 1,
                                                      delta=(0, -0.2, 0))
                            x = 3.05 if case == "foot_over_edge" else 4.96
                            self.correct_position(x, 105.62, 0.5, on_ground=True, tick=tick)

                self.stream.on_send = report
                result = await self.bot.navigate(goal, rules=rules, ticks=140, timeout=8)
                self.assertEqual(result.status, expected)
                self.assertIsNone(result.stand_position)
                self.assertIsNotNone(brake)
                self.assertTrue(all(p.client_tick < brake for p in self.moving_input()))
                self.assertEqual(self.jump_input(), [])

    async def test_ladder_orders_ordinary_pose_reports_with_ground_corrections(self):
        _, target = self.prepare_ladder()
        goal = FootGoal(target)
        rules = NavigationRules(search_radius=7)
        for returned_to_bottom in (False, True):
            with self.subTest(returned_to_bottom=returned_to_bottom):
                self.set_position(0.5, 0.5, teleport=True, on_ground=True)
                self.stream.sent.clear()
                first = brake = land_tick = None
                b = self.binding

                def report():
                    nonlocal first, brake, land_tick
                    frame = self.stream.sent[-1]
                    tick = int(frame.client_tick)
                    if first is None:
                        if self.up in frame.input_data:
                            first = tick
                        return
                    if tick == first + 12:
                        self.correct_position(2.5125, 102.818, 0.5, on_ground=False, tick=tick - 5, delta=(0, 0.2, 0))
                        self.correct_position(2.575, 105.938, 0.5, on_ground=False, tick=tick, delta=(0.03, -0.119, 0))
                    if not any((frame.move.x, frame.move.y)) and brake is None:
                        brake = tick
                    if brake is None:
                        return
                    if tick == brake + 1:
                        land_tick = tick
                        self.correct_position(4.96, 105.62, 0.5, on_ground=True, tick=tick)
                    if brake + 2 <= tick <= brake + 7:
                        if returned_to_bottom:
                            self.set_position(0.5, 0.5, on_ground=True)
                        else:
                            self.set_position(4.96, 0.5, y=105.62, on_ground=True)
                        packet = b.ServerPlayerPostMovePositionPacket(pos=b.Vec3(x=4.96, y=105.62, z=0.5))
                        self.movement._on_packet(self.stream.protocol.encode(packet))

                self.stream.on_send = report
                result = await self.bot.navigate(goal, rules=rules, ticks=140, timeout=8)
                continued = [p for p in self.moving_input() if p.client_tick > brake]
                if returned_to_bottom:
                    self.assertEqual((result.status, result.stand_position), ("unknown", None))
                    self.assertEqual(continued, [])
                else:
                    self.assertEqual((result.status, result.stand_position), ("reached", target))
                    self.assertEqual(continued[0].client_tick, land_tick + 7)

    async def test_stop_releases_ladder_input_at_the_last_observed_height(self):
        landing, _ = self.prepare_ladder()
        climbing = asyncio.Event()

        def report():
            frame = self.stream.sent[-1]
            if self.up in frame.input_data:
                self.correct_position(2.5125, 102.818, 0.5, on_ground=False, tick=frame.client_tick,
                                      delta=(0, 0.2, 0))
                climbing.set()

        self.stream.on_send = report
        action = asyncio.create_task(
            self.bot.navigate(FootGoal(landing), rules=NavigationRules(search_radius=7), ticks=140, timeout=8)
        )
        await asyncio.wait_for(climbing.wait(), 1)
        await self.bot.stop_moving()
        result = await action
        self.assertEqual((result.status, result.frames, result.stand_position), ("stopped", 1, None))
        self.assertEqual(len(self.stream.sent), 3)
        for packet in self.stream.sent[-2:]:
            self.assertEqual(list(packet.input_data), [])
            self.assertAlmostEqual(packet.pos.y, 102.818, delta=0.0001)
            self.assertEqual(packet.pos_delta.y, 0)
        self.stream.on_send = None
        self.set_position(0.5, 0.5, teleport=True, on_ground=True)
        self.assertEqual(await self.bot.walk_forward(1), 1)

    async def test_ladder_descent_plan_is_directed_bounded_and_costs_one_down_per_rung(self):
        for face, target_face in ((2, 3), (3, 2), (4, 5), (5, 4)):
            with self.subTest(face=face):
                at, pose = self.prepare_ladder_descent(face)
                top, landing = at(0, 104, 3), at(0, 100, 0)
                runoff, target = at(0, 100, -1), at(0, 100, -2)
                self.set_block(target, self.STONE)
                rules = NavigationRules(search_radius=5, vertical_radius=4, costs=NavigationCosts(down=2.5))
                plan = self.bot.plan_navigation(BlockFaceGoal(target), rules=rules)
                self.assertEqual((plan.status, plan.nodes, plan.cost), ("found", (top, landing, runoff), Fraction(11)))
                self.assertEqual(plan.matches, (BlockFaceMatch(target, target_face),))
                for bounds in (NavigationRules(search_radius=3), NavigationRules(vertical_radius=3)):
                    self.assertEqual(self.bot.plan_navigation(FootGoal(landing), rules=bounds).status, "unreachable")
                x, y, z = pose(0.5, 101.62, 0.5)
                self.set_position(x, z, y=y, teleport=True, on_ground=True)
                reverse = self.bot.plan_navigation(FootGoal(top), rules=NavigationRules(search_radius=8))
                self.assertEqual(reverse.status, "unreachable")
        self.assertEqual(self.stream.sent, [])

    async def test_ladder_descent_rejects_missing_clearance_support_and_rung_evidence(self):
        for face, reverse_face in ((2, 3), (3, 2), (4, 5), (5, 4)):
            at, _ = self.prepare_ladder_descent(face)
            goal = FootGoal(at(0, 100, 0))
            rung, reversed_rung = (self.LADDER_DESCENT_FIXTURES[f][-1] for f in (face, reverse_face))
            for cell, layer, bad, good, status in (
                ((0, 99, 1), 0, self.AIR, self.STONE, "unreachable"),
                ((0, 99, -1), 0, self.AIR, self.STONE, "unreachable"),
                ((0, 102, 3), 0, self.AIR, self.STONE, "unreachable"),
                ((0, 103, 4), 0, self.AIR, self.STONE, "unreachable"),
                ((0, 104, 2), 0, self.STONE, self.AIR, "unreachable"),
                ((0, 105, 1), 0, self.STONE, self.AIR, "unreachable"),
                ((0, 102, 2), 0, reversed_rung, rung, "unreachable"),
                ((0, 102, 2), 1, self.STONE, self.AIR, "unreachable"),
                ((0, 102, 2), 0, 0xFFFFFFFE, rung, "unknown"),
                ((0, 102, -1), 1, 0xFFFFFFFE, self.AIR, "unknown"),
            ):
                cell = at(*cell)
                with self.subTest(face=face, cell=cell, layer=layer, bad=bad):
                    self.set_block(cell, bad, layer=layer)
                    self.assertEqual(self.bot.plan_navigation(goal).status, status)
                    result = await self.bot.navigate(goal)
                    self.assertEqual((result.status, result.frames), (status, 0))
                    self.assertEqual(self.stream.sent, [])
                    self.set_block(cell, good, layer=layer)

    async def test_ladder_descent_needs_time_and_frame_budget_before_entering(self):
        at, _ = self.prepare_ladder_descent()
        landing = at(0, 100, 0)
        for ticks, timeout in ((100, 15), (160, 1)):
            result = await self.bot.navigate(FootGoal(landing), ticks=ticks, timeout=timeout)
            self.assertEqual((result.status, result.frames), ("budget", 0))
        self.assertEqual(self.stream.sent, [])

    async def test_ladder_descent_requires_ordered_hold_lowering_and_ground_corrections(self):
        for face in self.LADDER_DESCENT_FIXTURES:
            with self.subTest(face=face):
                await self.check_ladder_descent_observations(face)

    async def check_ladder_descent_observations(self, face):
        at, pose = self.prepare_ladder_descent(face)
        _, dx, dz, heading, _ = self.LADDER_DESCENT_FIXTURES[face]
        flags = self.binding.PlayerAuthInputPacket.InputData
        goal = FootGoal(at(0, 100, -1))
        edge_status = "reached" if face in (2, 4) else "unreachable"
        edge_x, _, edge_z = pose(0.5, 101.62, 2.0)
        self.assertEqual((math.floor(edge_x), 100, math.floor(edge_z)) == at(0, 100, 2),
                         edge_status == "reached")
        sleep = asyncio.sleep

        async def yield_frame(_delay):
            await sleep(0)

        def correct(x, y, z, *, tick, on_ground=False, delta=(0.0, 0.0, 0.0)):
            self.correct_position(*pose(x, y, z), on_ground=on_ground, tick=tick, delta=delta)

        def report_position(source, x, y, z, *, tick, on_ground=False):
            x, y, z = pose(x, y, z)
            if source == "correction":
                self.correct_position(x, y, z, on_ground=on_ground, tick=tick)
            elif source == "move_player":
                self.set_position(x, z, y=y, on_ground=on_ground)
            else:
                packet = self.binding.ServerPlayerPostMovePositionPacket(pos=self.binding.Vec3(x=x, y=y, z=z))
                self.movement._on_packet(self.stream.protocol.encode(packet))

        cases = [
            ("confirmed", None, "reached"), ("align", None, "reached"), ("off_rung", None, "unreachable"),
            ("sliding_hold", None, "unreachable"), ("untimed_hold", None, "unknown"),
            ("stale_lowering", None, "unknown"), ("no_lowering", None, "unknown"),
            ("ground_at_integer_edge", None, edge_status),
        ]
        positions = {
            "left_rung": (0.5, 102.715, 1.945, "unreachable"),
            "off_axis": (0.70, 102.715, 2.194, "unreachable"),
            "below_base": (0.5, 101.50, 2.194, "unreachable"),
            "in_rung": (0.5, 102.715, 2.194, "reached"),
            "at_base_edge": (0.5, 101.62, 2.03, "reached"),
            "integer_edge_then_ground": (0.5, 101.62, 2.0, edge_status),
            "missing_ground_correction": (0.5, 101.62, 2.03, "unknown"),
        }
        cases.extend((case, source, details[-1]) for source in ("correction", "move_player", "post_move")
                     for case, details in positions.items())
        for case, source, status in cases:
            with self.subTest(case=case, source=source):
                self.stream.sent.clear()
                x, y, z = pose(0.43, 105.62, 3.5) if case == "align" else pose(0.5, 105.62, 3.75)
                self.set_position(x, z, y=y, teleport=True, on_ground=True)
                hold = release = ground = None

                def report():
                    nonlocal hold, release, ground
                    frame = self.stream.sent[-1]
                    tick = int(frame.client_tick)
                    if flags.START_SNEAKING in frame.input_data:
                        hold = tick
                    elif hold is not None and tick == hold + 7:
                        z = 1.945 if case == "off_rung" else 2.260
                        if case == "untimed_hold":
                            report_position("move_player", 0.5, 104.916, z, tick=tick)
                        else:
                            correct(0.5, 104.916, z, tick=tick, delta=(0, -0.0784, 0))
                    elif case == "sliding_hold" and hold is not None and tick == hold + 13:
                        correct(0.5, 104.316, 2.260, tick=tick - 3, delta=(0, -0.20, 0))
                        correct(0.5, 103.716, 2.260, tick=tick, delta=(0, -0.20, 0))
                    if flags.STOP_SNEAKING in frame.input_data:
                        release = tick
                    if release is None:
                        return
                    if case != "no_lowering" and tick in (release + 6, release + 12):
                        y = 104.115 if tick == release + 6 else 103.115
                        correction_tick = release - 2 if case == "stale_lowering" and tick == release + 6 else tick
                        correct(0.5, y, 2.194, tick=correction_tick)
                    if tick == release + 34:
                        # Both reports can be dispatched before navigation resumes.
                        if source is not None:
                            x, y, z, _ = positions[case]
                            report_position(source, x, y, z, tick=tick - 1,
                                            on_ground=case == "missing_ground_correction" and source == "move_player")
                        if case != "missing_ground_correction":
                            ground = tick
                            z = {"at_base_edge": 2.03, "ground_at_integer_edge": 2.0}.get(case, 2.194)
                            correct(0.5, 101.62, z, on_ground=True, tick=tick)

                self.stream.on_send = report
                with patch("endpilot.movement.asyncio.sleep", yield_frame):
                    result = await self.bot.navigate(goal)
                self.assertEqual(result.status, status)
                self.assertIsNotNone(hold)
                self.assertIsNotNone(release)
                moving = [p for p in self.stream.sent if p.move.x or p.move.y]
                approach = [p for p in moving if p.client_tick < hold and math.isclose(p.move.y, -0.15, abs_tol=1e-6)]
                first = approach[0]
                self.assertTrue(all(p.rot.y == heading and not p.move.x for p in approach))
                self.assertTrue(all(p.pos_delta.x * dx + p.pos_delta.z * dz < 0 for p in approach))
                ox, _, oz = pose(0.5, 105.62, 3.70)
                start_dx, start_dz = first.pos.x - first.pos_delta.x - ox, first.pos.z - first.pos_delta.z - oz
                self.assertTrue(-0.00001 <= start_dx * dx + start_dz * dz <= 0.22)
                self.assertAlmostEqual(start_dx * dz - start_dz * dx, 0, delta=0.02)
                ox, _, oz = pose(0.5, 105.62, 2.20)
                last = approach[-1].pos
                self.assertTrue(0 <= (last.x - ox) * dx + (last.z - oz) * dz <= 0.13)
                if case == "align":
                    self.assertTrue(any(p.move.y > 0 and p.client_tick < first.client_tick for p in moving))
                self.assertTrue(all(not p.pos_delta.y for p in self.stream.sent))
                self.assertEqual(self.jump_input(), [])
                self.assertFalse(self.movement.state.sneaking)
                after = [p for p in moving if p.client_tick >= hold]
                if status == "reached":
                    self.assertEqual(result.stand_position, goal.position)
                    self.assertGreaterEqual(release - hold, 24)
                    self.assertTrue(after)
                    self.assertGreater(after[0].client_tick, ground)
                    self.assertAlmostEqual(after[0].move.y, -0.65)
                    self.assertEqual(after[0].rot.y, heading)
                    self.assertLess(after[0].pos_delta.x * dx + after[0].pos_delta.z * dz, 0)
                else:
                    self.assertIsNone(result.stand_position)
                    self.assertEqual(after, [])
                    if case in ("left_rung", "off_axis", "below_base", "ground_at_integer_edge",
                                "integer_edge_then_ground"):
                        self.assertIsNotNone(ground)
                        self.assertEqual(self.movement._ground_correction_tick, ground)

    async def check_ladder_descent_interruption(self, cause, expected, face=2):
        at, pose = self.prepare_ladder_descent(face)
        landing = at(0, 100, 0)
        flags = self.binding.PlayerAuthInputPacket.InputData
        send, sleep, timeout_at = self.stream.send, asyncio.sleep, asyncio.timeout_at
        held, resume = asyncio.Event(), asyncio.Event()
        timeouts = []

        async def yield_frame(_delay):
            await sleep(0)

        def record_timeout(at):
            timeout = timeout_at(at)
            timeouts.append(timeout)
            return timeout

        async def block_sneak(frame):
            await send(frame)
            if flags.START_SNEAKING in frame.input_data:
                self.correct_position(*pose(0.5, 104.916, 2.260), on_ground=False, tick=frame.client_tick)
                held.set()
                await resume.wait()

        with (patch.object(self.stream, "send", block_sneak),
              patch("endpilot.movement.asyncio.sleep", yield_frame),
              patch("endpilot.movement.asyncio.timeout_at", record_timeout)):
            action = asyncio.create_task(self.bot.navigate(FootGoal(landing)))
            try:
                await asyncio.wait_for(held.wait(), 1)
                hold_count = len(self.stream.sent)
                if cause == "stop":
                    stop = asyncio.create_task(self.bot.stop_moving())
                    await sleep(0)
                    resume.set()
                    await stop
                elif cause == "cancel":
                    action.cancel()
                elif cause == "timeout":
                    _, deadline = timeouts
                    deadline.reschedule(asyncio.get_running_loop().time())
                elif cause == "support":
                    self.set_block(at(0, 99, 1), self.AIR)
                    resume.set()
                elif cause == "unknown_rung":
                    self.set_block(at(0, 102, 2), 0xFFFFFFFE)
                    resume.set()
                elif cause == "dimension":
                    raw = self.stream.protocol.encode(self.binding.ChangeDimensionPacket(dimension_id=1))
                    self.blocks._on_packet(raw)
                    self.movement._on_packet(raw)
                    resume.set()
                elif cause == "disconnect":
                    self.movement._on_packet(RawPacket(self.binding.DisconnectPacket.ID, b""))
                    resume.set()
                if isinstance(expected, str):
                    result = await asyncio.wait_for(action, 1)
                    self.assertEqual((result.status, result.stand_position), (expected, None))
                else:
                    with self.assertRaises(expected):
                        await asyncio.wait_for(action, 1)
            finally:
                if not action.done():
                    action.cancel()
                    await asyncio.gather(action, return_exceptions=True)
        self.assertFalse(self.movement.state.sneaking)
        tail = self.stream.sent[hold_count:]
        if cause in ("dimension", "disconnect"):
            self.assertEqual(tail, [])
        else:
            self.assertEqual(len(tail), 2)
            self.assertIn(flags.STOP_SNEAKING, tail[0].input_data)
            self.assertTrue(all(not p.move.x and not p.move.y and flags.SNEAKING not in p.input_data for p in tail))
            self.assertTrue(all(math.isclose(p.pos.y, 104.916, abs_tol=1e-5) for p in tail))

    async def test_ladder_descent_releases_held_sneak_on_stop(self):
        await self.check_ladder_descent_interruption("stop", "stopped")

    async def test_ladder_descent_releases_held_sneak_on_cancel(self):
        await self.check_ladder_descent_interruption("cancel", asyncio.CancelledError)

    async def test_ladder_descent_releases_held_sneak_after_timeout(self):
        await self.check_ladder_descent_interruption("timeout", "budget")

    async def test_ladder_descent_releases_held_sneak_when_support_disappears(self):
        for face in self.LADDER_DESCENT_FIXTURES:
            with self.subTest(face=face):
                await self.check_ladder_descent_interruption("support", "unreachable", face)

    async def test_ladder_descent_releases_held_sneak_when_rung_becomes_unknown(self):
        await self.check_ladder_descent_interruption("unknown_rung", "unknown", face=5)

    async def test_ladder_descent_drops_held_input_on_dimension_change(self):
        await self.check_ladder_descent_interruption("dimension", "unknown")

    async def test_ladder_descent_drops_held_input_on_disconnect(self):
        await self.check_ladder_descent_interruption("disconnect", ConnectionError)

    def prepare_drop_two(self):
        self.blocks._player_game_type = "SURVIVAL"
        self.movement._gap_jump_allowed = lambda: True
        self.correct_position(0.5, 101.62, 0.5, on_ground=True, tick=0)
        support = {(0, 99, 0), (1, 97, 0), (2, 97, 0), (3, 97, 0)}
        for x in range(-3, 4):
            for z in range(-3, 4):
                for y in range(97, 103):
                    cell = x, y, z
                    self.set_block(cell, self.STONE if cell in support else self.AIR)
                    self.set_block(cell, self.AIR, layer=1)

    async def test_drop_two_is_a_distinct_zero_send_edge_with_two_downward_units(self):
        self.prepare_drop_two()
        goal = FootGoal((1, 98, 0))
        costs = NavigationCosts(down=2.5)
        rules = NavigationRules(search_radius=3, vertical_radius=2, costs=costs)
        plan = self.bot.plan_navigation(goal, rules=rules)
        self.assertEqual((plan.status, plan.nodes, plan.cost), ("found", ((0, 100, 0), (1, 98, 0)), Fraction(5)))
        too_narrow = NavigationRules(search_radius=2, vertical_radius=2, costs=costs)
        self.assertEqual(self.bot.plan_navigation(goal, rules=too_narrow).status, "unreachable")
        self.assertEqual(self.stream.sent, [])

    async def test_drop_two_requires_known_head_space_and_all_three_low_supports(self):
        self.prepare_drop_two()
        goal = FootGoal((1, 98, 0))
        rules = NavigationRules(search_radius=3, vertical_radius=2)
        for cell, layer, blocked, restore, status in (
            ((1, 101, 0), 1, self.STONE, self.AIR, "unreachable"),
            ((3, 97, 0), 0, self.AIR, self.STONE, "unreachable"),
            ((3, 101, 0), 0, 0xFFFFFFFE, self.AIR, "unknown"),
        ):
            with self.subTest(cell=cell, layer=layer):
                self.set_block(cell, blocked, layer=layer)
                self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, status)
                result = await self.bot.navigate(goal, rules=rules, ticks=100)
                self.assertEqual((result.status, result.frames), (status, 0))
                self.assertEqual(self.stream.sent, [])
                self.set_block(cell, restore, layer=layer)

    async def test_drop_two_needs_budget_for_the_first_descent_before_moving(self):
        self.prepare_drop_two()
        rules = NavigationRules(search_radius=3, vertical_radius=2)
        goal = FootGoal((1, 98, 0))
        for ticks, timeout in ((20, 4), (100, 0.2)):
            with self.subTest(ticks=ticks, timeout=timeout):
                result = await self.bot.navigate(goal, rules=rules, ticks=ticks, timeout=timeout)
                self.assertEqual((result.status, result.frames), ("budget", 0))
                self.assertEqual(self.stream.sent, [])

    async def test_drop_two_accepts_same_batch_air_and_ground_then_walks_from_the_actual_low_cell(self):
        self.prepare_drop_two()
        rules = NavigationRules(search_radius=3, vertical_radius=2)
        goal = FootGoal((3, 98, 0))
        step_tick = ground_tick = None

        def report():
            nonlocal step_tick, ground_tick
            frame = self.stream.sent[-1]
            tick = int(frame.client_tick)
            if step_tick is None and self.up in frame.input_data:
                step_tick = tick
            if step_tick is not None and tick == step_tick + 14:
                self.correct_position(1.5, 101.2, 0.5, on_ground=False, tick=tick - 2, delta=(0.0, -0.23, 0.0))
                self.correct_position(2.5, 99.62, 0.5, on_ground=True, tick=tick)
                ground_tick = tick

        self.stream.on_send = report
        result = await self.bot.navigate(goal, rules=rules, ticks=100)
        self.assertEqual((result.status, result.stand_position), ("reached", goal.position))
        self.assertGreater(result.frames, 9)
        self.assertEqual(self.jump_input(), [])
        before = [frame for frame in self.moving_input() if frame.client_tick <= ground_tick]
        self.assertEqual(len(before), 9)
        self.assertTrue(all(abs(frame.pos.y - 101.62) < 0.001 for frame in before))
        self.assertTrue(all(frame.pos.x >= 2.5 for frame in self.stream.sent if frame.client_tick > ground_tick))

    async def test_drop_two_rejects_corrections_outside_the_current_ordered_descent(self):
        self.prepare_drop_two()
        rules = NavigationRules(search_radius=3, vertical_radius=2)
        goal = FootGoal((1, 98, 0))
        for stale_air in (True, False):
            with self.subTest(stale_air=stale_air):
                self.set_position(0.5, 0.5, teleport=True, on_ground=True)
                self.stream.sent.clear()
                step = None

                def report():
                    nonlocal step
                    frame = self.stream.sent[-1]
                    tick = int(frame.client_tick)
                    if step is None and self.up in frame.input_data:
                        step = tick
                    if step is not None and tick == step + 14:
                        air_tick = step - 1 if stale_air else tick - 1
                        ground_tick = tick if stale_air else tick - 2
                        self.correct_position(1.5, 101.2, 0.5, on_ground=False, tick=air_tick, delta=(0.0, -0.23, 0.0))
                        self.correct_position(1.5, 99.62, 0.5, on_ground=True, tick=ground_tick)
                        # A lower raw position cannot replace a fresh correction pair.
                        self.set_position(1.5, 0.5, y=99.62, on_ground=True)

                self.stream.on_send = report
                result = await self.bot.navigate(goal, rules=rules, ticks=26)
                self.assertEqual((result.status, result.frames, result.stand_position), ("budget", 9, None))
                self.assertEqual(self.jump_input(), [])

    async def test_drop_two_lower_air_position_with_upward_delta_cannot_confirm_descent(self):
        self.prepare_drop_two()
        rules = NavigationRules(search_radius=3, vertical_radius=2)
        goal = FootGoal((1, 98, 0))
        step = None

        def report():
            nonlocal step
            frame = self.stream.sent[-1]
            tick = int(frame.client_tick)
            if step is None and self.up in frame.input_data:
                step = tick
            if step is not None and tick == step + 14:
                self.correct_position(1.5, 101.2, 0.5, on_ground=False, tick=tick - 2, delta=(0.0, 0.42, 0.0))
                self.correct_position(1.5, 99.62, 0.5, on_ground=True, tick=tick)

        self.stream.on_send = report
        result = await self.bot.navigate(goal, rules=rules, ticks=26)
        self.assertEqual((result.status, result.frames, result.stand_position), ("budget", 9, None))
        self.assertEqual(self.jump_input(), [])

    async def test_drop_two_return_to_upper_ground_interrupts_same_batch_and_split_corrections(self):
        self.prepare_drop_two()
        rules = NavigationRules(search_radius=3, vertical_radius=2)
        goal = FootGoal((1, 98, 0))
        for same_batch in (False, True):
            with self.subTest(same_batch=same_batch):
                self.set_position(0.5, 0.5, teleport=True, on_ground=True)
                self.stream.sent.clear()
                step = None
                delivered = []

                def report():
                    nonlocal step
                    frame = self.stream.sent[-1]
                    tick = int(frame.client_tick)
                    if step is None and self.up in frame.input_data:
                        step = tick
                    if step is None:
                        return
                    for offset, x, y, grounded, vertical_delta in (
                        (11, 1.5, 101.2, False, -0.23),
                        (12, 0.5, 101.62, True, 0.0),
                        (13, 1.5, 99.62, True, 0.0),
                    ):
                        if tick == step + (13 if same_batch else offset):
                            delivered.append(offset)
                            self.correct_position(x, y, 0.5, on_ground=grounded, tick=step + offset,
                                                  delta=(0.0, vertical_delta, 0.0))

                self.stream.on_send = report
                result = await self.bot.navigate(goal, rules=rules, ticks=100)
                self.assertEqual(delivered, [11, 12, 13])
                self.assertEqual((result.status, result.frames, result.stand_position), ("unreachable", 9, None))

    async def test_drop_two_landing_center_near_the_lip_does_not_establish_full_support(self):
        self.prepare_drop_two()
        rules = NavigationRules(search_radius=3, vertical_radius=2)
        goal = FootGoal((1, 98, 0))
        step = None

        def report():
            nonlocal step
            frame = self.stream.sent[-1]
            tick = int(frame.client_tick)
            if step is None and self.up in frame.input_data:
                step = tick
            if step is not None and tick == step + 14:
                self.correct_position(1.5, 101.2, 0.5, on_ground=False, tick=tick - 2, delta=(0.0, -0.23, 0.0))
                self.correct_position(1.05, 99.62, 0.5, on_ground=True, tick=tick)

        self.stream.on_send = report
        result = await self.bot.navigate(goal, rules=rules, ticks=100)
        self.assertEqual((result.status, result.frames, result.stand_position), ("unreachable", 9, None))
        self.assertEqual(len([frame for frame in self.stream.sent if frame.client_tick > step + 14]), 2)

    async def test_drop_two_cancellation_releases_forward_without_predicting_a_landing(self):
        self.prepare_drop_two()
        rules = NavigationRules(search_radius=3, vertical_radius=2)
        sent = asyncio.Event()

        def observe():
            if self.up in self.stream.sent[-1].input_data:
                sent.set()

        self.stream.on_send = observe
        action = asyncio.create_task(self.bot.navigate(FootGoal((1, 98, 0)), rules=rules, ticks=100))
        await asyncio.wait_for(sent.wait(), 2)
        action.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await action
        self.assertEqual(len(self.moving_input()), 1)
        self.assertIsNotNone(self.movement._pending_landing)
        self.assertTrue(all(frame.pos_delta.y == 0 and self.up not in frame.input_data
                            for frame in self.stream.sent[1:]))

    def prepare_ascent(self):
        target = (2, 101, 0)
        solids = {(0, 99, 0), (1, 100, 0), target}
        for x in range(-2, 3):
            for z in range(-2, 3):
                for y in range(99, 104):
                    pos = (x, y, z)
                    self.set_block(pos, self.STONE if pos in solids else self.AIR)
                    self.set_block(pos, self.AIR, layer=1)
        return target

    def prepare_gap(self, *, blocked=None, landing=True):
        self.blocks._player_game_type = "SURVIVAL"
        self.movement._gap_jump_allowed = lambda: True
        self.correct_position(0.5, 101.62, 0.5, on_ground=True, tick=0)
        supports = {(0, 99, 0), (2, 99, 0)} if landing else {(0, 99, 0)}
        for x in range(-2, 5):
            for z in range(-2, 3):
                for y in range(99, 104):
                    cell = (x, y, z)
                    self.set_block(cell, self.STONE if cell in supports else self.AIR)
                    self.set_block(cell, self.STONE if cell == blocked else self.AIR, layer=1)
        return FootGoal((2, 100, 0))

    async def test_gap_plan_uses_a_distinct_two_walk_edge_without_sending_input(self):
        goal = self.prepare_gap()
        costs = NavigationCosts(walk=2.5)
        rules = NavigationRules(search_radius=2, vertical_radius=0, costs=costs)
        plan = self.bot.plan_navigation(goal, rules=rules)
        self.assertEqual((plan.status, plan.nodes, plan.cost), ("found", ((0, 100, 0), (2, 100, 0)), Fraction(5)))
        self.assertEqual(self.stream.sent, [])

        self.set_block((1, 99, 0), self.STONE)
        walked = self.bot.plan_navigation(goal, rules=rules)
        self.assertEqual(walked.nodes, ((0, 100, 0), (1, 100, 0), (2, 100, 0)))
        self.assertEqual(walked.cost, Fraction(5))
        self.assertEqual(self.stream.sent, [])

    async def test_gap_known_blockers_and_unknown_clearance_emit_no_input(self):
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        for obstacle, layer, runtime_id, status in (
            ((1, 103, 0), 1, self.STONE, "unreachable"),
            ((2, 99, 0), 0, self.AIR, "unreachable"),
            ((1, 99, 0), 1, 0xFFFFFFFE, "unknown"),
            ((2, 103, 0), 0, 0xFFFFFFFE, "unknown"),
        ):
            with self.subTest(obstacle=obstacle, layer=layer, status=status):
                goal = self.prepare_gap()
                self.set_block(obstacle, runtime_id, layer=layer)
                self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, status)
                result = await self.bot.navigate(goal, rules=rules, ticks=100)
                self.assertEqual((result.status, result.frames), (status, 0))
                self.assertEqual(self.stream.sent, [])

    async def test_gap_requires_valid_ordinary_player_state_and_game_mode(self):
        b = self.binding
        goal = self.prepare_gap()
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        state = self.bot._player_state = PlayerState(self.stream, 1, unique_id=-21)
        self.movement._gap_jump_allowed = state._gap_jump_allowed
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unreachable")

        flying = 1 << int(b.AbilitiesIndex.FLYING)
        data = b.SerializedAbilitiesData
        state._on_packet(self.stream.protocol.encode(b.UpdateAbilitiesPacket(data=data(
            target_player=-21, layers=[data.SerializedLayer(
                serialized_layer=data.SerializedAbilitiesLayer.BASE, abilities_set=flying, ability_values=0,
            )],
        ))))
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "found")
        for event, status in ((b.MobEffectPacket.Event.ADD, "unreachable"),
                              (b.MobEffectPacket.Event.REMOVE, "found")):
            state._on_packet(self.stream.protocol.encode(b.MobEffectPacket(
                runtime_id=1, event_id=event, effect_id=8, effect_amplifier=0, effect_duration_ticks=100,
            )))
            self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, status)

        state.flight_submitted(True, state.flight_target(require_permission=False)[1])
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unreachable")
        state.flight_submitted(False, state.flight_target(require_permission=False)[1])
        self.blocks._player_game_type = "CREATIVE"
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unreachable")
        self.assertEqual(self.stream.sent, [])

    async def test_gap_declines_short_budget_before_any_jump_frame(self):
        goal = self.prepare_gap()
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        for ticks, timeout in ((20, 5), (100, 0.2)):
            with self.subTest(ticks=ticks, timeout=timeout):
                result = await self.bot.navigate(goal, rules=rules, ticks=ticks, timeout=timeout)
                self.assertEqual((result.status, result.frames), ("budget", 0))
                self.assertEqual(self.stream.sent, [])

    async def test_gap_needs_an_observed_center_after_predicted_alignment(self):
        goal = self.prepare_gap()
        self.set_position(0.57, 0.5, on_ground=True)
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "found")
        result = await self.bot.navigate(goal, rules=rules, ticks=100)
        self.assertEqual(result.status, "unknown")
        self.assertTrue(self.stream.sent)
        self.assertEqual(self.jump_input(), [])
        self.assertTrue(all(frame.pos.x < 0.57 for frame in self.stream.sent))

    async def test_gap_uses_a_fresh_center_correction_for_the_launch(self):
        goal = self.prepare_gap()
        self.set_position(0.57, 0.5, on_ground=True)
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        flags = self.binding.PlayerAuthInputPacket.InputData
        centered = False
        launch_tick = None

        def report_position():
            nonlocal centered, launch_tick
            frame = self.stream.sent[-1]
            tick = int(frame.client_tick)
            if not centered and float(frame.pos.x) < 0.51:
                self.correct_position(0.5, 101.62, 0.5, on_ground=True, tick=tick)
                centered = True
            if flags.START_JUMPING in frame.input_data:
                launch_tick = tick
            if launch_tick is not None and tick == launch_tick + 14:
                self.correct_position(2.5, 101.62, 0.5, on_ground=True, tick=tick)

        self.stream.on_send = report_position
        result = await self.bot.navigate(goal, rules=rules, ticks=100)
        self.assertEqual((result.status, result.stand_position), ("reached", goal.position))
        launch, = self.jump_input()
        self.assertAlmostEqual(float(launch.pos.x) - float(launch.pos_delta.x), 0.5, places=5)

    async def test_gap_counts_committed_frames_and_waits_for_fresh_far_grounding_and_release(self):
        goal = self.prepare_gap()
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        launch = None
        flags = self.binding.PlayerAuthInputPacket.InputData

        def report_landing():
            nonlocal launch
            frame = self.stream.sent[-1]
            if flags.START_JUMPING in frame.input_data:
                launch = int(frame.client_tick)
            if launch is None:
                return
            tick = int(frame.client_tick)
            if tick == launch + 11:
                # Prefix corrections cannot complete a jump at the other end.
                self.correct_position(2.5, 101.62, 0.5, on_ground=True, tick=launch - 1)
            if tick == launch + 14:
                self.correct_position(2.5, 101.62, 0.5, on_ground=True, tick=tick)

        self.stream.on_send = report_landing
        result = await self.bot.navigate(goal, rules=rules, ticks=21)
        self.assertEqual((result.status, result.frames, result.stand_position), ("reached", 12, goal.position))
        self.assertEqual(len(self.stream.sent), 21)
        self.assertEqual(len(self.jump_input()), 1)
        self.assertTrue(all(self.up not in frame.input_data for frame in self.stream.sent[12:]))

    async def test_gap_repeated_grounding_at_the_same_pose_preserves_braking_progress(self):
        goal = self.prepare_gap()
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        launch = None
        landings = []
        flags = self.binding.PlayerAuthInputPacket.InputData

        def report_position():
            nonlocal launch
            frame = self.stream.sent[-1]
            tick = int(frame.client_tick)
            if flags.START_JUMPING in frame.input_data:
                launch = tick
            if launch is not None and tick >= launch + 14:
                landings.append(tick)
                self.correct_position(2.5, 101.62, 0.5, on_ground=True, tick=tick)

        self.stream.on_send = report_position
        result = await self.bot.navigate(goal, rules=rules, ticks=21)
        self.assertEqual((result.status, result.frames, result.stand_position), ("reached", 12, goal.position))
        self.assertEqual(landings, list(range(launch + 14, launch + 21)))

    async def test_gap_keeps_forward_frames_on_the_checked_axis_after_public_rotation(self):
        goal = self.prepare_gap()
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        flags = self.binding.PlayerAuthInputPacket.InputData
        launch = None
        original_send = self.stream.send

        async def rotate_after_launch(packet):
            nonlocal launch
            await original_send(packet)
            tick = int(packet.client_tick)
            if flags.START_JUMPING in packet.input_data:
                launch = tick
                await self.bot.set_rotation(0, 0)
            if launch is not None and tick == launch + 14:
                self.correct_position(2.5, 101.62, 0.5, on_ground=True, tick=tick)

        self.stream.send = rotate_after_launch
        result = await self.bot.navigate(goal, rules=rules, ticks=21)
        for frame in self.moving_input():
            self.assertAlmostEqual(frame.pos_delta.x, 0.09, places=5)
            self.assertAlmostEqual(frame.pos_delta.z, 0.0, places=5)
        self.assertEqual((result.status, result.frames, result.stand_position), ("reached", 12, goal.position))

    async def test_gap_requires_wire_precision_for_unconfirmed_takeoff_height(self):
        goal = self.prepare_gap()
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        flags = self.binding.PlayerAuthInputPacket.InputData
        for height, expected in (
            (101.66, "unknown"), (101.6200180053711, "unknown"),
            (101.62, "reached"), (101.62001037597656, "reached"),
        ):
            with self.subTest(height=height):
                launch = None
                self.stream.sent.clear()
                self.set_position(0.5, 0.5, y=height, teleport=True)

                def report_landing():
                    nonlocal launch
                    frame = self.stream.sent[-1]
                    tick = int(frame.client_tick)
                    if flags.START_JUMPING in frame.input_data:
                        launch = tick
                    if launch is not None and tick == launch + 14:
                        self.correct_position(2.5, 101.62, 0.5, on_ground=True, tick=tick)

                self.stream.on_send = report_landing
                result = await self.bot.navigate(goal, rules=rules, ticks=25)
                self.assertEqual(result.status, expected)
                self.assertEqual(len(self.jump_input()), int(expected == "reached"))

    async def test_gap_does_not_accept_edge_contact_or_position_only_at_the_far_cell(self):
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        flags = self.binding.PlayerAuthInputPacket.InputData
        for confirmed, expected in ((True, "unreachable"), (False, "budget")):
            with self.subTest(confirmed=confirmed):
                self.stream.sent.clear()
                self.set_position(0.5, 0.5, teleport=True)
                goal = self.prepare_gap()
                launch = None

                def report_position():
                    nonlocal launch
                    frame = self.stream.sent[-1]
                    if flags.START_JUMPING in frame.input_data:
                        launch = int(frame.client_tick)
                    if launch is not None and frame.client_tick == launch + 14:
                        if confirmed:
                            self.correct_position(1.98, 101.62, 0.5, on_ground=True, tick=frame.client_tick)
                        else:
                            self.set_position(2.5, 0.5)

                self.stream.on_send = report_position
                result = await self.bot.navigate(goal, rules=rules, ticks=26)
                self.assertEqual((result.status, result.frames, result.stand_position), (expected, 12, None))
                self.assertEqual(len(self.jump_input()), 1)

    async def test_gap_cancel_preserves_successful_input_and_unconfirmed_landing(self):
        goal = self.prepare_gap()
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        launch = asyncio.Event()
        flags = self.binding.PlayerAuthInputPacket.InputData

        def on_send():
            if flags.START_JUMPING in self.stream.sent[-1].input_data:
                launch.set()

        self.stream.on_send = on_send
        action = asyncio.create_task(self.bot.navigate(goal, rules=rules, ticks=100))
        await asyncio.wait_for(launch.wait(), 2)
        action.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await action
        self.assertEqual(len(self.jump_input()), 1)
        self.assertIsNotNone(self.movement._pending_landing)
        self.assertTrue(all(self.up not in frame.input_data for frame in self.stream.sent[1:]))
        self.assertNotEqual(self.bot.plan_navigation(goal, rules=rules).status, "found")

    async def test_gap_cancel_before_launch_submission_preserves_the_starting_pose(self):
        goal = self.prepare_gap()
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        flags = self.binding.PlayerAuthInputPacket.InputData
        entered = asyncio.Event()
        original_send = self.stream.send

        async def hold_launch(packet):
            if flags.START_JUMPING in packet.input_data:
                entered.set()
                await asyncio.Future()
            await original_send(packet)

        self.stream.send = hold_launch
        start = self.movement.input_position
        action = asyncio.create_task(self.bot.navigate(goal, rules=rules, ticks=100))
        await asyncio.wait_for(entered.wait(), 2)
        action.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await action
        self.assertEqual(self.stream.sent, [])
        self.assertEqual(self.movement.input_position, start)
        self.assertIsNone(self.movement._pending_landing)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "found")

    async def test_gap_teleport_uses_the_new_pose_instead_of_the_previous_landing(self):
        goal = self.prepare_gap()
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        flags = self.binding.PlayerAuthInputPacket.InputData

        def report_teleport():
            if flags.START_JUMPING in self.stream.sent[-1].input_data:
                self.set_position(0.5, 1.5, teleport=True, on_ground=True)

        self.stream.on_send = report_teleport
        result = await self.bot.navigate(goal, rules=rules, ticks=100)
        self.assertEqual((result.status, result.frames, result.stand_position), ("unreachable", 1, None))
        self.assertEqual(len(self.jump_input()), 1)
        self.assertIsNone(self.movement._pending_landing)
        self.assertAlmostEqual(self.movement.position[2], 1.5)

    def prepare_multilevel(self, target, supports, radius, *, unknown=()):
        solids = {target, *supports}
        for x in range(-radius, radius + 1):
            for z in range(-radius, radius + 1):
                for y in range(95, max(107, target[1] + 3)):
                    cell = (x, y, z)
                    if cell not in unknown:
                        self.set_block(cell, self.STONE if cell in solids else self.AIR)
                    self.set_block(cell, self.AIR, layer=1)

    def ascent_route(self, steps=2):
        target = (steps + 1, 100 + steps, 0)
        self.prepare_multilevel(target, ((x, 99 + x, 0) for x in range(steps + 1)), steps + 1)
        jump_ticks = []
        landing_ticks = []
        start_jumping = self.binding.PlayerAuthInputPacket.InputData.START_JUMPING

        def report_landings():
            frame = self.stream.sent[-1]
            if start_jumping in frame.input_data:
                jump_ticks.append(frame.client_tick)
            if len(jump_ticks) > len(landing_ticks) and len(jump_ticks) <= steps:
                column = len(jump_ticks)
                if self.up in frame.input_data and frame.pos.x >= column + 0.2:
                    self.correct_position(column + 0.5, 101.62 + column, 0.5,
                                          on_ground=True, tick=frame.client_tick)
                    landing_ticks.append(frame.client_tick)

        self.stream.on_send = report_landings
        return target, jump_ticks, landing_ticks

    async def test_multilevel_confirmation_precedes_each_next_ascent(self):
        target, jumps, landings = self.ascent_route()
        result = await self.bot.approach_block(target, search_radius=3)
        self.assertEqual((result.status, result.stand_position, result.face),
                         ("reached", (2, 102, 0), 4))
        first_jump, second_jump = jumps
        first_landing, second_landing = landings
        self.assertLessEqual(first_jump, first_landing)
        self.assertLess(first_landing, second_jump)
        self.assertLessEqual(second_jump, second_landing)
        self.assert_released()

    async def test_multilevel_edges_share_one_action_budget(self):
        target, jumps, landings = self.ascent_route()
        result = await self.bot.approach_block(target, ticks=17, search_radius=3)
        self.assertEqual(result.status, "budget_exhausted")
        self.assertEqual(len(jumps), 2)
        self.assertEqual(len(landings), 1)
        self.assertLessEqual(len(self.stream.sent), 19)
        self.assert_released()

    async def test_same_level_target_can_require_ascent_and_descent(self):
        target = (4, 100, 0)
        self.prepare_multilevel(target, ((0, 99, 0), (1, 100, 0), (2, 99, 0), (3, 99, 0)), 4)
        upper_tick = None
        drop_started = False
        paused = 0
        lower_tick = None

        def report_landings():
            nonlocal upper_tick, drop_started, paused, lower_tick
            frame = self.stream.sent[-1]
            if upper_tick is None and self.up in frame.input_data and frame.pos.x >= 1.2:
                upper_tick = frame.client_tick
                self.correct_position(1.5, 102.62, 0.5, on_ground=True, tick=upper_tick)
            elif upper_tick is not None and not drop_started and frame.pos.x >= 2.3:
                drop_started = True
                self.correct_position(2.25, 102.2, 0.5, on_ground=False, tick=frame.client_tick)
            elif drop_started and lower_tick is None and self.up not in frame.input_data:
                paused += 1
                if paused == 2:
                    lower_tick = frame.client_tick
                    self.correct_position(3.5, 101.62, 0.5, on_ground=True, tick=lower_tick)

        self.stream.on_send = report_landings
        result = await self.bot.approach_block(target, search_radius=4)
        self.assertEqual((result.status, result.stand_position, result.face),
                         ("reached", (3, 100, 0), 4))
        launch, = self.jump_input()
        self.assertLessEqual(launch.client_tick, upper_tick)
        self.assertLess(upper_tick, lower_tick)

    async def test_multilevel_retry_rejects_position_only_descent_until_fresh_grounding(self):
        target = (4, 100, 0)
        self.prepare_multilevel(target, ((0, 99, 0), (1, 100, 0), (2, 99, 0), (3, 99, 0)), 4)
        b = self.binding
        upper_tick = None
        relocated = False

        def report_position():
            nonlocal upper_tick, relocated
            frame = self.stream.sent[-1]
            if upper_tick is None and self.up in frame.input_data and frame.pos.x >= 1.2:
                upper_tick = frame.client_tick
                self.correct_position(1.5, 102.62, 0.5, on_ground=True, tick=upper_tick)
            elif upper_tick is not None and not relocated and frame.pos.x >= 2.3:
                relocated = True
                update = b.ServerPlayerPostMovePositionPacket(pos=b.Vec3(x=3.5, y=101.62, z=0.5))
                self.movement._on_packet(self.stream.protocol.encode(update))

        self.stream.on_send = report_position
        result = await self.bot.approach_block(target, ticks=32, search_radius=4)
        self.assertEqual(result.status, "budget_exhausted")
        self.assertTrue(relocated)
        self.assertEqual(self.bot.movement_state.correction_count, 1)
        before = len(self.stream.sent)
        self.assertEqual(await self.bot.approach_block(target, ticks=0, search_radius=4),
                         BlockApproach("unreachable", 0))
        self.assertEqual(len(self.stream.sent), before)
        self.correct_position(3.5, 101.62, 0.5, on_ground=True, tick=self.stream.sent[-1].client_tick)
        self.assertEqual(await self.bot.approach_block(target, ticks=0, search_radius=4),
                         BlockApproach("reached", 0, (3, 100, 0), 4))
        self.assertEqual(len(self.jump_input()), 1)

    async def test_multilevel_dimension_change_during_ascent_invalidates_the_old_edge(self):
        target, _, _ = self.ascent_route()
        b = self.binding
        new_pos = b.Vec3(x=12.5, y=103.62, z=0.5)

        def change_dimension():
            if self.jump_input() and len(self.stream.sent) == 1:
                change = self.stream.protocol.encode(b.ChangeDimensionPacket(dimension_id=1, pos=new_pos))
                self.blocks._on_packet(change)
                self.movement._on_packet(change)
                moved = b.ServerPlayerPostMovePositionPacket(pos=new_pos)
                self.movement._on_packet(self.stream.protocol.encode(moved))

        self.stream.on_send = change_dimension
        self.assertEqual(await self.bot.approach_block(target, search_radius=3),
                         BlockApproach("unobserved", 0))
        self.set_block((12, 101, 0), self.STONE)
        for y in (102, 103):
            self.set_block((12, y, 0), self.AIR)
            self.set_block((12, y, 0), self.AIR, layer=1)
        self.set_block((13, 102, 0), self.STONE)
        self.assertEqual(await self.bot.approach_block((13, 102, 0), ticks=0),
                         BlockApproach("reached", 0, (12, 102, 0), 4))
        self.assertEqual(len(self.jump_input()), 1)

    async def test_multilevel_can_search_beyond_the_default_vertical_radius(self):
        target, jumps, landings = self.ascent_route(steps=5)
        self.assertEqual(await self.bot.approach_block(target, search_radius=6),
                         BlockApproach("unreachable", 0))
        self.assertEqual(self.stream.sent, [])
        result = await self.bot.approach_block(target, search_radius=6, vertical_radius=5)
        self.assertEqual((result.status, result.stand_position, result.face),
                         ("reached", (5, 105, 0), 4))
        self.assertEqual(len(jumps), 5)
        self.assertEqual(len(landings), 5)
        self.assertTrue(all(landing < jump for landing, jump in zip(landings, jumps[1:])))

    async def test_multilevel_rejects_route_when_second_ascent_is_unobserved(self):
        target, jumps, _ = self.ascent_route()
        self.set_block((2, 104, 0), 0xFFFFFFFE, layer=1)
        self.assertEqual(await self.bot.approach_block(target, search_radius=3),
                         BlockApproach("unobserved", 0))
        self.assertEqual(jumps, [])
        self.assertEqual(self.stream.sent, [])

    async def test_multilevel_teleport_keeps_the_original_vertical_bounds(self):
        target, _, _ = self.ascent_route()
        self.set_block((1, 104, 1), self.STONE)

        def teleport():
            if self.jump_input() and len(self.stream.sent) == 1:
                self.set_position(1.5, 1.5, y=106.62, teleport=True)

        self.stream.on_send = teleport
        result = await self.bot.approach_block(target, search_radius=3)
        self.assertEqual(result, BlockApproach("unreachable", 0))
        self.assertEqual(len(self.jump_input()), 1)

    async def test_multilevel_does_not_jump_the_same_directed_edge_after_a_teleport(self):
        target, _, _ = self.ascent_route()

        def teleport():
            if self.jump_input() and len(self.stream.sent) == 1:
                self.set_position(0.5, 0.5, teleport=True)

        self.stream.on_send = teleport
        result = await self.bot.approach_block(target, search_radius=3)
        self.assertEqual(result, BlockApproach("unreachable", 0))
        self.assertEqual(len(self.jump_input()), 1)

    def jump_input(self):
        flag = self.binding.PlayerAuthInputPacket.InputData.START_JUMPING
        return [frame for frame in self.stream.sent if flag in frame.input_data]

    async def assert_late_ascent_landing_releases_jump(self):
        tick = self.stream.sent[-1].client_tick
        self.correct_position(1.5, 102.62, 0.5, on_ground=True, tick=tick)
        for _ in range(2):
            await self.movement._send_input(False, move_vector=self.movement._manual_directions())
        for frame in self.stream.sent[-2:]:
            self.assertAlmostEqual(frame.pos.y, 102.62, places=4)
            self.assertEqual(frame.pos_delta.y, 0.0)
        self.assertEqual(len(self.jump_input()), 1)

    async def test_ascent_requires_fresh_grounding_and_rebases_the_jump_prediction(self):
        target = self.prepare_ascent()
        airborne_tick = None
        resting = 0

        def report_position():
            nonlocal airborne_tick, resting
            frame = self.stream.sent[-1]
            if airborne_tick is None and self.up in frame.input_data and frame.pos.x >= 1.2:
                airborne_tick = frame.client_tick
                self.correct_position(1.5, 102.62, 0.5, on_ground=False, tick=airborne_tick)
            elif airborne_tick is not None and self.up not in frame.input_data:
                resting += 1
                if resting == 1:
                    self.correct_position(1.5, 102.62, 0.5, on_ground=True, tick=airborne_tick - 1)
                if resting == 3:
                    self.correct_position(1.5, 102.62, 0.5, on_ground=True, tick=frame.client_tick)

        self.stream.on_send = report_position
        result = await self.bot.approach_block(target, search_radius=2)
        self.assertEqual((result.status, result.stand_position, result.face),
                         ("reached", (1, 101, 0), 4))
        self.assertEqual(result.frames, len(self.moving_input()))
        launch, = self.jump_input()
        self.assertNotIn(self.up, launch.input_data)
        self.assertGreaterEqual(resting, 3)
        self.assertTrue(all(abs(frame.pos.y - 102.62) < 0.05 for frame in self.stream.sent[-2:]))
        self.assert_released()

    async def test_ascent_checks_both_columns_and_both_layers_above_the_ledge(self):
        for cell, layer, runtime_id, status in (
            ((0, 102, 0), 0, self.STONE, "unreachable"),
            ((0, 103, 0), 1, self.STONE, "unreachable"),
            ((1, 103, 0), 0, self.STONE, "unreachable"),
            ((1, 103, 0), 1, 0xFFFFFFFE, "unobserved"),
            ((1, 100, 0), 0, self.AIR, "unreachable"),
        ):
            with self.subTest(cell=cell, layer=layer, status=status):
                target = self.prepare_ascent()
                self.set_block(cell, runtime_id, layer=layer)
                self.assertEqual(await self.bot.approach_block(target, search_radius=2),
                                 BlockApproach(status, 0))
                self.assertEqual(self.stream.sent, [])

    async def test_ascent_lands_with_foot_overlap_before_the_center_reaches_the_upper_cell(self):
        target = self.prepare_ascent()
        corrected = False
        overlap = None

        def report_position():
            nonlocal corrected, overlap
            frame = self.stream.sent[-1]
            if self.up not in frame.input_data:
                return
            if not corrected and frame.pos.x > 0.9 and frame.pos.y < 102.7:
                corrected = True
                self.correct_position(0.75, frame.pos.y, 0.5, on_ground=False, tick=frame.client_tick)
            elif corrected and overlap is None:
                overlap = frame
                self.correct_position(frame.pos.x, 102.62, 0.5, on_ground=True, tick=frame.client_tick)
            elif frame.pos.x >= 1.2:
                self.correct_position(1.5, 102.62, 0.5, on_ground=True, tick=frame.client_tick)

        self.stream.on_send = report_position
        result = await self.bot.approach_block(target, search_radius=2)
        self.assertEqual((result.status, result.stand_position), ("reached", (1, 101, 0)))
        self.assertIsNotNone(overlap)
        self.assertTrue(0.7 < overlap.pos.x < 1)
        self.assertAlmostEqual(overlap.pos.y, 102.62, places=4)
        self.assertTrue(any(frame.pos.x >= 1.2 for frame in self.moving_input()))
        self.assertEqual(len(self.jump_input()), 1)

    async def test_ascent_still_descends_when_the_feet_do_not_overlap_the_upper_cell(self):
        target = self.prepare_ascent()
        corrected = False
        falling = asyncio.get_running_loop().create_future()
        released = asyncio.get_running_loop().create_future()

        def report_position():
            nonlocal corrected
            frame = self.stream.sent[-1]
            if falling.done() and not released.done():
                released.set_result(frame)
            if self.up not in frame.input_data:
                return
            if corrected and not falling.done():
                falling.set_result(frame)
            elif not corrected and frame.pos.x > 0.9 and frame.pos.y < 102.7:
                corrected = True
                self.correct_position(0.55, frame.pos.y, 0.5, on_ground=False, tick=frame.client_tick)

        self.stream.on_send = report_position
        task = asyncio.create_task(self.bot.approach_block(target, search_radius=2))
        try:
            frame = await asyncio.wait_for(falling, 2)
            self.assertLess(frame.pos.x + 0.30, 1.0)
            self.assertLess(frame.pos.y, 102.60)
            after = await asyncio.wait_for(released, 2)
            self.assertNotIn(self.up, after.input_data)
            self.assertEqual(after.pos.x, frame.pos.x)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_ascent_consumes_the_same_budget_while_waiting_at_takeoff(self):
        target = self.prepare_ascent()
        result = await self.bot.approach_block(target, ticks=2, search_radius=2)
        self.assertEqual(result, BlockApproach("budget_exhausted", 0))
        self.assertEqual(len(self.jump_input()), 1)
        # Two budgeted frames, then the action's ordinary release frames.
        self.assertEqual(len(self.stream.sent), 4)
        self.assert_released()
        await self.assert_late_ascent_landing_releases_jump()

    async def test_ascent_retry_needs_server_grounding_after_predicted_contact(self):
        target = self.prepare_ascent()
        exhausted = await self.bot.approach_block(target, ticks=16, search_radius=2)
        self.assertEqual(exhausted.status, "budget_exhausted")
        self.assertAlmostEqual(self.movement.input_position[1], 102.62, places=4)
        self.assertEqual(self.bot.movement_state.correction_count, 0)
        launch, = self.jump_input()
        sent = len(self.stream.sent)

        self.assertEqual(await self.bot.approach_block(target, ticks=2, search_radius=2),
                         BlockApproach("unreachable", 0))
        # A position update and a pre-jump correction cannot confirm this landing.
        self.set_position(1.5, 0.5, y=102.62)
        self.correct_position(1.5, 102.62, 0.5, on_ground=True, tick=launch.client_tick - 1)
        self.assertEqual(await self.bot.approach_block(target, ticks=2, search_radius=2),
                         BlockApproach("unreachable", 0))
        self.correct_position(0.85, 102.62, 0.5, on_ground=True, tick=self.stream.sent[-2].client_tick)
        self.set_position(1.5, 0.5, y=102.62)
        self.assertEqual(await self.bot.approach_block(target, ticks=2, search_radius=2),
                         BlockApproach("unreachable", 0))
        self.assertEqual(len(self.stream.sent), sent)
        self.correct_position(1.5, 102.62, 0.5, on_ground=True, tick=self.stream.sent[-1].client_tick)
        self.assertEqual(await self.bot.approach_block(target, ticks=2, search_radius=2),
                         BlockApproach("reached", 0, (1, 101, 0), 4))
        self.assertEqual(len(self.jump_input()), 1)

    async def test_ascent_retry_uses_a_different_observed_safe_landing(self):
        target = self.prepare_ascent()
        self.set_block((2, 100, 1), self.STONE)
        exhausted = await self.bot.approach_block(target, ticks=16, search_radius=2)
        self.assertEqual(exhausted.status, "budget_exhausted")
        self.assertEqual(await self.bot.approach_block(target, ticks=2, search_radius=2),
                         BlockApproach("unreachable", 0))
        self.correct_position(2.5, 102.62, 1.5, on_ground=True, tick=self.stream.sent[-1].client_tick)
        self.assertEqual(await self.bot.approach_block(target, ticks=2, search_radius=2),
                         BlockApproach("reached", 0, (2, 101, 1), 3))
        self.assertEqual(len(self.jump_input()), 1)

    async def test_ascent_pending_rebases_only_after_dimension_position_sync(self):
        target = self.prepare_ascent()
        exhausted = await self.bot.approach_block(target, ticks=16, search_radius=2)
        self.assertEqual(exhausted.status, "budget_exhausted")
        b = self.binding
        change = self.stream.protocol.encode(b.ChangeDimensionPacket(
            dimension_id=1, pos=b.Vec3(x=2.5, y=102.62, z=1.5),
        ))
        self.blocks._on_packet(change)
        self.movement._on_packet(change)
        self.prepare_ascent()
        self.set_block((2, 100, 1), self.STONE)
        # A correction to the old dimension's landing may arrive late.
        self.correct_position(1.5, 102.62, 0.5, on_ground=True, tick=self.stream.sent[-1].client_tick)
        self.assertEqual(await self.bot.approach_block(target, ticks=2, search_radius=2),
                         BlockApproach("unreachable", 0))
        moved = b.ServerPlayerPostMovePositionPacket(pos=b.Vec3(x=2.5, y=102.62, z=1.5))
        self.movement._on_packet(self.stream.protocol.encode(moved))
        self.assertEqual(await self.bot.approach_block(target, ticks=2, search_radius=2),
                         BlockApproach("reached", 0, (2, 101, 1), 3))
        self.assertEqual(len(self.jump_input()), 1)

    async def test_respawn_releases_pending_ascent_after_position_resynchronizes(self):
        target = self.prepare_ascent()
        result = await self.bot.approach_block(target, ticks=16, search_radius=2)
        self.assertEqual(result.status, "budget_exhausted")
        self.assertEqual(await self.bot.approach_block(target, ticks=2, search_radius=2),
                         BlockApproach("unreachable", 0))
        b = self.binding
        respawn = b.RespawnPacket(pos=b.Vec3(x=1.5, y=102.62, z=0.5),
                                  state=b.PlayerRespawnState.READY_TO_SPAWN, runtime_id=1)
        self.movement._on_packet(self.stream.protocol.encode(respawn))
        self.assertEqual(await self.bot.approach_block(target, ticks=2, search_radius=2),
                         BlockApproach("reached", 0, (1, 101, 0), 4))
        self.assertEqual(len(self.jump_input()), 1)

    async def test_ascent_stop_and_cancellation_release_input_without_a_second_jump(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                target = self.prepare_ascent()
                self.set_position(0.5, 0.5, teleport=True)
                self.stream.sent.clear()
                launched = asyncio.Event()
                self.stream.on_send = lambda: launched.set() if self.jump_input() else None
                task = asyncio.create_task(self.bot.approach_block(target, search_radius=2))
                await asyncio.wait_for(launched.wait(), 2)
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    await asyncio.wait_for(self.bot.stop_moving(), 2)
                    self.assertEqual(await task, BlockApproach("stopped", 0))
                self.assertEqual(len(self.jump_input()), 1)
                self.assertEqual(self.moving_input(), [])
                release = self.binding.PlayerAuthInputPacket.InputData.JUMP_RELEASED_RAW
                self.assertIn(release, self.stream.sent[-2].input_data)
                await self.assert_late_ascent_landing_releases_jump()

    async def test_teleport_during_ascent_replans_from_the_actual_upper_stance(self):
        target = self.prepare_ascent()

        def teleport():
            if len(self.stream.sent) == 1:
                self.set_position(1.5, 0.5, y=102.62, teleport=True)

        self.stream.on_send = teleport
        self.assertEqual(await self.bot.approach_block(target, search_radius=2),
                         BlockApproach("reached", 0, (1, 101, 0), 4))
        self.assertEqual(len(self.jump_input()), 1)
        handled = self.binding.PlayerAuthInputPacket.InputData.HANDLED_TELEPORT
        self.assertIn(handled, self.stream.sent[1].input_data)
        self.assertEqual(await self.bot.approach_block(target, ticks=0, search_radius=2),
                         BlockApproach("reached", 0, (1, 101, 0), 4))

    async def test_descent_rechecks_a_removed_or_unknown_target_before_the_edge(self):
        for runtime_id, status in ((self.AIR, "unreachable"), (0xFFFFFFFE, "unobserved")):
            with self.subTest(status=status):
                target = self.prepare_descent()
                self.set_position(0.5, 0.5, teleport=True)
                self.stream.sent.clear()

                def change_target():
                    if len(self.stream.sent) == 1:
                        self.set_block(target, runtime_id)

                self.stream.on_send = change_target
                result = await self.bot.approach_block(target, search_radius=3)
                self.assertEqual(result, BlockApproach(status, 1))
                self.assertEqual(len(self.moving_input()), 1)
                self.assertLess(self.moving_input()[0].pos.x, 1)
                self.assert_released()

    async def test_descends_only_after_a_fresh_ground_correction_at_the_actual_low_cell(self):
        target = self.prepare_descent()
        crossed = False
        resting = 0

        def report_descent():
            nonlocal crossed, resting
            packet = self.stream.sent[-1]
            if not crossed and packet.pos.x >= 1.0:
                crossed = True
                self.correct_position(1.6, 101.38, 0.5, on_ground=False, tick=packet.client_tick)
            elif crossed and self.up not in packet.input_data:
                resting += 1
                if resting == 1:
                    # Correct height and column alone do not prove landing.
                    self.correct_position(2.5, 100.62, 0.5, on_ground=False, tick=packet.client_tick)
                if resting == 3:
                    self.correct_position(2.5, 100.62, 0.5, on_ground=True, tick=packet.client_tick)

        self.stream.on_send = report_descent
        result = await self.bot.approach_block(target, search_radius=3)
        self.assertEqual(result, BlockApproach("reached", 6, (2, 99, 0), 4))
        self.assertGreaterEqual(resting, 3)
        self.assertTrue(all(packet.pos_delta.y == 0 for packet in self.stream.sent))
        self.assertTrue(all(packet.pos.x < 1.1 for packet in self.moving_input()))
        self.assert_released()

    async def test_descent_needs_runout_support_and_two_clear_layers_above_each_column(self):
        for obstruction in ("narrow", "landing_head", "runout_extra", "radius"):
            with self.subTest(obstruction=obstruction):
                search_radius = 3
                target = self.prepare_descent((1, 99, 1) if obstruction == "radius" else (3, 99, 0))
                if obstruction == "narrow":
                    self.set_block((2, 98, 0), self.AIR)
                elif obstruction == "landing_head":
                    self.set_block((1, 101, 0), self.STONE)
                elif obstruction == "runout_extra":
                    self.set_block((2, 101, 0), self.STONE, layer=1)
                else:
                    search_radius = 1
                self.assertEqual(await self.bot.approach_block(target, search_radius=search_radius),
                                 BlockApproach("unreachable", 0))
                self.assertEqual(self.stream.sent, [])

    async def test_one_block_descent_requires_air_at_the_source_landing_and_runout(self):
        self.prepare_descent()
        source, landing, runoff = (0, 100, 0), (1, 99, 0), (2, 99, 0)
        goal = FootGoal(landing)
        rules = NavigationRules(search_radius=2, vertical_radius=1)
        plan = self.bot.plan_navigation(goal, rules=rules)
        self.assertEqual((plan.status, plan.nodes), ("found", (source, landing)))
        self.assertEqual(self.stream.sent, [])

        for cell in (source, landing, runoff):
            with self.subTest(cell=cell):
                self.set_block(cell, self.DANDELION)
                plan = self.bot.plan_navigation(goal, rules=rules)
                self.assertEqual((plan.status, plan.nodes), ("unreachable", ()))
                self.assertEqual(self.stream.sent, [])
                plans = []
                result = await self.bot.navigate(goal, rules=rules, on_plan=plans.append)
                self.assertEqual((result.status, result.frames), ("unreachable", 0))
                self.assertEqual((plans, self.stream.sent), ([], []))
                self.set_block(cell, self.AIR)

    async def test_neutral_descent_frames_share_the_budget_and_stale_ground_is_ignored(self):
        target = self.prepare_descent()
        corrected = False

        def report_stale_ground():
            nonlocal corrected
            packet = self.stream.sent[-1]
            if not corrected and packet.pos.x >= 1.0:
                corrected = True
                self.correct_position(2.5, 100.62, 0.5, on_ground=True, tick=0)

        self.stream.on_send = report_stale_ground
        self.assertEqual(await self.bot.approach_block(target, ticks=8, search_radius=3),
                         BlockApproach("budget_exhausted", 6))
        self.assertEqual(len(self.stream.sent), 8)
        self.assert_released()

    async def test_stop_during_descent_releases_input_without_landing_success(self):
        target = self.prepare_descent()
        waiting = asyncio.Event()

        def cross_the_edge():
            packet = self.stream.sent[-1]
            if self.up not in packet.input_data:
                waiting.set()

        self.stream.on_send = cross_the_edge
        pending = asyncio.create_task(self.bot.approach_block(target, search_radius=3))
        await asyncio.wait_for(waiting.wait(), 2)
        await asyncio.wait_for(self.bot.stop_moving(), 2)
        self.assertEqual(await pending, BlockApproach("stopped", 6))
        self.assert_released()
        self.assertEqual(await self.bot.walk_forward(1), 1)

    async def test_teleport_during_descent_discards_the_interrupted_landing(self):
        target = self.prepare_descent()
        teleported = False

        def teleport():
            nonlocal teleported
            if not teleported and self.up not in self.stream.sent[-1].input_data:
                teleported = True
                self.set_position(0.5, 0.5, y=102.62, teleport=True)

        self.stream.on_send = teleport
        result = await self.bot.approach_block(target, search_radius=3)
        self.assertTrue(teleported)
        self.assertEqual(result, BlockApproach("unreachable", 6))
        self.assertIn(self.binding.PlayerAuthInputPacket.InputData.HANDLED_TELEPORT,
                      self.stream.sent[-2].input_data)
        self.assertAlmostEqual(self.stream.sent[-1].pos.y, 102.62, places=4)

    async def test_cancel_during_descent_releases_the_action_for_reuse(self):
        target = self.prepare_descent()
        waiting = asyncio.Event()

        def mark_release():
            if self.up not in self.stream.sent[-1].input_data:
                waiting.set()

        self.stream.on_send = mark_release
        pending = asyncio.create_task(self.bot.approach_block(target, search_radius=3))
        await asyncio.wait_for(waiting.wait(), 2)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assert_released()
        self.assertEqual(await self.bot.walk_forward(1), 1)

    async def test_descent_retry_rejects_air_at_ground_height_until_grounding_or_teleport(self):
        for interruption in ("budget", "cancel"):
            with self.subTest(interruption=interruption):
                target = self.prepare_descent()
                self.set_position(0.5, 0.5, teleport=True)
                airborne = asyncio.Event()
                airborne_tick = None

                def observe_air():
                    nonlocal airborne_tick
                    packet = self.stream.sent[-1]
                    if airborne_tick is None and packet.pos.x >= 1:
                        airborne_tick = packet.client_tick
                        self.correct_position(2.5, 100.62, 0.5, on_ground=False, tick=airborne_tick)
                        airborne.set()

                self.stream.on_send = observe_air
                if interruption == "cancel":
                    pending = asyncio.create_task(self.bot.approach_block(target, search_radius=3))
                    await asyncio.wait_for(airborne.wait(), 2)
                    pending.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await pending
                else:
                    result = await self.bot.approach_block(target, ticks=8, search_radius=3)
                    self.assertEqual(result, BlockApproach("budget_exhausted", 6))
                self.stream.on_send = None

                sent_before_retry = len(self.stream.sent)
                self.assertEqual(await self.bot.approach_block(target, ticks=0),
                                 BlockApproach("unreachable", 0))
                self.set_position(2.5, 0.5, y=100.62)
                self.assertEqual(await self.bot.approach_block(target, ticks=0),
                                 BlockApproach("unreachable", 0))
                self.correct_position(2.5, 100.62, 0.5, on_ground=True, tick=airborne_tick - 1)
                self.assertEqual(await self.bot.approach_block(target, ticks=0),
                                 BlockApproach("unreachable", 0))
                self.assertEqual(len(self.stream.sent), sent_before_retry)

                if interruption == "cancel":
                    self.set_position(2.5, 0.5, y=100.62, teleport=True)
                else:
                    tick = self.stream.sent[-1].client_tick
                    self.correct_position(2.5, 100.62, 0.5, on_ground=True, tick=tick)
                self.assertEqual(await self.bot.approach_block(target, ticks=0),
                                 BlockApproach("reached", 0, (2, 99, 0), 4))

    def moving_input(self):
        return [p for p in self.stream.sent if self.up in p.input_data]

    def assert_released(self):
        self.assertEqual([list(p.input_data) for p in self.stream.sent[-2:]], [[], []])

    def prepare_boat_link(self):
        b = self.binding
        entities = Entities(self.stream, own_runtime_id=1, own_entity_id=-2001)
        self.bot._entities = self.movement._entities = entities
        entities._on_packet(self.stream.protocol.encode(b.AddActorPacket(
            entity_id=-4001, runtime_id=50, actor_type="minecraft:boat",
            pos=b.Vec3(x=20, y=100, z=7), rot=b.Vec2(y=28),
        )))
        return self.stream.protocol.encode(b.SetActorLinkPacket(
            link=b.ActorLink(a=-4001, b=-2001, type=b.ActorLinkType.RIDING),
        ))

    async def test_approach_skips_ground_release_when_the_driver_seat_arrives_after_moving(self):
        self.prepare_terrain()
        link = self.prepare_boat_link()

        def board_after_first_frame():
            if len(self.stream.sent) == 1:
                self.bot._entities._on_packet(link)

        self.stream.on_send = board_after_first_frame
        with self.assertRaisesRegex(RuntimeError, "requires leaving the vehicle"):
            await self.bot.approach_block(self.TARGET)
        self.assertEqual(len(self.stream.sent), 1)
        walking, = [self.stream.protocol.decode(self.stream.protocol.encode(frame))
                    for frame in self.stream.sent]
        self.assertIsInstance(walking, self.binding.PlayerAuthInputPacket)
        self.assertIn(self.up, walking.input_data)
        self.assertIsNone(walking.client_predicted_vehicle)

        await self.bot.set_boat_input(forward=True)
        await self.movement._send_input(False)
        driving = self.stream.protocol.decode(self.stream.protocol.encode(self.stream.sent[-1]))
        self.assertEqual(len(self.stream.sent), 2)
        self.assertEqual((driving.client_tick, driving.client_predicted_vehicle), (walking.client_tick + 1, -4001))
        self.assertEqual((driving.pos.x, driving.pos.y, driving.pos.z), (20, 100, 7))

    async def test_cancelled_approach_rechecks_the_driver_seat_between_release_frames(self):
        self.prepare_terrain()
        link = self.prepare_boat_link()
        moving = asyncio.Event()

        def board_on_first_release():
            if len(self.stream.sent) == 1:
                moving.set()
            elif len(self.stream.sent) == 2:
                self.bot._entities._on_packet(link)

        self.stream.on_send = board_on_first_release
        task = asyncio.create_task(self.bot.approach_block(self.TARGET))
        await asyncio.wait_for(moving.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.stream.sent), 2)
        walking, release = [self.stream.protocol.decode(self.stream.protocol.encode(frame))
                            for frame in self.stream.sent]
        self.assertTrue(all(isinstance(frame, self.binding.PlayerAuthInputPacket)
                            for frame in (walking, release)))
        self.assertIn(self.up, walking.input_data)
        self.assertEqual((release.client_tick, release.move.x, release.move.y), (walking.client_tick + 1, 0, 0))
        self.assertIsNone(release.client_predicted_vehicle)
        self.assertEqual(self.bot.vehicle.role, "RIDING")

    def assert_avoided(self, *cells):
        for packet in self.moving_input():
            self.assertNotIn((math.floor(packet.pos.x), math.floor(packet.pos.z)), cells)

    async def test_selects_shortest_reachable_side_and_aims_at_its_face(self):
        self.prepare_terrain(blocked={(3, 100, 0), (3, 100, -1)})
        result = await self.bot.approach_block(self.TARGET)
        self.assertEqual(result.status, "reached")
        self.assertEqual((result.stand_position, result.face), ((4, 100, 1), 3))
        self.assertEqual(result.frames, len(self.moving_input()))
        self.assert_avoided((3, -1), (3, 0), (4, 0))
        last = self.stream.sent[-1]
        self.assertAlmostEqual(last.pos.x, 4.5, delta=0.12)
        self.assertAlmostEqual(last.pos.z, 1.5, delta=0.12)
        hit = (4.5 - last.pos.x, 100.5 - last.pos.y, 1.0 - last.pos.z)
        direction = last.camera_orientation
        for actual, expected in zip((direction.x, direction.y, direction.z), hit):
            self.assertAlmostEqual(actual, expected / math.hypot(*hit), places=5)
        self.assert_released()

    async def test_takes_a_side_route_when_the_next_cell_changes(self):
        self.prepare_terrain()
        changed = False

        def add_obstacle():
            nonlocal changed
            packet = self.stream.sent[-1]
            if not changed and self.up in packet.input_data and packet.pos.x > 1.55:
                changed = True
                self.set_block((2, 100, 0), self.STONE)

        self.stream.on_send = add_obstacle
        result = await self.bot.approach_block(self.TARGET)
        self.assertTrue(changed)
        self.assertEqual(result.status, "reached")
        self.assertGreater(max(p.pos.z for p in self.moving_input()), 1.3)
        self.assert_avoided((2, 0), (4, 0))
        self.assertEqual(result.frames, len(self.moving_input()))
        self.assert_released()

    async def test_approaches_over_glass_oriented_logs_and_double_slabs(self):
        self.prepare_terrain(blocked={(5, 100, 0), (4, 100, -1), (4, 100, 1)})
        for x, runtime_id in enumerate((0x374B17D2, 0xF253CAD8, 0x0A53C99B, 0xB8866A44)):
            self.set_block((x, 99, 0), runtime_id)
        result = await self.bot.approach_block(self.TARGET)
        self.assertEqual((result.status, result.stand_position, result.face),
                         ("reached", (3, 100, 0), 4))
        self.assertGreater(result.frames, 0)
        self.assertTrue(all(abs(packet.pos.z - 0.5) <= 0.12 for packet in self.moving_input()))

    async def test_replans_from_the_new_position_after_a_teleport(self):
        self.prepare_terrain(blocked={(1, 100, 1)})

        def teleport():
            if len(self.stream.sent) == 1:
                self.set_position(1.2, 2.8, teleport=True)

        self.stream.on_send = teleport
        result = await self.bot.approach_block(self.TARGET)
        self.assertEqual(result.status, "reached")
        first_after_teleport = self.stream.sent[1]
        self.assertIn(self.binding.PlayerAuthInputPacket.InputData.HANDLED_TELEPORT,
                      first_after_teleport.input_data)
        self.assertAlmostEqual(first_after_teleport.pos.x, 1.2)
        self.assertAlmostEqual(first_after_teleport.pos.z, 2.8)
        first_move_after_teleport = self.moving_input()[1]
        self.assertGreater(first_move_after_teleport.pos.x, 1.2)
        self.assertLess(first_move_after_teleport.pos.z, 2.8)
        self.assert_avoided((1, 1), (4, 0))
        self.assert_released()

    async def test_rechecks_the_stand_and_face_after_the_final_release_frame(self):
        self.prepare_terrain()
        self.set_position(3.5, 0.5)

        def teleport():
            if len(self.stream.sent) == 2:
                self.set_position(4.5, 1.5, teleport=True)

        self.stream.on_send = teleport
        result = await self.bot.approach_block(self.TARGET, ticks=1)
        self.assertEqual(result, BlockApproach("reached", 0, (4, 100, 1), 3))
        last = self.stream.sent[-1]
        self.assertEqual((last.pos.x, last.pos.z), (4.5, 1.5))
        self.assertAlmostEqual(abs(last.rot.y), 180.0)
        self.assertEqual(self.moving_input(), [])

    async def test_rechecks_the_route_with_the_original_budget_after_release(self):
        self.prepare_terrain()
        self.set_position(3.5, 0.5)

        def teleport():
            if len(self.stream.sent) == 1:
                self.set_position(0.5, 0.5, teleport=True)

        self.stream.on_send = teleport
        result = await self.bot.approach_block(self.TARGET, ticks=0)
        self.assertEqual(result, BlockApproach("budget_exhausted", 0))
        self.assertEqual(self.moving_input(), [])

    async def test_immediate_public_open_uses_the_final_input_position_and_face(self):
        self.prepare_terrain()
        b = self.binding
        reply = asyncio.Event()
        response = self.stream.protocol.encode(b.ContainerOpenPacket(
            container_id=5, type=b.ContainerType.CONTAINER,
            pos=b.BlockPos(x=4, y=100, z=0), entity_unique_id=-1,
        ))

        async def wait_for_packet(packet_id, *, timeout, match):
            self.assertEqual(packet_id, b.ContainerOpenPacket.ID)
            await asyncio.wait_for(reply.wait(), timeout)
            self.assertTrue(match(response))
            return response

        def notify_open():
            if isinstance(self.stream.sent[-1], b.InventoryTransactionPacket):
                reply.set()

        self.stream.wait_for_packet = wait_for_packet
        self.stream.on_send = notify_open
        self.bot._inventory = Inventory(self.stream, 1)
        self.bot._containers = Containers(self.stream, self.movement, self.bot._inventory)
        result = await self.bot.approach_block(self.TARGET)
        window = await self.bot.open_container(self.TARGET, face=result.face)

        self.assertEqual(window.position, self.TARGET)
        self.assertEqual((result.status, result.face), ("reached", 4))
        frame, click = self.stream.sent[-2:]
        click = self.stream.protocol.decode(self.stream.protocol.encode(click)).variant_transaction
        self.assertEqual((click.from_pos.x, click.from_pos.y, click.from_pos.z),
                         (frame.pos.x, frame.pos.y, frame.pos.z))
        self.assertEqual((click.face, click.click_pos.x, click.click_pos.y, click.click_pos.z),
                         (4, 0.0, 0.5, 0.5))
        self.assertEqual(self.bot.position[0], 0.5)
        self.assertGreater(click.from_pos.x, 3.3)

    async def test_sealed_faces_are_unreachable_even_with_unrelated_unknown_cells(self):
        self.prepare_terrain(blocked={(3, 100, 0), (5, 101, 0), (4, 100, -1), (4, 100, 1)},
                             unknown={(2, 100, 0)})
        self.assertEqual(await self.bot.approach_block(self.TARGET),
                         BlockApproach("unreachable", 0))
        self.assertEqual(self.stream.sent, [])

    async def test_refuses_an_unknown_route_until_supported_terrain_is_observed(self):
        self.prepare_terrain(blocked={(5, 100, 0), (4, 100, -1), (4, 100, 1)},
                             unknown={(3, 101, 0)})
        self.assertEqual(await self.bot.approach_block(self.TARGET),
                         BlockApproach("unobserved", 0))
        self.set_block((3, 101, 0), self.AIR)
        self.set_block((3, 99, 0), 0x12345678)
        self.assertEqual(await self.bot.approach_block(self.TARGET),
                         BlockApproach("unobserved", 0))
        self.assertEqual(self.stream.sent, [])
        self.set_block((3, 99, 0), self.STONE)
        result = await self.bot.approach_block(self.TARGET)
        self.assertEqual((result.status, result.stand_position, result.face),
                         ("reached", (3, 100, 0), 4))

    async def test_unresolved_placeholder_target_is_observed_but_not_approached(self):
        target = (1, 100, 0)
        stand = (0, 100, 0)
        for hashed, air, stone, unknown in (
            (True, self.AIR, self.STONE, 0xFFFFFFFE),
            (False, 18894, 3317, 11244),
        ):
            with self.subTest(hashed=hashed):
                self.blocks.hashed_runtime_ids = hashed
                self.set_block((0, 99, 0), stone)
                for cell in (stand, (0, 101, 0)):
                    self.set_block(cell, air)
                    self.set_block(cell, air, layer=1)
                self.set_block(target, unknown)
                self.assertEqual(await self.bot.approach_block(target), BlockApproach("unobserved", 0))
                observed = self.bot.observed_block(target)
                self.assertEqual(observed.runtime_id, unknown)
                self.assertIsNone(observed.is_air)
                self.set_block(target, stone)
                self.assertEqual(await self.bot.approach_block(target), BlockApproach("reached", 0, stand, 4))

    async def test_replanning_keeps_the_original_search_bounds(self):
        self.prepare_terrain()
        self.assertEqual(await self.bot.approach_block((12, 100, 0), search_radius=2),
                         BlockApproach("unreachable", 0))
        changed = False

        def block_last_face():
            nonlocal changed
            packet = self.stream.sent[-1]
            if not changed and self.up in packet.input_data and packet.pos.x > 1.55:
                changed = True
                self.set_block((3, 100, 0), self.STONE)

        self.stream.on_send = block_last_face
        result = await self.bot.approach_block(self.TARGET, search_radius=3)
        self.assertTrue(changed)
        self.assertEqual(result, BlockApproach("unreachable", len(self.moving_input())))
        self.assert_avoided((3, 0))
        self.assert_released()

    async def test_replanning_keeps_the_original_frame_budget(self):
        self.prepare_terrain()

        def block_route():
            if len(self.stream.sent) == 1:
                self.set_block((1, 100, 0), self.STONE)

        self.stream.on_send = block_route
        self.assertEqual(await self.bot.approach_block(self.TARGET, ticks=9),
                         BlockApproach("budget_exhausted", 9))
        self.assertEqual(len(self.moving_input()), 9)
        self.assert_avoided((1, 0))
        self.assert_released()

    async def test_reports_stop_and_cancellation_releases_the_public_entry_for_reuse(self):
        self.prepare_terrain()
        moving = asyncio.Event()

        def mark_moving():
            if self.up in self.stream.sent[-1].input_data:
                moving.set()

        self.stream.on_send = mark_moving
        task = asyncio.create_task(self.bot.approach_block(self.TARGET))
        await asyncio.wait_for(moving.wait(), 2)
        await asyncio.wait_for(self.bot.stop_moving(), 2)
        self.assertEqual(await task, BlockApproach("stopped", 1))
        self.assert_released()

        moving.clear()
        task = asyncio.create_task(self.bot.approach_block(self.TARGET))
        await asyncio.wait_for(moving.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_released()
        self.set_position(3.5, 0.5)
        self.assertEqual(await self.bot.approach_block(self.TARGET, ticks=0),
                         BlockApproach("reached", 0, (3, 100, 0), 4))
        self.assert_released()

    def prepare_interaction_terrain(self, target, *, blocked=(), unknown=()):
        floor = ((x, 99, z) for x in range(-4, 5) for z in range(-4, 5))
        self.prepare_multilevel(target, (*floor, *blocked), 4, unknown=unknown)

    async def test_placement_goal_routes_to_the_face_adjoining_the_destination(self):
        support, destination, missing = (2, 101, 0), (2, 101, 1), (1, 101, 0)
        self.prepare_interaction_terrain(support, unknown=(missing,))
        goal = BlockPlacementGoal(destination)
        start_only = NavigationRules(search_radius=0, vertical_radius=0)
        interaction = BlockInteractionGoal(support)
        self.assertEqual(self.bot.plan_navigation(interaction, rules=start_only).status, "unknown")
        self.assertEqual(self.bot.plan_navigation(goal, rules=start_only).status, "unreachable")

        self.set_block(missing, self.AIR)
        hit, = self.bot.plan_navigation(interaction, rules=start_only).matches
        self.assertEqual(hit.face, 4)
        self.assertEqual(self.bot.plan_navigation(goal, rules=start_only).status, "unreachable")
        result = await self.bot.navigate(goal, rules=start_only)
        self.assertEqual((result.status, result.frames, result.matches), ("unreachable", 0, ()))

        rules = NavigationRules(search_radius=2, vertical_radius=0)
        pose = self.bot.rotation
        plan = self.bot.plan_navigation(goal, rules=rules)
        match = BlockPlacementMatch(support, 3, (0.5, 0.5, 1.0), destination)
        self.assertEqual((plan.status, plan.endpoint, plan.matches), ("found", (0, 100, 1), (match,)))
        self.assertEqual((self.stream.sent, self.bot.rotation), ([], pose))

        result = await self.bot.navigate(goal, rules=rules)
        self.assertEqual((result.status, result.stand_position, result.matches), ("reached", plan.endpoint, (match,)))
        self.assertGreater(result.frames, 0)
        ray = self.bot.raycast_block()
        self.assertEqual((ray.status, ray.position, ray.face), ("hit", support, 3))
        self.assertTrue(all(isinstance(frame, self.binding.PlayerAuthInputPacket) and frame.item_use_transaction is None
                            for frame in self.stream.sent))

    async def test_placement_goal_distinguishes_missing_destination_support_and_view(self):
        support, destination, missing = (3, 101, 0), (2, 101, 0), (1, 101, 0)
        self.prepare_interaction_terrain(support, unknown=(destination,))
        goal = BlockPlacementGoal(destination)
        too_far = BlockPlacementGoal(destination, max_distance=1)
        rules = NavigationRules(search_radius=0, vertical_radius=0)

        async def expect_status(status):
            plan = self.bot.plan_navigation(goal, rules=rules)
            self.assertEqual((plan.status, plan.matches), (status, ()))
            result = await self.bot.navigate(goal, rules=rules)
            self.assertEqual((result.status, result.frames, result.matches), (status, 0, ()))

        await expect_status("unknown")  # The ray would work if the destination were air.
        self.assertEqual(self.bot.plan_navigation(too_far, rules=rules).status, "unreachable")
        self.set_block(support, self.AIR)
        await expect_status("unreachable")  # No support even if the missing destination were air.
        self.set_block(support, self.STONE)
        self.set_block(missing, self.STONE)
        await expect_status("unreachable")  # A known blocker screens the desired face.

        self.set_block(destination, self.STONE)
        self.set_block(missing, self.AIR)
        await expect_status("unreachable")
        self.set_block(destination, self.AIR)
        self.set_block(missing, 0xFFFFFFFE)
        await expect_status("unknown")  # Desired face ray still crosses missing terrain.
        self.set_block(missing, self.AIR)

        self.set_block(destination, 0xFFFFFFFE, layer=1)
        await expect_status("unknown")
        self.set_block(destination, 0x483B8B2A, layer=1)  # Source water.
        await expect_status("unreachable")
        self.set_block(destination, self.AIR, layer=1)

        self.set_block(support, 0xFFFFFFFE)
        await expect_status("unknown")
        self.assertEqual(self.bot.plan_navigation(too_far, rules=rules).status, "unreachable")
        self.set_block(support, self.AIR)
        await expect_status("unreachable")
        self.assertEqual(self.bot.plan_navigation(goal, rules=NavigationRules(max_expansions=0)).status, "budget")
        self.assertEqual(self.stream.sent, [])

    async def test_placement_goal_requires_full_collision_outline_and_empty_support_layer(self):
        support, destination = (3, 101, 0), (2, 101, 0)
        self.prepare_interaction_terrain(support)
        goal = BlockPlacementGoal(destination)
        rules = NavigationRules(search_radius=0, vertical_radius=0)
        for name, runtime_id in (
            ("trial spawner", 3330348502),  # Full collision, slightly inset outline.
            ("spruce stairs", 1187134793),  # Full outline, noncube collision.
        ):
            with self.subTest(support=name):
                self.set_block(support, runtime_id)
                self.assertEqual(self.bot.plan_navigation(BlockInteractionGoal(support), rules=rules).status, "found")
                self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unreachable")
        self.set_block(support, self.STONE)
        self.set_block(support, 0x483B8B2A, layer=1)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unreachable")
        self.set_block(support, 0xFFFFFFFE, layer=1)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unknown")
        self.set_block(support, self.AIR, layer=1)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "found")
        self.assertEqual(self.stream.sent, [])

    async def test_placement_goal_prefers_a_confirmed_face_over_an_unresolved_alternative(self):
        east, below, destination = (3, 101, 0), (2, 100, 0), (2, 101, 0)
        self.prepare_interaction_terrain(east, blocked=(below,))
        self.set_block(below, 0xFFFFFFFE, layer=1)
        goal = BlockPlacementGoal(destination)
        rules = NavigationRules(search_radius=0, vertical_radius=0)
        side = BlockPlacementMatch(east, 4, (0.0, 0.5, 0.5), destination)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).matches, (side,))
        result = await self.bot.navigate(goal, rules=rules, ticks=0)
        self.assertEqual((result.status, result.matches), ("reached", (side,)))

        self.set_block(below, self.AIR, layer=1)
        top = BlockPlacementMatch(below, 1, (0.5, 1.0, 0.5), destination)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).matches, (top,))
        self.assertEqual(self.stream.sent, [])

    async def test_placement_goal_excludes_the_standing_feet_and_head_cells(self):
        self.prepare_interaction_terrain((1, 101, 0))
        rules = NavigationRules(search_radius=0, vertical_radius=0)
        for destination, point, support, face in (
            ((0, 100, 0), (0.5, 100.0, 0.5), (0, 99, 0), 1),
            ((0, 101, 0), (1.0, 101.5, 0.5), (1, 101, 0), 4),
        ):
            with self.subTest(destination=destination):
                hit = self.bot.raycast_block_towards(point)
                self.assertEqual((hit.status, hit.position, hit.face), ("hit", support, face))
                goal = BlockPlacementGoal(destination)
                self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unreachable")
                result = await self.bot.navigate(goal, rules=rules, ticks=0)
                self.assertEqual((result.status, result.frames, result.matches), ("unreachable", 0, ()))
        self.assertEqual(self.stream.sent, [])

    async def test_placement_goal_rechecks_a_corrected_eye_after_movement(self):
        support, destination = (3, 101, 0), (2, 101, 0)
        self.prepare_interaction_terrain(support)
        goal = BlockPlacementGoal(destination, max_distance=1.61)
        rules = NavigationRules(search_radius=1, vertical_radius=0)
        plan = self.bot.plan_navigation(goal, rules=rules)
        self.assertEqual((plan.status, plan.endpoint), ("found", (1, 100, 0)))
        shifted = False

        def shift_eye_during_release():
            nonlocal shifted
            frame = self.stream.sent[-1]
            if not shifted and self.moving_input() and self.up not in frame.input_data:
                shifted = True
                self.correct_position(1.39, 101.62, 0.5, on_ground=True, tick=frame.client_tick)

        self.stream.on_send = shift_eye_during_release
        result = await self.bot.navigate(goal, rules=rules)
        self.assertTrue(shifted)
        self.assertEqual((result.status, result.stand_position, result.matches), ("unreachable", None, ()))
        self.assert_released()

    async def test_interaction_goal_rejects_occlusion_and_returns_a_clickable_side(self):
        target, wall = (2, 101, 0), (1, 101, 0)
        self.prepare_interaction_terrain(target, blocked=(wall,))
        goal = BlockInteractionGoal(target)
        start_only = NavigationRules(search_radius=0, vertical_radius=0)
        self.assertEqual(self.bot.plan_navigation(goal, rules=start_only).status, "unreachable")
        result = await self.bot.navigate(goal, rules=start_only)
        self.assertEqual((result.status, result.frames), ("unreachable", 0))
        self.assertEqual(self.stream.sent, [])

        rules = NavigationRules(search_radius=2, vertical_radius=0)
        pose = self.bot.rotation
        plan = self.bot.plan_navigation(goal, rules=rules)
        self.assertEqual((plan.status, plan.endpoint), ("found", (0, 100, 1)))
        match = BlockInteractionMatch(target, 3, (0.5, 0.5, 1.0))
        self.assertEqual(plan.matches, (match,))
        self.assertEqual((self.stream.sent, self.bot.rotation), ([], pose))

        result = await self.bot.navigate(goal, rules=rules)
        self.assertEqual((result.status, result.stand_position, result.matches), ("reached", plan.endpoint, (match,)))
        self.bot._inventory = Inventory(self.stream, 1)
        self.bot._containers = Containers(self.stream, self.movement, self.bot._inventory)
        await self.bot.use_item_on(match.position, face=match.face, click_pos=match.click_pos)
        transaction = self.stream.protocol.decode(self.stream.protocol.encode(self.stream.sent[-1])).variant_transaction
        self.assertEqual((transaction.pos.x, transaction.pos.y, transaction.pos.z), target)
        self.assertEqual((transaction.face, transaction.click_pos.x, transaction.click_pos.y, transaction.click_pos.z),
                         (3, 0.5, 0.5, 1.0))
        self.assertAlmostEqual(transaction.from_pos.z, self.movement.input_position[2], places=6)

    async def test_interaction_goal_reports_unknown_only_for_possible_sample_rays(self):
        target, missing = (2, 101, 0), (1, 101, 0)
        self.prepare_interaction_terrain(target, unknown=(missing,))
        rules = NavigationRules(search_radius=0, vertical_radius=0)
        goal = BlockInteractionGoal(target)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unknown")
        result = await self.bot.navigate(goal, rules=rules)
        self.assertEqual((result.status, result.frames), ("unknown", 0))

        # The west plane is exactly 1.5 away, and every supported sample is farther.
        out_of_range = BlockInteractionGoal(target, max_distance=1.5)
        self.assertEqual(self.bot.plan_navigation(out_of_range, rules=rules).status, "unreachable")
        either = AnyGoal((goal, FootGoal((0, 100, 0))))
        self.assertEqual(self.bot.plan_navigation(either, rules=rules).matches, ())
        self.set_block(missing, self.STONE)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unreachable")
        self.set_block(missing, self.AIR)
        self.set_block(target, 0xFFFFFFFE)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unknown")
        self.assertEqual(self.stream.sent, [])

    async def test_interaction_goal_resolves_an_observed_chest_with_an_observed_ray(self):
        chest, missing = (2, 101, 0), (1, 101, 0)
        cases = (("trapped_chest", 1235022161), ("chest", 3162850062))
        self.prepare_interaction_terrain(chest, unknown=(missing,))
        goal = BlockInteractionGoal(chest)
        rules = NavigationRules(search_radius=0, vertical_radius=0)
        self.assertIsNone(self.bot.observed_block_entity(chest))
        for family, runtime_id in cases:
            with self.subTest(family=family, missing=missing):
                self.set_block(chest, runtime_id)
                self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unknown")
                result = await self.bot.navigate(goal, rules=rules)
                self.assertEqual((result.status, result.frames), ("unknown", 0))
                self.assertEqual(self.stream.sent, [])

        self.set_block(missing, self.AIR)
        for family, runtime_id in cases:
            with self.subTest(family=family):
                self.set_block(chest, runtime_id)
                plan = self.bot.plan_navigation(goal, rules=rules)
                self.assertEqual((plan.status, plan.endpoint), ("found", (0, 100, 0)))
                self.assertEqual(len(plan.matches), 1)
                match = plan.matches[0]
                self.assertEqual((match.position, match.face), (chest, 4))
                # Stock 1.26.66.1 rays measure a .025 west inset and a .95 top.
                for actual, expected in zip(match.click_pos, (.025, .475, .5), strict=True):
                    self.assertAlmostEqual(actual, expected, delta=1e-6)
                result = await self.bot.navigate(goal, rules=rules, ticks=0)
                self.assertEqual((result.status, result.frames, result.matches), ("reached", 0, plan.matches))
                self.assertEqual(self.stream.sent, [])

    async def test_interaction_goal_matches_a_thin_outline_in_a_composite(self):
        button, behind = (2, 101, 0), (-1, 100, 0)
        self.prepare_interaction_terrain(button, blocked=(behind,))
        self.set_block(button, 577382071)
        goal = AllGoal((BlockFaceGoal(behind), BlockInteractionGoal(button)))
        rules = NavigationRules(search_radius=0, vertical_radius=0)
        expected = (BlockFaceMatch(behind, 5), BlockInteractionMatch(button, 4, (0.3125, 0.5, 0.0625)))
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).matches, expected)
        result = await self.bot.navigate(goal, rules=rules, ticks=0)
        self.assertEqual((result.status, result.matches), ("reached", expected))
        self.assertLess(self.bot.rotation[1], 0)
        self.assertEqual(self.stream.sent, [])

    async def test_interaction_goal_revalidates_actual_eye_after_the_final_release(self):
        target = (3, 101, 0)
        self.prepare_interaction_terrain(target)
        goal = BlockInteractionGoal(target, max_distance=1.61)
        rules = NavigationRules(search_radius=1, vertical_radius=0)
        plan = self.bot.plan_navigation(goal, rules=rules)
        self.assertEqual(plan.endpoint, (1, 100, 0))
        shifted = False

        def move_in_the_same_cell():
            nonlocal shifted
            last = self.stream.sent[-1]
            if not shifted and self.moving_input() and self.up not in last.input_data:
                shifted = True
                self.correct_position(1.39, 101.62, 0.5, on_ground=True, tick=last.client_tick)

        self.stream.on_send = move_in_the_same_cell
        result = await self.bot.navigate(goal, rules=rules)
        self.assertTrue(shifted)
        self.assertEqual((result.status, result.stand_position, result.matches), ("unreachable", None, ()))
        self.assertGreater(result.expanded, plan.expanded)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).endpoint, plan.endpoint)
        self.assert_released()

    async def test_interaction_goal_reaims_from_the_corrected_eye_at_the_same_click_point(self):
        button = (2, 101, 0)
        self.prepare_interaction_terrain(button)
        self.set_block(button, 577382071)
        self.set_position(0.5, 0.5, teleport=True)
        goal = BlockInteractionGoal(button)
        rules = NavigationRules(search_radius=0, vertical_radius=0)
        point = (0.3125, 0.5, 0.0625)
        changed = False

        def shift_eye_during_release():
            nonlocal changed
            if not changed:
                changed = True
                self.correct_position(0.5, 101.62, 0.39, on_ground=True, tick=self.stream.sent[-1].client_tick)

        self.stream.on_send = shift_eye_during_release
        result = await self.bot.navigate(goal, rules=rules, ticks=0)
        self.assertTrue(changed)
        self.assertAlmostEqual(self.movement.input_position[2], 0.39, places=6)
        self.assertEqual((result.status, result.matches), ("reached", (BlockInteractionMatch(button, 4, point),)))
        rotation = self.bot.rotation
        for _ in range(2):
            hit = self.bot.raycast_block(max_distance=3)
            self.assertEqual((hit.status, hit.position, hit.face), ("hit", button, 4))
            self.assertEqual(self.bot.rotation, rotation)
        self.assertEqual(len(self.stream.sent), 2)

    async def test_unknown_target_requires_a_possible_view_beyond_a_single_solid_screen(self):
        target, screen = (5, 101, 0), (1, 101, 0)
        self.prepare_interaction_terrain(target, blocked=(screen,))
        self.set_block(target, 0xFFFFFFFE)
        goal = BlockInteractionGoal(target)
        rules = NavigationRules(search_radius=0, vertical_radius=0)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unreachable")
        result = await self.bot.navigate(goal, rules=rules)
        self.assertEqual((result.status, result.frames), ("unreachable", 0))

        self.set_block(screen, self.AIR)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unknown")
        result = await self.bot.navigate(goal, rules=rules)
        self.assertEqual((result.status, result.frames), ("unknown", 0))

        # Different blocks intercept the corners, with a clear opening in the middle.
        ring = {(3, y, z) for y in (100, 101, 102) for z in (-1, 0, 1)} - {(3, 101, 0)}
        for position in ring:
            self.set_block(position, self.STONE)
        for x in (4, 7):
            for y in (100, 103):
                for z in (-1, 2):
                    self.assertIn(self.bot.raycast_block_towards((x, y, z)).position, ring)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unknown")
        self.assertEqual(self.stream.sent, [])

    async def test_unknown_target_keeps_the_solid_screen_behind_a_nearer_upper_slab(self):
        target, screen, slab = (8, 101, 0), (2, 101, 0), (1, 101, 0)
        self.prepare_interaction_terrain(target, blocked=(screen,))
        self.set_block(target, 0xFFFFFFFE)
        goal = BlockInteractionGoal(target, max_distance=8)
        rules = NavigationRules(search_radius=0, vertical_radius=0)

        async def expect_status(status):
            self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, status)
            result = await self.bot.navigate(goal, rules=rules)
            self.assertEqual((result.status, result.frames, result.matches), (status, 0, ()))

        await expect_status("unreachable")
        self.set_block(slab, 0xE782783B)  # Upper smooth stone slab.
        center_ray = self.bot.raycast_block_towards((8.5, 101.5, 0.5), max_distance=8)
        self.assertEqual((center_ray.status, center_ray.position), ("hit", slab))
        await expect_status("unreachable")

        self.set_block(screen, self.AIR)
        await expect_status("unknown")
        self.assertEqual(self.stream.sent, [])

    async def test_interaction_goal_replanning_spends_the_original_expansion_budget(self):
        target, wall = (3, 101, 0), (2, 101, 0)
        self.prepare_interaction_terrain(target)
        goal = BlockInteractionGoal(target, max_distance=2)
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        initial = self.bot.plan_navigation(goal, rules=rules)
        self.assertEqual(initial.endpoint, (1, 100, 0))

        def block_the_route_endpoint():
            if len(self.stream.sent) == 1:
                self.set_block(wall, self.STONE)

        self.stream.on_send = block_the_route_endpoint
        limit = initial.expanded + 1
        limited = NavigationRules(search_radius=2, vertical_radius=0, max_expansions=limit)
        result = await self.bot.navigate(goal, rules=limited)
        self.assertEqual((result.status, result.expanded, result.frames), ("budget", limit, 1))
        self.assert_released()

        self.stream.on_send = None
        self.set_position(0.5, 0.5)
        alternate = await self.bot.navigate(goal, rules=rules)
        self.assertEqual(alternate.status, "reached")
        self.assertNotEqual(alternate.stand_position, initial.endpoint)
        hit, = alternate.matches
        self.assertEqual(hit.position, target)

    async def test_axis_goals_start_at_a_negative_column_and_include_radius_boundaries(self):
        start = (-1, 100, -1)
        self.prepare_multilevel((-1, 99, -1), (), 1)
        self.set_position(-0.5, -0.5)
        rules = NavigationRules(search_radius=0, vertical_radius=0)
        for goal in (
            XZGoal((-1, -1)),
            NearXZGoal((-1, -1), 0),
            NearXZGoal((2, 3), 5),
            NearXZGoal((0, 0), 1e308),
            YGoal(100),
        ):
            with self.subTest(goal=goal):
                plan = self.bot.plan_navigation(goal, rules=rules)
                self.assertEqual((plan.status, plan.nodes, plan.cost, plan.matches), ("found", (start,), Fraction(0), ()))
                self.assertEqual(await self.bot.navigate(goal, rules=rules, ticks=0),
                                 NavigationResult("reached", 0, 1, start))
        for goal in (XZGoal((0, 0)), NearXZGoal((2, 3), math.nextafter(5, 0)),
                     NearXZGoal((10**309, 0), 1e308), YGoal(101)):
            with self.subTest(goal=goal):
                self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unreachable")
        self.assertEqual(self.stream.sent, [])

    async def test_axis_goals_follow_only_the_specified_axes_and_compose_on_the_same_stance(self):
        _, jumps, landings = self.ascent_route()
        rules = NavigationRules(search_radius=3, vertical_radius=2)
        start, first, second = (0, 100, 0), (1, 101, 0), (2, 102, 0)
        first_goals = (XZGoal((1, 0)), NearXZGoal((3, 0), 2), YGoal(101), AnyGoal((XZGoal((0, 3)), YGoal(101))))
        for goal in first_goals:
            with self.subTest(goal=goal):
                self.assertEqual(self.bot.plan_navigation(goal, rules=rules).nodes, (start, first))
        goal = AllGoal((XZGoal((2, 0)), AnyGoal((YGoal(101), YGoal(102))), NearXZGoal((3, 0), 1)))
        plan = self.bot.plan_navigation(goal, rules=rules)
        self.assertEqual((plan.nodes, plan.matches), ((start, first, second), ()))
        self.assertEqual(self.bot.plan_navigation(AllGoal((XZGoal((2, 0)), YGoal(101))), rules=rules).status, "unreachable")
        self.assertEqual(self.stream.sent, [])

        result = await self.bot.navigate(goal, rules=rules)
        self.assertEqual((result.status, result.stand_position, result.matches), ("reached", second, ()))
        self.assertGreater(result.frames, 0)
        self.assertEqual(len(jumps), 2)
        self.assertEqual(len(landings), 2)
        self.assertLess(landings[0], jumps[1])
        self.assert_released()

    async def test_axis_goals_use_observed_terrain_and_keep_unknown_inside_search_bounds(self):
        self.prepare_terrain(unknown={(1, 101, 0)})
        rules = NavigationRules(search_radius=1, vertical_radius=0)
        near = NearXZGoal((2, 0), 1)
        for goal, status in (
            (XZGoal((1, 0)), "unknown"),
            (near, "unknown"),
            (AllGoal((near, YGoal(101))), "unreachable"),
            (XZGoal((2, 0)), "unreachable"),
        ):
            with self.subTest(goal=goal):
                self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, status)
                result = await self.bot.navigate(goal, rules=rules)
                self.assertEqual((result.status, result.frames), (status, 0))
        fallback = AnyGoal((near, YGoal(100)))
        self.assertEqual(self.bot.plan_navigation(fallback, rules=rules).endpoint, (0, 100, 0))
        self.set_block((1, 101, 0), self.AIR)
        self.assertEqual(self.bot.plan_navigation(near, rules=rules).endpoint, (1, 100, 0))
        self.assertEqual(self.stream.sent, [])

    async def test_plan_checks_every_goal_at_one_endpoint_and_keeps_face_order(self):
        self.prepare_terrain()
        nearby, stand = (3, 100, 1), (3, 100, 0)
        self.set_block(nearby, self.STONE)
        rules = NavigationRules(search_radius=4, vertical_radius=0)
        first, second = BlockFaceGoal(nearby), BlockFaceGoal(self.TARGET)
        goal = AllGoal((AnyGoal((first, FootGoal((1, 100, 1)))), second,
                        NearGoal((3.5, 103, 0.5), 3)))

        pose = self.movement.rotation
        plan = self.bot.plan_navigation(goal, rules=rules)
        self.assertEqual((plan.status, plan.start, plan.endpoint), ("found", (0, 100, 0), stand))
        self.assertEqual(plan.nodes, tuple((x, 100, 0) for x in range(4)))
        self.assertEqual((plan.rules, plan.cost), (rules, Fraction(3)))
        self.assertEqual(plan.matches, (BlockFaceMatch(nearby, 2), BlockFaceMatch(self.TARGET, 4)))
        with self.assertRaises(FrozenInstanceError):
            plan.status = "unknown"
        with self.assertRaises(FrozenInstanceError):
            plan.rules.search_radius = 1

        split = AllGoal((FootGoal(stand), FootGoal((2, 100, 0))))
        self.assertEqual(self.bot.plan_navigation(split, rules=rules).status, "unreachable")
        above = AllGoal((FootGoal(stand), NearGoal((3.5, 103, 0.5), 2.9)))
        self.assertEqual(self.bot.plan_navigation(above, rules=rules).status, "unreachable")
        self.assertEqual((self.stream.sent, self.movement.rotation), ([], pose))

        self.set_position(3.5, 0.5)
        result = await self.bot.navigate(goal, rules=rules, ticks=0)
        self.assertEqual(result, NavigationResult("reached", 0, 1, stand, plan.matches))
        self.assertEqual(plan.start, (0, 100, 0))
        self.assertEqual(self.stream.sent, [])

    async def test_plan_reports_unknown_only_when_missing_evidence_could_satisfy_the_goal(self):
        self.prepare_terrain(unknown={(2, 101, 0)})
        start = FootGoal((0, 100, 0))
        missing = EntityNearGoal(-21, 0.5)
        rules = NavigationRules(search_radius=3, vertical_radius=0)

        self.assertEqual(self.bot.plan_navigation(AnyGoal((missing, start)), rules=rules).status, "found")
        self.assertEqual(self.bot.plan_navigation(AllGoal((missing, start)), rules=rules).status, "unknown")
        impossible = AllGoal((missing, BlockFaceGoal((3, 100, 0))))
        self.assertEqual(self.bot.plan_navigation(impossible, rules=rules).status, "unreachable")
        target = FootGoal((2, 100, 0))
        self.assertEqual(self.bot.plan_navigation(target, rules=rules).status, "unknown")
        self.set_block((2, 101, 0), self.AIR)
        self.assertEqual(self.bot.plan_navigation(target, rules=rules).status, "found")
        empty = self.bot.plan_navigation(start, rules=NavigationRules(max_expansions=0))
        self.assertEqual((empty.status, empty.expanded, empty.nodes, empty.cost), ("budget", 0, (), None))
        self.assertEqual(self.stream.sent, [])

    async def test_plan_chooses_the_cheapest_observed_directed_route(self):
        supports = {(0, 99, 0), (1, 100, 0), (2, 99, 0), (3, 99, 0),
                    (0, 99, 1), (1, 99, 1), (2, 99, 1)}
        self.prepare_multilevel((4, 100, 4), supports, 4)
        start, goal = (0, 100, 0), FootGoal((2, 100, 0))
        standard = NavigationRules(search_radius=4)
        plan = self.bot.plan_navigation(goal, rules=standard)
        self.assertEqual(plan.nodes, (start, (1, 101, 0), goal.position))
        self.assertEqual(plan.cost, Fraction(2))
        weighted = NavigationRules(search_radius=4, costs=NavigationCosts(walk=0.75, up=5, down=0.125))
        path = (start, (0, 100, 1), (1, 100, 1), (2, 100, 1), goal.position)
        plan = self.bot.plan_navigation(goal, rules=weighted)
        self.assertEqual((plan.nodes, plan.cost), (path, Fraction(3)))

        self.set_position(2.5, 0.5)
        reverse = self.bot.plan_navigation(FootGoal(start), rules=standard)
        self.assertEqual(reverse.nodes, tuple(reversed(path)))

    def prepare_peer(self, *, x=1.5, z=0.5):
        self.bot._entities = self.movement._entities = Entities(self.stream, own_runtime_id=1)
        b = self.binding
        uuid, unique_id, runtime_id = UUID(int=9), -209, 42
        self.observe_entity(b.AddPlayerPacket(uuid=uuid, name="Peer", runtime_id=runtime_id,
                                              pos=b.Vec3(x=x, y=100, z=z)))
        roster = b.PlayerListPacket(entries=[
            b.PlayerListPacket.AddEntry(action=b.PlayerListPacketType.ADD, uuid=uuid, name="Peer", id=unique_id),
        ])
        return unique_id, runtime_id, roster

    def observe_entity(self, packet):
        self.bot._entities._on_packet(self.stream.protocol.encode(packet))

    def move_peer(self, runtime_id, *, x, z):
        b = self.binding
        self.observe_entity(b.MovePlayerPacket(player_id=runtime_id, pos=b.Vec3(x=x, y=101.62, z=z)))

    def spawn_navigation_actor(self, *, entity_id=-4294967294, runtime_id=2, x=1.5, z=0.5,
                               actor_type="minecraft:villager_v2"):
        if self.bot._entities is None:
            self.bot._entities = self.movement._entities = Entities(self.stream, own_runtime_id=1)
        b = self.binding
        self.observe_entity(b.AddActorPacket(entity_id=entity_id, runtime_id=runtime_id, actor_type=actor_type,
                                            pos=b.Vec3(x=x, y=100, z=z)))
        return entity_id, runtime_id

    def move_villager(self, runtime_id, *, x=None, y=None, z=None):
        b = self.binding
        self.observe_entity(b.MoveActorDeltaPacket(move_data=b.MoveActorDeltaData(
            runtime_id=runtime_id, new_position_x=x, new_position_y=y, new_position_z=z,
        )))

    def prepare_visibility_terrain(self, *, blocked=(), unknown=()):
        self.prepare_interaction_terrain((4, 100, 3), blocked=blocked, unknown=unknown)
        self.set_position(0.5, 0.5, on_ground=True)

    def observe_own_sneaking(self, sneaking):
        b = self.binding
        flags = 1 << int(b.ActorFlags.SNEAKING) if sneaking else 0
        entry = b.DataItemEntry(id=0, payload=b.DataItemInt64Payload(value=flags))
        packet = b.SetActorDataPacket(id=1, packed_items=b.SynchedActorData.CopyableDataList(data=[entry]))
        self.movement._on_packet(self.stream.protocol.encode(packet))

    async def test_entity_visibility_crouched_start_is_unknown_and_other_goals_still_match(self):
        block = (1, 100, 0)
        self.prepare_interaction_terrain(block)
        self.set_position(0.5, 0.5, on_ground=True)
        unique_id, runtime_id = self.spawn_navigation_actor(x=0.5)
        start = (0, 100, 0)
        sight = EntityVisibilityGoal(unique_id, aim_offset=(0, 1.52, 0), max_distance=0.05)
        near = EntityNearGoal(unique_id, 0.05)
        face = BlockFaceGoal(block)
        both = AllGoal((near, sight))
        either = AnyGoal((both, face))
        rules = NavigationRules(search_radius=0, vertical_radius=0)

        async def check_goal(goal, plan_status, status, matches=()):
            sent = len(self.stream.sent)
            plan = self.bot.plan_navigation(goal, rules=rules)
            result = await self.bot.navigate(goal, rules=rules, ticks=0)
            self.assertEqual((plan.status, plan.matches), (plan_status, matches))
            self.assertEqual((result.status, result.frames, result.matches), (status, 0, matches))
            self.assertEqual(result.stand_position, start if status == "reached" else None)
            self.assertEqual(len(self.stream.sent), sent)

        await check_goal(sight, "found", "reached")
        await self.bot.set_sneaking(True)
        await check_goal(sight, "unknown", "unknown")
        self.observe_own_sneaking(True)
        await check_goal(sight, "unknown", "unknown")
        await check_goal(both, "unknown", "unknown")
        await check_goal(near, "found", "reached")
        await check_goal(face, "found", "reached", (BlockFaceMatch(block, 4),))
        await check_goal(either, "found", "reached", (BlockFaceMatch(block, 4),))

        sent = len(self.stream.sent)
        self.move_villager(runtime_id, z=1.5)
        future = self.bot.plan_navigation(sight, rules=NavigationRules(search_radius=1, vertical_radius=0))
        self.assertEqual((future.status, future.nodes), ("found", (start, (0, 100, 1))))
        self.assertEqual(len(self.stream.sent), sent)
        self.move_villager(runtime_id, z=0.5)

        await self.bot.set_sneaking(False)
        await check_goal(sight, "unknown", "unknown")
        self.observe_own_sneaking(False)
        for swimming, crawling in ((True, False), (False, True)):
            with self.subTest(swimming=swimming, crawling=crawling):
                self.observe_swimming(0, swimming, crawling=crawling)
                await check_goal(sight, "unknown", "unknown")
        self.observe_swimming(0, False, crawling=False)
        await check_goal(both, "found", "reached")

    async def test_entity_visibility_rechecks_crouch_after_input_release(self):
        self.prepare_visibility_terrain()
        unique_id, _ = self.spawn_navigation_actor(x=0.5, z=1.5)
        sight = EntityVisibilityGoal(unique_id, aim_offset=(0, 1.52, 0), max_distance=0.25)
        goal = AllGoal((FootGoal((0, 100, 1)), sight))
        rules = NavigationRules(search_radius=1, vertical_radius=0)
        crouched = False

        def crouch_on_release():
            nonlocal crouched
            frame = self.stream.sent[-1]
            if not crouched and self.up not in frame.input_data and frame.pos_delta.x == frame.pos_delta.z == 0.0:
                self.observe_own_sneaking(True)
                crouched = True

        self.stream.on_send = crouch_on_release
        result = await self.bot.navigate(goal, rules=rules)
        self.assertTrue(crouched)
        self.assertEqual((result.status, result.stand_position), ("unknown", None))
        self.assertGreater(result.frames, 0)
        self.assert_released()

    async def test_entity_visibility_airborne_start_is_unknown_inside_the_standing_height_tolerance(self):
        self.prepare_interaction_terrain((4, 100, 3))
        unique_id, runtime_id = self.spawn_navigation_actor(x=0.5)
        start = (0, 100, 0)
        sight = EntityVisibilityGoal(unique_id, aim_offset=(0, 1.52, 0), max_distance=0.05)
        near = EntityNearGoal(unique_id, 0.05)
        both = AllGoal((sight, near))
        rules = NavigationRules(search_radius=0, vertical_radius=0)

        self.set_position(0.5, 0.5, y=101.655, on_ground=False)
        for goal, plan_status, status in ((sight, "unknown", "unknown"), (both, "unknown", "unknown"),
                                           (AnyGoal((both, near)), "found", "reached")):
            with self.subTest(goal=goal):
                self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, plan_status)
                result = await self.bot.navigate(goal, rules=rules, ticks=0)
                self.assertEqual((result.status, result.frames), (status, 0))
                self.assertEqual(result.stand_position, start if status == "reached" else None)
        self.move_villager(runtime_id, z=1.5)
        plan = self.bot.plan_navigation(sight, rules=NavigationRules(search_radius=1, vertical_radius=0))
        self.assertEqual((plan.status, plan.nodes), ("found", (start, (0, 100, 1))))
        self.move_villager(runtime_id, z=0.5)

        self.set_position(0.5, 0.5, y=101.655, on_ground=True)
        self.assertEqual(self.bot.plan_navigation(sight, rules=rules).status, "found")
        result = await self.bot.navigate(sight, rules=rules, ticks=0)
        self.assertEqual((result.status, result.frames, result.stand_position), ("reached", 0, start))
        self.assertEqual(self.stream.sent, [])

    async def test_entity_visibility_rechecks_airborne_after_input_release(self):
        self.prepare_interaction_terrain((4, 100, 3))
        self.set_position(0.5, 0.5, on_ground=False)
        unique_id, _ = self.spawn_navigation_actor(x=0.5, z=1.5)
        endpoint = (0, 100, 1)
        sight = EntityVisibilityGoal(unique_id, aim_offset=(0, 1.52, 0), max_distance=0.25)
        goal = AllGoal((FootGoal(endpoint), sight))
        rules = NavigationRules(search_radius=1, vertical_radius=0)
        airborne = False

        def observe_ground_then_airborne_on_release():
            nonlocal airborne
            frame = self.stream.sent[-1]
            if len(self.stream.sent) == 1:
                self.assertIn(self.up, frame.input_data)
                self.set_position(frame.pos.x, frame.pos.z, on_ground=True)
            elif not airborne and self.up not in frame.input_data:
                self.set_position(0.5, 1.5, y=101.655, on_ground=False)
                airborne = True

        self.stream.on_send = observe_ground_then_airborne_on_release
        result = await self.bot.navigate(goal, rules=rules)
        self.assertTrue(airborne)
        self.assertEqual((result.status, result.stand_position), ("unknown", None))
        self.assertGreater(result.frames, 0)
        self.assertTrue(all(self.up in packet.input_data for packet in self.stream.sent[:-2]))
        self.assert_released()

    async def test_entity_visibility_uses_current_trusted_feet_and_unique_id(self):
        self.prepare_visibility_terrain()
        rules = NavigationRules(search_radius=0, vertical_radius=0)
        unique_id, runtime_id = self.spawn_navigation_actor(x=2.5)
        cow_id, _ = self.spawn_navigation_actor(entity_id=-33, runtime_id=4, actor_type="minecraft:cow", x=2.5)
        for target_id, expected in ((unique_id, "found"), (runtime_id, "unknown"), (cow_id, "unknown")):
            goal = EntityVisibilityGoal(target_id, aim_offset=(-1, 1.5, 0.25), max_distance=1.1)
            self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, expected)

        goal = EntityVisibilityGoal(unique_id, aim_offset=(-1, 1.5, 0.25), max_distance=1.1)
        self.move_villager(runtime_id, x=3.5)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unreachable")
        b = self.binding
        self.observe_entity(b.MoveActorAbsolutePacket(move_data=b.MoveActorAbsoluteData(
            runtime_id=runtime_id, pos=b.Vec3(x=2.5, y=100, z=0.5),
        )))
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unknown")
        self.move_villager(runtime_id, x=2.5, y=100, z=0.5)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "found")

        peer_id, peer_runtime, roster = self.prepare_peer(x=0.5)
        peer_goal = EntityVisibilityGoal(peer_id, aim_offset=(0, 1.52, 0), max_distance=0.05)
        self.assertEqual(self.bot.plan_navigation(peer_goal, rules=rules).status, "unknown")
        self.observe_entity(roster)
        self.move_peer(peer_runtime, x=0.5, z=0.5)
        self.assertEqual(self.bot.plan_navigation(peer_goal, rules=rules).status, "found")
        result = await self.bot.navigate(peer_goal, rules=rules, ticks=0)
        self.assertEqual((result.status, result.stand_position, result.matches), ("reached", (0, 100, 0), ()))
        input_origin = EntityVisibilityGoal(peer_id, aim_offset=(0, 1.62, 0), max_distance=0.05)
        self.assertEqual(self.bot.plan_navigation(input_origin, rules=rules).status, "unreachable")
        self.assertEqual((await self.bot.navigate(input_origin, rules=rules, ticks=0)).status, "unreachable")
        self.assertEqual(self.stream.sent, [])

    async def test_entity_visibility_proves_blockage_with_an_unresolved_outline_in_either_order(self):
        first, second = (1, 101, 0), (2, 101, 0)
        self.prepare_visibility_terrain(unknown=(first, second))
        unique_id, _ = self.spawn_navigation_actor(x=3.5)
        goal = EntityVisibilityGoal(unique_id, aim_offset=(0, 1.5, 0), max_distance=3.1)
        rules = NavigationRules(search_radius=0, vertical_radius=0)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unknown")

        for stone, unresolved in ((first, second), (second, first)):
            with self.subTest(stone=stone):
                self.set_block(stone, self.STONE)
                self.set_block(unresolved, 0xFFFFFFFE)
                self.assertEqual(self.bot.raycast_block_towards((3.5, 101.5, 0.5)).status, "unknown")
                self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unreachable")
                result = await self.bot.navigate(goal, rules=rules)
                self.assertEqual((result.status, result.frames), ("unreachable", 0))
        self.assertEqual(self.stream.sent, [])

    async def test_entity_visibility_ray_stops_at_the_aim_point(self):
        missing, far = (1, 101, 0), (4, 101, 0)
        self.prepare_visibility_terrain(unknown=(missing, far))
        unique_id, _ = self.spawn_navigation_actor(x=2.5)
        goal = EntityVisibilityGoal(unique_id, aim_offset=(0, 1.5, 0), max_distance=5)
        rules = NavigationRules(search_radius=0, vertical_radius=0)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unknown")
        out_of_range = EntityVisibilityGoal(unique_id, aim_offset=(0, 1.5, 0), max_distance=2)
        self.assertEqual(self.bot.plan_navigation(out_of_range, rules=rules).status, "unreachable")

        self.set_block(missing, self.STONE)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "unreachable")
        self.set_block(missing, self.AIR)
        beyond = (3, 101, 0)
        for runtime_id in (self.STONE, 1211861802):  # Stone and a confined, unresolved water outline.
            self.set_block(beyond, runtime_id)
            plan = self.bot.plan_navigation(goal, rules=rules)
            self.assertEqual((plan.status, plan.nodes, plan.matches), ("found", ((0, 100, 0),), ()))
        self.set_block(beyond, self.STONE)
        self.set_block(far, self.AIR)
        on_face = EntityVisibilityGoal(unique_id, aim_offset=(0.5, 1.5, 0), max_distance=5)
        self.assertEqual(self.bot.plan_navigation(on_face, rules=rules).status, "unreachable")
        self.assertEqual(self.stream.sent, [])

    async def test_entity_visibility_uses_an_inclusive_euclidean_range(self):
        self.prepare_visibility_terrain()
        unique_id, runtime_id = self.spawn_navigation_actor(x=3.5)
        rules = NavigationRules(search_radius=0, vertical_radius=0)
        for distance, expected in ((5, "found"), (math.nextafter(5, 0), "unreachable")):
            goal = EntityVisibilityGoal(unique_id, aim_offset=(0, 5.52, 0), max_distance=distance)
            self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, expected)
        self.move_villager(runtime_id, x=0.5)
        coincident = EntityVisibilityGoal(unique_id, aim_offset=(0, 1.52, 0), max_distance=0)
        self.assertEqual(self.bot.plan_navigation(coincident, rules=rules).status, "found")
        self.assertEqual(self.stream.sent, [])

    async def test_entity_visibility_caps_ray_work_independently_of_node_expansions(self):
        self.prepare_visibility_terrain(unknown=((1, 101, 0),))
        unique_id, _ = self.spawn_navigation_actor()
        goal = EntityVisibilityGoal(unique_id, aim_offset=(1e100, 1.52, 0), max_distance=1e101)
        rules = NavigationRules(search_radius=0, vertical_radius=0, max_expansions=3)
        observe = self.blocks._outline_at

        def guarded_observation(position):
            self.assertLess(position[0], 5)
            return observe(position)

        with patch.object(self.blocks, "_outline_at", guarded_observation):
            plan = self.bot.plan_navigation(goal, rules=rules)
            result = await self.bot.navigate(goal, rules=rules)
            foot = self.bot.plan_navigation(FootGoal((0, 100, 0)), rules=rules)
        self.assertEqual((plan.status, plan.expanded), ("budget", 1))
        self.assertEqual((result.status, result.expanded, result.frames), ("budget", 1, 0))
        self.assertEqual((foot.status, foot.expanded), ("found", 1))
        self.assertEqual(self.stream.sent, [])

    async def test_entity_visibility_checks_the_deadline_during_search_and_arrival_rays(self):
        self.prepare_visibility_terrain()
        unique_id, _ = self.spawn_navigation_actor(x=3.5)
        goal = EntityVisibilityGoal(unique_id, aim_offset=(0, 1.52, 0), max_distance=3.1)
        rules = NavigationRules(search_radius=0, vertical_radius=0)
        loop = asyncio.get_running_loop()
        time = loop.time
        observe = self.blocks._outline_at

        for phase in ("search", "arrival"):
            with self.subTest(phase=phase):
                offset = 0
                armed = phase == "search"
                reads = []

                def arm_arrival(_):
                    nonlocal armed
                    armed = True

                def advance_time(position):
                    nonlocal offset
                    if armed:
                        offset = 12
                        reads.append(position)
                    return observe(position)

                with (patch.object(loop, "time", lambda: time() + offset),
                      patch.object(loop, "slow_callback_duration", 20),
                      patch.object(self.blocks, "_outline_at", advance_time)):
                    result = await self.bot.navigate(goal, rules=rules, on_plan=arm_arrival, timeout=10)
                self.assertEqual((result.status, result.expanded, result.frames), ("budget", 1, 0))
                self.assertIn((0, 101, 0), reads)
                self.assertTrue(all(x <= 1 for x, _, _ in reads))
        self.assertEqual(self.stream.sent, [])

    async def test_entity_visibility_shares_its_ray_limit_with_route_checks_and_releases_input(self):
        self.prepare_visibility_terrain()
        unique_id, _ = self.spawn_navigation_actor(x=0.5, z=1.5)
        goal = EntityVisibilityGoal(unique_id, aim_offset=(0, 1.52, 0), max_distance=0.25)
        rules = NavigationRules(search_radius=1, vertical_radius=0, max_expansions=2)
        plan = self.bot.plan_navigation(goal, rules=rules)
        self.assertEqual((plan.status, plan.endpoint, plan.expanded), ("found", (0, 100, 1), 2))

        result = await self.bot.navigate(goal, rules=rules)
        self.assertEqual((result.status, result.expanded, result.frames), ("budget", 2, 2))
        self.assertEqual(len(self.stream.sent), 4)
        self.assert_released()

    async def test_entity_visibility_checks_the_shared_ray_limit_at_final_stance(self):
        self.prepare_visibility_terrain()
        unique_id, _ = self.spawn_navigation_actor(x=0.5)
        goal = EntityVisibilityGoal(unique_id, aim_offset=(0, 1.52, 0), max_distance=0.05)
        for limit, status in ((2, "budget"), (3, "reached")):
            rules = NavigationRules(search_radius=0, vertical_radius=0, max_expansions=limit)
            result = await self.bot.navigate(goal, rules=rules, ticks=0)
            self.assertEqual((result.status, result.expanded, result.frames), (status, 1, 0))
        self.assertEqual(self.stream.sent, [])

    async def test_entity_visibility_detours_to_a_clear_reachable_stance(self):
        wall_top = (1, 101, 0)
        self.prepare_visibility_terrain(blocked=((1, 100, 0), wall_top))
        unique_id, _ = self.spawn_navigation_actor(x=2.5, z=0.75)
        goal = EntityVisibilityGoal(unique_id, aim_offset=(0, 1.5, 0), max_distance=2.2)
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        start_only = NavigationRules(search_radius=0, vertical_radius=0)
        start = (0, 100, 0)
        self.assertEqual(self.bot.plan_navigation(EntityNearGoal(unique_id, 2.2), rules=rules).nodes, (start,))
        self.assertEqual(self.bot.plan_navigation(goal, rules=start_only).status, "unreachable")

        self.set_block(wall_top, 1211861802)  # Confined, unresolved water outline.
        self.assertEqual(self.bot.plan_navigation(goal, rules=start_only).status, "unknown")
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, "found")
        self.set_block(wall_top, self.STONE)
        plan = self.bot.plan_navigation(goal, rules=rules)
        self.assertEqual((plan.status, plan.nodes), ("found", (start, (0, 100, 1), (1, 100, 1))))
        self.assertEqual(self.stream.sent, [])

        result = await self.bot.navigate(goal, rules=rules)
        self.assertEqual((result.status, result.stand_position, result.matches), ("reached", plan.endpoint, ()))
        self.assertGreater(result.frames, 0)
        self.assertEqual(self.bot.raycast_block_towards((2.5, 101.5, 0.75), max_distance=2.2).status, "miss")
        self.assert_released()

    async def test_entity_visibility_rechecks_the_synchronized_eye_after_input_release(self):
        self.prepare_visibility_terrain()
        unique_id, _ = self.spawn_navigation_actor(x=0.5, z=2.5)
        endpoint = (0, 100, 1)
        sight = EntityVisibilityGoal(unique_id, aim_offset=(0, 1.5, -0.25), max_distance=0.86)
        goal = AllGoal((FootGoal(endpoint), sight))
        rules = NavigationRules(search_radius=1, vertical_radius=0)
        self.assertEqual(self.bot.plan_navigation(goal, rules=rules).endpoint, endpoint)

        corrected = False

        def correct_on_release():
            nonlocal corrected
            frame = self.stream.sent[-1]
            if not corrected and self.up not in frame.input_data and frame.pos_delta.x == frame.pos_delta.z == 0.0:
                self.correct_position(0.5, 101.65625, 1.386, on_ground=True, tick=frame.client_tick)
                corrected = True

        self.stream.on_send = correct_on_release
        result = await self.bot.navigate(goal, rules=rules)
        self.assertTrue(corrected)
        self.assertEqual((result.status, result.stand_position), ("unreachable", None))
        self.assertGreater(result.frames, 0)
        self.assert_released()

    async def test_entity_visibility_replans_to_the_same_moving_actor(self):
        self.prepare_visibility_terrain()
        unique_id, runtime_id = self.spawn_navigation_actor(x=0.5, z=2.5)
        goal = EntityVisibilityGoal(unique_id, aim_offset=(0, 1.5, 0), max_distance=0.8)
        rules = NavigationRules(search_radius=2, vertical_radius=0)
        plans = []

        def move_after_first_frame():
            if len(self.stream.sent) == 1:
                self.move_villager(runtime_id, z=1.5)

        self.stream.on_send = move_after_first_frame
        result = await self.bot.navigate(goal, rules=rules, on_plan=plans.append)
        self.assertEqual([plan.endpoint for plan in plans], [(0, 100, 2), (0, 100, 1)])
        self.assertEqual((result.status, result.stand_position), ("reached", (0, 100, 1)))
        self.assert_released()

    async def test_queued_entity_visibility_goal_does_not_bind_a_reappearing_target(self):
        self.prepare_visibility_terrain()
        unique_id, _ = self.spawn_navigation_actor()
        sight = EntityVisibilityGoal(unique_id, aim_offset=(0, 1.5, 0), max_distance=2)
        goal = AllGoal((FootGoal((0, 100, 0)), sight))
        async with self.movement._action_lock:
            queued = asyncio.create_task(self.bot.navigate(goal))
            await asyncio.sleep(0)
            self.observe_entity(self.binding.RemoveActorPacket(entity_id=unique_id))
            self.spawn_navigation_actor()
        result = await queued
        self.assertEqual((result.status, result.frames, self.stream.sent), ("unknown", 0, []))
        self.assertEqual(self.bot.plan_navigation(goal).status, "found")

    async def test_villager_goal_uses_wire_feet_and_unique_id(self):
        self.prepare_terrain()
        unique_id, runtime_id = self.spawn_navigation_actor()
        cow_id, _ = self.spawn_navigation_actor(entity_id=-33, runtime_id=4, actor_type="minecraft:cow")
        goal = EntityNearGoal(unique_id, 0.25)
        self.assertEqual(self.bot.plan_navigation(goal).endpoint, (1, 100, 0))
        self.assertEqual(self.bot.plan_navigation(EntityNearGoal(cow_id, 0.25)).status, "unknown")
        self.assertEqual(self.bot.plan_navigation(EntityNearGoal(runtime_id, 0.25)).status, "unknown")

        self.move_villager(runtime_id, x=2.5, z=1.5)
        self.assertEqual(self.bot.observed_entities[0].position, (2.5, 100, 1.5))
        self.assertEqual(self.bot.plan_navigation(goal).endpoint, (2, 100, 1))
        self.assertEqual(self.stream.sent, [])

    async def test_villager_goal_requires_fresh_feet_after_an_unconfirmed_position(self):
        self.prepare_terrain()
        unique_id, runtime_id = self.spawn_navigation_actor()
        goal = EntityNearGoal(unique_id, 0.25)
        b = self.binding
        self.observe_entity(b.MoveActorAbsolutePacket(move_data=b.MoveActorAbsoluteData(
            runtime_id=runtime_id, pos=b.Vec3(x=3.5, y=100, z=1.5),
        )))
        self.move_villager(runtime_id, x=2.5)
        self.assertEqual(self.bot.observed_entities[0].position, (2.5, 100, 1.5))
        self.assertEqual(self.bot.plan_navigation(goal).status, "unknown")
        self.assertEqual((await self.bot.navigate(goal)).status, "unknown")

        self.move_villager(runtime_id, x=2.5, y=100, z=1.5)
        self.assertEqual(self.bot.plan_navigation(goal).endpoint, (2, 100, 1))
        self.observe_entity(b.SetActorLinkPacket(link=b.ActorLink(
            a=-38, b=unique_id, type=b.ActorLinkType.PASSENGER,
        )))
        self.move_villager(runtime_id, x=2.5, y=100, z=1.5)
        self.observe_entity(b.SetActorLinkPacket(link=b.ActorLink(
            a=-38, b=unique_id, type=b.ActorLinkType.NONE,
        )))
        self.assertEqual(self.bot.plan_navigation(goal).status, "unknown")
        self.move_villager(runtime_id, x=2.5, y=100, z=1.5)
        self.assertEqual(self.bot.plan_navigation(goal).endpoint, (2, 100, 1))
        self.assertEqual(self.stream.sent, [])

    async def test_navigation_detours_and_replans_for_the_same_moving_villager(self):
        self.prepare_terrain(blocked={(1, 100, 0), (1, 101, 0)})
        unique_id, runtime_id = self.spawn_navigation_actor(x=3.5)
        goal = EntityNearGoal(unique_id, 0.25)
        rules = NavigationRules(search_radius=4, vertical_radius=0)
        initial = self.bot.plan_navigation(goal, rules=rules)
        self.assertEqual((initial.status, initial.endpoint), ("found", (3, 100, 0)))
        self.assertNotIn((1, 100, 0), initial.nodes)
        self.assertEqual(self.stream.sent, [])

        def move_after_first_frame():
            if len(self.stream.sent) == 1:
                self.move_villager(runtime_id, z=2.5)

        self.stream.on_send = move_after_first_frame
        result = await self.bot.navigate(goal, rules=rules)
        self.assertEqual((result.status, result.stand_position), ("reached", (3, 100, 2)))
        self.assertGreater(result.expanded, initial.expanded)
        self.assertTrue(all(not (1 <= p.pos.x < 2 and 0 <= p.pos.z < 1) for p in self.moving_input()))
        self.assert_released()

    async def test_open_trade_after_navigation_uses_the_last_committed_input_origin(self):
        self.prepare_terrain()
        unique_id, runtime_id = self.spawn_navigation_actor(x=3.5)
        self.bot._inventory = Inventory(self.stream, 1)
        self.bot._trading = Trading(self.stream, self.bot._entities, self.movement, self.bot._inventory, None)
        b = self.binding
        result = await self.bot.navigate(EntityNearGoal(unique_id, 1.2))
        self.assertEqual((result.status, result.stand_position), ("reached", (2, 100, 0)))
        self.assertEqual(self.movement.position[0], 0.5)
        frame = self.stream.sent[-1]
        reply = asyncio.Event()
        response = self.stream.protocol.encode(b.UpdateTradePacket(
            container_id=5, type=b.ContainerType.TRADE, entity_unique_id=unique_id,
            last_trading_player=1, display_name="Farmer", data=nbt.CompoundTag({}),
        ))

        async def wait_for_packet(packet_id, *, timeout, match):
            self.assertEqual(packet_id, b.UpdateTradePacket.ID)
            await asyncio.wait_for(reply.wait(), timeout)
            self.assertTrue(match(response))
            return response

        self.stream.wait_for_packet = wait_for_packet
        self.stream.on_send = reply.set
        offers = await self.bot.open_trade(runtime_id)
        self.assertEqual((offers.window_id, offers.trader_unique_id), (5, unique_id))
        wire = self.stream.protocol
        action = wire.decode(wire.encode(self.stream.sent[-1])).variant_transaction
        self.assertEqual(action.action_type, b.ItemUseOnActorInventoryTransaction.ActionType.INTERACT)
        self.assertEqual(action.runtime_id, runtime_id)
        self.assertEqual((action.from_pos.x, action.from_pos.y, action.from_pos.z),
                         (frame.pos.x, frame.pos.y, frame.pos.z))

        self.correct_position(2.625, 101.62, 0.625, on_ground=True, tick=frame.client_tick)
        await self.bot.interact_entity(runtime_id)
        corrected = wire.decode(wire.encode(self.stream.sent[-1])).variant_transaction
        self.assertEqual((corrected.from_pos.x, corrected.from_pos.z), (2.625, 0.625))

    async def test_entity_interaction_rejects_a_dimension_loading_position(self):
        _, runtime_id = self.spawn_navigation_actor()
        self.bot._inventory = Inventory(self.stream, 1)
        b = self.binding
        transition = self.stream.protocol.encode(b.ChangeDimensionPacket(
            dimension_id=1, pos=b.Vec3(x=9.5, y=101.62, z=9.5), loading_screen_id=3,
        ))
        self.movement._on_packet(transition)
        self.bot._entities._on_packet(transition)
        with self.assertRaisesRegex(RuntimeError, "position is not synchronized"):
            await self.bot.interact_entity(runtime_id)
        self.assertEqual(self.stream.sent, [])

        self.set_position(1.5, 0.5)
        self.spawn_navigation_actor()
        await self.bot.interact_entity(runtime_id)
        self.assertEqual(self.stream.sent[-1].variant_transaction.from_pos.x, 1.5)

    async def test_navigation_does_not_substitute_removed_or_replaced_villagers(self):
        self.prepare_terrain()
        other_id, _ = self.spawn_navigation_actor(entity_id=-34, runtime_id=5, x=2.5)
        b = self.binding
        for new_runtime in (None, 2, 6):
            with self.subTest(new_runtime=new_runtime):
                self.set_position(0.5, 0.5)
                self.stream.sent.clear()
                unique_id, runtime_id = self.spawn_navigation_actor()
                goal = EntityNearGoal(unique_id, 0.25)

                def remove_during_navigation():
                    if len(self.stream.sent) != 1:
                        return
                    self.observe_entity(b.RemoveActorPacket(entity_id=unique_id))
                    if new_runtime is not None:
                        self.spawn_navigation_actor(entity_id=unique_id, runtime_id=new_runtime)
                    self.move_villager(runtime_id, z=0.5)

                self.stream.on_send = remove_during_navigation
                result = await self.bot.navigate(goal)
                self.assertEqual((result.status, result.frames, result.stand_position), ("unknown", 1, None))
                self.assert_released()
                self.assertEqual(self.bot.plan_navigation(EntityNearGoal(other_id, 0.25)).status, "found")
                self.assertEqual(self.bot.plan_navigation(goal).status, "unknown" if new_runtime is None else "found")

    async def test_queued_entity_goal_does_not_bind_a_replacement(self):
        self.prepare_terrain()
        unique_id, _ = self.spawn_navigation_actor()
        goal = AllGoal((EntityNearGoal(unique_id, 0.25), FootGoal((1, 100, 0))))
        async with self.movement._action_lock:
            queued = asyncio.create_task(self.bot.navigate(goal))
            await asyncio.sleep(0)
            self.observe_entity(self.binding.RemoveActorPacket(entity_id=unique_id))
            self.spawn_navigation_actor()
        result = await queued
        self.assertEqual((result.status, result.frames, self.stream.sent), ("unknown", 0, []))
        self.assertEqual(self.bot.plan_navigation(goal).status, "found")

    async def test_dimension_change_discards_the_villager_observation(self):
        self.prepare_terrain()
        unique_id, _ = self.spawn_navigation_actor()
        goal = EntityNearGoal(unique_id, 0.25)
        b = self.binding

        def change_after_first_frame():
            if len(self.stream.sent) == 1:
                transition = self.stream.protocol.encode(b.ChangeDimensionPacket(
                    dimension_id=1, pos=b.Vec3(x=0.5, y=101.62, z=0.5),
                ))
                self.blocks._on_packet(transition)
                self.movement._on_packet(transition)
                self.bot._entities._on_packet(transition)

        self.stream.on_send = change_after_first_frame
        result = await self.bot.navigate(goal)
        self.assertEqual((result.status, result.frames, len(self.stream.sent)), ("unknown", 1, 1))
        self.assertEqual(self.bot.observed_entities, ())
        self.set_position(0.5, 0.5)
        self.prepare_terrain()
        self.assertEqual(self.bot.plan_navigation(goal).status, "unknown")

    async def test_entity_goal_needs_visible_unique_id_and_normalizes_only_verified_player_packets(self):
        self.prepare_terrain()
        unique_id, runtime_id, roster = self.prepare_peer()
        goal = EntityNearGoal(unique_id, 0.25)
        self.assertEqual(self.bot.plan_navigation(goal).status, "unknown")
        self.observe_entity(roster)
        self.assertEqual(self.bot.plan_navigation(goal).endpoint, (1, 100, 0))
        self.assertEqual(self.bot.plan_navigation(EntityNearGoal(runtime_id, 0.25)).status, "unknown")
        self.move_peer(runtime_id, x=2.5, z=1.5)
        self.assertEqual(self.bot.plan_navigation(goal).endpoint, (2, 100, 1))
        self.assertGreater(self.bot.observed_entities[0].position[1], 101.6)

        b = self.binding
        self.observe_entity(b.RemoveActorPacket(entity_id=unique_id))
        self.assertEqual(self.bot.observed_players[0].entity_id, unique_id)
        self.move_peer(runtime_id, x=2.5, z=1.5)
        self.assertEqual(self.bot.plan_navigation(goal).status, "unknown")
        self.assertEqual(self.stream.sent, [])

    async def test_navigation_replans_when_the_observed_player_moves(self):
        self.prepare_terrain()
        unique_id, runtime_id, roster = self.prepare_peer(x=3.5)
        self.observe_entity(roster)
        goal = EntityNearGoal(unique_id, 0.25)
        initial = self.bot.plan_navigation(goal)
        self.assertEqual(initial.endpoint, (3, 100, 0))

        def move_peer():
            if len(self.stream.sent) == 1:
                self.move_peer(runtime_id, x=2.5, z=1.5)

        self.stream.on_send = move_peer
        result = await self.bot.navigate(goal)
        self.assertEqual((result.status, result.stand_position), ("reached", (2, 100, 1)))
        self.assertGreater(result.expanded, initial.expanded)
        self.assert_released()

    async def test_navigation_rechecks_entity_visibility_after_input_release(self):
        self.prepare_terrain()
        unique_id, _, roster = self.prepare_peer()
        self.observe_entity(roster)
        gone = False

        def remove_at_end():
            nonlocal gone
            if not gone and self.up not in self.stream.sent[-1].input_data:
                self.observe_entity(self.binding.RemoveActorPacket(entity_id=unique_id))
                gone = True

        self.stream.on_send = remove_at_end
        result = await self.bot.navigate(EntityNearGoal(unique_id, 0.25))
        self.assertTrue(gone)
        self.assertEqual((result.status, result.stand_position), ("unknown", None))
        self.assertGreater(result.frames, 0)
        self.assert_released()

    async def test_navigation_charges_braking_before_resuming_after_tentative_arrival(self):
        self.prepare_terrain()
        for ticks, status, frames in ((21, "budget", 19), (23, "reached", 21)):
            with self.subTest(ticks=ticks):
                self.set_position(0.5, 0.5)
                self.stream.sent.clear()
                unique_id, runtime_id, roster = self.prepare_peer()
                self.observe_entity(roster)
                moved = False

                def move_on_first_release():
                    nonlocal moved
                    if not moved and self.up not in self.stream.sent[-1].input_data:
                        self.move_peer(runtime_id, x=2.5, z=0.5)
                        moved = True

                self.stream.on_send = move_on_first_release
                result = await self.bot.navigate(EntityNearGoal(unique_id, 0.25), ticks=ticks)
                self.assertTrue(moved)
                self.assertEqual((result.status, result.frames), (status, frames))
                self.assertEqual([self.up in packet.input_data for packet in self.stream.sent],
                                 [True] * 10 + [False] * 2 + [True] * (frames - 10) + [False] * 2)

    async def test_navigation_expansion_and_frame_budgets_are_cumulative_after_replan(self):
        goal = FootGoal((3, 100, 0))
        for resource in ("search", "frames"):
            with self.subTest(resource=resource):
                self.prepare_terrain()
                self.set_position(0.5, 0.5)
                self.stream.sent.clear()
                initial = self.bot.plan_navigation(goal, rules=NavigationRules(vertical_radius=0))
                limit = initial.expanded + 1 if resource == "search" else 16384
                plans = []

                def block_after_first_frame():
                    if len(self.stream.sent) == 1:
                        self.set_block((1, 100, 0), self.STONE)

                self.stream.on_send = block_after_first_frame
                result = await self.bot.navigate(goal, rules=NavigationRules(vertical_radius=0, max_expansions=limit),
                                                 ticks=1 if resource == "frames" else 160,
                                                 on_plan=plans.append)
                self.assertEqual((result.status, result.frames, len(self.stream.sent)), ("budget", 1, 3))
                plan, = plans
                self.assertEqual((plan.status, plan.nodes, plan.expanded),
                                 ("found", initial.nodes, initial.expanded))
                if resource == "search":
                    self.assertEqual(result.expanded, limit)
                else:
                    self.assertGreater(result.expanded, initial.expanded)
                self.assert_released()

    async def test_navigate_observes_the_adopted_route_before_moving_and_again_after_a_block_update(self):
        self.prepare_terrain()
        goal = FootGoal((3, 100, 0))
        rules = NavigationRules(vertical_radius=0)
        obstacle = (1, 100, 0)
        observed = []

        def block_after_first_frame():
            if len(self.stream.sent) == 1:
                self.set_block(obstacle, self.STONE)

        def on_plan(plan):
            observed.append((plan, len(self.stream.sent)))

        self.stream.on_send = block_after_first_frame
        result = await self.bot.navigate(goal, rules=rules, on_plan=on_plan)

        (initial, before_initial), (detour, before_detour) = observed
        self.assertEqual((before_initial, before_detour), (0, 1))
        self.assertEqual(initial.nodes, tuple((x, 100, 0) for x in range(4)))
        self.assertNotIn(obstacle, detour.nodes)
        self.assertEqual((initial.start, detour.start), ((0, 100, 0), (0, 100, 0)))
        self.assertEqual((initial.cost, detour.cost), (Fraction(3), Fraction(5)))
        for plan in (initial, detour):
            self.assertEqual((plan.status, plan.goal, plan.rules, plan.dimension_id, plan.endpoint),
                             ("found", goal, rules, 0, goal.position))
        with self.assertRaises(FrozenInstanceError):
            initial.nodes = detour.nodes
        self.assertEqual((result.status, result.stand_position, result.expanded),
                         ("reached", goal.position, initial.expanded + detour.expanded))
        self.assertEqual(result.frames, len(self.moving_input()))
        self.assert_avoided((1, 0))
        self.assert_released()

    async def test_navigate_observes_the_single_node_route_when_already_at_the_goal(self):
        self.prepare_terrain()
        goal = FootGoal((0, 100, 0))
        plans = []

        result = await self.bot.navigate(goal, ticks=0, on_plan=plans.append)

        plan, = plans
        self.assertEqual((plan.status, plan.nodes, plan.cost), ("found", (goal.position,), Fraction(0)))
        self.assertEqual(result, NavigationResult("reached", 0, plan.expanded, goal.position))
        self.assertEqual(self.stream.sent, [])

        unreachable = await self.bot.navigate(FootGoal((3, 100, 0)), rules=NavigationRules(search_radius=0),
                                              on_plan=plans.append)
        self.assertEqual(unreachable.status, "unreachable")
        self.assertEqual(plans, [plan])

    async def test_navigate_does_not_observe_a_moving_route_with_no_frames_left(self):
        self.prepare_terrain()
        goal = FootGoal((3, 100, 0))
        rules = NavigationRules(vertical_radius=0)
        search = self.bot.plan_navigation(goal, rules=rules)
        plans = []

        result = await self.bot.navigate(goal, rules=rules, ticks=0, on_plan=plans.append)

        self.assertEqual(search.status, "found")
        self.assertEqual(result, NavigationResult("budget", 0, search.expanded))
        self.assertEqual(plans, [])
        self.assertEqual(self.stream.sent, [])

    async def test_navigate_observes_a_route_requiring_recentering_only_with_frames_left(self):
        self.prepare_terrain()
        self.set_position(0.2, 0.5)
        goal = FootGoal((0, 100, 0))
        plans = []

        result = await self.bot.navigate(goal, ticks=0, on_plan=plans.append)

        self.assertEqual(result, NavigationResult("budget", 0, 1))
        self.assertEqual(plans, [])
        self.assertEqual(self.stream.sent, [])

        result = await self.bot.navigate(goal, on_plan=plans.append)

        plan, = plans
        self.assertEqual((plan.status, plan.nodes), ("found", (goal.position,)))
        self.assertEqual((result.status, result.stand_position), ("reached", goal.position))
        self.assertGreater(result.frames, 0)
        self.assertEqual(result.frames, len(self.moving_input()))
        self.assertGreater(self.moving_input()[-1].pos.x, 0.2)

    async def test_navigate_rechecks_for_a_stop_requested_by_the_callback(self):
        self.prepare_terrain()
        goal = FootGoal((3, 100, 0))
        loop = asyncio.get_running_loop()
        stops = []

        def on_plan(plan):
            stops.append(asyncio.eager_task_factory(loop, self.bot.stop_moving()))

        result = await self.bot.navigate(goal, on_plan=on_plan)

        stop, = stops
        await asyncio.wait_for(stop, 1)
        self.assertEqual((result.status, result.frames), ("stopped", 0))
        self.assertEqual(self.stream.sent, [])
        self.assertEqual(await asyncio.wait_for(self.bot.walk_forward(1), 1), 1)

    async def test_navigate_callback_time_counts_toward_the_deadline(self):
        self.prepare_terrain()
        loop = asyncio.get_running_loop()
        time = loop.time
        offset = 0

        def on_plan(plan):
            nonlocal offset
            offset = 12

        with patch.object(loop, "time", lambda: time() + offset), patch.object(loop, "slow_callback_duration", 20):
            result = await self.bot.navigate(FootGoal((3, 100, 0)), on_plan=on_plan, timeout=10)
        self.assertEqual((result.status, result.frames), ("budget", 0))
        self.assertEqual(self.stream.sent, [])

    async def test_navigate_stops_delivering_plans_on_stop_or_cancellation_and_releases_movement(self):
        self.prepare_terrain()
        goal = FootGoal((3, 100, 0))
        plans = []
        first_frame = asyncio.Event()
        self.stream.on_send = first_frame.set

        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                plans.clear()
                first_frame.clear()
                self.set_position(0.5, 0.5)
                self.stream.sent.clear()
                task = asyncio.create_task(self.bot.navigate(goal, on_plan=plans.append))
                await asyncio.wait_for(first_frame.wait(), 1)
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 1)
                else:
                    await asyncio.wait_for(self.bot.stop_moving(), 1)
                    self.assertEqual((await task).status, "stopped")
                self.assert_released()
                self.assertEqual(await asyncio.wait_for(self.bot.walk_forward(1), 1), 1)
                result = await self.bot.navigate(FootGoal((0, 100, 0)))
                self.assertEqual(result.status, "reached")
                self.assertEqual(len(plans), 1)

    async def test_navigate_callback_error_propagates_after_movement_cleanup(self):
        goal = FootGoal((3, 100, 0))
        rules = NavigationRules(vertical_radius=0)
        send = self.stream.send

        for error_type, stall_release in ((TimeoutError, False), (TimeoutError, True), (asyncio.CancelledError, True)):
            with self.subTest(error_type=error_type, stall_release=stall_release):
                self.prepare_terrain()
                self.set_position(0.5, 0.5)
                self.stream.sent.clear()
                plans = []
                cleanup_started = asyncio.Event()
                expected_error = error_type("route observer failed")

                async def send_with_obstruction(packet):
                    if self.stream.sent:
                        cleanup_started.set()
                        if stall_release:
                            await asyncio.Future()
                    await send(packet)
                    if len(self.stream.sent) == 1:
                        self.set_block((1, 100, 0), self.STONE)

                def on_plan(plan):
                    plans.append(plan)
                    if len(plans) == 2:
                        raise expected_error

                with patch.object(self.stream, "send", send_with_obstruction):
                    with self.assertRaises(error_type) as caught:
                        await self.bot.navigate(goal, rules=rules, on_plan=on_plan, timeout=0.2 if stall_release else 5)
                self.assertIs(caught.exception, expected_error)
                self.assertTrue(cleanup_started.is_set())
                self.assertEqual(len(self.moving_input()), 1)
                if not stall_release:
                    self.assert_released()
                self.assertEqual(await asyncio.wait_for(self.bot.walk_forward(1), 1), 1)
                self.assertEqual(len(plans), 2)

    async def test_navigation_dimension_change_during_release_invalidates_same_id_stance(self):
        self.prepare_terrain()
        b = self.binding
        target = FootGoal((1, 100, 0))
        changed = False

        def reload_dimension():
            nonlocal changed
            frame = self.stream.sent[-1]
            if changed or self.up in frame.input_data:
                return
            changed = True
            change = self.stream.protocol.encode(b.ChangeDimensionPacket(dimension_id=0, pos=frame.pos))
            self.blocks._on_packet(change)
            self.movement._on_packet(change)
            self.assertEqual(self.bot.plan_navigation(target).status, "unknown")
            self.prepare_terrain()
            self.set_position(1.5, 0.5)
            self.movement._dimensions = _DimensionTransitions(self.stream)
            self.movement._dimensions.active = DimensionTransition(0, (1.5, 101.62, 0.5), False, None)
            self.assertEqual(self.bot.plan_navigation(target).status, "unknown")

        self.stream.on_send = reload_dimension
        result = await self.bot.navigate(target)
        self.assertEqual((result.status, result.stand_position), ("unknown", None))
        self.assertTrue(changed)
        self.assertEqual(len(self.stream.sent), result.frames + 1)
        self.assertEqual((await self.bot.navigate(target, ticks=0)).status, "unknown")
        self.movement._dimensions.active = None
        self.assertEqual((await self.bot.navigate(target, ticks=0)).status, "reached")

    async def test_navigation_keeps_its_deadline_after_replanning(self):
        self.prepare_terrain()
        goal = FootGoal((3, 100, 0))
        rules = NavigationRules(vertical_radius=0)
        initial = self.bot.plan_navigation(goal, rules=rules)
        loop = asyncio.get_running_loop()
        time = loop.time
        offset = 0

        def change_terrain_and_advance_time():
            nonlocal offset
            if len(self.stream.sent) == 1:
                self.set_block((1, 100, 0), self.STONE)
                offset = 6
            elif self.up in self.stream.sent[-1].input_data:
                offset = 12

        self.stream.on_send = change_terrain_and_advance_time
        with patch.object(loop, "time", lambda: time() + offset), patch.object(loop, "slow_callback_duration", 20):
            result = await self.bot.navigate(goal, rules=rules, timeout=10)
        self.assertEqual((result.status, result.frames), ("budget", 2))
        self.assertGreater(result.expanded, initial.expanded)
        self.assertEqual([self.up in frame.input_data for frame in self.stream.sent], [True, False, False, True])

    async def test_navigation_deadline_cancels_pending_route_and_release_sends(self):
        self.prepare_terrain()
        send = self.stream.send
        for phase, ticks, blocked_send, frames, target in (
            ("first", 160, 1, 0, (3, 100, 0)),
            ("moving", 160, 2, 1, (3, 100, 0)),
            ("cleanup", 1, 2, 1, (3, 100, 0)),
            ("arrival", 0, 1, 0, (0, 100, 0)),
        ):
            with self.subTest(phase=phase):
                self.set_position(0.5, 0.5, teleport=True)
                self.stream.sent.clear()
                resume = asyncio.Event()
                attempts = []

                async def send_with_gate(packet):
                    attempts.append(packet)
                    if len(attempts) == blocked_send:
                        await resume.wait()
                    await send(packet)

                self.stream.send = send_with_gate
                try:
                    result = await asyncio.wait_for(self.bot.navigate(FootGoal(target), ticks=ticks, timeout=0.2), 1)
                    self.assertEqual((result.status, result.frames), ("budget", frames))
                    self.assertEqual(len(attempts), blocked_send)
                    resume.set()
                    await asyncio.sleep(0)
                    self.assertEqual(self.stream.sent, attempts[:frames])
                finally:
                    self.stream.send = send

    async def test_navigation_external_cancellation_remains_cancellation_during_stalled_cleanup(self):
        self.prepare_terrain()
        start_send = asyncio.Event()
        resume = asyncio.Event()
        send = self.stream.send
        attempts = []

        async def send_with_gate(packet):
            attempts.append(packet)
            if len(attempts) == 1:
                await send(packet)
                return
            start_send.set()
            await resume.wait()
            await send(packet)

        self.stream.send = send_with_gate
        task = asyncio.create_task(self.bot.navigate(FootGoal((3, 100, 0)), timeout=0.2))
        try:
            await asyncio.wait_for(start_send.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            resume.set()
            await asyncio.sleep(0)
            self.assertEqual(len(attempts), 3)
            self.assertNotIn(self.up, attempts[-1].input_data)
            self.assertEqual(self.stream.sent, attempts[:1])
        finally:
            self.stream.send = send

    async def test_timed_out_ascent_preserves_stance_and_the_next_teleport_ack(self):
        target, jumps, landings = self.ascent_route(steps=1)
        self.set_position(0.5, 0.5, teleport=True)
        goal = BlockFaceGoal(target)
        plan = self.bot.plan_navigation(goal)
        state = self.bot.movement_state
        self.assertEqual(plan.status, "found")
        flags = self.binding.PlayerAuthInputPacket.InputData
        resume = asyncio.Event()
        send = self.stream.send
        attempted = []

        async def block_send(packet):
            attempted.append(packet)
            await resume.wait()
            await send(packet)

        self.stream.send = block_send
        try:
            result = await asyncio.wait_for(self.bot.navigate(goal, timeout=0.2), 1)
            self.assertEqual((result.status, result.frames), ("budget", 0))
            frame, = attempted
            self.assertIn(flags.START_JUMPING, frame.input_data)
            self.assertIn(flags.HANDLED_TELEPORT, frame.input_data)
            self.assertEqual((self.bot.movement_state, self.bot.plan_navigation(goal)), (state, plan))
            resume.set()
            await asyncio.sleep(0)
            self.assertEqual(self.stream.sent, [])
        finally:
            self.stream.send = send

        retry = await self.bot.navigate(goal)
        self.assertEqual((retry.status, retry.stand_position), ("reached", plan.endpoint))
        self.assertEqual(len(jumps), 1)
        self.assertEqual(len(landings), 1)
        actual_jump = self.stream.sent[0]
        self.assertIn(flags.START_JUMPING, actual_jump.input_data)
        self.assertIn(flags.HANDLED_TELEPORT, actual_jump.input_data)
        self.assertTrue(all(flags.HANDLED_TELEPORT not in frame.input_data for frame in self.stream.sent[1:]))

    async def test_cancelled_unsent_ascent_emits_no_jump_release_or_vertical_motion(self):
        target = self.prepare_ascent()
        self.set_position(0.5, 0.5, teleport=True)
        state = self.bot.movement_state
        flags = self.binding.PlayerAuthInputPacket.InputData
        start_send = asyncio.Event()
        resume = asyncio.Event()
        send = self.stream.send
        attempted = []

        async def block_jump(packet):
            attempted.append(packet)
            if flags.START_JUMPING in packet.input_data:
                start_send.set()
                await resume.wait()
            await send(packet)

        self.stream.send = block_jump
        task = asyncio.create_task(self.bot.navigate(BlockFaceGoal(target)))
        try:
            await asyncio.wait_for(start_send.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            resume.set()
            await asyncio.sleep(0)
            self.assertEqual((len(attempted), self.stream.sent), (1, []))
            self.assertEqual(self.bot.movement_state, state)
        finally:
            self.stream.send = send

        self.assertEqual((await self.bot.navigate(FootGoal((0, 100, 0)), ticks=0)).status, "reached")
        self.assertIn(flags.HANDLED_TELEPORT, self.stream.sent[0].input_data)
        for frame in self.stream.sent:
            self.assertAlmostEqual(frame.pos.y, 101.62, places=4)
            self.assertNotIn(flags.JUMP_RELEASED_RAW, frame.input_data)

    async def test_unreachable_block_approach_preserves_scheduled_movement(self):
        self.prepare_terrain()
        sent = asyncio.Event()
        self.stream.on_send = sent.set
        await self.bot.set_move_vector(0, 1)
        self.movement.start()
        self.assertEqual(await self.bot.approach_block((12, 100, 0), search_radius=2),
                         BlockApproach("unreachable", 0))
        await asyncio.wait_for(sent.wait(), 1)
        frame = self.stream.protocol.decode(self.stream.protocol.encode(self.stream.sent[0]))
        self.assertIn(self.up, frame.input_data)
        self.assertGreater(frame.pos.z, 0.5)

    async def test_unknown_start_still_requires_a_possible_goal_inside_search_bounds(self):
        self.prepare_terrain(unknown={(0, 100, 0)})
        rules = NavigationRules(search_radius=0)
        for foot, expected in (((1, 100, 0), "unreachable"), ((0, 100, 0), "unknown")):
            with self.subTest(foot=foot):
                goal = FootGoal(foot)
                self.assertEqual(self.bot.plan_navigation(goal, rules=rules).status, expected)
                result = await self.bot.navigate(goal, rules=rules)
                self.assertEqual((result.status, result.frames), (expected, 0))
        self.assertEqual(self.stream.sent, [])

    async def test_stop_and_deadline_while_waiting_for_navigation_pose(self):
        self.prepare_terrain()
        self.bot._movement = self.movement = Movement(self.stream, 1)
        self.blocks.movement = self.movement
        target = FootGoal((0, 100, 0))
        plan = self.bot.plan_navigation(target)
        self.assertEqual((plan.status, plan.start, plan.expanded), ("unknown", None, 0))
        self.assertEqual(await self.bot.navigate(target, timeout=0.02), NavigationResult("budget", 0))
        pending = asyncio.create_task(self.bot.navigate(target))
        await asyncio.sleep(0)
        await asyncio.wait_for(self.bot.stop_moving(), 1)
        self.assertEqual(await pending, NavigationResult("stopped", 0))
        self.assertEqual(self.stream.sent, [])

        pending = asyncio.create_task(self.bot.navigate(target, ticks=0))
        await asyncio.sleep(0)
        self.set_position(0.5, 0.5)
        result = await asyncio.wait_for(pending, 1)
        self.assertEqual(result, NavigationResult("reached", 0, 1, target.position))
