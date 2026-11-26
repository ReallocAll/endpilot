import asyncio
import json
import os
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

if find_spec("mcp") is None:
    raise unittest.SkipTest("optional MCP SDK unavailable")

from mcp.server.mcpserver.exceptions import ToolError

import endpilot
from endpilot.blocks import BlockObservation
from endpilot.collect_and_store import CollectAndStoreResult
from endpilot.mcp_server import MCPPlayer, create_server
from endpilot.range_transfer import RangeTransferResult, RangeTransferStep
from examples.collect_and_store_mcp import _CollectAndStoreTools, register_tools
from test_mcp import FakeBot


TARGET = (4, 100, 2)
CHEST = (2, 100, 2)
ITEM = "minecraft:diamond"
PARTIAL = RangeTransferResult(3, (
    RangeTransferStep(2, 0, 1, 1, 17, "success"),
    RangeTransferStep(2, 1, 2, None, 19, None),
), "cancelled")


class CollectAndStoreLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch("endpilot.mcp_server.Bot", FakeBot))
        self.player = MCPPlayer()
        self.tools = _CollectAndStoreTools(self.player)
        await self.player.connect("http://localhost:19132")
        self.bot = self.player.connected_bot()
        self.addAsyncCleanup(self.player.disconnect)

    async def _start(self):
        return await self.tools.start_collect_and_store([TARGET], ITEM, CHEST)

    async def _install_blocked_task(self):
        entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def collect(bot, positions, **options):
            self.assertIs(bot, self.bot)
            self.assertEqual(positions, [TARGET])
            options["on_progress"](CollectAndStoreResult("storing", picked_up_count=3))
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError as error:
                draining.set()
                await release.wait()
                error.collect_and_store_progress = CollectAndStoreResult(
                    "storing", picked_up_count=3, transfer=PARTIAL, stop_reason="cancelled",
                )
                raise

        self.enterContext(patch("examples.collect_and_store_mcp.collect_and_store", collect))
        self.addCleanup(release.set)
        self.assertEqual(await self._start(), {"started": True})
        await asyncio.wait_for(entered.wait(), 2)
        return draining, release

    async def test_interrupted_stop_leaves_cleanup_and_result_with_same_connection(self):
        draining, release = await self._install_blocked_task()
        stopping = asyncio.create_task(self.tools.stop_collect_and_store())
        try:
            await asyncio.wait_for(draining.wait(), 2)
            with self.assertRaisesRegex(ToolError, "already in progress"):
                await self._start()
            self.assertEqual((await self.tools.observe_collect_and_store())["state"], "stopping")

            stopping.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stopping
            self.assertEqual((await self.tools.observe_collect_and_store())["state"], "stopping")

            retry = asyncio.create_task(self.tools.stop_collect_and_store())
            await asyncio.sleep(0)
            self.assertFalse(retry.done())
        finally:
            release.set()
            await asyncio.gather(stopping, return_exceptions=True)
        settled = await retry
        self.assertEqual(settled, {
            "state": "cancelled", "phase": "storing", "picked_up_count": 3, "stored_count": 1,
            "transfer": PARTIAL, "stop_reason": "cancelled", "error": None,
        })
        self.assertEqual(await self.tools.observe_collect_and_store(), settled)
        self.assertIs(self.player.connected_bot(), self.bot)
        self.assertFalse(self.bot.closed)

    async def _check_connection_cleanup(self, operation):
        draining, release = await self._install_blocked_task()
        cleaning = asyncio.create_task(getattr(self.player, operation)())
        try:
            await asyncio.wait_for(draining.wait(), 2)
            self.assertFalse(cleaning.done())
            self.assertFalse(self.bot.closed)
            with self.assertRaisesRegex(ToolError, "connect a player"):
                await self._start()
        finally:
            release.set()
            result = await cleaning
        if operation == "handoff":
            self.assertIs(result, self.bot)
            self.assertFalse(result.closed)
            await result.close()
        else:
            self.assertEqual(result, {"disconnected": True})
            self.assertTrue(self.bot.closed)
        await self.player.connect("http://localhost:19132")
        with self.assertRaisesRegex(ToolError, "start_collect_and_store"):
            await self.tools.observe_collect_and_store()

    async def test_disconnect_waits_for_sdk_cleanup_before_closing(self):
        await self._check_connection_cleanup("disconnect")

    async def test_interrupted_disconnect_still_drains_before_closing(self):
        draining, release = await self._install_blocked_task()
        disconnecting = asyncio.create_task(self.player.disconnect())
        try:
            await asyncio.wait_for(draining.wait(), 2)
            disconnecting.cancel()
            await asyncio.sleep(0)
            self.assertFalse(disconnecting.done())
            self.assertFalse(self.bot.closed)
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await disconnecting
        self.assertTrue(self.bot.closed)
        await self.player.connect("http://localhost:19132")
        with self.assertRaisesRegex(ToolError, "start_collect_and_store"):
            await self.tools.observe_collect_and_store()

    async def test_handoff_waits_for_sdk_cleanup_and_returns_the_live_bot(self):
        await self._check_connection_cleanup("handoff")


