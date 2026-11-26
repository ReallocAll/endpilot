import asyncio
from dataclasses import replace
from fractions import Fraction
from importlib.util import find_spec
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

import endpilot
from endpilot.blocks import BlockChange, BlockObservation, BlockUpdate
from endpilot.inventory import HeldItemStack
from endpilot.mcp_server import MCPPlayer
from endpilot.navigation import BlockInteractionGoal, BlockInteractionMatch, NavigationPlan, NavigationResult, NavigationRules
from endpilot.placement import PlacementInventoryChange, PlacementResult


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
        self.blocks = {}

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

    def observed_block(self, position, *, layer):
        return self.blocks.get((position, layer))

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
    async def test_current_block_snapshot_preserves_air_and_unknown_without_watching(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132")
            position = (2, 99, 0)
            self.assertEqual(await player.observed_block(*position), {
                "position": position, "layer": 0, "observed": False, "runtime_id": None, "is_air": None,
            })
            player.bot.blocks[position, 0] = BlockObservation(position, None, 0, True)
            self.assertEqual(await player.observed_block(*position), {
                "position": position, "layer": 0, "observed": True, "runtime_id": None, "is_air": True,
            })
            player.bot.blocks[position, 1] = BlockObservation(position, 31, 1, None)
            self.assertEqual(await player.observed_block(*position, layer=1), {
                "position": position, "layer": 1, "observed": True, "runtime_id": 31, "is_air": None,
            })
            await player.disconnect()

    async def test_placement_delegates_and_preserves_separate_observations(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132")
            target = (2, 100, 0)
            change = BlockChange(0, BlockObservation(target, None, 0, True), BlockObservation(target, 42, 0, False))
            before = HeldItemStack(9, 3, 0, 71, 42, b"\xff\x00")
            after = replace(before, count=2, net_id=72)
            complete = PlacementResult(0, target, 0, True, "observations_complete", change,
                                       PlacementInventoryChange(before, after))
            partial = replace(complete, reason="timed_out", inventory_change=None)
            player.bot.place_block = AsyncMock(side_effect=(complete, partial))
            observed = await player.place_block(2, 99, 0, 42, click_pos=(.25, 1, .75), timeout=2)
            player.bot.place_block.assert_awaited_once_with((2, 99, 0), face=1, expected_runtime_id=42,
                                                           click_pos=(.25, 1, .75), timeout=2)
            self.assertEqual(observed, {
                "dimension_id": 0, "target": target, "slot": 0, "submitted": True,
                "reason": "observations_complete", "world_change": change,
                "inventory_change": {
                    "before": {"item_id": 9, "count": 3, "aux_value": 0, "net_id": 71,
                               "block_runtime_id": 42, "user_data_hex": "ff00"},
                    "after": {"item_id": 9, "count": 2, "aux_value": 0, "net_id": 72,
                              "block_runtime_id": 42, "user_data_hex": "ff00"},
                },
            })
            result = await player.place_block(2, 99, 0, 42)
            self.assertEqual((result["reason"], result["world_change"], result["inventory_change"]),
                             ("timed_out", change, None))
            await player.disconnect()

    async def test_placement_reports_only_progress_supplied_by_sdk_cancellation(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132")
            cancellation = asyncio.CancelledError()
            cancellation.placement_progress = PlacementResult(1, (2, 100, 0), 4, None, "cancelled", None, None)
            player.bot.place_block = AsyncMock(side_effect=(cancellation, asyncio.CancelledError()))
            report = await player.place_block(2, 99, 0, 42)
            self.assertEqual(report, {"dimension_id": 1, "target": (2, 100, 0), "slot": 4, "submitted": None,
                                      "reason": "cancelled", "world_change": None, "inventory_change": None})
            with self.assertRaises(asyncio.CancelledError):
                await player.place_block(2, 99, 0, 42)
            await player.disconnect()

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

    async def test_connect_raknet_addresses_use_the_sdk_endpoint_rules(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            for address, endpoint in (
                ("raknet://[::1]", ("::1", 19132)),
                ("raknet://[::1]:19140", ("::1", 19140)),
                ("raknet://127.0.0.1", ("127.0.0.1", 19132)),
                ("http://localhost:19132/signaling", "http://localhost:19132/signaling"),
            ):
                with self.subTest(address=address):
                    await player.connect(address)
                    try:
                        self.assertEqual(player.bot.url, endpoint)
                    finally:
                        await player.disconnect()

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
            self.assertIsNone(player._movement)

    async def test_handoff_stops_walk_and_returns_same_live_bot(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132")
            bot = player.bot
            await player.watch_block(2, 99, 0)
            await player.start_walk(100)
            await asyncio.sleep(0.02)
            handed = await player.handoff()
            self.assertIs(handed, bot)
            self.assertIsNone(player.bot)
            self.assertIsNone(player._movement)
            self.assertEqual(player._pending, {})
            self.assertTrue(bot.walk_stopped.is_set())
            self.assertFalse(bot.closed)
            self.assertEqual(await bot.walk_forward(2), 2)
            self.assertEqual(await player.disconnect(), {"disconnected": False})
            self.assertFalse(bot.closed)
            await bot.close()
            self.assertTrue(bot.closed)

    async def test_handoff_cancels_in_flight_block_read(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132")
            bot = player.bot
            await player.watch_block(2, 99, 0)
            read_task = asyncio.create_task(player.read_block(2, 99, 0))
            await asyncio.sleep(0)
            self.assertEqual(player._pending, {})
            self.assertFalse(read_task.done())

            self.assertIs(await player.handoff(), bot)
            self.assertTrue(read_task.cancelled())
            bot.updated.set()
            await asyncio.sleep(0)
            self.assertEqual(player._observed, {})
            self.assertFalse(bot.closed)
            await bot.close()

    async def test_disconnect_cancels_in_flight_block_read(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132")
            await player.watch_block(2, 99, 0)
            read_task = asyncio.create_task(player.read_block(2, 99, 0))
            await asyncio.sleep(0)
            self.assertEqual(player._pending, {})

            self.assertEqual(await player.disconnect(), {"disconnected": True})
            self.assertTrue(read_task.cancelled())
            self.assertEqual(player._observed, {})

    async def test_handoff_requires_connected_player(self):
        with self.assertRaisesRegex(RuntimeError, "connect a player"):
            await MCPPlayer().handoff()

    async def test_navigation_plan_and_section_request_delegate_in_world_coordinates(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132")
            bot = player.bot
            goal = BlockInteractionGoal((4, 100, -1), 4.5)
            rules = NavigationRules(6, 2, 75)
            hit = BlockInteractionMatch(goal.position, 2, (.25, .5, 0.0))
            bot.plan_navigation = Mock(return_value=NavigationPlan(goal, rules, 1, (2, 100, -1), "found",
                ((2, 100, -1), (3, 100, -1)), (hit,), Fraction(3, 2), 2))
            bot.request_sub_chunk = AsyncMock()
            self.assertEqual(await player.plan_block_interaction(4, 100, -1, 4.5, 6, 2, 75), {
                "target": goal.position, "dimension_id": 1, "start": (2, 100, -1), "status": "found",
                "nodes": ((2, 100, -1), (3, 100, -1)), "endpoint": (3, 100, -1), "matches": (hit,),
                "cost": {"numerator": 3, "denominator": 2}, "expanded": 2,
            })
            bot.plan_navigation.assert_called_once_with(goal, rules=rules)
            bot.request_sub_chunk.assert_not_awaited()
            self.assertEqual(await player.request_sub_chunk(-17, 101, 34), {
                "position": (-17, 101, 34), "requested": True,
            })
            bot.request_sub_chunk.assert_awaited_once_with((-17, 101, 34))
            await player.disconnect()

    async def test_navigation_does_not_release_the_slot_until_the_result_is_consumed(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132")
            bot = player.bot
            goal = BlockInteractionGoal((8, 100, 0), 5)
            bot.navigate = AsyncMock(side_effect=(NavigationResult("unknown", 0),
                                                  NavigationResult("unreachable", 0, 5),
                                                  NavigationResult("budget", 3, 7)))
            for status, frames, expanded in (("unknown", 0, 0), ("unreachable", 0, 5), ("budget", 3, 7)):
                with self.subTest(status=status):
                    ack = await player.start_block_interaction(*goal.position, ticks=11, timeout=2.5)
                    self.assertEqual(ack, {"started": True, "action": "block_interaction",
                                           "target": goal.position, "requested_frames": 11})
                    with self.assertRaisesRegex(RuntimeError, "previous walk"):
                        await player.start_walk(10)
                    result = await player.observe_walk()
                    self.assertEqual((result["state"], result["status"], result["submitted_frames"], result["expanded"]),
                                     ("finished", status, frames, expanded))
                    self.assertIsNone(result["stand_position"])
                    self.assertEqual(result["matches"], [])
            bot.navigate.assert_awaited_with(goal, rules=NavigationRules(), ticks=11, timeout=2.5)
            await player.disconnect()

    async def test_natural_completion_during_stop_remains_owned_and_keeps_its_actual_status(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132")
            bot = player.bot
            self.addAsyncCleanup(player.disconnect)
            first = (8, 100, 0)
            second = (2, 100, 3)
            finish, entered, released = asyncio.Event(), asyncio.Event(), asyncio.Event()
            hit = BlockInteractionMatch(first, 4, (0.0, .5, .5))

            async def navigate(goal, **_kwargs):
                if goal.position == second:
                    return NavigationResult("budget", 2)
                await finish.wait()
                return NavigationResult("reached", 17, 24, (6, 100, 0), (hit,))

            async def stop():
                entered.set()
                await released.wait()

            bot.navigate = navigate
            bot.stop_moving = stop
            await player.start_block_interaction(*first)
            stopping = asyncio.create_task(player.stop_walk())
            try:
                await entered.wait()
                self.assertEqual((await player.observe_walk())["state"], "stopping")
                finish.set()
                await asyncio.sleep(0)
                self.assertEqual((await player.observe_walk())["state"], "stopping")
                with self.assertRaisesRegex(RuntimeError, "previous walk"):
                    await player.start_block_interaction(*second)
                with self.assertRaisesRegex(RuntimeError, "already stopping"):
                    await player.stop_walk()
            finally:
                released.set()
                terminal = await stopping
            self.assertEqual(terminal, {"state": "finished", "action": "block_interaction", "target": first,
                                        "status": "reached", "submitted_frames": 17, "expanded": 24,
                                        "stand_position": (6, 100, 0),
                                        "matches": [{"position": first, "face": 4, "click_pos": (0.0, .5, .5)}],
                                        "server_position": bot.position})
            await player.start_block_interaction(*second)
            self.assertEqual((await player.observe_walk())["target"], second)

    async def test_cancelled_stop_keeps_the_sdk_result_for_the_same_owner(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132")
            bot = player.bot
            self.addAsyncCleanup(player.disconnect)
            stop_requested, release = asyncio.Event(), asyncio.Event()

            async def navigate(*_args, **_kwargs):
                await stop_requested.wait()
                return NavigationResult("stopped", 4)

            async def stop():
                stop_requested.set()
                await release.wait()

            bot.navigate, bot.stop_moving = navigate, stop
            await player.start_block_interaction(8, 100, 0)
            stopping = asyncio.create_task(player.stop_walk())
            await stop_requested.wait()
            stopping.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stopping
            with self.assertRaisesRegex(RuntimeError, "previous walk"):
                await player.start_walk()
            final = await player.observe_walk()
            self.assertEqual((final["state"], final["status"], final["submitted_frames"]), ("stopped", "stopped", 4))

    async def test_handoff_drains_navigation_before_transferring_the_same_connection(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132")
            bot = player.bot
            self.addAsyncCleanup(bot.close)
            stopping, release, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def navigate(*_args, **_kwargs):
                await stopping.wait()
                await release.wait()
                drained.set()
                return NavigationResult("stopped", 1)

            async def stop():
                stopping.set()
                await drained.wait()

            bot.navigate, bot.stop_moving = navigate, stop
            await player.start_block_interaction(8, 100, 0)
            handing_off = asyncio.create_task(player.handoff())
            try:
                await stopping.wait()
                self.assertFalse(handing_off.done())
                with self.assertRaisesRegex(RuntimeError, "connect a player"):
                    await player.start_walk()
            finally:
                release.set()
                handed = await handing_off
            self.assertIs(handed, bot)
            self.assertTrue(drained.is_set())
            self.assertFalse(bot.closed)
            self.assertIsNone(player._movement)
            self.assertEqual(await player.disconnect(), {"disconnected": False})

    async def _check_queued_reconnect(self, cleanup):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132", "A")
            original = player.bot
            entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def navigate(*_args, **_kwargs):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    draining.set()
                    await release.wait()

            original.navigate = navigate
            await player.start_block_interaction(8, 100, 0)
            await entered.wait()
            close_original = asyncio.create_task(player.disconnect())
            queued = [close_original]
            try:
                await draining.wait()
                reconnect = asyncio.create_task(player.connect("http://localhost:19132", "B"))
                queued.append(reconnect)
                await asyncio.sleep(0)
                stale = asyncio.create_task(getattr(player, cleanup)())
                queued.append(stale)
                await asyncio.sleep(0)
                self.assertIs(player.bot, original)
                self.assertTrue(all(not task.done() for task in queued))
                release.set()
                self.assertEqual(await close_original, {"disconnected": True})
                self.assertTrue(original.closed)
                self.assertEqual((await reconnect)["name"], "B")
                if cleanup == "disconnect":
                    self.assertEqual(await stale, {"disconnected": False})
                else:
                    with self.assertRaisesRegex(RuntimeError, "connection changed"):
                        await stale
                replacement = player.bot
                self.assertIsNot(replacement, original)
                self.assertFalse(replacement.closed)
                await player.start_walk(1)
                await replacement.walk_done.wait()
                self.assertEqual((await player.observe_walk())["submitted_frames"], 1)
            finally:
                release.set()
                await asyncio.gather(*queued, return_exceptions=True)
                await player.disconnect()

    async def test_queued_disconnect_does_not_close_the_replacement_connection(self):
        await self._check_queued_reconnect("disconnect")

    async def test_queued_handoff_does_not_transfer_the_replacement_connection(self):
        await self._check_queued_reconnect("handoff")

    async def test_disconnect_cancelled_before_lock_does_not_close_current_bot(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132")
            bot = player.connected_bot()
            self.addAsyncCleanup(player.disconnect)
            async with player._lifecycle:
                disconnect = asyncio.create_task(player.disconnect())
                await asyncio.sleep(0)
                disconnect.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await disconnect
            self.assertIs(player.connected_bot(), bot)
            self.assertFalse(bot.closed)

    async def test_repeated_asyncio_cancellation_waits_for_owned_disconnect(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132")
            bot = player.connected_bot()
            closing, release = asyncio.Event(), asyncio.Event()
            close = bot.close

            async def gated_close():
                closing.set()
                await release.wait()
                await close()

            bot.close = gated_close
            disconnect = asyncio.create_task(player.disconnect())
            try:
                await asyncio.wait_for(closing.wait(), 1)
                for _ in range(2):
                    disconnect.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(disconnect.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await disconnect
                self.assertTrue(bot.closed)
            finally:
                release.set()
                await asyncio.gather(disconnect, return_exceptions=True)

    async def test_extension_cleanup_failure_still_drains_the_connection(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            for release in ("disconnect", "handoff"):
                with self.subTest(release=release):
                    player = MCPPlayer()
                    calls = []
                    failure = OSError("extension cleanup failed")

                    async def first_cleanup():
                        calls.append("first")
                        if calls == ["first"]:
                            raise failure

                    async def second_cleanup():
                        calls.append("second")

                    player.add_connection_cleanup(first_cleanup)
                    self.assertEqual(await player.disconnect(), {"disconnected": False})
                    self.assertEqual(calls, [])
                    await player.connect("http://localhost:19132")
                    bot = player.connected_bot()
                    self.addAsyncCleanup(bot.close)
                    player.add_connection_cleanup(second_cleanup)
                    await player.start_walk()
                    await player.watch_block(2, 99, 0)
                    reading = asyncio.create_task(player.read_block(2, 99, 0))
                    await asyncio.sleep(0)

                    with self.assertRaises(OSError) as raised:
                        await getattr(player, release)()
                    self.assertIs(raised.exception, failure)
                    self.assertEqual(calls, ["first", "second"])
                    self.assertTrue(reading.cancelled())
                    self.assertTrue(bot.walk_done.is_set())
                    if release == "handoff":
                        self.assertIs(player.connected_bot(), bot)
                        self.assertFalse(bot.closed)
                        self.assertIs(await player.handoff(), bot)
                        self.assertFalse(bot.closed)
                        self.assertEqual(calls, ["first", "second", "first", "second"])
                    else:
                        self.assertTrue(bot.closed)
                    self.assertEqual(await player.disconnect(), {"disconnected": False})
                    self.assertEqual(calls.count("first"), 2 if release == "handoff" else 1)


@unittest.skipUnless(find_spec("mcp") is not None, "optional MCP SDK unavailable")
class MCPStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_disconnect_closes_transport_before_reconnect(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        source = Path(endpilot.__file__).resolve().parents[1]
        script = """
import asyncio
from unittest.mock import patch

from endpilot.mcp_server import MCPPlayer, create_server
from endpilot.protocol import Protocol, write_batch

closing, release, cancelled, reconnecting = (asyncio.Event() for _ in range(4))
protocol = Protocol(2225)
binding = protocol.binding

class Transport:
    def __init__(self, player_id):
        self.player_id = player_id
        self.closed = False
        self.interrupted = False
        self.incoming = asyncio.Queue()
        settings = binding.NetworkSettingsPacket(compression_algorithm=binding.PacketCompressionAlgorithm.NONE)
        start = binding.StartGamePacket(runtime_id=player_id, entity_id=player_id)
        spawn = binding.PlayStatusPacket(status=binding.PlayStatus.PLAYER_SPAWN)
        for batch in ((settings,), (start, spawn)):
            self.incoming.put_nowait(write_batch(protocol.encode(packet) for packet in batch))

    async def send(self, data):
        pass

    async def recv(self):
        return await self.incoming.get()

    async def close(self):
        if self.player_id == 41:
            closing.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                self.interrupted = True
                raise
        self.closed = True

original = Transport(41)

async def dial(url, **kwargs):
    if url.endswith("/first"):
        return original
    if not original.closed:
        raise RuntimeError("new dial started before old transport closed")
    return Transport(42)

class Player(MCPPlayer):
    async def connect(self, url: str, name: str = "EndPilot") -> dict:
        if url.endswith("/second"):
            reconnecting.set()
        return await super().connect(url, name)

    async def disconnect(self):
        try:
            return await super().disconnect()
        except asyncio.CancelledError:
            cancelled.set()
            raise

def register(server, player):
    @server.tool()
    async def close_status() -> dict:
        await closing.wait()
        return {"interrupted": original.interrupted, "closed": original.closed}

    @server.tool()
    async def release_close() -> dict:
        release.set()
        return {"released": True}

    @server.tool()
    async def reconnect_status() -> dict:
        await reconnecting.wait()
        return {"closed": original.closed}

    @server.tool()
    async def wait_cancelled() -> dict:
        await cancelled.wait()
        return {"closed": original.closed}

with patch("endpilot.mcp_server.MCPPlayer", Player), patch("endpilot.transport.nethernet.connect", dial):
    create_server(register).run(transport="stdio")
"""
        params = StdioServerParameters(command=sys.executable, args=["-B", "-c", script],
                                       env={"PYTHONPATH": str(source)})
        async with stdio_client(params) as (incoming, outgoing), ClientSession(
            incoming, outgoing, read_timeout_seconds=5,
        ) as session:
            await session.initialize()

            async def call(tool, **arguments):
                result = await session.call_tool(tool, arguments)
                self.assertFalse(result.is_error, result)
                return result.structured_content or json.loads(result.content[0].text)

            joined = await call("connect", url="http://127.0.0.1/first")
            self.assertEqual(joined["player_id"], 41)
            disconnect = asyncio.create_task(session.call_tool("disconnect"))
            try:
                self.assertEqual(await call("close_status"), {"interrupted": False, "closed": False})
                disconnect.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await disconnect
                self.assertEqual(await call("close_status"), {"interrupted": False, "closed": False})
                async with asyncio.TaskGroup() as tasks:
                    reconnect = tasks.create_task(call("connect", url="http://127.0.0.1/second"))
                    self.assertEqual(await call("reconnect_status"), {"closed": False})
                    await call("release_close")
                self.assertEqual(reconnect.result()["player_id"], 42)
                self.assertEqual(await call("wait_cancelled"), {"closed": True})
            finally:
                await call("release_close")
                if not disconnect.done():
                    await disconnect

    async def test_registered_legacy_tools_and_new_structured_schemas(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        source = Path(endpilot.__file__).resolve().parents[1]
        params = StdioServerParameters(command=sys.executable, args=["-B", "-m", "endpilot.mcp_server"],
                                       env={"PYTHONPATH": str(source)})
        async with stdio_client(params) as (incoming, outgoing):
            async with ClientSession(incoming, outgoing, read_timeout_seconds=5) as session:
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                self.assertEqual(set(tools), {"connect", "watch_block", "read_block", "place_against", "start_walk",
                                              "observe_walk", "stop_walk", "disconnect", "observed_block", "place_block",
                                              "plan_block_interaction", "start_block_interaction", "request_sub_chunk"})
                self.assertEqual(set(tools["observed_block"].output_schema["required"]),
                                 {"position", "layer", "observed", "runtime_id", "is_air"})
                placement = tools["place_block"]
                self.assertEqual(set(placement.input_schema["required"]), {"x", "y", "z", "expected_runtime_id"})
                self.assertEqual(set(placement.output_schema["required"]), {
                    "dimension_id", "target", "slot", "submitted", "reason", "world_change", "inventory_change",
                })
                for name in ("plan_block_interaction", "start_block_interaction", "request_sub_chunk"):
                    self.assertEqual(set(tools[name].input_schema["required"]), {"x", "y", "z"})
                    self.assertIsNotNone(tools[name].output_schema)
