import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import PropertyMock, patch

from endpilot import Bot
from endpilot._mcp_scripts import MCPScripts, _MAX_FRAME
from endpilot.beds import SleepRejected
from endpilot.blocks import MiningFaceUnavailable
from endpilot.chat import ChatMessage
from endpilot.collection import BlockCollection, BlockCollectionBatch
from endpilot.entities import EntityState
from endpilot.world_environment import WorldClock, WorldTime
import test_placement
import test_scheduled_input


class MCPScriptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("ScriptFixture")
        self.addAsyncCleanup(self.bot.close)
        self.wire = await test_scheduled_input.ScheduledInputTests().join(self.bot, position=(0.5, 101.62, 0.5))
        self.scripts = MCPScripts(SimpleNamespace(connected_bot=lambda: self.bot))
        self.addAsyncCleanup(self.scripts.close)

    async def start(self, source=None, **kwargs):
        return (await self.scripts.run_script(source=source, **kwargs))["run_id"]

    async def wait_for(self, run, predicate):
        async with asyncio.timeout(7):
            while True:
                status = await self.scripts.observe_script(run)
                if predicate(status):
                    return status
                if status["state"] != "running":
                    self.fail(status)
                await asyncio.sleep(0.005)

    async def completed(self, run):
        return await self.wait_for(run, lambda status: status["state"] != "running")

    @staticmethod
    def text(status, stream="stdout"):
        return "".join(chunk["text"] for chunk in status["output"] if chunk["stream"] == stream)

    async def update_block(self, position, runtime_id, *, layer=0):
        b = self.bot.protocol.binding
        packet = b.UpdateBlockPacket(pos=b.BlockPos(x=position[0], y=position[1], z=position[2]),
                                     runtime_id=runtime_id, layer=layer)
        await test_scheduled_input.ScheduledInputTests().observe(self.bot, self.wire, packet)

    async def test_queries_actions_and_streams_use_the_resident_connection(self):
        await self.update_block((1, 100, 2), 42)
        script = """
import logging
import os
import sys

async def main(bot, expected_host_pid, label):
    assert os.getpid() != expected_host_pid
    assert bot.player_id == 1
    block = bot.observed_block((1, 100, 2))
    assert block.runtime_id == 42
    print('first', label)
    logging.warning('script log')
    sys.stderr.write('script stderr')
    await bot.wait_ticks(4)
    await bot.send_chat(label)
    print('second', label)
    return {'player_id': bot.player_id, 'block': block.runtime_id, 'position': block.position}
"""
        run = await self.start(script, args=[os.getpid(), "fixture chat"])
        initial = await self.scripts.observe_script(run)
        self.assertEqual(initial["output"], [])
        observed = await self.wait_for(run, lambda status: bool(self.text(status, "stderr")))
        self.assertEqual(observed["state"], "running")
        self.assertEqual(self.text(observed), "first fixture chat\n")
        self.assertIn("script log", self.text(observed, "log"))
        self.assertEqual(self.text(observed, "stderr"), "script stderr")
        await self.completed(run)
        final = await self.scripts.observe_script(run, observed["cursor"])
        self.assertEqual(final["state"], "succeeded", final)
        self.assertEqual(final["result"], {"player_id": 1, "block": 42, "position": [1, 100, 2]})
        self.assertEqual(self.text(final), "second fixture chat\n")
        self.assertTrue(any(isinstance(packet, self.bot.protocol.binding.TextPacket)
                            and packet.body.message == "fixture chat" for packet in self.wire.sent))

    async def test_command_output_preserves_ordered_packet_fields_and_zero_success(self):
        b = self.bot.protocol.binding
        data = ' \n{"键":"猫 🌱","accent":"e\u0301"} '
        cases = [
            ("/list", b.CommandOutputType.ALL_OUTPUT, 1, None, "",
             [("commands.players.list", True, ["1", "7"]),
              ("commands.players.list.names", True, ["ScriptFixture"])]),
            ("/packstack bogus verbose", b.CommandOutputType.DATA_SET, 0, data, "応答",
             [("commands.generic.syntax", False, ["", "猫", "e\u0301", "🌱", " verbose"]),
              ("commands.generic.unknown", False, [])]),
            ("/help", b.CommandOutputType.NONE, 0, None, "", []),
        ]
        requests = []
        send = self.wire.send

        async def respond(batch):
            await send(batch)
            request = self.wire.sent[-1]
            if not isinstance(request, b.CommandRequestPacket):
                return
            command, kind, success_count, opaque_data, request_id, messages = cases[len(requests)]
            self.assertEqual(request.command, command)
            requests.append(request)
            self.wire.push(b.CommandOutputPacket(
                origin_data=b.CommandOriginData(type=b.CommandOriginType.PLAYER, uuid=request.origin.uuid,
                                                request_id=request_id, player_id=0),
                output=b.CommandOutput(type=kind, success_count=success_count, data=opaque_data, messages=[
                    b.CommandOutputMessage(message_id=message_id, successful=successful, params=params)
                    for message_id, successful, params in messages
                ]),
            ))

        script = """
async def main(bot):
    try:
        await bot.execute_command_output("expired", timeout=0)
    except TimeoutError:
        pass
    else:
        raise AssertionError("nonpositive timeout should fail")
    replies = []
    for command in ("list", "/packstack bogus verbose", "help"):
        reply = await bot.execute_command_output(command, timeout=3)
        assert isinstance(reply.origin_data.uuid, str)
        assert not hasattr(reply, "ID")
        assert all(not hasattr(message, "raw_message") for message in reply.output.messages)
        replies.append(reply)
    return replies
"""
        with patch.object(self.wire, "send", side_effect=respond):
            observed = await self.completed(await self.start(script))

        self.assertEqual(observed["state"], "succeeded", observed)
        self.assertEqual([request.command for request in requests], [case[0] for case in cases])
        self.assertEqual(len({request.origin.uuid for request in requests}), len(cases))
        expected = [
            {"origin_data": {"type": "PLAYER", "uuid": str(request.origin.uuid), "request_id": request_id,
                             "player_id": 0},
             "output": {"type": kind.name, "success_count": success_count, "data": opaque_data,
                        "messages": [{"message_id": message_id, "successful": successful, "params": params}
                                     for message_id, successful, params in messages]}}
            for request, (_, kind, success_count, opaque_data, request_id, messages) in zip(requests, cases, strict=True)
        ]
        self.assertEqual(observed["result"], expected)

    async def test_form_reply_encoding_failure_leaves_the_script_usable(self):
        b = self.bot.protocol.binding
        scheduled = test_scheduled_input.ScheduledInputTests()
        frames = (
            json.dumps({"type": "modal", "content": "large form " + "x" * _MAX_FRAME}),
            '{"type":"modal","content":1e999}',
            '{"type":"modal","content":"usable form"}',
        )
        for form_id, frame in enumerate(frames, 1):
            await scheduled.observe(self.bot, self.wire, b.ModalFormRequestPacket(form_id=form_id, form_json=frame))
        run = await self.start("""
async def main(bot):
    for form_id in (1, 2):
        try:
            bot.get_pending_form(form_id)
        except ValueError as error:
            assert len(str(error)) < 200 and 'large form' not in str(error)
        else:
            raise AssertionError(f'expected an encoding error for form {form_id}')
    assert bot.get_pending_form(3)['form']['content'] == 'usable form'
    return bot.get_tick()
""")
        report = await self.completed(run)
        self.assertEqual(report["state"], "succeeded", report)
        self.assertIsInstance(report["result"], int)

    async def test_oversized_error_reply_has_a_bounded_fallback(self):
        with patch.object(self.bot, "send_chat", side_effect=ValueError("oversized detail " + "x" * _MAX_FRAME)):
            run = await self.start("""
async def main(bot):
    try:
        await bot.send_chat('fixture')
    except ValueError as error:
        assert len(str(error)) < 200 and 'oversized detail' not in str(error)
        return bot.get_tick()
""")
            report = await self.completed(run)
        self.assertEqual(report["state"], "succeeded", report)
        self.assertIsInstance(report["result"], int)

    async def test_file_and_keyword_arguments_are_pinned_at_admission(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd(), prefix=".test-script-") as directory:
            script = Path(directory) / "fixtures.py"
            script.write_bytes(b"# coding: latin-1\nasync def main(bot, *, value):\n    return ['\xe9', value]\n")
            args = {"value": ["original"]}
            run = await self.start(path=str(script.relative_to(Path.cwd())), args=args)
            script.write_text("async def main(bot, *, value): return ['changed', value]", encoding="utf-8")
            args["value"][0] = "modified"
            result = await self.completed(run)
        self.assertEqual(result["state"], "succeeded", result)
        self.assertEqual(result["result"], ["é", ["original"]])

    async def test_navigation_goals_and_plan_are_structured_gameplay_values(self):
        await self.bot.wait_for_position_sync()
        for x in range(-1, 2):
            for z in range(-1, 2):
                await self.update_block((x, 99, z), 3317)
                for y in (100, 101):
                    await self.update_block((x, y, z), 18894)
                    await self.update_block((x, y, z), 18894, layer=1)
        run = await self.start("""
from endpilot import AllGoal, FootGoal, NavigationCosts, NavigationRules, XZGoal

async def main(bot):
    rules = NavigationRules(search_radius=1, vertical_radius=0, costs=NavigationCosts(walk=1.5))
    goal = AllGoal((FootGoal((1, 100, 0)), XZGoal((1, 0))))
    found = bot.plan_navigation(goal, rules=rules)
    unreachable = bot.plan_navigation(FootGoal((5, 100, 0)), rules=rules)
    return {'position': found.goal.goals[0].position, 'endpoint': found.endpoint, 'status': found.status,
            'cost': [found.cost.numerator, found.cost.denominator], 'walk': found.rules.costs.walk,
            'unreachable': [unreachable.status, unreachable.cost]}
""")
        result = await self.completed(run)
        self.assertEqual(result["state"], "succeeded", result)
        self.assertEqual(result["result"], {"position": [1, 100, 0], "endpoint": [1, 100, 0], "status": "found",
                                            "cost": [3, 2], "walk": 1.5, "unreachable": ["unreachable", None]})

    async def test_errors_and_path_validation(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            await self.start()
        with tempfile.NamedTemporaryFile() as outside:
            with self.assertRaisesRegex(ValueError, "under the workspace"):
                await self.start(path=outside.name)
        for source, name, message in (
            ("def main(bot): pass", "TypeError", "async main"),
            ("def x(:", "SyntaxError", "invalid syntax"),
            ("async def main(bot): raise ValueError('script error')", "ValueError", "script error"),
            ("async def main(bot): raise SystemExit('script exit')", "SystemExit", "script exit"),
        ):
            with self.subTest(name=name, message=message):
                failed = await self.completed(await self.start(source))
                self.assertEqual(failed["state"], "failed", failed)
                self.assertEqual(failed["error"]["error_type"], name)
                self.assertIn(message, failed["error"]["message"])

    async def test_supported_sdk_errors_keep_types_and_only_public_attributes(self):
        sleep = SleepRejected("tile.bed.notSafe")
        sleep.secret = "fixture-only"
        partial = RuntimeError("collection fixture failed")
        partial.collection_progress = BlockCollectionBatch(0, ((2, 100, 3),), (),
                                                           BlockCollection((2, 100, 3), "inventory_unconfirmed", None))
        with (
            patch.object(self.bot, "sleep", side_effect=sleep),
            patch.object(self.bot, "mine_block", side_effect=MiningFaceUnavailable("no_sample")),
            patch.object(self.bot, "collect_targets", side_effect=partial),
        ):
            run = await self.start("""
from endpilot.beds import SleepRejected
from endpilot.blocks import MiningFaceUnavailable

async def main(bot):
    values = []
    try:
        await bot.sleep((2, 100, 3))
    except SleepRejected as error:
        assert not hasattr(error, 'secret')
        values.append([type(error).__name__, error.translation_key, str(error)])
    try:
        await bot.mine_block((2, 100, 3), face=None)
    except MiningFaceUnavailable as error:
        values.append([type(error).__name__, error.status, str(error)])
    try:
        await bot.collect_targets([(2, 100, 3)])
    except RuntimeError as error:
        values.append([type(error).__name__, error.collection_progress.active.position,
                       error.collection_progress.active.status, str(error)])
    return values
""")
            report = await self.completed(run)
            failed = await self.completed(await self.start("async def main(bot): await bot.sleep((2, 100, 3))"))
        self.assertEqual(report["state"], "succeeded", report)
        self.assertEqual(report["result"], [
            ["SleepRejected", "tile.bed.notSafe", "tile.bed.notSafe"],
            ["MiningFaceUnavailable", "no_sample", "automatic mining face unavailable: no_sample"],
            ["RuntimeError", [2, 100, 3], "inventory_unconfirmed", "collection fixture failed"],
        ])
        self.assertEqual(failed["state"], "failed", failed)
        self.assertEqual(failed["error"], {"error_type": "SleepRejected", "message": "tile.bed.notSafe",
                                          "attributes": {"translation_key": "tile.bed.notSafe"}})

    async def test_world_time_and_entity_properties_are_projected_as_snapshots(self):
        clocks = (WorldClock(1, "minecraft:overworld", 120_001, False),)
        world_time = WorldTime(daylight_ticks=48_123, clocks=clocks)
        entities = (EntityState(1, 2, "minecraft:sheep", (0, 0, 0), visible_effects_value=18 | (6 << 7)),
                    EntityState(3, 4, "minecraft:sheep", (1, 0, 0), visible_effects_value=0),
                    EntityState(5, 6, "minecraft:sheep", (2, 0, 0)))
        with (
            patch.object(Bot, "world_time", new_callable=PropertyMock, return_value=world_time),
            patch.object(Bot, "observed_entities", new_callable=PropertyMock, return_value=entities),
        ):
            run = await self.start("""
async def main(bot):
    time = bot.world_time
    return [time.daytime, time.day, time.observed_moon_phase,
            [entity.visible_effect_ids for entity in bot.observed_entities]]
""")
            report = await self.completed(run)
        self.assertEqual(report["state"], "succeeded", report)
        self.assertEqual(report["result"], [123, 2, 5, [[9, 3], [], None]])

    async def test_column_coverage_carries_sdk_invalidation_for_a_real_range(self):
        run = await self.start("""
import asyncio
from endpilot.blocks import ColumnCoverageInvalidated

async def main(bot):
    waiting = asyncio.create_task(bot.wait_for_column_coverage((2, 3), range(6, 7), timeout=4))
    await asyncio.sleep(0)
    bot.get_tick()
    print('coverage waiting')
    try:
        await waiting
    except ColumnCoverageInvalidated as error:
        return [type(error).__name__, error.reason, str(error)]
""")
        await self.wait_for(run, lambda status: "coverage waiting\n" in self.text(status))
        b = self.bot.protocol.binding
        self.wire.push(b.ChangeDimensionPacket(dimension_id=1, pos=b.Vec3(x=0, y=100, z=0)))
        report = await self.completed(run)
        self.assertEqual(report["state"], "succeeded", report)
        self.assertEqual(report["result"], ["ColumnCoverageInvalidated", "dimension_changed",
                                            "column coverage invalidated: dimension_changed"])

    async def test_imported_and_subscribed_callback_loops_are_stoppable(self):
        source = """
import asyncio
import difflib
import itertools

def endless():
    def candidates():
        print('entered imported loop')
        yield from itertools.repeat('different')
    difflib.get_close_matches('needle', candidates())

async def main(bot, where):
    if where == 'callback':
        bot.subscribe_ticks(lambda tick: endless())
        await asyncio.Event().wait()
    elif where == 'child':
        async def child():
            endless()
        asyncio.create_task(child())
        await asyncio.Event().wait()
    else:
        endless()
"""
        runs = [await self.start(source, args=[where]) for where in ("main", "callback", "child")]
        for run in runs:
            await self.wait_for(run, lambda status: "entered imported loop" in self.text(status))
        before = self.bot.get_tick()
        self.assertGreaterEqual(await self.bot.wait_ticks(2), before + 2)
        for run in runs:
            self.assertEqual((await self.scripts.observe_script(run))["state"], "running")
        async with asyncio.timeout(2):
            results = await asyncio.gather(*(self.scripts.stop_script(run) for run in runs))
        self.assertTrue(all(report["state"] == "stopped" for report in results), results)
        await self.bot.send_chat("still connected")
        self.assertEqual(self.wire.sent[-1].body.message, "still connected")

    async def test_async_notification_can_query_and_unsubscribe(self):
        notices = []
        subscribe = self.bot.subscribe_ticks

        def register(callback):
            unsubscribe = subscribe(callback)

            def release():
                notices.append("unsubscribed")
                unsubscribe()

            return release

        with patch.object(self.bot, "subscribe_ticks", register):
            run = await self.start("""
import asyncio
async def main(bot):
    received = asyncio.get_running_loop().create_future()
    def callback(tick):
        observed = bot.get_tick()
        unsubscribe()
        received.set_result([tick, observed])
    unsubscribe = bot.subscribe_ticks(callback)
    return await received
""")
            status = await self.completed(run)
        self.assertEqual(status["state"], "succeeded", status)
        self.assertGreaterEqual(status["result"][1], status["result"][0])
        self.assertEqual(notices, ["unsubscribed"])

    async def test_world_feedback_waits_and_notifications_copy_wire_fields(self):
        run = await self.start("""
import asyncio

def sound(event):
    assert not hasattr(event, 'packet')
    return [event.name, event.position, event.volume, event.pitch, event.loop_count]

def particle(event):
    assert not hasattr(event, 'packet')
    return [event.name, event.position, event.vanilla_dimension_id, event.actor_id, event.molang_variables]

def block(event):
    assert not hasattr(event, 'packet')
    return [event.kind, event.position, event.note_pitch, event.instrument_id, event.data]

async def main(bot):
    delivered = asyncio.Event()
    notifications = []
    def report(project, event):
        notifications.append(project(event))
        if len(notifications) == 3:
            delivered.set()

    stop_sound = bot.subscribe_sound(lambda event: report(sound, event), name='fixture.pop', kind='play')
    stop_particle = bot.subscribe_particle(lambda event: report(particle, event), name='fixture.flame')
    stop_block = bot.subscribe_block_events(lambda event: report(block, event), kind='note')
    waits = asyncio.gather(bot.wait_for_sound('fixture.pop', kind='play'), bot.wait_for_particle('fixture.flame'),
                           bot.wait_for_block_event(kind='note'))
    await asyncio.sleep(0)
    bot.get_tick()   # Wait for admission of preceding requests before triggering the fixture.
    print('feedback ready')
    results = await waits
    await delivered.wait()
    stop_sound()
    stop_particle()
    stop_block()
    return {'waits': [sound(results[0]), particle(results[1]), block(results[2])], 'notifications': notifications}
""")
        await self.wait_for(run, lambda status: "feedback ready" in self.text(status))
        b = self.bot.protocol.binding
        self.wire.push(b.PlaySoundPacket(name="fixture.pop", pos=b.BlockPos(x=8, y=800, z=16),
                                         volume=0.75, pitch=1.25, loop_count=3))
        spot = b.Vec3(x=1, y=100, z=2)
        self.wire.push(b.SpawnParticleEffectPacket(vanilla_dimension_id=2, actor_id=91, pos=spot,
                                                    effect_name="fixture.flame", molang_variables='{"a":1}'))
        self.wire.push(b.LevelSoundEventPacket(pos=spot, sound_event="note", data=257))
        status = await self.completed(run)
        self.assertEqual(status["state"], "succeeded", status)
        expected = [["fixture.pop", [1, 100, 2], 0.75, 1.25, 3],
                    ["fixture.flame", [1, 100, 2], 2, 91, '{"a":1}'],
                    ["note", [1, 100, 2], 1, 1, 257]]
        self.assertEqual(status["result"], {"waits": expected, "notifications": expected})

    async def test_stop_drains_host_action_before_original_bot_is_released(self):
        began, cancelled, cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        old = self.bot

        async def outstanding(_message):
            began.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
                await cleanup.wait()

        with patch.object(old, "send_chat", outstanding):
            run = await self.start("async def main(bot): await bot.send_chat('old command')")
            async with asyncio.timeout(7):
                await began.wait()
                closing = asyncio.create_task(self.scripts.close())
                try:
                    await cancelled.wait()
                    self.assertFalse(closing.done())
                    self.assertEqual((await self.scripts.observe_script(run))["state"], "running")
                finally:
                    cleanup.set()
                    await closing
        self.assertEqual((await self.completed(run))["state"], "stopped")
        old_wire = self.wire
        await old.close()
        self.bot = Bot("Replacement")
        self.addAsyncCleanup(self.bot.close)
        wire = await test_scheduled_input.ScheduledInputTests().join(self.bot)
        new = await self.start("async def main(bot): await bot.send_chat('new command')")
        self.assertEqual((await self.completed(new))["state"], "succeeded")
        self.assertEqual(wire.sent[-1].body.message, "new command")
        self.assertFalse(any(isinstance(packet, old.protocol.binding.TextPacket) for packet in old_wire.sent))

    async def test_worker_cancellation_propagates_to_an_admitted_host_action(self):
        began, released = asyncio.Event(), asyncio.Event()

        async def wait(ticks):
            began.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.set()

        with patch.object(self.bot, "wait_ticks", wait):
            run = await self.start("""
import asyncio
async def main(bot):
    task = asyncio.create_task(bot.wait_ticks(100))
    await asyncio.sleep(0.25)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    print('cancel sent')
    await asyncio.Event().wait()
""")
            async with asyncio.timeout(7):
                await began.wait()
                await released.wait()
            self.assertEqual((await self.scripts.observe_script(run))["state"], "running")

    async def test_unsupported_members_and_callbacks_never_reach_host(self):
        with patch.object(self.bot, "find_blocks", side_effect=AssertionError("host method was invoked")):
            run = await self.start("""
async def main(bot):
    for name in ('identity', 'packets', 'close', 'join_nethernet'):
        try:
            bot._channel.request('get', name)
        except AttributeError:
            continue
        raise AssertionError(name)
    try:
        bot.find_blocks(predicate=lambda match: True)
    except TypeError:
        return 'denied'
""")
            self.assertEqual((await self.completed(run))["result"], "denied")

    async def test_stalled_pipe_does_not_block_stop_and_reports_overflow(self):
        offered = []
        released = []

        def subscribe(callback):
            offered.append(callback)
            return lambda: released.append(True)

        with patch.object(self.bot, "subscribe_chat", subscribe):
            for flood in (False, True):
                run = await self.start("""
import ctypes
import os

async def main(bot):
    bot.subscribe_chat(lambda event: None)
    print('native call ready')
    if os.name == 'nt':
        ctypes.PyDLL('kernel32').Sleep(60_000)
    else:
        ctypes.PyDLL(None).sleep(60)
""")
                await self.wait_for(run, lambda status: "native call ready" in self.text(status))
                # More data than a pipe can carry with its worker reader parked.
                event = ChatMessage("fixture", "a" * (256 * 1024))
                offered[-1](event)
                await asyncio.sleep(0.02)
                before = self.bot.get_tick()
                async with asyncio.timeout(2):
                    self.assertGreater(await self.bot.wait_ticks(1), before)
                    if flood:
                        for _ in range(12):
                            offered[-1](event)
                        failed = await self.completed(run)
                        self.assertEqual(failed["state"], "failed")
                        self.assertIn("not consuming", failed["error"]["message"])
                    else:
                        self.assertEqual((await self.scripts.stop_script(run))["state"], "stopped")
        self.assertEqual(released, [True, True])

    async def test_slow_worker_callback_overflow_releases_subscription_and_keeps_bot_usable(self):
        notices = []
        released = asyncio.Event()

        def subscribe(callback):
            notices.append(callback)
            return released.set

        with patch.object(self.bot, "subscribe_chat", subscribe):
            run = await self.start("""
import asyncio

async def main(bot):
    def callback(event):
        print('callback entered', flush=True)
        while True: pass
    bot.subscribe_chat(callback)
    print('subscribed', flush=True)
    await asyncio.Event().wait()
""")
            await self.wait_for(run, lambda status: "subscribed\n" in self.text(status))
            callback, = notices
            callback(ChatMessage("fixture", "first"))
            await self.wait_for(run, lambda status: "callback entered\n" in self.text(status))
            # Pace these small frames so the host writer keeps draining. The
            # separate worker reader must bound its own pending callbacks.
            for index in range(192):
                if (await self.scripts.observe_script(run))["state"] != "running":
                    break
                callback(ChatMessage("fixture", str(index)))
                await asyncio.sleep(0.02)
            failed = await self.completed(run)
        self.assertEqual(failed["state"], "failed", failed)
        self.assertEqual(failed["error"]["message"], "MCP script worker notification queue is full")
        self.assertTrue(released.is_set())
        before = self.bot.get_tick()
        self.assertGreater(await self.bot.wait_ticks(1), before)

    async def test_stop_before_spawning_and_report_dropped_output(self):
        early = await self.start("async def main(bot): raise AssertionError('script was executed')")
        self.assertEqual((await self.scripts.stop_script(early))["state"], "stopped")
        with patch("endpilot._mcp_scripts._OUTPUT_CHUNKS", 2), patch("endpilot._mcp_scripts._CHUNK_SIZE", 4):
            run = await self.start("async def main(bot): print('abcdefghijklmnop', end='')")
            status = await self.completed(run)
        self.assertEqual(status["cursor"], 4)
        self.assertEqual(status["lost"], 2)
        self.assertEqual(self.text(status), "ijklmnop")
        self.assertEqual(self.text(await self.scripts.observe_script(run, 3)), "mnop")
        self.assertEqual((await self.scripts.observe_script(run, 4))["output"], [])

    @unittest.skipUnless(os.name == "posix", "requires POSIX signal semantics")
    async def test_hard_stop_worker_that_ignores_sigterm(self):
        run = await self.start("""
import signal

async def main(bot):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print('ready')
    while True: pass
""")
        await self.wait_for(run, lambda status: "ready\n" in self.text(status))
        async with asyncio.timeout(2):
            self.assertEqual((await self.scripts.stop_script(run))["state"], "stopped")


class MCPScriptPlacementTests(test_placement._PlacementHarness):
    async def test_direct_placement_result_contains_both_nested_observations(self):
        scripts = MCPScripts(SimpleNamespace(connected_bot=lambda: self.bot))
        self.addAsyncCleanup(scripts.close)
        submitted = asyncio.Event()

        def mark(packet_id):
            if packet_id == self.b.InventoryTransactionPacket.ID:
                submitted.set()

        unsubscribe = self.packets.subscribe_sent(mark)
        self.addCleanup(unsubscribe)
        run = (await scripts.run_script(source="""
async def main(bot, support, runtime):
    return await bot.place_block(support, face=1, expected_runtime_id=runtime, timeout=4)
""", args=[test_placement.SUPPORT, test_placement.PLACED_ID]))["run_id"]
        async with asyncio.timeout(7):
            await submitted.wait()
            await self.deliver(self.block(test_placement.TARGET, test_placement.PLACED_ID), self.slot(3, net_id=22))
            while (result := await scripts.observe_script(run))["state"] == "running":
                await asyncio.sleep(0.005)
        self.assertEqual(result["state"], "succeeded", result)
        result = result["result"]
        self.assertEqual(result["target"], list(test_placement.TARGET))
        self.assertTrue(result["submitted"])
        self.assertEqual(result["reason"], "observations_complete")
        self.assertEqual(result["world_change"]["after"]["runtime_id"], test_placement.PLACED_ID)
        self.assertEqual(result["inventory_change"]["after"]["count"], 3)

    async def test_script_cancellation_keeps_actual_host_placement_progress(self):
        scripts = MCPScripts(SimpleNamespace(connected_bot=lambda: self.bot))
        self.addAsyncCleanup(scripts.close)
        submitted = asyncio.Event()

        def mark(packet_id):
            if packet_id == self.b.InventoryTransactionPacket.ID:
                submitted.set()

        self.addCleanup(self.packets.subscribe_sent(mark))
        run = (await scripts.run_script(source="""
import asyncio

async def main(bot, support, target, runtime):
    observation = asyncio.create_task(bot.wait_for_block_update(target, timeout=4))
    await asyncio.sleep(0)
    placing = asyncio.create_task(bot.place_block(support, face=1, expected_runtime_id=runtime, timeout=4))
    await observation
    placing.cancel('script cancellation')
    try:
        await placing
    except asyncio.CancelledError as error:
        receipt = error.placement_progress
        return [str(error), receipt.reason, receipt.submitted, receipt.world_change.after.runtime_id,
                receipt.inventory_change]
""", args=[test_placement.SUPPORT, test_placement.TARGET, test_placement.PLACED_ID]))["run_id"]
        async with asyncio.timeout(7):
            await submitted.wait()
            await self.deliver(self.block(test_placement.TARGET, test_placement.PLACED_ID))
            while (result := await scripts.observe_script(run))["state"] == "running":
                await asyncio.sleep(0.005)
        self.assertEqual(result["state"], "succeeded", result)
        self.assertEqual(result["result"], ["script cancellation", "cancelled", True, test_placement.PLACED_ID, None])
