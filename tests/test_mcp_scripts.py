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
from endpilot.mcp_server import MCPPlayer
from endpilot.protocol import RawPacket, write_batch
from endpilot.world_environment import WorldClock, WorldTime
import test_placement
import test_recipe_auto_tags
import test_scheduled_input
import test_trading


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

    async def test_world_entity_interaction_uses_the_resident_connection(self):
        b = self.bot.protocol.binding
        await test_scheduled_input.ScheduledInputTests().observe(
            self.bot, self.wire, b.AddActorPacket(
                entity_id=42, runtime_id=77, actor_type="minecraft:armor_stand",
                pos=b.Vec3(x=2.5, y=100, z=0.5),
            ),
        )
        script = """
async def main(bot):
    result = await bot.interact_entity_at(77, position=(2.625, 100.375, 0.125))
    return {"interaction": result}
"""
        status = await self.completed(await self.start(script))
        self.assertEqual(status["state"], "succeeded", status)
        self.assertEqual(status["result"], {"interaction": None})
        txn, = [packet.variant_transaction for packet in self.wire.sent
                if isinstance(packet, b.InventoryTransactionPacket)]
        self.assertEqual((txn.runtime_id, txn.action_type.name), (77, "INTERACT"))
        self.assertEqual((txn.hit_pos.x, txn.hit_pos.y, txn.hit_pos.z), (2.625, 100.375, 0.125))

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

    async def test_script_can_stop_another_mining_call_and_catch_mining_aborted(self):
        await self.update_block((1, 100, 0), 42)
        run = await self.start("""
import asyncio
from endpilot import MiningAborted

async def main(bot):
    mining = asyncio.create_task(bot.mine_block((1, 100, 0), face=4, timeout=4))
    async with asyncio.timeout(2):
        while bot.mining_target is None:
            await asyncio.sleep(0.01)
    released = await bot.stop_mining()
    try:
        await mining
    except MiningAborted as error:
        return [type(error).__name__, released]
""")
        report = await self.completed(run)
        actions = [(packet.client_tick, [action.player_action_type.name for action in packet.player_block_actions])
                   for packet in self.wire.sent if getattr(packet, "player_block_actions", None)]
        start, release = actions
        self.assertEqual(start[1], ["START_DESTROY_BLOCK"])
        self.assertEqual(release[1], ["ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK"])
        self.assertEqual(report["state"], "succeeded", report)
        self.assertEqual(report["result"], ["MiningAborted", release[0]])

    async def test_world_time_and_entity_properties_are_projected_as_snapshots(self):
        clocks = (WorldClock(1, "minecraft:overworld", 120_001, False),)
        world_time = WorldTime(daylight_ticks=48_123, clocks=clocks)
        entities = (EntityState(1, 2, "minecraft:sheep", (0, 0, 0), visible_effects_value=18 | (6 << 7),
                                pitch=-12.25, yaw=0.0, head_yaw=137.75),
                    EntityState(3, 4, "minecraft:sheep", (1, 0, 0), visible_effects_value=0),
                    EntityState(5, 6, "minecraft:villager_v2", (2, 0, 0), trade_tier=1, trade_experience=10),
                    EntityState(7, 8, "minecraft:villager_v2", (3, 0, 0), trade_tier=0))
        with (
            patch.object(Bot, "world_time", new_callable=PropertyMock, return_value=world_time),
            patch.object(Bot, "observed_entities", new_callable=PropertyMock, return_value=entities),
        ):
            run = await self.start("""
async def main(bot):
    time = bot.world_time
    entities = bot.observed_entities
    return [time.daytime, time.day, time.observed_moon_phase,
            [entity.visible_effect_ids for entity in entities],
            [[entity.trade_tier, entity.trade_experience] for entity in entities],
            [[entity.pitch, entity.yaw, entity.head_yaw] for entity in entities]]
""")
            report = await self.completed(run)
        self.assertEqual(report["state"], "succeeded", report)
        self.assertEqual(report["result"], [
            123, 2, 5, [[9, 3], [], None, None], [[None, None], [None, None], [1, 10], [0, None]],
            [[-12.25, 0.0, 137.75], [None, None, None], [None, None, None], [None, None, None]],
        ])

    async def test_ability_speeds_are_projected_in_read_and_wait_snapshots(self):
        b = self.bot.protocol.binding
        data = b.SerializedAbilitiesData
        fly, vertical, walk = (1 << bit for bit in (13, 19, 14))
        may_fly, flying = (1 << int(index) for index in (b.AbilitiesIndex.MAY_FLY, b.AbilitiesIndex.FLYING))
        declared = fly | vertical | walk | may_fly | flying
        initial_layer = data.SerializedLayer(serialized_layer=data.SerializedAbilitiesLayer.BASE,
                                             abilities_set=declared, ability_values=may_fly, fly_speed=float("nan"),
                                             vertical_fly_speed=float("inf"), walk_speed=-float("inf"))
        initial = b.UpdateAbilitiesPacket(data=data(target_player=1, layers=[initial_layer]))
        await test_scheduled_input.ScheduledInputTests().observe(self.bot, self.wire, initial)
        run = await self.start("""
import asyncio

async def main(bot):
    can_fly = bot.abilities.may_fly
    original = bot.abilities
    waiting = asyncio.create_task(bot.wait_for_abilities(timeout=3))
    await asyncio.sleep(0)
    bot.get_tick()
    print('abilities waiting')
    return [can_fly, original, await waiting, bot.abilities]
""")
        await self.wait_for(run, lambda status: "abilities waiting\n" in self.text(status))
        layer = data.SerializedLayer(serialized_layer=data.SerializedAbilitiesLayer.BASE,
                                     abilities_set=fly | vertical | may_fly | flying, ability_values=may_fly,
                                     fly_speed=0.0, vertical_fly_speed=-0.75, walk_speed=0.25)
        self.wire.push(b.UpdateAbilitiesPacket(data=data(target_player=1, layers=[layer])))
        report = await self.completed(run)
        self.assertEqual(report["state"], "succeeded", report)
        original = {"abilities_set": declared, "ability_values": may_fly, "may_fly": True, "flying": False,
                    "fly_speed": None, "vertical_fly_speed": None, "walk_speed": None}
        updated = dict(original, abilities_set=declared & ~walk, fly_speed=0.0, vertical_fly_speed=-0.75)
        self.assertEqual(report["result"], [True, original, updated, updated])

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
        await self.update_block((-3, 220, 17), 2359)  # Sequential vanilla note block runtime ID.
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
    return [event.kind, event.position, event.note_pitch, event.note_tuning, event.instrument_id,
            event.data, event.instrument_sound, event.b0, event.b1]

