import asyncio
import unittest
from unittest.mock import patch

from endpilot import Bot, LoginRefused
from endpilot.movement import Movement
from endpilot.protocol import read_batch, write_batch
from endpilot.qa import PacketTimeline
from test_join import _online_identity
from test_scheduled_input import Wire


class SpawnEventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Bot("SpawnProbe")
        self.b = self.bot.protocol.binding
        self.addAsyncCleanup(self.bot.close)

    def push(self, wire, *packets):
        wire.incoming.put_nowait(write_batch(map(wire.protocol.encode, packets)))

    def wire(self, *before_spawn, after_spawn=(), spawn=True):
        b = self.b
        wire = Wire(self.bot.protocol)
        wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        start = b.StartGamePacket(runtime_id=42, entity_id=7)
        status = (b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),) if spawn else ()
        self.push(wire, start, *before_spawn, *status, *after_spawn)
        return wire

    def actor(self, event, runtime_id=42):
        return self.b.ActorEventPacket(runtime_id=runtime_id, event_id=event)

    def respawn_packet(self, state):
        b = self.b
        return b.RespawnPacket(runtime_id=0, state=state, pos=b.Vec3(x=1.25, y=65.0, z=-2.0))

    async def connect(self, wire, *, online=False, timeline=None):
        with patch("endpilot.transport.nethernet.connect", return_value=wire):
            if online:
                with patch("endpilot.bot.acquire_online_identity", return_value=_online_identity()):
                    await self.bot.join_nethernet(
                        "http://127.0.0.1:19132", timeout=1, credential_provider=object(), timeline=timeline,
                    )
            else:
                await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1, timeline=timeline)

    async def processed(self, wire, *packets):
        waiter = asyncio.create_task(self.bot.packets.wait_for_packet(self.b.SetHealthPacket.ID, timeout=1))
        await asyncio.sleep(0)
        self.push(wire, *packets, self.b.SetHealthPacket(health=20))
        await waiter

    async def test_initial_waits_for_initialize_and_precedes_later_packets_in_the_spawn_batch(self):
        b = self.b
        loop = asyncio.get_running_loop()
        original_factory = loop.get_task_factory()
        self.addCleanup(loop.set_task_factory, original_factory)
        for eager, online in ((False, False), (True, False), (False, True)):
            with self.subTest(eager=eager, online=online):
                loop.set_task_factory(asyncio.eager_task_factory if eager else original_factory)
                wire = self.wire(
                    self.respawn_packet(b.PlayerRespawnState.READY_TO_SPAWN),
                    self.actor(b.ActorEvent.DEATH), b.SetHealthPacket(health=17),
                    after_spawn=(
                        self.actor(b.ActorEvent.SPAWN_ALIVE),
                        b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
                        b.SetHealthPacket(health=14), self.actor(b.ActorEvent.DEATH),
                        self.actor(b.ActorEvent.SPAWN_ALIVE), b.SetHealthPacket(health=15),
                    ),
                )
                entered, allow_send, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
                original_send = wire.send
                events = []

                async def pause_initialize(batch):
                    if any(raw.packet_id == b.SetLocalPlayerAsInitializedPacket.ID for raw in read_batch(batch)):
                        entered.set()
                        await allow_send.wait()
                    await original_send(batch)

                def observe_health(raw):
                    if raw.packet_id == b.SetHealthPacket.ID:
                        events.append(("health", self.bot.health))
                        if self.bot.health == 15:
                            drained.set()

                def spawned(kind):
                    if kind == "initial":
                        self.assertIsNotNone(self.bot.scoreboard)
                        self.assertEqual(
                            [packet.player_id for packet in wire.sent
                             if isinstance(packet, b.SetLocalPlayerAsInitializedPacket)], [42],
                        )
                        self.bot.packets.subscribe_raw(observe_health)
                    events.append((kind, self.bot.health))

                unsubscribe = self.bot.subscribe_spawns(spawned)
                with patch.object(wire, "send", new=pause_initialize):
                    joining = asyncio.create_task(self.connect(wire, online=online))
                    try:
                        await asyncio.wait_for(entered.wait(), 1)
                        self.assertEqual(events, [])
                        allow_send.set()
                        await joining
                        await asyncio.wait_for(drained.wait(), 1)
                        self.assertEqual(events, [
                            ("initial", 17.0), ("health", 14.0), ("respawn", 14.0), ("health", 15.0),
                        ])
                    finally:
                        unsubscribe()
                        joining.cancel()
                        await asyncio.gather(joining, return_exceptions=True)
                        await self.bot.close()

    async def test_initial_precedes_first_read_and_buffered_disconnect_observations(self):
        b = self.b
        loop = asyncio.get_running_loop()
        original_factory = loop.get_task_factory()
        self.addCleanup(loop.set_task_factory, original_factory)
        for eager, online in ((False, True), (True, False), (True, True)):
            loop.set_task_factory(asyncio.eager_task_factory if eager else original_factory)
            for eof in (False, True):
                with self.subTest(eager=eager, online=online, eof=eof):
                    timeline = PacketTimeline()
                    wire = self.wire(after_spawn=() if eof else (b.DisconnectPacket(),))
                    if eof:
                        wire.incoming.put_nowait(EOFError("first play read"))
                    observed = []

                    def spawned(kind):
                        observed.append((
                            kind, self.bot.session_end, timeline.received_count("DisconnectPacket"),
                            timeline.disconnect_reason(), wire.incoming.qsize(),
                        ))

                    unsubscribe = self.bot.subscribe_spawns(spawned)
                    try:
                        await self.connect(wire, online=online, timeline=timeline)
                        self.assertEqual(observed, [("initial", None, 0, None, int(eof))])
                        end = await self.bot.wait_for_session_end(timeout=1)
                        if eof:
                            self.assertEqual((end.kind, end.error_type), ("reader_end", "EOFError"))
                        else:
                            self.assertEqual(end.kind, "server_disconnect")
                            self.assertEqual(timeline.received_count("DisconnectPacket"), 1)
                    finally:
                        unsubscribe()
                        if eof:
                            with self.assertRaisesRegex(EOFError, "first play read"):
                                await self.bot.close()
                        else:
                            await self.bot.close()

    async def test_cancelled_online_assembly_leaves_post_spawn_packets_unread(self):
        b = self.b
        timeline = PacketTimeline()
        wire = self.wire(after_spawn=(b.DisconnectPacket(),))
        events = []
        self.bot.subscribe_spawns(events.append)
        original_start = self.bot._start_session

        def cancel_after_assembly(session, **options):
            original_start(session, **options)
            joining.cancel()

        with patch.object(self.bot, "_start_session", new=cancel_after_assembly):
            joining = asyncio.create_task(self.connect(wire, online=True, timeline=timeline))
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(joining, 1)
        self.assertTrue(wire.closed)
        self.assertEqual(events, [])
        self.assertEqual(timeline.received_count("DisconnectPacket"), 0)

    async def test_two_deaths_in_one_batch_ignore_duplicates_and_other_actors(self):
        b = self.b
        wire = self.wire()
        await self.connect(wire)
        events = []
        self.bot.subscribe_spawns(lambda kind: events.append((kind, self.bot.health)))
        self.assertEqual(events, [])
        death, alive = b.ActorEvent.DEATH, b.ActorEvent.SPAWN_ALIVE
        await self.processed(
            wire, self.actor(alive), self.actor(death, 43), self.actor(alive, 43),
            b.SetHealthPacket(health=0), b.SetHealthPacket(health=18), self.actor(alive),
            b.ChangeDimensionPacket(dimension_id=1, loading_screen_id=9, pos=b.Vec3(x=1.0, y=64.0, z=2.0)),
            b.PlayerActionPacket(action=b.PlayerActionType.CHANGE_DIMENSION_ACK, runtime_id=42),
            self.actor(alive), self.actor(death), self.actor(death), self.actor(alive, 43),
            b.SetHealthPacket(health=11), self.actor(alive), self.actor(alive),
            self.actor(death), b.SetHealthPacket(health=12), self.actor(alive), self.actor(alive),
        )
        self.assertEqual(events, [("respawn", 11.0), ("respawn", 12.0)])

    async def test_subscriptions_are_independent_reentrant_and_isolate_callback_errors(self):
        b = self.b
        observed, late, once = [], [], []
        duplicate = observed.append

        def first(kind):
            once.append(kind)
            remove_first()
            remove_first()
            self.bot.subscribe_spawns(late.append)

        def fails(kind):
            if kind == "initial":
                raise ValueError("broken initial observer")
            raise asyncio.CancelledError("broken respawn observer")

        remove_first = self.bot.subscribe_spawns(first)
        self.bot.subscribe_spawns(fails)
        remove_duplicate = self.bot.subscribe_spawns(duplicate)
        self.bot.subscribe_spawns(duplicate)
        wire = self.wire()
        with patch.object(asyncio.get_running_loop(), "call_exception_handler") as report:
            await self.connect(wire)
            self.assertEqual(observed, ["initial", "initial"])
            self.assertEqual(late, [])
            remove_duplicate()
            remove_duplicate()
            await self.processed(wire, self.actor(b.ActorEvent.DEATH), self.actor(b.ActorEvent.SPAWN_ALIVE))
        self.assertEqual(once, ["initial"])
        self.assertEqual(observed, ["initial", "initial", "respawn"])
        self.assertEqual(late, ["respawn"])
        self.assertEqual([type(call.args[0]["exception"]) for call in report.call_args_list],
                         [ValueError, asyncio.CancelledError])

    async def test_close_and_rejoin_during_old_reader_drain_keep_death_state_local(self):
        b = self.b
        events = []
        self.bot.subscribe_spawns(events.append)
        first = self.wire()
        await self.connect(first)
        await self.processed(first, self.actor(b.ActorEvent.DEATH))
        old_packets = self.bot.packets
        old_state = self.bot._player_state
        detached, release = asyncio.Event(), asyncio.Event()
        original_drain = old_state.aclose

        async def pause_drain():
            detached.set()
            await release.wait()
            await original_drain()

        with patch.object(old_state, "aclose", new=pause_drain):
            closing = asyncio.create_task(self.bot.close())
            try:
                await asyncio.wait_for(detached.wait(), 1)
                replacement = self.wire()
                await self.connect(replacement)
                old_observation = asyncio.create_task(old_packets.wait_for_packet(b.ActorEventPacket.ID, timeout=1))
                await asyncio.sleep(0)
                first.push(self.actor(b.ActorEvent.SPAWN_ALIVE))
                await old_observation
                await self.processed(replacement, self.actor(b.ActorEvent.SPAWN_ALIVE))
                self.assertEqual(events, ["initial", "initial"])
                await self.processed(replacement, self.actor(b.ActorEvent.DEATH), self.actor(b.ActorEvent.SPAWN_ALIVE))
                self.assertEqual(events, ["initial", "initial", "respawn"])
            finally:
                release.set()
                await closing
        self.assertTrue(first.closed)
        self.assertFalse(replacement.closed)

    async def test_failed_or_refused_joins_do_not_publish_and_subscription_survives(self):
        b = self.b
        events = []
        self.bot.subscribe_spawns(events.append)
        refused = self.wire(self.actor(b.ActorEvent.DEATH), b.DisconnectPacket(), spawn=False)
        with self.assertRaises(LoginRefused):
            await self.connect(refused)
        self.assertTrue(refused.closed)
        self.assertEqual(events, [])

        unsent = self.wire()
        original_send = unsent.send

        async def fail_initialize(batch):
            if any(raw.packet_id == b.SetLocalPlayerAsInitializedPacket.ID for raw in read_batch(batch)):
                raise OSError("initialize send failed")
            await original_send(batch)

        with patch.object(unsent, "send", new=fail_initialize):
            with self.assertRaisesRegex(OSError, "initialize send failed"):
                await self.connect(unsent)
        self.assertTrue(unsent.closed)
        self.assertEqual(events, [])

        unassembled = self.wire(after_spawn=(self.actor(b.ActorEvent.DEATH), self.actor(b.ActorEvent.SPAWN_ALIVE)))
        original_start = Movement.start

        def fail_after_start(movement, **options):
            original_start(movement, **options)
            raise RuntimeError("assembly failed")

        with patch.object(Movement, "start", new=fail_after_start):
            with self.assertRaisesRegex(RuntimeError, "assembly failed"):
                await self.connect(unassembled)
        self.assertTrue(unassembled.closed)
        self.assertEqual(events, [])
        await self.connect(self.wire())
        self.assertEqual(events, ["initial"])

    async def test_manual_and_automatic_handshakes_publish_only_on_spawn_alive(self):
        b = self.b
        states = b.PlayerRespawnState
        for automatic in (False, True):
            with self.subTest(automatic=automatic):
                self.bot = Bot("SpawnProbe", auto_respawn=automatic)
                self.addAsyncCleanup(self.bot.close)
                wire = self.wire()
                events = []
                self.bot.subscribe_spawns(events.append)
                await self.connect(wire)
                sent = asyncio.Queue()
                self.bot.packets.subscribe_sent(
                    lambda packet_id: sent.put_nowait(packet_id)
                    if packet_id in (b.RespawnPacket.ID, b.PlayerActionPacket.ID) else None,
                )
                manual = None if automatic else asyncio.create_task(self.bot.respawn(timeout=1))
                try:
                    await self.processed(wire, self.actor(b.ActorEvent.DEATH),
                                         self.respawn_packet(states.SEARCHING_FOR_SPAWN))
                    self.assertEqual(await asyncio.wait_for(sent.get(), 1), b.RespawnPacket.ID)
                    self.assertEqual(events, ["initial"])
                    await self.processed(wire, self.respawn_packet(states.READY_TO_SPAWN))
                    self.assertEqual(await asyncio.wait_for(sent.get(), 1), b.PlayerActionPacket.ID)
                    if manual is not None:
                        self.assertEqual(await manual, (1.25, 65.0, -2.0))
                    self.assertEqual(events, ["initial"])
                    await self.processed(wire, self.actor(b.ActorEvent.SPAWN_ALIVE))
                    self.assertEqual(events, ["initial", "respawn"])
                finally:
                    if manual is not None:
                        manual.cancel()
                        await asyncio.gather(manual, return_exceptions=True)
                    await self.bot.close()
