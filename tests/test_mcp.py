import asyncio
from dataclasses import replace
from fractions import Fraction
from importlib.util import find_spec
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

import endpilot
from endpilot.blocks import BlockChange, BlockObservation, BlockUpdate
from endpilot.inventory import HeldItemStack
from endpilot.login import SessionEnd, _SessionEndState
from endpilot.mcp_server import MCPPlayer
from endpilot.navigation import BlockInteractionGoal, BlockInteractionMatch, NavigationPlan, NavigationResult, NavigationRules
from endpilot.placement import PlacementInventoryChange, PlacementResult
from test_placement import PLACED_ID, SUPPORT, TARGET, _PlacementHarness


# Stock 1.26.65.1, /list with sendCommandFeedback=false and the UUIDv8 SDK origin.
_SILENT_REQUEST = (
    "052f6c69737406706c617965726a89a525e882d23801000000000000802066613332323538396263303334373133"
    "6263316165366330613539313439313107000000ffffffff00066c6174657374"
)
_SILENT_RESPONSE = (
    "06706c617965726a89a525e882d23801000000000000800000000000000000000673696c656e74010000000215"
    "636f6d6d616e64732e706c61796572732e6c6973740102013101341b636f6d6d616e64732e706c61796572732e"
    "6c6973742e6e616d657301010a5265616c6c6f63416c6c00"
)