async def main(bot):
    delivered = asyncio.Event()
    notifications = []
    def report(project, event):
        notifications.append(project(event))
        if len(notifications) == 4:
            delivered.set()

    stop_sound = bot.subscribe_sound(lambda event: report(sound, event), name='fixture.pop', kind='play')
    stop_particle = bot.subscribe_particle(lambda event: report(particle, event), name='fixture.flame')
    stop_block = bot.subscribe_block_events(lambda event: report(block, event), kind='note')
    stop_tuning = bot.subscribe_block_events(lambda event: report(block, event), kind='note_tuning')
    waits = asyncio.gather(bot.wait_for_sound('fixture.pop', kind='play'), bot.wait_for_particle('fixture.flame'),
                           bot.wait_for_block_event(kind='note'), bot.wait_for_block_event(kind='note_tuning'))
    await asyncio.sleep(0)
    bot.get_tick()   # Wait for admission of preceding requests before triggering the fixture.
    print('feedback ready')
    results = await waits
    await delivered.wait()
    stop_sound()
    stop_particle()
    stop_block()
    stop_tuning()
    return {'waits': [sound(results[0]), particle(results[1]), block(results[2]), block(results[3])],
            'notifications': notifications}
""")
        await self.wait_for(run, lambda status: "feedback ready" in self.text(status))
        b = self.bot.protocol.binding
        self.wire.push(b.PlaySoundPacket(name="fixture.pop", pos=b.BlockPos(x=8, y=800, z=16),
                                         volume=0.75, pitch=1.25, loop_count=3))
        spot = b.Vec3(x=1, y=100, z=2)
        self.wire.push(b.SpawnParticleEffectPacket(vanilla_dimension_id=2, actor_id=91, pos=spot,
                                                    effect_name="fixture.flame", molang_variables='{"a":1}'))
        # Vanilla 2225 skeleton head above a note block after its second click.
        note = RawPacket(123, bytes.fromhex("046e6f7465000040c000005c43000088419254000000ffffffffffffffff00"))
        tuning = RawPacket(26, bytes.fromhex("05b803222a04"))
        self.wire.incoming.put_nowait(write_batch((note, tuning)))
        status = await self.completed(run)
        self.assertEqual(status["state"], "succeeded", status)
        expected = [["fixture.pop", [1, 100, 2], 0.75, 1.25, 3],
                    ["fixture.flame", [1, 100, 2], 2, 91, '{"a":1}'],
                    ["note", [-3, 220, 17], 9, None, 21, 5385, "note.skeleton", None, None],
                    ["note_tuning", [-3, 220, 17], None, 2, 21, None, "note.skeleton", 21, 2]]
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

    async def test_inventory_range_exposes_ordered_host_verdicts_and_rejects_bad_inputs(self):
        b = self.bot.protocol.binding
        paper = 389
        slots = [b.SerializedNetworkItemStackDescriptor() for _ in range(36)]
        slots[9] = b.SerializedNetworkItemStackDescriptor(id=paper, stack_size=5, net_id_variant=20)
        slots[18] = b.SerializedNetworkItemStackDescriptor(id=paper, stack_size=62, net_id_variant=21)
        self.bot._session._replace_item_registry(b.ItemRegistryPacket(items=[
            b.ItemData(id=paper, name="minecraft:paper"),
        ]))
        for packet in (
            b.ContainerOpenPacket(container_id=7, type=b.ContainerType.INVENTORY,
                                  pos=b.BlockPos(x=0, y=0, z=0), entity_unique_id=-1),
            b.InventoryContentPacket(inventory_id=b.ContainerID.INVENTORY, slots=slots),
        ):
            await test_scheduled_input.ScheduledInputTests().observe(self.bot, self.wire, packet)

        sent = asyncio.Queue()
        self.addCleanup(self.bot.packets.subscribe_sent(
            lambda packet_id: sent.put_nowait(None) if packet_id == b.ItemStackRequestPacket.ID else None,
        ))
        run = await self.start("""
