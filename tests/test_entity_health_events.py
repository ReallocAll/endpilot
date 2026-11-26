"""Exercise remote actor observations through a joined Bot."""

import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from endpilot import Bot, EntityActorEvent, EntityHealthEvent
from endpilot.protocol import write_batch
from test_scheduled_input import ManualClock, Wire


class EntityHealthEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("HealthWatch", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)
        for packet in (
            b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
            b.StartGamePacket(runtime_id=7, entity_id=17),
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        ):
            self.wire.push(packet)
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132")

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket(time=23)
        ready = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in (*packets, marker)))
        await ready

    def spawn(self, runtime_id=8, entity_id=108):
        b = self.b
        return b.AddActorPacket(runtime_id=runtime_id, entity_id=entity_id, actor_type="minecraft:zombie",
                                pos=b.Vec3(x=1.5, y=100, z=1.5))

    def actor_event(self, event="HURT", runtime_id=8, data=0, fire_at_position=None):
        if isinstance(event, str):
            event = self.b.ActorEvent[event]
        return self.b.ActorEventPacket(runtime_id=runtime_id, event_id=event, data=data,
                                       fire_at_position=fire_at_position)

    async def arm(self, wait_for_event=None, **filters):
        wait_for_event = wait_for_event or self.bot.wait_for_entity_health_event
        wait = asyncio.create_task(wait_for_event(timeout=1, **filters))
        self.addAsyncCleanup(self.cancel, wait)
        await asyncio.sleep(0)
        return wait

    @staticmethod
    async def cancel(wait):
        wait.cancel()
        await asyncio.gather(wait, return_exceptions=True)

    async def test_actor_events_preserve_codec_fields_order_and_packet_time_state(self):
        b = self.b
        eat_grass = b.ActorEvent.EAT_GRASS
        await self.deliver(self.spawn(), self.spawn(9, 109))
        actors = {actor.runtime_id: actor for actor in self.bot.observed_entities}
        original, other = actors[8], actors[9]
        beginning = len(self.wire.sent)
        seen, eating, unknown, health = [], [], [], []
        self.bot.subscribe_entity_actor_events(seen.append)
        self.bot.subscribe_entity_actor_events(eating.append, runtime_id=8, event_id=eat_grass)
        self.bot.subscribe_entity_actor_events(unknown.append, event_id=255)
        self.bot.subscribe_entity_health_events(health.append)
        wait_for_actor_event = self.bot.wait_for_entity_actor_event
        first_eat = await self.arm(wait_for_actor_event, runtime_id=8, event_id=eat_grass)
        first_unknown = await self.arm(wait_for_actor_event, event_id=255)

        await self.deliver(self.actor_event(255, runtime_id=99), self.actor_event(255, runtime_id=7),
                           self.actor_event(eat_grass, runtime_id=9, data=10), self.actor_event(data=12))
        self.assertFalse(first_eat.done())
        self.assertFalse(first_unknown.done())
        self.assertEqual(eating, [])
        self.assertEqual(unknown, [])

        position = (2.25, -4.5, 12.75)
        first = self.actor_event(eat_grass, data=-13, fire_at_position=b.Vec3(x=2.25, y=-4.5, z=12.75))
        move = b.MoveActorAbsolutePacket(move_data=b.MoveActorAbsoluteData(
            runtime_id=8, pos=b.Vec3(x=3.5, y=100, z=1.5),
        ))
        await self.deliver(first, first, move, self.actor_event(eat_grass), self.actor_event(255, data=-128))
        current = next(actor for actor in self.bot.observed_entities if actor.runtime_id == 8)
        self.assertEqual(current.position, (3.5, 100.0, 1.5))
        first_event = EntityActorEvent(original, eat_grass, -13, position)
        later_event = EntityActorEvent(current, eat_grass, 0, None)
        unknown_event = EntityActorEvent(current, 255, -128, None)
        self.assertEqual(seen, [EntityActorEvent(other, eat_grass, 10, None),
                                EntityActorEvent(original, b.ActorEvent.HURT, 12, None),
                                first_event, first_event, later_event, unknown_event])
        self.assertEqual(eating, [first_event, first_event, later_event])
        self.assertEqual(unknown, [unknown_event])
        self.assertEqual(await first_eat, first_event)
        self.assertEqual(await first_unknown, unknown_event)
        self.assertEqual(health, [EntityHealthEvent("hurt", original)])
        self.assertEqual(seen[2].entity.position, (1.5, 100.0, 1.5))
        with self.assertRaises(FrozenInstanceError):
            seen[2].fire_at_position = None
        self.assertEqual(len(self.wire.sent), beginning)

    async def test_actor_event_filters_end_when_the_tracked_instance_ends(self):
        eat_grass = self.b.ActorEvent.EAT_GRASS
        await self.deliver(self.spawn())
        old, seen = [], []
        subscribe = self.bot.subscribe_entity_actor_events
        wait_for_actor_event = self.bot.wait_for_entity_actor_event
        subscribe(old.append, runtime_id=8, event_id=eat_grass)
        subscribe(seen.append, event_id=eat_grass)
        waiting = await self.arm(wait_for_actor_event, runtime_id=8, event_id=eat_grass)
        await self.deliver(self.b.RemoveActorPacket(entity_id=108), self.actor_event(eat_grass))
        self.assertTrue(waiting.done())
        with self.assertRaisesRegex(LookupError, "no longer observed"):
            await waiting
        self.assertEqual(seen, [])

        await self.deliver(self.spawn(entity_id=109), self.actor_event(eat_grass))
        displaced, replacement = [], []
        subscribe(displaced.append, runtime_id=8)
        waiting = await self.arm(wait_for_actor_event, runtime_id=8)
        global_wait = await self.arm(wait_for_actor_event, event_id=eat_grass)
        await self.deliver(self.spawn(entity_id=110), self.actor_event(eat_grass))
        self.assertTrue(waiting.done())
        with self.assertRaisesRegex(LookupError, "no longer observed"):
            await waiting
        self.assertEqual(old, [])
        self.assertEqual(displaced, [])
        self.assertEqual([event.entity.entity_id for event in seen], [109, 110])
        self.assertEqual(await global_wait, seen[-1])

        subscribe(replacement.append, runtime_id=8)
        await self.deliver(self.actor_event(eat_grass))
        self.assertEqual(replacement, [seen[-1]])
        self.assertEqual(seen[-1].entity.entity_id, 110)

    async def test_supported_remote_events_filter_and_keep_packet_time_state(self):
        b = self.b
        await self.deliver(self.spawn(), self.spawn(9, 109), self.actor_event())
        beginning = len(self.wire.sent)
        seen, deaths = [], []
        self.bot.subscribe_entity_health_events(seen.append)
        self.bot.subscribe_entity_health_events(deaths.append, kind="death", runtime_id=8)
        hurt = await self.arm(kind="hurt", runtime_id=8)
        death = await self.arm(kind="death", runtime_id=8)
        await self.deliver(self.actor_event(runtime_id=99), self.actor_event(runtime_id=7),
                           self.actor_event("DEATH", 7), self.actor_event("SPAWN_ALIVE"), self.actor_event("DEATH", 9))
        self.assertFalse(hurt.done())
        self.assertFalse(death.done())
        self.assertEqual(deaths, [])

        original = next(actor for actor in self.bot.observed_entities if actor.runtime_id == 8)
        move = b.MoveActorAbsolutePacket(move_data=b.MoveActorAbsoluteData(
            runtime_id=8, pos=b.Vec3(x=3.5, y=100, z=1.5),
        ))
        await self.deliver(self.actor_event(data=2), move, self.actor_event("DEATH"))
        current = next(actor for actor in self.bot.observed_entities if actor.runtime_id == 8)
        self.assertEqual(current.position, (3.5, 100.0, 1.5))
        self.assertEqual(await hurt, EntityHealthEvent("hurt", original))
        self.assertEqual(await death, EntityHealthEvent("death", current))
        self.assertEqual(deaths, [await death])
        self.assertEqual([(event.kind, event.entity.runtime_id) for event in seen],
                         [("death", 9), ("hurt", 8), ("death", 8)])
        self.assertEqual(seen[1].entity.position, (1.5, 100.0, 1.5))
        with self.assertRaises(FrozenInstanceError):
            seen[1].kind = "death"
        with self.assertRaises(FrozenInstanceError):
            seen[1].entity.position = (0, 0, 0)
        self.assertEqual(len(self.wire.sent), beginning)

    async def test_filters_reject_wrong_kind_own_and_unobserved_runtime_ids(self):
        for filters, error in (({"kind": "HURT"}, ValueError), ({"runtime_id": 7}, LookupError),
                               ({"runtime_id": 8}, LookupError)):
            with self.subTest(filters=filters):
                with self.assertRaises(error):
                    self.bot.subscribe_entity_health_events(self.fail, **filters)
                with self.assertRaises(error):
                    await self.bot.wait_for_entity_health_event(timeout=0, **filters)

    async def test_attack_submission_does_not_deliver_an_event(self):
        b = self.b
        await self.deliver(self.spawn(), b.MovePlayerPacket(
            player_id=7, pos=b.Vec3(y=101.62), reset_position=2, on_ground=True,
        ))
        seen = []
        self.bot.subscribe_entity_health_events(seen.append)
        waiting = await self.arm()
        beginning = len(self.wire.sent)
        await asyncio.wait_for(self.bot.attack_entity(8), timeout=1)
        await self.deliver()
        transaction, = self.wire.sent[beginning:]
        self.assertIsInstance(transaction, self.b.InventoryTransactionPacket)
        self.assertFalse(waiting.done())
        self.assertEqual(seen, [])
        await self.deliver(self.actor_event())
        self.assertEqual(seen, [await waiting])

    async def test_removal_and_reuse_end_the_previous_filtered_instance(self):
        await self.deliver(self.spawn())
        old, seen = [], []
        self.bot.subscribe_entity_health_events(old.append, runtime_id=8)
        self.bot.subscribe_entity_health_events(seen.append)
        waiting = await self.arm(runtime_id=8)
        await self.deliver(self.b.RemoveActorPacket(entity_id=108), self.actor_event("DEATH"),
                           self.spawn(entity_id=109), self.actor_event())
        with self.assertRaisesRegex(LookupError, "no longer observed"):
            await waiting
        self.assertEqual(old, [])
        self.assertEqual([(event.kind, event.entity.entity_id) for event in seen], [("hurt", 109)])

        displaced = await self.arm(runtime_id=8)
        self.bot.subscribe_entity_health_events(old.append, runtime_id=8)
        await self.deliver(self.spawn(entity_id=110), self.actor_event("DEATH"))
        with self.assertRaises(LookupError):
            await displaced
        self.assertEqual(old, [])
        self.assertEqual(seen[-1].entity.entity_id, 110)

    async def test_dimension_change_ends_all_waits_and_only_runtime_filtered_subscriptions(self):
        await self.deliver(self.spawn())
        seen, filtered = [], []
        self.bot.subscribe_entity_health_events(seen.append, kind="hurt")
        self.bot.subscribe_entity_health_events(filtered.append, runtime_id=8)
        waits = (await self.arm(), await self.arm(runtime_id=8))
        await self.deliver(self.b.ChangeDimensionPacket(dimension_id=1), self.actor_event(),
                           self.spawn(entity_id=109), self.actor_event("DEATH"), self.actor_event())
        for waiting in waits:
            with self.assertRaisesRegex(RuntimeError, "dimension changed"):
                await waiting
        self.assertEqual(filtered, [])
        self.assertEqual([(event.kind, event.entity.entity_id) for event in seen], [("hurt", 109)])

    async def test_subscription_from_link_cleanup_cannot_follow_reused_runtime_id(self):
        b = self.b
        for dimension_change in (False, True):
            with self.subTest(dimension_change=dimension_change):
                await self.deliver(self.spawn(), b.SetActorLinkPacket(
                    link=b.ActorLink(a=108, b=17, type=b.ActorLinkType.RIDING),
                ))
                health, actor_events, waits = [], [], []

                def register(change):
                    if change.reason == "unobserved":
                        loop = asyncio.get_running_loop()
                        for subscribe, wait_for_event, seen in (
                            (self.bot.subscribe_entity_health_events, self.bot.wait_for_entity_health_event, health),
                            (self.bot.subscribe_entity_actor_events, self.bot.wait_for_entity_actor_event, actor_events),
                        ):
                            subscribe(seen.append, runtime_id=8)
                            waiting = asyncio.eager_task_factory(loop, wait_for_event(runtime_id=8, timeout=1))
                            self.addAsyncCleanup(self.cancel, waiting)
                            waits.append(waiting)

                stop_links = self.bot.subscribe_vehicle_links(register)
                try:
                    if dimension_change:
                        await self.deliver(b.ChangeDimensionPacket(dimension_id=1))
                    await self.deliver(self.spawn(entity_id=109), self.actor_event())
                finally:
                    stop_links()
                self.assertEqual(len(waits), 2)
                for waiting in waits:
                    self.assertTrue(waiting.done())
                    with self.assertRaises(RuntimeError if dimension_change else LookupError):
                        await waiting
                self.assertEqual(health, [])
                self.assertEqual(actor_events, [])

    async def test_callback_failure_timeout_cancellation_and_unsubscribe_leave_stream_running(self):
        await self.deliver(self.spawn())
        seen, errors = [], []
        loop = asyncio.get_running_loop()
        self.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
        loop.set_exception_handler(lambda _loop, details: errors.append(details["exception"]))

        def fail(_):
            raise asyncio.CancelledError("synthetic callback failure")

        unsubscribe = self.bot.subscribe_entity_health_events(fail)
        self.bot.subscribe_entity_health_events(seen.append)
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_entity_health_event(timeout=0)
        cancelled = await self.arm()
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        waiting = await self.arm()
        await self.deliver(self.actor_event())
        self.assertEqual(seen, [await waiting])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], asyncio.CancelledError)
        unsubscribe()
        unsubscribe()
        await self.deliver(self.actor_event())
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(seen), 2)

    async def test_disconnect_ends_subscriptions_and_waits(self):
        await self.deliver(self.spawn())
        seen = []
        unsubscribe = self.bot.subscribe_entity_health_events(seen.append)
        waiting = await self.arm(runtime_id=8)
        await self.deliver(self.b.DisconnectPacket(), self.actor_event())
        with self.assertRaises(ConnectionError):
            await waiting
        self.assertEqual(seen, [])
        with self.assertRaises(ConnectionError):
            self.bot.subscribe_entity_health_events(seen.append)
        unsubscribe()

    async def test_reader_failure_ends_wait_and_preserves_cause(self):
        waits = (await self.arm(), await self.arm(self.bot.wait_for_entity_actor_event))
        cause = OSError("synthetic receive failure")
        self.wire.incoming.put_nowait(cause)
        for waiting in waits:
            with self.assertRaises(ConnectionError) as caught:
                await waiting
            self.assertIs(caught.exception.__cause__, cause)
        with self.assertRaises(OSError):
            await self.bot.close()