def _serve_scripted_task():
    async def collect(bot, positions, *, item, chest, chest_slots, inventory_slots,
                      ticks, search_radius, timeout, on_progress):
        if positions == [TARGET]:
            assert (item, chest, chest_slots, inventory_slots, ticks, search_radius, timeout) == (
                ITEM, CHEST, range(0, 2), range(2, 3), 0, 1, 3.0,
            )
            on_progress(CollectAndStoreResult("storing", picked_up_count=3))
            try:
                await asyncio.Future()
            except asyncio.CancelledError as error:
                bot.blocks[TARGET, 0] = BlockObservation(TARGET, 42, 0, None)
                error.collect_and_store_progress = CollectAndStoreResult(
                    "storing", picked_up_count=3, transfer=PARTIAL, stop_reason="cancelled",
                )
                raise
        assert positions == [CHEST] and item == 446
        assert bot.blocks[TARGET, 0].runtime_id == 42
        assert chest_slots is None and inventory_slots == range(0, 36)
        transfer = RangeTransferResult(1, (RangeTransferStep(2, 0, 1, 1, 21, "success"),), "complete")
        return CollectAndStoreResult("finished", picked_up_count=1, transfer=transfer, stop_reason="complete")

    with (patch("endpilot.mcp_server.Bot", FakeBot),
          patch("examples.collect_and_store_mcp.collect_and_store", collect)):
        create_server(register_tools).run(transport="stdio")


class CollectAndStoreStdioTests(unittest.IsolatedAsyncioTestCase):
    def session(self, *args):
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        root = Path(__file__).resolve().parents[1]
        source = Path(endpilot.__file__).resolve().parents[1]
        return stdio_client(StdioServerParameters(
            command=sys.executable, args=["-B", *args], cwd=root,
            env={"PYTHONPATH": os.pathsep.join((str(source), str(root / "tests")))},
        ))

    async def test_example_entry_registers_the_named_task_with_the_builtin_tools(self):
        from mcp import ClientSession

        async with self.session("-m", "examples.collect_and_store_mcp") as (incoming, outgoing):
            async with ClientSession(incoming, outgoing, read_timeout_seconds=5) as session:
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                baseline = {tool.name: tool for tool in await create_server().list_tools()}
                names = {"start_collect_and_store", "observe_collect_and_store", "stop_collect_and_store"}
                self.assertEqual(set(tools), set(baseline) | names)
                self.assertEqual({name: tools[name] for name in baseline}, baseline)
                self.assertEqual(set(tools["start_collect_and_store"].input_schema["required"]),
                                 {"positions", "item", "chest"})
                self.assertTrue(all(tools[name].output_schema for name in names))
                error = await session.call_tool("observe_collect_and_store", arguments={})
                self.assertTrue(error.is_error)
                self.assertIn("connect a player", error.content[0].text)

    async def test_start_observe_stop_and_restart_through_stdio_on_one_bot(self):
        from mcp import ClientSession

        source = "from test_collect_and_store_mcp import _serve_scripted_task; _serve_scripted_task()"
        async with self.session("-c", source) as (incoming, outgoing):
            async with ClientSession(incoming, outgoing, read_timeout_seconds=5) as session:
                await session.initialize()

                async def call(name, **kwargs):
                    result = await session.call_tool(name, arguments=kwargs)
                    self.assertFalse(result.is_error, result.content)
                    return result.structured_content or json.loads(result.content[0].text)

                async def observe_phase(phase):
                    async with asyncio.timeout(5):
                        while True:
                            report = await call("observe_collect_and_store")
                            if report["phase"] == phase:
                                return report
                            self.assertEqual(report["state"], "running", report)
                            await asyncio.sleep(0.01)

                self.assertEqual((await call("connect", url="http://localhost:19132"))["player_id"], 17)
                args = dict(positions=[TARGET], item=ITEM, chest=CHEST, chest_slots=(0, 2),
                            inventory_slots=(2, 3), ticks=0, search_radius=1, timeout=3.0)
                self.assertEqual(await call("start_collect_and_store", **args), {"started": True})
                observed = await observe_phase("storing")
                self.assertEqual((observed["state"], observed["phase"], observed["picked_up_count"],
                                  observed["stored_count"]), ("running", "storing", 3, 0))
                duplicate = await session.call_tool("start_collect_and_store", arguments=args)
                self.assertTrue(duplicate.is_error)
                self.assertIn("already in progress", duplicate.content[0].text)

                stopped = await call("stop_collect_and_store")
                self.assertEqual((stopped["state"], stopped["phase"], stopped["stop_reason"],
                                  stopped["picked_up_count"], stopped["stored_count"]),
                                 ("cancelled", "storing", "cancelled", 3, 1))
                self.assertEqual([(step["request_id"], step["confirmed"], step["verdict"])
                                  for step in stopped["transfer"]["steps"]],
                                 [(17, 1, "success"), (19, None, None)])
                self.assertEqual(await call("observe_collect_and_store"), stopped)
                x, y, z = TARGET
                self.assertEqual((await call("observed_block", x=x, y=y, z=z))["runtime_id"], 42)

                await call("start_collect_and_store", positions=[CHEST], item=446, chest=CHEST)
                finished = await observe_phase("finished")
                self.assertEqual((finished["state"], finished["phase"], finished["stop_reason"],
                                  finished["picked_up_count"], finished["stored_count"]),
                                 ("finished", "finished", "complete", 1, 1))
                self.assertEqual(await call("stop_collect_and_store"), finished)
                self.assertEqual(await call("disconnect"), {"disconnected": True})