async def main(bot):
    for source, target, match, expected, message in (
        (range(9, 10), range(18, 20), lambda item: True, TypeError, 'callbacks'),
        (range(9, 10, 2), range(18, 20), None, ValueError, 'forward half-open range'),
        (range(9, 19), range(18, 20), None, ValueError, 'must not overlap'),
        (range(9, 10), range(36, 37), None, ValueError, 'within 0..36'),
    ):
        try:
            await bot.transfer_inventory_range(item='minecraft:paper', count=4, source_slots=source,
                                               destination_slots=target, match=match)
        except expected as error:
            assert message in str(error), str(error)
        else:
            raise AssertionError('invalid input accepted')
    result = await bot.transfer_inventory_range(item='minecraft:paper', count=4, source_slots=range(9, 10),
                                               destination_slots=range(18, 20), match=None)
    assert result.steps[0].confirmed == 2 and result.steps[1].confirmed == 0
    return result
""")
        async with asyncio.timeout(7):
            requests = []
            for target, verdict in ((18, "SUCCESS"), (19, "ERROR")):
                await self.wait_for(run, lambda _: not sent.empty())
                sent.get_nowait()
                packet = next(packet for packet in reversed(self.wire.sent)
                              if isinstance(packet, b.ItemStackRequestPacket))
                request, = packet.requests
                requests.append(request.client_request_id.raw_id)
                action, = request.actions
                self.assertEqual((action.source.slot, action.destination.slot, action.amount), (9, target, 2))
                containers = [b.ItemStackResponseContainerInfo(
                    full_container_name=b.FullContainerName(name=b.ContainerEnumName.COMBINED_HOTBAR_AND_INVENTORY_CONTAINER),
                    slots=[b.ItemStackResponseSlotInfo(requested_slot=slot, slot=slot, amount=count,
                                                       item_stack_net_id=b.ItemStackNetId(raw_id=net_id))
                           for slot, count, net_id in ((9, 3, 20), (18, 64, 21))],
                )] if verdict == "SUCCESS" else None
                response = b.ItemStackResponseInfo(client_request_id=request.client_request_id,
                                                   result=b.ItemStackNetResult[verdict], containers=containers)
                self.wire.push(b.ItemStackResponsePacket(responses=[response]))
        final = await self.completed(run)
        self.assertEqual(final["state"], "succeeded", final)
        self.assertEqual(final["result"], {"requested": 4, "stop_reason": "rejected", "confirmed_count": 2, "steps": [
            {"source_slot": 9, "destination_slot": 18, "requested": 2, "confirmed": 2,
             "request_id": requests[0], "verdict": "SUCCESS"},
            {"source_slot": 9, "destination_slot": 19, "requested": 2, "confirmed": 0,
             "request_id": requests[1], "verdict": "ERROR"},
        ]})
        self.assertTrue(sent.empty())

    async def test_range_reply_received_before_local_cancellation_keeps_progress_and_cancel_message(self):
        script = """