class FakeBot:
    def __init__(self, name, *, chat_languages=None):
        self.player_id = 17
        self.chat_languages = chat_languages
        self._session_end = _SessionEndState()
        self.updated = asyncio.Event()
        self.closed = False
        self.placed = []
        self.position = (0.5, 101.62, 0.5)
        self.walk_started = asyncio.Event()
        self.walk_stopped = asyncio.Event()
        self.walk_done = asyncio.Event()
        self.blocks = {}

    async def join_nethernet(self, url, *, locale="en_US"):
        self.url = url
        self.locale = locale

    async def join_raknet(self, host, port, *, locale="en_US"):
        self.url = (host, port)
        self.locale = locale

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
        self._session_end.finish(SessionEnd("local_close"))

    @property
    def session_end(self):
        return self._session_end.result

    async def wait_for_session_end(self, timeout=15):
        return await self._session_end.wait(timeout)


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
        authenticated_url = "https://fixture:secret@localhost:443/lobby/%2Fbedrock?profile=fixture#section"
        ipv6_url = "http://[::1]:19132/signaling"
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            for address, endpoint in (
                ("raknet://[::1]", ("::1", 19132)),
                ("raknet://[::1]:19140", ("::1", 19140)),
                ("raknet://127.0.0.1", ("127.0.0.1", 19132)),
                ("http://localhost:19132/signaling", "http://localhost:19132/signaling"),
                (authenticated_url, authenticated_url),
                (ipv6_url, ipv6_url),
            ):
                with self.subTest(address=address):
                    await player.connect(address)
                    try:
                        self.assertEqual(player.bot.url, endpoint)
                        self.assertEqual(player.bot.locale, "en_US")
                    finally:
                        await player.disconnect()

    async def test_connect_rejects_invalid_or_missing_explicit_address(self):
        with patch("endpilot.mcp_server.Bot", wraps=FakeBot) as bot_factory:
            for address in (
                "http://:19132/signaling",
                "http://bad host.invalid:19132/signaling",
                "http://bad\x01host.invalid:19132/signaling",
                "http://bad\thost.invalid:19132/signaling",
                "http://bad\nhost.invalid:19132/signaling",
                "http://bad\rhost.invalid:19132/signaling",
                "https://localhost:65536/signaling",
                "http://localhost:0/signaling",
                "localhost:19132",
                "ftp://localhost:19132",
            ):
                with self.subTest(address=address):
                    player = MCPPlayer()
                    with self.assertRaises(ValueError):
                        await player.connect(address)
                    self.assertIsNone(player.bot)
            bot_factory.assert_not_called()

    async def test_connect_keeps_locale_and_host_language_bytes_for_replacement(self):
        language = b"commands.players.list.names=%s\n"
        config = {"zh_CN": language}
        player = MCPPlayer(chat_languages=config)
        config.clear()
        self.addAsyncCleanup(player.disconnect)
        with patch("endpilot.mcp_server.Bot", wraps=FakeBot) as bot_factory:
            with self.assertRaisesRegex(ValueError, "locale"):
                await player.connect("http://localhost:19132", locale="../zh_CN")
            bot_factory.assert_not_called()
            await player.connect("http://localhost:19132", locale="zh_CN")
            self.assertEqual(player.bot.locale, "zh_CN")
            self.assertEqual(player.bot.chat_languages, {"zh_CN": language})
            await player.reconnect()
            self.assertEqual(player.bot.locale, "zh_CN")
            await player.switch_account(name="OtherPlayer")
            self.assertEqual(player.bot.locale, "zh_CN")
            self.assertEqual(player.bot.chat_languages, {"zh_CN": language})
            self.assertEqual(bot_factory.call_count, 3)

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
            player.bot.walk_done.clear()
            await player.start_walk(2)
            await asyncio.wait_for(player.bot.walk_done.wait(), timeout=1)
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

    async def test_handoff_drains_legacy_place_and_section_request_without_serializing(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132")
            bot = player.connected_bot()
            self.addAsyncCleanup(bot.close)
            await player.watch_block(2, 99, 0)
            bot.updated.set()
            await player.read_block(2, 99, 0)
            place_started, section_started = asyncio.Event(), asyncio.Event()
            place_release, section_release = asyncio.Event(), asyncio.Event()

            async def place(*args, **kwargs):
                place_started.set()
                await place_release.wait()
                return 123

            async def request(position):
                section_started.set()
                await section_release.wait()

            bot.place_against, bot.request_sub_chunk = place, request
            placed = asyncio.create_task(player.place_against(2, 99, 0))
            requested = asyncio.create_task(player.request_sub_chunk(2, 99, 0))
            tasks = [placed, requested]
            try:
                async with asyncio.timeout(2):
                    await place_started.wait()
                    await section_started.wait()
                    handoff = asyncio.create_task(player.handoff())
                    tasks.append(handoff)
                    await asyncio.sleep(0)
                    self.assertFalse(handoff.done())
                    place_release.set()
                    self.assertEqual((await placed)["submitted_tick"], 123)
                    self.assertFalse(handoff.done())
                    section_release.set()
                    self.assertTrue((await requested)["requested"])
                    self.assertIs(await handoff, bot)
            finally:
                place_release.set()
                section_release.set()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_disconnect_does_not_carry_old_actions_to_the_next_handoff(self):
        with patch("endpilot.mcp_server.Bot", FakeBot):
            player = MCPPlayer()
            await player.connect("http://localhost:19132", "old")
            old = player.connected_bot()
            started, release = asyncio.Event(), asyncio.Event()

            async def request(position):
                started.set()
                await release.wait()

            old.request_sub_chunk = request
            pending = asyncio.create_task(player.request_sub_chunk(2, 99, 0))
            try:
                async with asyncio.timeout(2):
                    await started.wait()
                    self.assertTrue((await player.disconnect())["disconnected"])
                    self.assertTrue(old.closed)
                    self.assertFalse(pending.done())
                    await player.connect("http://localhost:19132", "new")
                    new = player.connected_bot()
                    self.addAsyncCleanup(new.close)
                    self.assertIsNot(new, old)
                    self.assertIs(await player.handoff(), new)
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)

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


