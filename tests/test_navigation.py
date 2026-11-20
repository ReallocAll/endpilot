"""Known-flat-ground route selection and input release behavior."""
import asyncio
from types import SimpleNamespace
import unittest

from endpilot.movement import Movement
from endpilot.protocol import Protocol


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


class Sink:
    def __init__(self):
        self.protocol = Protocol(2193)
        self.sent = []

    async def send(self, packet):
        self.sent.append(packet)


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