import asyncio

async def main(bot, count):
    channel = bot._channel
    start = channel.start
    replies = []
    def capture(*args, **kwargs):
        started = start(*args, **kwargs)
        replies.append(started[1])
        return started
    channel.start = capture

    task = asyncio.create_task(bot.transfer_container_range(item='minecraft:paper', count=count,
                                                           source_slots=range(20, 22),
                                                           destination_slots=range(29, 31)))
    await asyncio.sleep(0)
    frame = replies[0].result(timeout=5)
    if count:
        assert frame['error']['type'] == 'RuntimeError', frame
    else:
        assert 'value' in frame, frame
    task.cancel('worker cancelled after reply')
    await task
"""
        for count, reason in ((0, "complete"), (1, "unconfirmed")):
            with self.subTest(count=count):
                run = await self.start(script, args=[count])
                final = await self.completed(run)
                self.assertEqual(final["state"], "failed", final)
                self.assertEqual(final["error"], {
                    "error_type": "CancelledError", "message": "worker cancelled after reply",
                    "attributes": {"transfer_progress": {"requested": count, "steps": [], "stop_reason": reason,
                                                          "confirmed_count": 0}},
                })

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


class MCPScriptRecipeTests(unittest.IsolatedAsyncioTestCase):
    join = test_recipe_auto_tags.RecipeAutoTagTests.join
    deliver = test_recipe_auto_tags.RecipeAutoTagTests.deliver
    item = test_recipe_auto_tags.RecipeAutoTagTests.item
    verdict = test_recipe_auto_tags.RecipeAutoTagTests.verdict
    start = MCPScriptTests.start
    wait_for = MCPScriptTests.wait_for
    completed = MCPScriptTests.completed

    async def asyncSetUp(self):
        self.enterContext(test_scheduled_input.ManualClock().patch_scheduler())
        await self.join()
        self.scripts = MCPScripts(SimpleNamespace(connected_bot=lambda: self.bot))
        self.addAsyncCleanup(self.scripts.close)

    async def test_recipe_queries_copy_nested_plans_and_crafting_returns_matched_verdict(self):
        b = self.b
        oak = b.SerializedRecipeIngredient(descriptor={"name": "minecraft:oak_planks"}, stack_size=1, aux_value=0)
        birch = b.SerializedRecipeIngredient(descriptor={"name": "minecraft:birch_planks"}, stack_size=2, aux_value=0)
        blank = b.SerializedRecipeIngredient(stack_size=0, aux_value=32767)
        data = b"\0" * 10
        output = b.SerializedNetworkItemInstanceDescriptor(id=3, stack_size=1, user_data_buffer=data)
        catalog = b.CraftingDataPacket(shaped_recipes=[b.ShapedRecipePayload(
            recipe_id="test:mcp_craft", net_id=40, width=2, height=2, tag="crafting_table",
            ingredients=[oak, blank, birch, blank], results=[output],
        )])
        await self.deliver(catalog, b.InventoryContentPacket(inventory_id=b.ContainerID.INVENTORY,
                                                            slots=[self.item(1, 1, net_id=21),
                                                                   self.item(2, 2, net_id=22), self.item()]))
        baseline = len(self.wire.sent)
        sent = asyncio.Queue()
        self.addCleanup(self.bot.packets.subscribe_sent(
            lambda packet_id: sent.put_nowait(None) if packet_id == b.ItemStackRequestPacket.ID else None,
        ))
        run = await self.start("""