class MCPPlacementHandoffTests(_PlacementHarness):
    async def test_cancelled_handoff_keeps_owner_and_successful_handoff_waits_for_placement(self):
        controller = MCPPlayer()
        with patch("endpilot.mcp_server.Bot", return_value=self.bot), patch.object(
            self.bot, "join_nethernet", new=AsyncMock(),
        ):
            self.bot._session = Mock(player_id=17)
            self.bot._session_end_state = _SessionEndState()
            await controller.connect("http://localhost:19132")

        started, release, sent = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def sync(*, timeout):
            started.set()
            await release.wait()
            return self.movement.input_position

        self.movement.wait_for_position_sync.side_effect = sync
        stop = self.packets.subscribe_sent(lambda packet_id: sent.set())
        tasks = [asyncio.create_task(controller.place_block(*SUPPORT, expected_runtime_id=PLACED_ID))]
        placement = tasks[0]
        try:
            async with asyncio.timeout(2):
                await started.wait()
                self.assertEqual(self.wire.sent, [])
                first = asyncio.create_task(controller.handoff())
                tasks.append(first)
                await asyncio.sleep(0)
                self.assertFalse(first.done())
                with self.assertRaisesRegex(RuntimeError, "connect a player first"):
                    controller.connected_bot()
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                self.assertIs(controller.connected_bot(), self.bot)
                self.assertFalse(placement.done())

                handoff = asyncio.create_task(controller.handoff())
                tasks.append(handoff)
                await asyncio.sleep(0)
                self.assertFalse(handoff.done())
                release.set()
                await sent.wait()
                click, = self.wire.sent
                self.assertEqual(click.variant_transaction.action_type,
                                 self.b.ItemUseInventoryTransaction.ActionType.PLACE)
                self.assertFalse(handoff.done())  # The click was sent, but its effects are not yet observed.
                await self.deliver(self.block(TARGET, PLACED_ID), self.slot(3))
                report = await placement
                self.assertEqual((report["reason"], report["submitted"]), ("observations_complete", True))
                self.assertIs(await handoff, self.bot)
                with self.assertRaisesRegex(RuntimeError, "connect a player first"):
                    await controller.place_block(*SUPPORT, expected_runtime_id=PLACED_ID)
                self.assertEqual(self.wire.sent, [click])
        finally:
            release.set()
            stop()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@unittest.skipUnless(find_spec("mcp") is not None, "optional MCP SDK unavailable")
class MCPStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_item_projection_preserves_worker_and_json_contracts(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        source = Path(endpilot.__file__).resolve().parents[1]
        peer = """
import asyncio
from unittest.mock import patch

from endpilot.inventory import HeldItemStack
from endpilot.mcp_server import create_server
from endpilot.placement import PlacementInventoryChange, PlacementResult
from endpilot.protocol import Protocol, write_batch

protocol = Protocol(2225)
b = protocol.binding
data = bytes.fromhex('00ffc3a9')

def stack(item_id, count=1, data=b''):
    return b.SerializedNetworkItemStackDescriptor(id=item_id, stack_size=count, user_data_buffer=data)

class Transport:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.deliver(b.StartGamePacket(runtime_id=7, entity_id=27),
                     b.InventoryContentPacket(inventory_id=b.ContainerID.INVENTORY, slots=[
                         stack(-321, 2, data), b.SerializedNetworkItemStackDescriptor(), stack(412), stack(261)]),
                     b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))

    def deliver(self, *packets):
        self.incoming.put_nowait(write_batch(map(protocol.encode, packets)))

    async def recv(self):
        return await self.incoming.get()

    async def send(self, data):
        pass

    async def close(self):
        pass

wire = Transport()

def register(server, player):
    @server.tool()
    async def fixture_install():
        bot = player.connected_bot()
        bot.nearest_entity = lambda: {'item_id': -321, 'user_data_buffer': data}

        async def place_block(position, **options):
            before = HeldItemStack(-321, 4, 0, 41, 42, data)
            after = HeldItemStack(0, 0, 0, None, 0, b'')
            return PlacementResult(0, position, 0, True, 'observations_complete', None,
                                   PlacementInventoryChange(before, after))

        bot.place_block = place_block

    @server.tool()
    async def fixture_inventory(phase: int):
        bot = player.connected_bot()
        labels = ('test:old', 'test:new')
        items = b.ItemRegistryPacket(items=[b.ItemData(id=-321, name=labels[phase - 1]),
                                           b.ItemData(id=261, name='minecraft:bread')])
        slot = b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=0, item=stack(-321, 2 + phase, data))
        packets = [items]
        if phase == 1:
            packets.append(b.CraftingDataPacket(shapeless_recipes=[b.ShapelessRecipePayload(
                recipe_id='test:projection', net_id=8, tag='crafting_table',
                ingredients=[b.SerializedRecipeIngredient(descriptor={'name': 'minecraft:bread'}, stack_size=1)],
                results=[b.SerializedNetworkItemInstanceDescriptor(id=-321, stack_size=1, user_data_buffer=data)],
            )]))
        else:
            original_recipes = bot.recipes_all
            retained = original_recipes('test:old')

            def recipes_all(item, *args, **options):
                return retained if item == 'test:retained_recipe' else original_recipes(item, *args, **options)

            bot.recipes_all = recipes_all
        wait = asyncio.create_task(bot.packets.wait_for_packet(b.InventorySlotPacket.ID, timeout=5))
        await asyncio.sleep(0)
        wire.deliver(*packets, slot)
        await wait
        return bot.item_registry.name_for_id(-321)

with patch('endpilot.transport.nethernet.connect', return_value=wire):
    create_server(register).run(transport='stdio')
"""
        script = """
import asyncio

def inspect_buffer(item, expected):
    assert isinstance(item.user_data_buffer, str) and item.user_data_buffer == expected.hex()
    assert item.user_data_hex == expected.hex()
    assert type(item.user_data_bytes) is bytes and item.user_data_bytes == expected

async def main(bot):
    expected = bytes.fromhex('00ffc3a9')
    slots = bot.inventory_slots
    no_registry = bot.held_item.observed_item
    assert no_registry.item_id == -321 and no_registry.item_name is None
    assert slots.get(9) is None and bot.offhand_item is None
    assert slots[1].item_id == 0 and slots[1].count == 0 and slots[1].item_name is None
    inspect_buffer(slots[1], b'')
    inspect_buffer(no_registry, expected)
    _, pending = bot._channel.start('get', 'held_item', (), {})
    fields = pending.result(timeout=5)['value']['fields']['observed_item']['fields']
    assert {key for key in fields if key.startswith('user_data_')} == {'user_data_buffer'}
    assert fields['user_data_buffer'] == expected.hex()
    try:
        no_registry.user_data_bytes = b'x'
    except AttributeError:
        pass
    else:
        raise AssertionError('mutable bytes view')
    mapping = bot.nearest_entity()
    assert type(mapping) is dict and mapping == {'item_id': -321, 'user_data_buffer': expected.hex()}

    queue = asyncio.Queue()
    unsubscribe = bot.subscribe_inventory_changes(queue.put_nowait, slot=0)
    try:
        print('observing')
        old = await queue.get()
        assert old.after.item_name == 'test:old' and old.after.item_id == -321
        inspect_buffer(old.after, expected)
        print('first registry observed')
        updated_event = await queue.get()
        current = bot.held_item.observed_item
        assert bot.inventory_slots[2].item_id == 412 and bot.inventory_slots[2].item_name is None
        assert bot.inventory_slots[3].item_id == 261 and bot.inventory_slots[3].item_name == 'minecraft:bread'
        assert current.item_name == updated_event.after.item_name == 'test:new'
        assert no_registry.item_name is None and old.after.item_name == 'test:old'
        recipe, = bot.recipes_all('test:retained_recipe')
        result, = recipe.results
        assert result.name == 'test:old' and result.item_id == -321 and result.item_name == 'test:new'
        inspect_buffer(result, expected)
        placement = await bot.place_block((2, 100, 0), expected_runtime_id=42)
        assert placement.inventory_change.before.item_name == 'test:new'
        inspect_buffer(placement.inventory_change.before, expected)
        inspect_buffer(placement.inventory_change.after, b'')
        return {'held': current, 'event': old, 'result': result, 'bytes_as_hex': current.user_data_bytes.hex()}
    finally:
        unsubscribe()
"""
        params = StdioServerParameters(command=sys.executable, args=["-B", "-c", peer], env={"PYTHONPATH": str(source)})
        async with stdio_client(params) as (incoming, outgoing), ClientSession(
            incoming, outgoing, read_timeout_seconds=10,
        ) as session:
            await session.initialize()

            async def call(tool, **arguments):
                response = await session.call_tool(tool, arguments)
                self.assertFalse(response.is_error, response)
                return response.structured_content

            async def observe(run_id, marker=None):
                async with asyncio.timeout(10):
                    while True:
                        report = await call("observe_script", run_id=run_id)
                        if report["state"] != "running" or (marker is not None and any(
                            marker in entry["text"] for entry in report["output"]
                        )):
                            return report
                        await asyncio.sleep(.01)

            await call("connect", url="http://127.0.0.1")
            await call("fixture_install")
            run_id = (await call("run_script", source=script))["run_id"]
            observing = await observe(run_id, "observing")
            self.assertEqual(observing["state"], "running", observing)
            await call("fixture_inventory", phase=1)
            first = await observe(run_id, "first registry observed")
            self.assertEqual(first["state"], "running", first)
            await call("fixture_inventory", phase=2)
            final = await observe(run_id)
            self.assertEqual(final["state"], "succeeded", final)
            result = final["result"]
            self.assertEqual(result["bytes_as_hex"], "00ffc3a9")
            self.assertEqual(result["held"], {"item_id": -321, "item_name": "test:new", "count": 4, "aux_value": 0,
                                             "net_id": None, "block_runtime_id": 0, "user_data_buffer": "00ffc3a9"})
            self.assertEqual(result["event"]["after"]["item_name"], "test:old")
            self.assertEqual((result["result"]["name"], result["result"]["item_name"]), ("test:old", "test:new"))

            binary_return = (await call("run_script", source="""
async def main(bot):
    return bot.held_item.observed_item.user_data_bytes
"""))["run_id"]
            rejected = await observe(binary_return)
            self.assertEqual(rejected["state"], "failed", rejected)
            self.assertEqual(rejected["error"]["error_type"], "TypeError")
            self.assertIn("bytes is not JSON serializable", rejected["error"]["message"])

            built_in = await call("place_block", x=2, y=100, z=0, expected_runtime_id=42)
            self.assertEqual(built_in, {"dimension_id": 0, "target": [2, 100, 0], "slot": 0, "submitted": True,
                                        "reason": "observations_complete", "world_change": None, "inventory_change": {
                "before": {"item_id": -321, "count": 4, "aux_value": 0, "net_id": 41,
                           "block_runtime_id": 42, "user_data_hex": "00ffc3a9"},
                "after": {"item_id": 0, "count": 0, "aux_value": 0, "net_id": None,
                          "block_runtime_id": 0, "user_data_hex": ""},
            }})
            await call("disconnect")

    async def test_host_chat_language_reaches_worker_through_stdio_connect(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        source = Path(endpilot.__file__).resolve().parents[1]
        peer = """
import asyncio
import runpy
import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from endpilot import commands
from endpilot.protocol import Protocol, RawPacket, read_batch, write_batch

protocol = Protocol(2225)
b = protocol.binding
request = RawPacket(b.CommandRequestPacket.ID, bytes.fromhex(sys.argv.pop(1)))
silent = RawPacket(b.CommandOutputPacket.ID, bytes.fromhex(sys.argv.pop(1)))
origin = protocol.decode(request).origin
# A compound /tellraw TEXT_OBJECT_WHISPER from the stock resource-pack test.
compound = RawPacket(b.TextPacket.ID, bytes.fromhex(
    '0000099b017b2272617774657874223a5b7b2274657874223a22455054582d73657175656e7469616c7c227d2c'
    '7b227472616e736c617465223a22656e6470696c6f742e7061636b746578742e73657175656e6365222c227769'
    '7468223a7b2272617774657874223a5b7b2274657874223a22416c696365227d2c7b2274657874223a22736e'
    '6f77227d5d7d7d2c7b2274657874223a227c656e64227d5d7d0a000000'
))

class Transport:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.deliver(b.StartGamePacket(runtime_id=41, entity_id=origin.player_id),
                     b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))

    def deliver(self, *packets):
        self.incoming.put_nowait(write_batch(packet if isinstance(packet, RawPacket) else protocol.encode(packet)
                                            for packet in packets))

    async def send(self, data):
        for raw in read_batch(data):
            if raw.packet_id == b.CommandRequestPacket.ID:
                assert raw == request
                self.deliver(
                    silent,
                    compound,
                    b.TextPacket(body=b.TextPacket.AuthorAndMessage(type=b.TextPacketType.CHAT,
                                                                    author='Player', message='barrier')),
                )

    async def recv(self):
        return await self.incoming.get()

    async def close(self):
        pass

async def dial(url, **kwargs):
    return Transport()

with (patch('endpilot.transport.nethernet.connect', dial),
      patch.object(commands, 'secrets', SimpleNamespace(randbits=lambda _: origin.uuid.int >> 64)),
      patch.object(commands, 'uuid', SimpleNamespace(UUID=UUID, uuid4=lambda: UUID(hex=origin.request_id)))):
    runpy.run_module('endpilot.mcp_server', run_name='__main__')
"""
        worker = """
import asyncio

async def main(bot):
    rows = []
    barrier = asyncio.Event()
    def record(event):
        rows.append([event.message, event.kind, event.translation_key, event.params, event.raw_message, event.successful])
        if event.message == 'barrier':
            barrier.set()
    unsubscribe = bot.subscribe_chat(record)
    try:
        reply = await bot.execute_command_output('/list', timeout=5)
        assert reply.output.type == 'SILENT'
        await asyncio.wait_for(barrier.wait(), 5)
        return rows
    finally:
        unsubscribe()
"""
        with TemporaryDirectory() as folder:
            language = Path(folder) / "local.zh_CN.lang"
            language.write_text("commands.players.list=目前有 %1$d/%2$d 个玩家在线：\n"
                                "commands.players.list.names=%s\n"
                                "endpilot.packtext.sequence=%s 获得 %s\n", encoding="utf-8")
            raw_message = (
                '{"rawtext":[{"text":"EPTX-sequential|"},{"translate":"endpilot.packtext.sequence",'
                '"with":{"rawtext":[{"text":"Alice"},{"text":"snow"}]}},{"text":"|end"}]}\n'
            )
            players = "ReallocAll"
            for configured, message in ((False, None), (True, "目前有 1/4 个玩家在线：")):
                with self.subTest(configured=configured):
                    args = ["-B", "-c", peer, _SILENT_REQUEST, _SILENT_RESPONSE]
                    if configured:
                        args.extend(("--chat-language", "zh_CN", str(language)))
                    params = StdioServerParameters(command=sys.executable, args=args,
                                                   env={"PYTHONPATH": str(source)})
                    async with stdio_client(params) as (incoming, outgoing), ClientSession(
                        incoming, outgoing, read_timeout_seconds=10,
                    ) as session:
                        await session.initialize()

                        async def call(tool, **arguments):
                            result = await session.call_tool(tool, arguments)
                            self.assertFalse(result.is_error, result)
                            return result.structured_content or json.loads(result.content[0].text)

                        await call("connect", url="http://127.0.0.1", locale="zh_CN")
                        run_id = (await call("run_script", source=worker))["run_id"]
                        async with asyncio.timeout(10):
                            while (report := await call("observe_script", run_id=run_id))["state"] == "running":
                                await asyncio.sleep(0.01)
                        self.assertEqual(report["state"], "succeeded", report)
                        self.assertEqual(report["result"], [
                            [message, "command", "commands.players.list", ["1", "4"], None, True],
                            [players if configured else None, "command", "commands.players.list.names", [players],
                             None, True],
                            ["EPTX-sequential|Alice 获得 snow|end" if configured else None,
                             "whisper", None, None, raw_message, None],
                            ["barrier", "chat", None, None, None, None],
                        ])
                        self.assertEqual(await call("shutdown"), {"shutdown": True})

    async def test_resident_chat_observes_late_command_replies_across_worker_lifetimes(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        source = Path(endpilot.__file__).resolve().parents[1]
        peer = """
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import patch

from endpilot import commands
from endpilot.mcp_server import create_server
from endpilot.protocol import Protocol, RawPacket, read_batch, write_batch

protocol = Protocol(2225)
b = protocol.binding
silent = RawPacket(b.CommandOutputPacket.ID, bytes.fromhex(sys.argv[1]))
captured = protocol.decode(silent)
prefix = captured.origin_data.uuid.int >> 64
prefixes = iter((prefix, prefix ^ 1))
connections = []
requests = asyncio.Queue()
submitted = []

class Transport:
    def __init__(self, player_id):
        self.incoming = asyncio.Queue()
        self.deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.deliver(b.StartGamePacket(runtime_id=player_id, entity_id=player_id),
                     b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))

    def deliver(self, *packets):
        self.incoming.put_nowait(write_batch(packet if isinstance(packet, RawPacket) else protocol.encode(packet)
                                            for packet in packets))

    async def send(self, batch):
        for raw in read_batch(batch):
            if raw.packet_id == b.CommandRequestPacket.ID:
                requests.put_nowait(protocol.decode(raw))

    async def recv(self):
        return await self.incoming.get()

    async def close(self):
        pass

async def dial(url, **kwargs):
    connection = Transport(41 + len(connections))
    connections.append(connection)
    return connection

def marker(label):
    return b.TextPacket(body=b.TextPacket.AuthorAndMessage(type=b.TextPacketType.CHAT,
                                                           author='Fixture', message=label))

def response(request, output):
    return b.CommandOutputPacket(origin_data=b.CommandOriginData(type=b.CommandOriginType.PLAYER,
                                                                 uuid=request.origin.uuid), output=output)

def register(server, player):
    @server.tool()
    async def next_request() -> dict:
        request = await asyncio.wait_for(requests.get(), 5)
        submitted.append(request)
        return {'command': request.command, 'uuid': str(request.origin.uuid), 'version': request.origin.uuid.version}

    @server.tool()
    async def cancel_marker() -> None:
        connections[-1].deliver(marker('cancel requester'))

    @server.tool()
    async def release_reply(index: int) -> None:
        request = submitted[index]
        connection = connections[-1]
        if index == 0:
            assert request.origin.uuid == captured.origin_data.uuid
            connection.deliver(silent)
        elif index == 1:
            failure = b.CommandOutputMessage(message_id='commands.fixture.missing_translation', successful=False,
                                             params=['untranslated'])
            connection.deliver(response(request, b.CommandOutput(type=b.CommandOutputType.ALL_OUTPUT,
                                                                 success_count=0, messages=[failure])), marker('done'))
        else:
            assert len(connections) == 2 and request.origin.uuid.int >> 64 != prefix
            fresh = b.CommandOutputMessage(message_id='commands.players.list.names', successful=True,
                                           params=['NewResident'])
            connection.deliver(silent, response(request, b.CommandOutput(type=b.CommandOutputType.SILENT,
                                                                         success_count=1, messages=[fresh])), marker('done'))

language = b'commands.players.list=There are %1$d/%2$d players online:\\ncommands.players.list.names=%s\\n'
with (patch('endpilot.transport.nethernet.connect', dial),
      patch.object(commands, 'secrets', SimpleNamespace(randbits=lambda _: next(prefixes)))):
    create_server(register, chat_languages={'en_US': language}).run(transport='stdio')
"""
        subscriber = """
import asyncio

async def main(bot):
    done = asyncio.Event()
    rows = []
    def record(event):
        if event.kind == 'command':
            rows.append([event.translation_key, event.params, event.message, event.successful])
        elif event.message == 'done':
            done.set()
    unsubscribe = bot.subscribe_chat(record)
    try:
        print('subscribed')
        await done.wait()
        return [bot.player_id, rows]
    finally:
        unsubscribe()
"""
        cancellable = """
import asyncio

async def main(bot):
    cancel = asyncio.Event()
    def record(event):
        if event.message == 'cancel requester':
            cancel.set()
    unsubscribe = bot.subscribe_chat(record)
    try:
        request = asyncio.create_task(bot.execute_command_output('/list', timeout=15))
        await cancel.wait()
        request.cancel('requester cancelled')
        try:
            await request
        except asyncio.CancelledError as error:
            return str(error)
        raise AssertionError('command wait was not cancelled')
    finally:
        unsubscribe()
"""
        await_command = """
async def main(bot, command):
    reply = await bot.execute_command_output(command, timeout=15)
    return reply.origin_data.uuid
"""
        params = StdioServerParameters(command=sys.executable, args=["-B", "-c", peer, _SILENT_RESPONSE],
                                       env={"PYTHONPATH": str(source)})
        async with stdio_client(params) as (incoming, outgoing), ClientSession(
            incoming, outgoing, read_timeout_seconds=10,
        ) as session:
            await session.initialize()

            async def call(tool, **arguments):
                result = await session.call_tool(tool, arguments)
                self.assertFalse(result.is_error, result)
                if result.structured_content is not None:
                    return result.structured_content
                if result.content:
                    return json.loads(result.content[0].text)

            async def start(script, *args):
                return (await call("run_script", source=script, args=list(args)))["run_id"]

            async def observe(run_id, predicate=lambda _: False):
                async with asyncio.timeout(10):
                    while True:
                        report = await call("observe_script", run_id=run_id)
                        if predicate(report) or report["state"] != "running":
                            return report
                        await asyncio.sleep(0.01)

            async def watch():
                run = await start(subscriber)
                ready = await observe(run, lambda report: any("subscribed" in chunk["text"] for chunk in report["output"]))
                self.assertEqual(ready["state"], "running", ready)
                return run

            await call("connect", url="http://127.0.0.1")
            listener = await watch()
            requester = await start(cancellable)
            cancelled_origin = await call("next_request")
            self.assertEqual((cancelled_origin["command"], cancelled_origin["version"]), ("/list", 8))
            await call("cancel_marker")
            cancelled = await observe(requester)
            self.assertEqual((cancelled["state"], cancelled["result"]), ("succeeded", "requester cancelled"))
            await call("release_reply", index=0)

            requester = await start(await_command, "/ep_unknown")
            stopping_origin = await call("next_request")
            self.assertEqual((stopping_origin["command"], stopping_origin["version"]), ("/ep_unknown", 8))
            self.assertEqual((await call("stop_script", run_id=requester))["state"], "stopped")
            await call("release_reply", index=1)
            original = await observe(listener)
            expected = [["commands.players.list", ["1", "4"], "There are 1/4 players online:", True],
                        ["commands.players.list.names", ["ReallocAll"], "ReallocAll", True]]
            self.assertEqual(original["state"], "succeeded", original)
            self.assertEqual(original["result"], [41, [*expected,
                ["commands.fixture.missing_translation", ["untranslated"], None, False],
            ]])
            self.assertEqual((await call("session_status"))["player_id"], 41)

            self.assertEqual((await call("reconnect"))["player_id"], 42)
            listener = await watch()
            requester = await start(await_command, "/list")
            fresh = await call("next_request")
            self.assertEqual(fresh["version"], 8)
            self.assertNotEqual(fresh["uuid"], cancelled_origin["uuid"])
            await call("release_reply", index=2)
            current = await observe(listener)
            self.assertEqual(current["state"], "succeeded", current)
            self.assertEqual(current["result"], [42, [
                ["commands.players.list.names", ["NewResident"], "NewResident", True],
            ]])
            matched = await observe(requester)
            self.assertEqual((matched["state"], matched["result"]), ("succeeded", fresh["uuid"]))
            self.assertEqual(await call("disconnect"), {"disconnected": True})

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
    async def connect(self, url: str, name: str = "EndPilot", *args, **kwargs) -> dict:
        if url.endswith("/second"):
            reconnecting.set()
        return await super().connect(url, name, *args, **kwargs)

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

        from endpilot.mcp_server import create_server

        source = Path(endpilot.__file__).resolve().parents[1]
        params = StdioServerParameters(command=sys.executable, args=["-B", "-m", "endpilot.mcp_server"],
                                       env={"PYTHONPATH": str(source)})
        async with stdio_client(params) as (incoming, outgoing):
            async with ClientSession(incoming, outgoing, read_timeout_seconds=5) as session:
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                self.assertEqual(set(tools), {"connect", "session_status", "reconnect", "switch_account",
                                              "watch_block", "read_block", "place_against", "start_walk",
                                              "observe_walk", "stop_walk", "disconnect", "observed_block", "place_block",
                                              "plan_block_interaction", "start_block_interaction", "request_sub_chunk",
                                              "account_list", "account_add", "account_forget", "account_skin_set",
                                              "account_skin_clear", "account_authorization_status",
                                              "account_authorization_cancel", "run_script", "observe_script",
                                              "stop_script", "shutdown"})
                self.assertEqual({tool.name for tool in await create_server().list_tools()}, set(tools) - {"shutdown"})
                self.assertIs(tools["shutdown"].output_schema["properties"]["shutdown"]["const"], True)
                connect = tools["connect"].input_schema
                self.assertEqual(set(connect["required"]), {"url"})
                self.assertEqual(set(connect["properties"]), {"url", "name", "xuid", "accept_resource_packs", "locale"})
                self.assertEqual(connect["properties"]["locale"]["default"], "en_US")
                self.assertEqual(connect["properties"]["accept_resource_packs"]["type"], "boolean")
                self.assertIs(connect["properties"]["accept_resource_packs"]["default"], False)
                rejected = await session.call_tool("connect", {"url": "invalid", "accept_resource_packs": "true"})
                self.assertTrue(rejected.is_error)
                self.assertIn("accept_resource_packs", rejected.content[0].text)
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
