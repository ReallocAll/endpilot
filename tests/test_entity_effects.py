"""Observe remote entity effects through the native packet codec and Bot API."""

import asyncio
from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import patch
from uuid import UUID

from endpilot import Bot, EntityEffectUpdate, EntityVisibleEffectUpdate, PlayerEffect
from endpilot.protocol import write_batch
from test_scheduled_input import ManualClock, Wire


class EntityEffectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("EffectWatch", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)

    async def join(self, *, before_start=(), before_spawn=()):
        b = self.b
        self.wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.feed(*before_start, b.StartGamePacket(runtime_id=7, entity_id=17), *before_spawn,
                  b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    def feed(self, *packets):
        self.wire.incoming.put_nowait(write_batch(map(self.bot.protocol.encode, packets)))

    async def deliver(self, *packets):
        marker = asyncio.create_task(self.bot.packets.wait_for_packet(self.b.SetTimePacket.ID, timeout=1))
        await asyncio.sleep(0)
        self.feed(*packets, self.b.SetTimePacket())
        await marker

    def sheep(self, entity_id=108):
        return self.b.AddActorPacket(runtime_id=8, entity_id=entity_id, actor_type="minecraft:sheep",
                                     pos=self.b.Vec3(x=1.5, y=100, z=1.5))

    def effect(self, action, effect_id=1, *, runtime_id=8, duration=0, amplifier=0,
               particles=False, ambient=False, tick=0):
        packet = self.b.MobEffectPacket
        event = packet.Event[action] if isinstance(action, str) else action
        return packet(runtime_id=runtime_id, event_id=event, effect_id=effect_id,
                      effect_duration_ticks=duration, effect_amplifier=amplifier,
                      show_particles=particles, ambient=ambient, tick=tick)

    def visible_effect_metadata(self, value, *, typed=True):
        b = self.b
        if value is None:
            return b.SynchedActorData.CopyableDataList()
        payload = b.DataItemInt64Payload if typed else b.DataItemIntPayload
        item = b.DataItemEntry(id=int(b.ActorDataIDs.VISIBLE_MOB_EFFECTS),
                               payload=payload(value=value))
        return b.SynchedActorData.CopyableDataList(data=[item])

    def visible_effects(self, value, *, runtime_id=8, tick=0, typed=True):
        return self.b.SetActorDataPacket(id=runtime_id, tick=tick,
                                         packed_items=self.visible_effect_metadata(value, typed=typed))

    def entity(self, runtime_id=8):
        return next(entity for entity in self.bot.observed_entities if entity.runtime_id == runtime_id)

    async def arm(self, runtime_id=None):
        task = asyncio.create_task(self.bot.wait_for_entity_effect_update(runtime_id=runtime_id, timeout=1))
        self.addAsyncCleanup(self.cancel, task)
        await asyncio.sleep(0)
        return task

    @staticmethod
    async def cancel(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def arm_visible(self, runtime_id):
        task = asyncio.create_task(self.bot.wait_for_entity_visible_effect_change(runtime_id=runtime_id, timeout=1))
        self.addAsyncCleanup(self.cancel, task)
        await asyncio.sleep(0)
        return task

    async def test_visible_metadata_publishes_committed_changes_independently_of_mob_effect(self):
        await self.join()
        seen, states_at_dispatch, mob_effects = [], [], []

        def record(update):
            seen.append(update)
            states_at_dispatch.append(self.entity(update.entity.runtime_id))

        self.bot.subscribe_entity_visible_effect_changes(record)
        self.bot.subscribe_entity_effect_updates(mob_effects.append)
        sheep = self.sheep()
        sheep.data = self.visible_effect_metadata(24)
        await self.deliver(sheep)
        initial = self.entity()
        self.assertEqual((initial.visible_effects_value, initial.status_effects), (24, ()))
        self.assertEqual(seen, [])
        waiting = await self.arm_visible(8)

        await self.deliver(
            self.effect("ADD", duration=60, particles=True),
            self.visible_effects(None), self.visible_effects(0, typed=False),
            self.visible_effects(0, runtime_id=7), self.visible_effects(0, runtime_id=99),
        )
        speed = PlayerEffect(1, 0, 60, True, False)
        self.assertEqual(self.entity(), replace(initial, status_effects=(speed,)))
        self.assertEqual(seen, [])
        self.assertFalse(waiting.done())

        tick = 0x1_0000_0006
        await self.deliver(
            self.visible_effects(280, tick=tick), self.visible_effects(280, tick=tick + 1),
            self.effect("REMOVE", particles=True),
            self.visible_effects(24, tick=tick + 1), self.visible_effects(0, tick=tick + 1),
        )
        combined = replace(initial, visible_effects_value=280, status_effects=(speed,))
        cleared = replace(initial, visible_effects_value=0)
        self.assertEqual(seen, [EntityVisibleEffectUpdate(combined, tick),
                                EntityVisibleEffectUpdate(initial, tick + 1),
                                EntityVisibleEffectUpdate(cleared, tick + 1)])
        for update, committed in zip(seen, states_at_dispatch, strict=True):
            self.assertIs(update.entity, committed)
        self.assertEqual(await waiting, seen[0])
        self.assertEqual(self.entity(), cleared)
        self.assertEqual([(event.action, event.entity.visible_effects_value) for event in mob_effects],
                         [("add", 24), ("remove", 280)])

    async def test_visible_effect_spawn_baseline_and_scoped_wait_follow_the_entity_instance(self):
        await self.join()
        b = self.b
        seen, old_instance = [], []
        subscribe = self.bot.subscribe_entity_visible_effect_changes
        subscribe(seen.append)
        await self.deliver(b.AddPlayerPacket(
            uuid=UUID(int=108), runtime_id=8, name="Effects peer", unpack=self.visible_effect_metadata(24),
            abilities_data=b.SerializedAbilitiesData(target_player=108),
        ))
        self.assertEqual(self.entity().visible_effects_value, 24)
        self.assertEqual(seen, [])
        subscribe(old_instance.append, runtime_id=8)
        waiting = await self.arm_visible(8)

        await self.deliver(self.sheep(109))
        with self.assertRaises(LookupError):
            await waiting
        self.assertIsNone(self.entity().visible_effects_value)
        waiting = await self.arm_visible(8)
        signed = -(1 << 40) + 24
        await self.deliver(self.visible_effects(signed, tick=15))
        current = self.entity()
        self.assertEqual((current.entity_id, current.visible_effects_value), (109, signed))
        self.assertEqual(seen, [EntityVisibleEffectUpdate(current, 15)])
        self.assertEqual(await waiting, seen[0])
        self.assertEqual(old_instance, [])

    async def test_login_replays_effects_in_wire_order_across_start_game(self):
        await self.join(
            before_start=(
                self.sheep(),
                self.effect("ADD", 13, duration=73),
                self.b.RemoveActorPacket(entity_id=108),
                self.effect("ADD", 14, duration=74),
                self.sheep(109),
                self.effect("ADD", 10, duration=20),
                self.visible_effects(24),
                self.effect("UPDATE", 10, duration=80, amplifier=1),
                self.effect("ADD", 2, duration=50),
            ),
            before_spawn=(
                self.effect("REMOVE", 2),
                self.effect("ADD", 5, duration=300, ambient=True),
                self.visible_effects(0),
                self.effect("ADD", runtime_id=7, duration=40, particles=True),
            ),
        )
        sheep, = self.bot.observed_entities
        self.assertEqual(sheep.entity_id, 109)
        self.assertEqual(sheep.status_effects, (PlayerEffect(5, 0, 300, False, True),
                                                PlayerEffect(10, 1, 80, False, False)))
        self.assertEqual(self.bot.status_effects, (PlayerEffect(1, 0, 40, True, False),))

    async def test_same_batch_updates_publish_immutable_post_packet_snapshots_in_order(self):
        await self.join(before_spawn=(self.sheep(),))
        sheep = self.entity()
        seen, states_at_dispatch = [], []

        def record(update):
            seen.append(update)
            states_at_dispatch.append(self.entity())

        self.bot.subscribe_entity_effect_updates(record)
        early_tick = 0x1_0000_0006
        refresh = self.effect("UPDATE", 10, duration=-1, amplifier=2, ambient=True, tick=9)
        move = self.b.MoveActorAbsolutePacket(move_data=self.b.MoveActorAbsoluteData(
            runtime_id=8, pos=self.b.Vec3(x=3.5, y=100, z=1.5),
        ))
        await self.deliver(
            self.effect("ADD", 10, duration=120, particles=True, tick=early_tick),
            self.effect("UPDATE", 2, duration=60, tick=7),
            self.effect("ADD", 10, duration=125, tick=8),
            move, refresh, refresh,
            self.effect("REMOVE", 12, duration=17, tick=10),
            self.effect("REMOVE", 2, duration=23, amplifier=4, particles=True, ambient=True, tick=11),
            self.effect("REMOVE", 10, tick=12),
        )

        large = PlayerEffect(10, 0, 120, True, False)
        small = PlayerEffect(2, 0, 60, False, False)
        reapplied = PlayerEffect(10, 0, 125, False, False)
        unlimited = PlayerEffect(10, 2, -1, False, True)
        moved = replace(sheep, position=(3.5, 100, 1.5))
        refreshed = replace(moved, status_effects=(small, unlimited))
        update = EntityEffectUpdate(entity=refreshed, action="update", effect=unlimited, tick=9)
        self.assertEqual(seen, [
            EntityEffectUpdate(replace(sheep, status_effects=(large,)), "add", large, early_tick),
            EntityEffectUpdate(replace(sheep, status_effects=(small, large)), "update", small, 7),
            EntityEffectUpdate(replace(sheep, status_effects=(small, reapplied)), "add", reapplied, 8),
            update, update,
            EntityEffectUpdate(refreshed, "remove", PlayerEffect(12, 0, 17, False, False), 10),
            EntityEffectUpdate(replace(moved, status_effects=(unlimited,)), "remove",
                               PlayerEffect(2, 4, 23, True, True), 11),
            EntityEffectUpdate(moved, "remove", PlayerEffect(10, 0, 0, False, False), 12),
        ])
        self.assertEqual(states_at_dispatch, [event.entity for event in seen])
        self.assertEqual(self.entity(), moved)
        self.assertEqual(sheep.status_effects, ())
        for value, field, replacement in ((seen[0], "tick", 0), (seen[0].entity, "status_effects", ()),
                                          (seen[0].effect, "duration_ticks", 0)):
            with self.assertRaises(FrozenInstanceError):
                setattr(value, field, replacement)

    async def test_local_untracked_unknown_and_metadata_packets_leave_remote_effects_isolated(self):
        player = self.b.AddPlayerPacket(uuid=UUID(int=109), name="Other", runtime_id=9)
        await self.join(before_spawn=(self.sheep(), player))
        seen, selected, raw = [], [], []
        self.bot.subscribe_entity_effect_updates(seen.append)
        self.bot.subscribe_entity_effect_updates(selected.append, runtime_id=9)
        self.bot.packets.subscribe_raw(raw.append)
        first = await self.arm()
        first_player = await self.arm(9)
        ignored = (
            self.effect("ADD", runtime_id=7, duration=55),
            self.effect("ADD", runtime_id=99, duration=55),
            self.effect("INVALID", duration=55),
            self.effect(99, duration=200),
            self.visible_effects(24),
            self.visible_effects(24, runtime_id=9),
        )
        await self.deliver(*ignored)
        self.assertEqual(self.bot.status_effects, (PlayerEffect(1, 0, 55, False, False),))
        self.assertEqual({entity.runtime_id: entity.status_effects for entity in self.bot.observed_entities},
                         {8: (), 9: ()})
        self.assertEqual(seen, [])
        self.assertFalse(first.done())
        self.assertFalse(first_player.done())

        remote = (self.effect("ADD", duration=101), self.effect("UPDATE", runtime_id=9, duration=201),
                  self.visible_effects(0), self.visible_effects(0, runtime_id=9))
        await self.deliver(*remote)
        self.assertEqual([event.entity.runtime_id for event in seen], [8, 9])
        self.assertEqual(selected, [seen[1]])
        self.assertEqual(await first, seen[0])
        self.assertEqual(await first_player, seen[1])
        self.assertEqual(self.entity().status_effects, (PlayerEffect(1, 0, 101, False, False),))
        self.assertEqual(self.entity(9).status_effects, (PlayerEffect(1, 0, 201, False, False),))
        self.assertEqual(self.bot.status_effects, (PlayerEffect(1, 0, 55, False, False),))
        self.assertEqual([packet for packet in raw if packet.packet_id != self.b.SetTimePacket.ID],
                         [self.bot.protocol.encode(packet) for packet in (*ignored, *remote)])

    async def test_effect_state_and_scoped_waits_end_when_an_entity_instance_leaves(self):
        await self.join(before_spawn=(self.sheep(),))
        seen, old = [], []
        subscribe = self.bot.subscribe_entity_effect_updates
        subscribe(seen.append)

        transitions = (
            (109, (self.b.RemoveActorPacket(entity_id=108), self.effect("ADD", 11), self.sheep(109)), LookupError),
            (110, (self.sheep(110),), LookupError),
            (111, (self.b.ChangeDimensionPacket(dimension_id=1), self.effect("ADD", 11), self.sheep(111)),
             RuntimeError),
        )
        for entity_id, packets, error in transitions:
            with self.subTest(entity_id=entity_id):
                await self.deliver(self.effect("ADD", 2, duration=20))
                previous = self.entity()
                subscribe(old.append, runtime_id=8)
                scoped = await self.arm(8)
                all_entities = await self.arm() if error is RuntimeError else None
                await self.deliver(*packets)
                with self.assertRaises(error):
                    await scoped
                if all_entities is not None:
                    with self.assertRaisesRegex(RuntimeError, "dimension changed"):
                        await all_entities
                self.assertEqual((self.entity().entity_id, self.entity().status_effects), (entity_id, ()))
                self.assertEqual(previous.status_effects, (PlayerEffect(2, 0, 20, False, False),))

        waiting = await self.arm(8)
        await self.deliver(self.effect("ADD", 10, duration=120))
        self.assertEqual(old, [])
        self.assertEqual([event.entity.entity_id for event in seen], [108, 109, 110, 111])
        self.assertEqual(await waiting, seen[-1])

    async def test_cancelled_wait_does_not_interrupt_observation_and_disconnect_ends_it(self):
        await self.join(before_spawn=(self.sheep(),))
        seen = []
        self.bot.subscribe_entity_effect_updates(seen.append)
        cancelled = await self.arm(8)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled

        waiting = await self.arm(8)
        await self.deliver(self.effect("ADD", duration=30))
        update = await waiting
        self.assertEqual(seen, [update])
        pending = await self.arm(8)
        await self.deliver(self.b.DisconnectPacket(), self.effect("UPDATE", duration=60))
        with self.assertRaises(ConnectionError):
            await pending
        self.assertEqual(seen, [update])