async def main(bot):
    recipe, = bot.recipes_all('minecraft:crafting_table', grid_size=2)
    available, = bot.recipes_for('minecraft:crafting_table', grid_size=2)
    plan = bot.plan_recipe_delta(available.net_id)
    assert plan.recipe.shape[1] is None and plan.recipe.results[0].user_data_buffer == '00' * 10
    assert plan.materials[1][0].name == 'minecraft:birch_planks'
    assert bot.recipes_for('minecraft:crafting_table', min_result_count=2) == []
    assert bot.plan_recipe_delta(recipe.recipe_id, times=2) is None
    try:
        await bot.craft_recipe_auto(recipe.recipe_id, 2, times=2)
    except ValueError as error:
        assert 'not enough' in str(error)
    else:
        raise AssertionError('unexpected craft with insufficient materials')
    rejected = await bot.craft_recipe_auto(recipe.recipe_id, 2)
    accepted = await bot.craft_recipe_auto(available.net_id, 2)
    assert rejected.result.name == 'ERROR' and accepted.result.name == 'SUCCESS'
    assert bot.recipes_for('minecraft:crafting_table') == []
    assert bot.plan_recipe_delta(recipe.net_id) is None
    return {'recipe': recipe, 'available': available, 'plan': plan, 'replies': [rejected, accepted],
            'output': bot.inventory_slots[2].count, 'player_id': bot.player_id}
