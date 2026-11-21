import asyncio
import unittest
from unittest.mock import patch

from endpilot.blocks import BlockUpdate
from endpilot.mcp_server import MCPPlayer


class FakeBot:
    def __init__(self, name):
        self.player_id = 17
        self.updated = asyncio.Event()
        self.closed = False
        self.placed = []
        self.position = (0.5, 101.62, 0.5)
        self.walk_started = asyncio.Event()
        self.walk_stopped = asyncio.Event()
        self.walk_done = asyncio.Event()

    async def join_nethernet(self, url):
        self.url = url

    async def join_raknet(self, host, port):
        self.url = (host, port)

    async def wait_for_block_update(self, position, *, layer, timeout):
        async with asyncio.timeout(timeout):
            await self.updated.wait()
        return BlockUpdate(position, 42, layer)

    async def place_against(self, support, *, face, slot):
        self.placed.append((support, face, slot))
        return 123

    async def walk_forward(self, ticks):
        self.walk_stopped.clear()
        self.walk_done.clear()
        self.walk_started.set()
        count = 0
        try:
            for _ in range(ticks):
                if self.walk_stopped.is_set():
                    break
                count += 1
                await asyncio.sleep(0.005)
            return count
        finally:
            self.walk_stopped.set()
            self.walk_done.set()

    async def stop_moving(self):
        self.walk_stopped.set()
        await self.walk_done.wait()

    async def close(self):
        self.closed = True


class MCPPlayerTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_observation_before_sending_and_disconnect_clears_state(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            self.assertEqual((await player.connect("http://localhost:19132"))["player_id"], 17)
            bot = player.bot
            with self.assertRaisesRegex(RuntimeError, "server update"):
                await player.place_against(2, 99, 0)
            self.assertEqual(bot.placed, [])
            self.assertEqual(await player.watch_block(2, 99, 0), {"watching": [2, 99, 0], "layer": 0})
            bot.updated.set()
            self.assertEqual((await player.read_block(2, 99, 0))["runtime_id"], 42)
            self.assertEqual((await player.place_against(2, 99, 0))["submitted_tick"], 123)
            self.assertEqual(bot.placed, [(BlockUpdate((2, 99, 0), 42, 0), 1, 0)])
            self.assertEqual(await player.disconnect(), {"disconnected": True})
            self.assertTrue(bot.closed)
            with self.assertRaisesRegex(RuntimeError, "connect a player"):
                await player.place_against(2, 99, 0)

    async def test_disconnect_cancels_pending_block_wait(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("raknet://127.0.0.1:19132")
            bot = player.bot
            await player.watch_block(2, 99, 0)
            await player.disconnect()
            self.assertTrue(bot.closed)
            self.assertEqual(player._pending, {})

    async def test_timed_out_watch_can_be_rearmed_without_read(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132")
            await player.watch_block(2, 99, 0, timeout=0.01)
            with self.assertRaisesRegex(RuntimeError, "already being watched"):
                await player.watch_block(2, 99, 0)
            await asyncio.sleep(0.03)
            await player.watch_block(2, 99, 0)
            player.bot.updated.set()
            self.assertEqual((await player.read_block(2, 99, 0))["runtime_id"], 42)
            await player.disconnect()

    async def test_walk_start_observe_stop_then_reuse(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132")
            with self.assertRaisesRegex(RuntimeError, "start_walk"):
                await player.observe_walk()
            self.assertEqual(await player.start_walk(100),
                             {"started": True, "requested_frames": 100})
            self.assertEqual((await player.observe_walk())["state"], "running")
            with self.assertRaisesRegex(RuntimeError, "previous walk"):
                await player.start_walk(1)
            await asyncio.sleep(0.02)
            stopped = await player.stop_walk()
            self.assertEqual(stopped["state"], "stopped")
            self.assertTrue(0 < stopped["submitted_frames"] < 100)
            self.assertEqual(stopped["server_position"], (0.5, 101.62, 0.5))
            self.assertTrue(player.bot.walk_stopped.is_set())
            await player.start_walk(2)
            await asyncio.sleep(0.03)
            completed = await player.observe_walk()
            self.assertEqual(completed["state"], "finished")
            self.assertEqual(completed["submitted_frames"], 2)
            await player.disconnect()

    async def test_disconnect_cancels_active_walk(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132")
            await player.start_walk(100)
            bot = player.bot
            await player.disconnect()
            self.assertTrue(bot.closed)
            self.assertTrue(bot.walk_stopped.is_set())
            self.assertIsNone(player._walk)