""")
        replies = []
        for name in ("ERROR", "SUCCESS"):
            await self.wait_for(run, lambda _: not sent.empty())
            sent.get_nowait()
            packet = self.wire.sent[-1]
            self.assertIsInstance(packet, b.ItemStackRequestPacket)
            request, = packet.requests
            request_id = request.client_request_id.raw_id
            self.assertEqual(request.actions[0].recipe_net_id, 40)
            self.assertEqual(request.actions[0].num_crafts, 1)
            corrections = ((0, 0, None), (1, 0, None), (2, 1, 23)) if name == "SUCCESS" else ()
            await self.deliver(b.ItemStackResponsePacket(responses=[self.verdict(request_id, *corrections, result=name)]))
            replies.append({"result": {"name": name}, "client_request_id": {"raw_id": request_id}})
        final = await self.completed(run)
        self.assertEqual(final["state"], "succeeded", final)
        self.assertEqual([type(packet) for packet in self.wire.sent[baseline:]], [b.ItemStackRequestPacket] * 2)
        self.assertEqual(self.wire.closed, False)
        first = {"name": "minecraft:oak_planks", "tag": None, "count": 1, "aux_value": 0}
        second = {"name": "minecraft:birch_planks", "tag": None, "count": 2, "aux_value": 0}
        recipe = {"recipe_id": "test:mcp_craft", "net_id": 40, "kind": "shaped_recipes", "tag": "crafting_table",
                  "ingredients": [first, second], "results": [
                      {"item_id": 3, "name": "minecraft:crafting_table", "count": 1, "aux_value": 0,
                       "block_runtime_id": 0, "user_data_buffer": data.hex()},
                  ], "width": 2, "height": 2, "shape": [first, None, second, None]}
        oak_amount = {"item_id": 1, "count": 1, "aux_value": 0, "name": first["name"]}
        birch_amount = {"item_id": 2, "count": 2, "aux_value": 0, "name": second["name"]}
        self.assertEqual(final["result"], {
            "recipe": recipe, "available": recipe, "replies": replies, "output": 1, "player_id": 7,
            "plan": {"recipe": recipe, "times": 1, "materials": [[oak_amount], [birch_amount]], "delta": [
                oak_amount | {"count": -1}, birch_amount | {"count": -2},
                {"item_id": 3, "aux_value": 0, "count": 1, "name": "minecraft:crafting_table"},
            ]},
        })


class MCPScriptTradingTests(unittest.IsolatedAsyncioTestCase):
    deliver = test_recipe_auto_tags.RecipeAutoTagTests.deliver
    item = test_recipe_auto_tags.RecipeAutoTagTests.item
    native_recipe = staticmethod(test_trading.TradeManyTests.native_recipe)
    player_slot = staticmethod(test_trading.TradeManyTests.player_slot)
    input_slot = staticmethod(test_trading.TradeManyTests.input_slot)
    start = MCPScriptTests.start
    wait_for = MCPScriptTests.wait_for
    completed = MCPScriptTests.completed

    async def asyncSetUp(self):
        from bedrock_protocol import nbt

        self.enterContext(test_scheduled_input.ManualClock().patch_scheduler())
        self.bot = Bot("TradeFixture", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.wire = await test_scheduled_input.ScheduledInputTests().join(self.bot)
        self.b = b = self.bot.protocol.binding
        self.player = MCPPlayer()
        self.player.bot = self.bot
        self.scripts = MCPScripts(self.player)
        self.addAsyncCleanup(self.scripts.close)
        names = ("minecraft:emerald", "minecraft:book", "minecraft:enchanted_book", "minecraft:stick")
        self.bot._session._replace_item_registry(b.ItemRegistryPacket(items=[
            b.ItemData(id=item_id, name=name) for item_id, name in enumerate(names, 1)
        ]))
        slots = [self.item(1, 18, net_id=100), self.item(2, 2, net_id=200), *(self.item() for _ in range(34))]
        await self.deliver(
            b.InventoryContentPacket(inventory_id=b.ContainerID.INVENTORY, slots=slots),
            b.UpdateTradePacket(container_id=7, type=b.ContainerType.TRADE, size=1, trader_tier=0,
                                entity_unique_id=42, last_trading_player=1, display_name="Librarian",
                                use_new_trade_screen=True, using_economy_trade=True, data=nbt.CompoundTag({
                                    "Recipes": nbt.ListTag([self.native_recipe(123)], int(nbt.Type.COMPOUND)),
                                })),
        )
        self.sent = asyncio.Queue()
        self.addCleanup(self.bot.packets.subscribe_sent(
            lambda packet_id: self.sent.put_nowait(None) if packet_id == b.ItemStackRequestPacket.ID else None,
        ))

    async def request(self, run):
        await self.wait_for(run, lambda _: not self.sent.empty())
        self.sent.get_nowait()
        packet = self.wire.sent[-1]
        self.assertIsInstance(packet, self.b.ItemStackRequestPacket)
        request, = packet.requests
        return request

    async def reply(self, request, *rows, verdict="SUCCESS"):
        b = self.b
        groups = [b.ItemStackResponseContainerInfo(
            full_container_name=b.FullContainerName(name=b.ContainerEnumName[name]),
            slots=[b.ItemStackResponseSlotInfo(requested_slot=slot, slot=slot, amount=amount,
                                               item_stack_net_id=None if net_id is None else b.ItemStackNetId(raw_id=net_id))],
        ) for name, slot, amount, net_id in rows]
        response = b.ItemStackResponseInfo(client_request_id=request.client_request_id, result=b.ItemStackNetResult[verdict],
                                           containers=groups or None)
        await self.deliver(b.ItemStackResponsePacket(responses=[response]))

    async def test_cancelled_trade_keeps_pending_progress_and_late_inventory_on_the_same_connection(self):
        run = await self.start("""
import asyncio

async def main(bot):
    assert bot.trade_offers.offers[0].buy_b.name == 'minecraft:book'
    trade = asyncio.create_task(bot.trade_many(123, 3))
    await bot.wait_for_inventory_item_count(4, 1)
    trade.cancel('script paused the trade')
    try:
        await trade
    except asyncio.CancelledError as error:
        assert error.trade_progress.confirmed_count == 1
        assert error.trade_progress.requests[-1].status == 'unconfirmed'
        raise
""")
        ids = []
        for round_number in (1, 2):
            for ingredient, count, net_id in ((1, 9, 100), (2, 1, 200)):
                request = await self.request(run)
                ids.append(request.client_request_id.raw_id)
                remaining = count if round_number == 1 else 0
                await self.reply(request, self.player_slot(ingredient - 1, remaining, net_id if remaining else None),
                                 self.input_slot(ingredient, count, 500 + ingredient))
            exchange = await self.request(run)
            ids.append(exchange.client_request_id.raw_id)
            destination = exchange.actions[-1].destination.slot
            if round_number == 1:
                await self.reply(exchange, self.input_slot(1, 0), self.input_slot(2, 0),
                                 self.player_slot(destination, 1, 701))
        await self.deliver(self.b.InventorySlotPacket(inventory_id=self.b.ContainerID.INVENTORY, slot=35,
                                                      item=self.item(4, 1, net_id=810)))
        ended = await self.completed(run)
        self.assertEqual(ended["state"], "failed", ended)
        self.assertEqual(ended["error"]["error_type"], "CancelledError")
        self.assertEqual(ended["error"]["message"], "script paused the trade")
        progress = ended["error"]["attributes"]["trade_progress"]
        self.assertEqual((progress["stop_reason"], progress["confirmed_count"], progress["requested"]), ("cancelled", 1, 3))
        self.assertEqual([row["request_id"] for row in progress["requests"]], ids)
        self.assertEqual(progress["requests"][-1], {
            "trade": 2, "request_id": ids[-1], "ingredient": None, "inventory_slot": destination,
            "status": "unconfirmed", "verdict": None,
        })
        self.assertTrue(all(row["status"] == "confirmed" and row["verdict"] == "SUCCESS"
                            for row in progress["requests"][:-1]))
        await self.reply(exchange, self.input_slot(1, 0), self.input_slot(2, 0),
                         self.player_slot(destination, 1, 702))
        self.assertEqual(await self.completed(run), ended)
        self.assertTrue(self.sent.empty())
        self.assertFalse(self.wire.closed)
        self.assertEqual((await self.player.session_status())["state"], "connected")
        next_run = await self.start("""
async def main(bot):
    return [bot.player_id, bot.trade_offers.window_id, bot.inventory_count(3)]
""")
        self.assertEqual((await self.completed(next_run))["result"], [1, 7, 2])

    async def test_trade_timeout_keeps_the_submitted_id_and_unknown_verdict(self):
        run = await self.start("""
async def main(bot):
    return await bot.trade_many(123, 1, timeout=0.4)
""")
        request = await self.request(run)
        ended = await self.completed(run)
        self.assertEqual(ended["state"], "failed", ended)
        self.assertEqual(ended["error"]["error_type"], "TimeoutError")
        self.assertEqual(ended["error"]["attributes"]["trade_progress"], {
            "recipe_net_id": 123, "requested": 1, "window_id": 7, "trader_unique_id": 42,
            "stop_reason": "timeout", "confirmed_count": 0, "requests": [
                {"trade": 1, "request_id": request.client_request_id.raw_id, "ingredient": 1,
                 "inventory_slot": 0, "status": "unconfirmed", "verdict": None},
            ],
        })
        self.assertEqual(self.bot.inventory_count(1), 18)
        self.assertTrue(self.sent.empty())

    async def test_settled_trade_reply_survives_local_cancellation(self):
        run = await self.start("""
import asyncio

async def main(bot):
    channel = bot._channel
    start = channel.start
    replies = []
    def capture(*args, **kwargs):
        started = start(*args, **kwargs)
        replies.append(started[1])
        return started
    channel.start = capture
    trade = asyncio.create_task(bot.trade_many(123, 0))
    await asyncio.sleep(0)
    assert 'value' in replies[0].result(timeout=5)
    trade.cancel('pause after reply')
    await trade
""")
        ended = await self.completed(run)
        self.assertEqual(ended["state"], "failed", ended)
        self.assertEqual(ended["error"], {"error_type": "CancelledError", "message": "pause after reply",
                                         "attributes": {"trade_progress": {
                                             "recipe_net_id": 123, "requested": 0, "window_id": 7, "trader_unique_id": 42,
                                             "stop_reason": "complete", "confirmed_count": 0, "requests": [],
                                         }}})
        self.assertTrue(self.sent.empty())

    async def test_trade_distinguishes_rejection_from_a_step_blocked_before_send(self):
        script = "async def main(bot): return await bot.trade_many(123, 1)"
        rejected = await self.start(script)
        request = await self.request(rejected)
        await self.reply(request, verdict="ERROR")
        observed = (await self.completed(rejected))["result"]
        self.assertEqual((observed["stop_reason"], observed["confirmed_count"]), ("rejected", 0))
        self.assertEqual(observed["requests"], [{
            "trade": 1, "request_id": request.client_request_id.raw_id, "ingredient": 1,
            "inventory_slot": 0, "status": "rejected", "verdict": "ERROR",
        }])

        plan = self.bot._trading._plan

        def invalidate_destination(*args):
            proposal = plan(*args)
            self.bot._inventory._unconfirmed_transfer_slots.add(proposal[1])
            return proposal

        with patch.object(self.bot._trading, "_plan", side_effect=invalidate_destination):
            blocked = await self.start(script)
            observed = (await self.completed(blocked))["result"]
        self.assertEqual((observed["stop_reason"], observed["confirmed_count"]), ("inventory_unobserved", 0))
        self.assertEqual(observed["requests"], [{
            "trade": 1, "request_id": request.client_request_id.raw_id - 2, "ingredient": 1,
            "inventory_slot": 0, "status": "not_sent", "verdict": None,
        }])
        self.assertTrue(self.sent.empty())
        self.assertEqual(self.bot.inventory_count(1), 18)


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


class MCPScriptEntityPlacementTests(test_placement._EntityPlacementHarness):
    async def test_stop_and_connection_cleanup_release_only_the_worker_completion_subscriptions(self):
        scripts = MCPScripts(SimpleNamespace(connected_bot=lambda: self.bot))
        self.addAsyncCleanup(scripts.close)
        registered = self.bot._entity_placement_completion_observers
        originals = registered.copy()

        async def start_subscription():
            run = (await scripts.run_script(source="""
import asyncio

async def main(bot):
    bot.subscribe_entity_placement_completions(lambda event: None)
    print('subscribed', flush=True)
    await asyncio.Future()
"""))["run_id"]
            while True:
                status = await scripts.observe_script(run)
                self.assertEqual(status["state"], "running", status)
                if any("subscribed" in row["text"] for row in status["output"]):
                    break
                await asyncio.sleep(.01)
            self.assertEqual(len(registered), len(originals) + 1)
            return run

        async with asyncio.timeout(12):
            stopped = await start_subscription()
            self.assertEqual((await scripts.stop_script(stopped))["state"], "stopped")
            self.assertEqual(registered, originals)
            disconnected = await start_subscription()
            await scripts.close()
            self.assertEqual((await scripts.observe_script(disconnected))["state"], "stopped")
            self.assertEqual(registered, originals)
