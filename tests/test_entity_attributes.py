"""Exercise remote entity attributes through the native packet codec."""

import asyncio
from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import patch
from uuid import UUID

from endpilot import Bot, EntityAttribute, EntityAttributeModifier, EntityAttributeUpdate
from endpilot.compression import PacketConnection
from endpilot.entities import Entities, EntityState
from endpilot.protocol import Protocol, write_batch
from test_scheduled_input import ManualClock, Wire


HEALTH = "minecraft:health"
MOVEMENT = "minecraft:movement"
ATTACK = "minecraft:attack_damage"


class EntityAttributeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = ManualClock()
        self.enterContext(self.clock.patch_scheduler())
        self.bot = Bot("AttributeWatch", protocol=2225)
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

    def spawn(self, *attributes, runtime_id=8, entity_id=108):
        b = self.b
        return b.AddActorPacket(runtime_id=runtime_id, entity_id=entity_id, actor_type="minecraft:sheep",
                                pos=b.Vec3(x=1.5, y=100, z=1.5), attributes=attributes)

    def update(self, *attributes, runtime_id=8, tick=1):
        return self.b.UpdateAttributesPacket(runtime_id=runtime_id, attribute_data=attributes, tick=tick)

    def attribute(self, name, current_value, **fields):
        return self.b.AttributeData(name=name, current_value=current_value, **fields)

    def observed(self, runtime_id):
        return next(entity for entity in self.bot.observed_entities if entity.runtime_id == runtime_id)

    async def arm(self, runtime_id=None):
        task = asyncio.create_task(self.bot.wait_for_entity_attribute_update(runtime_id=runtime_id, timeout=1))
        self.addAsyncCleanup(self.cancel, task)
        await asyncio.sleep(0)
        return task

    @staticmethod
    async def cancel(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_updates_merge_attributes_and_preserve_each_packet_snapshot(self):
        await self.join()
        b = self.b
        seen, targeted, states_at_dispatch = [], [], []

        def record(update):
            seen.append(update)
            states_at_dispatch.append(self.observed(update.entity.runtime_id))

        self.bot.subscribe_entity_attribute_updates(record)
        first = await self.arm()
        start = len(self.wire.sent)
        await self.deliver(
            self.spawn(b.SyncedAttribute(name=HEALTH, min_value=0, current_value=8, max_value=8),
                       b.SyncedAttribute(name=MOVEMENT, min_value=0, current_value=0.25, max_value=1)),
            b.AddPlayerPacket(uuid=UUID(int=9), name="Other", runtime_id=9),
            self.update(self.attribute(HEALTH, 14), runtime_id=7),
            self.update(self.attribute(HEALTH, 17), runtime_id=99),
        )
        sheep, player = self.observed(8), self.observed(9)
        spawn_health = EntityAttribute(HEALTH, 0, 8, 8)
        speed = EntityAttribute(MOVEMENT, 0, 0.25, 1)
        self.assertEqual(sheep.attributes, (spawn_health, speed))
        self.assertEqual(player.attributes, ())
        self.assertEqual(EntityState(108, 8, "minecraft:sheep", (1.5, 100, 1.5)).attributes, ())
        self.assertEqual({entity.runtime_id for entity in self.bot.observed_entities}, {8, 9})
        self.assertEqual(self.bot.health, 14)
        self.assertFalse(first.done())
        self.assertEqual(seen, [])

        self.bot.subscribe_entity_attribute_updates(targeted.append, runtime_id=8)
        first_sheep = await self.arm(8)
        modifier = b.AttributeModifier(id="modifier-one", name="Constitution", amount=2.25,
                                       operation=1, operand=2, serialize=True)
        future_modifier = b.AttributeModifier(id="future", name="Future", amount=-0.5,
                                              operation=257, operand=258, serialize=False)
        player_health = self.attribute(HEALTH, 17, max_value=20, default_value=20)
        health = self.attribute(HEALTH, 5, min_value=1, max_value=10, default_min_value=2,
                                default_value=6, default_max_value=12, modifiers=[modifier, future_modifier])
        attack = self.attribute(ATTACK, 3, max_value=8, default_value=4)
        damage = self.update(self.attribute(HEALTH, 99), attack, health, tick=180)
        move = b.MoveActorAbsolutePacket(move_data=b.MoveActorAbsoluteData(
            runtime_id=8, pos=b.Vec3(x=3.5, y=100, z=1.5),
        ))
        await self.deliver(self.update(player_health, runtime_id=9, tick=179), damage, damage, move,
                           self.update(tick=181), self.update(self.attribute(HEALTH, 4), tick=182))

        expected_modifiers = (EntityAttributeModifier("modifier-one", "Constitution", 2.25, 1, 2, True),
                              EntityAttributeModifier("future", "Future", -0.5, 257, 258, False))
        damaged_health = EntityAttribute(HEALTH, 1, 5, 10, 2, 6, 12, expected_modifiers)
        attack_value = EntityAttribute(ATTACK, 0, 3, 8, 0, 4, 0, ())
        changed = replace(sheep, attributes=(damaged_health, speed, attack_value))
        moved = replace(changed, position=(3.5, 100, 1.5))
        latest = replace(moved, attributes=(EntityAttribute(HEALTH, 0, 4, 0, 0, 0, 0, ()),
                                            speed, attack_value))
        hit = EntityAttributeUpdate(changed, (HEALTH, ATTACK), 180)
        player_update = EntityAttributeUpdate(replace(
            player, attributes=(EntityAttribute(HEALTH, 0, 17, 20, 0, 20, 0, ()),),
        ), (HEALTH,), 179)
        self.assertEqual(seen, [player_update, hit, hit, EntityAttributeUpdate(moved, (), 181),
                                EntityAttributeUpdate(latest, (HEALTH,), 182)])
        self.assertEqual(states_at_dispatch, [update.entity for update in seen])
        self.assertEqual(targeted, seen[1:])
        self.assertEqual(await first, player_update)
        self.assertEqual(await first_sheep, hit)
        self.assertEqual(self.observed(8), latest)
        self.assertEqual(sheep.attributes, (spawn_health, speed))
        for item, field, value in ((seen[1], "tick", 0), (seen[1].entity.attributes[0], "current_value", 8),
                                   (seen[1].entity.attributes[0].modifiers[0], "amount", 0)):
            self.assertFalse(hasattr(item, "__dict__"))
            with self.assertRaises(FrozenInstanceError):
                setattr(item, field, value)
        self.assertEqual(self.bot.health, 14)
        self.assertEqual(len(self.wire.sent), start)

    async def test_runtime_observers_end_on_removal_replacement_and_dimension_change(self):
        await self.join()
        subscribe = self.bot.subscribe_entity_attribute_updates
        for runtime_id in (7, 8):
            with self.subTest(runtime_id=runtime_id):
                with self.assertRaises(LookupError):
                    subscribe(self.fail, runtime_id=runtime_id)
                with self.assertRaises(LookupError):
                    await self.bot.wait_for_entity_attribute_update(runtime_id=runtime_id, timeout=0)
        seen, old = [], []
        subscribe(seen.append)
        await self.deliver(self.spawn())
        subscribe(old.append, runtime_id=8)
        removed = await self.arm(8)
        await self.deliver(self.b.RemoveActorPacket(entity_id=108), self.update(),
                           self.spawn(entity_id=109), self.update())
        with self.assertRaisesRegex(LookupError, "no longer observed"):
            await removed

        subscribe(old.append, runtime_id=8)
        replaced = await self.arm(8)
        await self.deliver(self.spawn(entity_id=110), self.update())
        with self.assertRaisesRegex(LookupError, "no longer observed"):
            await replaced

        subscribe(old.append, runtime_id=8)
        scoped, every = await self.arm(8), await self.arm()
        await self.deliver(self.b.ChangeDimensionPacket(dimension_id=1), self.update(),
                           self.spawn(entity_id=111), self.update())
        for waiting in (scoped, every):
            with self.assertRaisesRegex(RuntimeError, "dimension changed"):
                await waiting
        self.assertEqual(old, [])
        self.assertEqual([event.entity.entity_id for event in seen], [109, 110, 111])

        new = []
        subscribe(new.append, runtime_id=8)
        await self.deliver(self.update(tick=2))
        self.assertEqual(new, [seen[-1]])
        self.assertEqual(new[0].entity.entity_id, 111)

    async def test_callback_cancellation_and_unsubscribe_leave_raw_observation_running(self):
        await self.join(before_spawn=(self.spawn(),))
        seen, errors, raw_packets = [], [], []
        loop = asyncio.get_running_loop()
        self.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
        loop.set_exception_handler(lambda _loop, details: errors.append(details["exception"]))

        def fail(_):
            raise asyncio.CancelledError("attribute callback failed")

        unsubscribe = self.bot.subscribe_entity_attribute_updates(fail)
        self.bot.subscribe_entity_attribute_updates(seen.append)
        self.bot.packets.subscribe_raw(raw_packets.append)
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_entity_attribute_update(timeout=0)
        cancelled = await self.arm()
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        waiting = await self.arm()
        packet = self.update(self.attribute(HEALTH, 5), tick=91)
        await self.deliver(packet)
        self.assertEqual(seen, [await waiting])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], asyncio.CancelledError)

        unsubscribe()
        unsubscribe()
        await self.deliver(packet)
        self.assertEqual(len(errors), 1)
        self.assertEqual(seen, [seen[0], seen[0]])
        self.assertEqual([raw for raw in raw_packets if raw.packet_id == packet.ID],
                         [self.bot.protocol.encode(packet)] * 2)

    async def test_local_close_ends_pending_observation(self):
        await self.join(before_spawn=(self.spawn(),))
        waiting = await self.arm(8)
        await self.bot.close()
        with self.assertRaises(ConnectionError):
            await waiting
        self.assertTrue(self.wire.closed)

    async def test_reader_failure_ends_pending_observation_with_cause(self):
        await self.join()
        waiting = await self.arm()
        cause = OSError("attribute receive failed")
        self.wire.incoming.put_nowait(cause)
        with self.assertRaises(ConnectionError) as caught:
            await waiting
        self.assertIs(caught.exception.__cause__, cause)
        with self.assertRaises(ConnectionError):
            self.bot.subscribe_entity_attribute_updates(self.fail)
        with self.assertRaises(OSError):
            await self.bot.close()

    async def test_login_replays_updates_in_spawn_order_and_keeps_local_vitals_and_speed(self):
        b = self.b
        await self.join(
            before_start=(
                self.update(self.attribute(ATTACK, 99)),
                self.spawn(b.SyncedAttribute(name=HEALTH, current_value=8, max_value=8)),
                self.update(self.attribute(HEALTH, 5, max_value=8)),
            ),
            before_spawn=(
                self.update(self.attribute(HEALTH, 14), self.attribute(MOVEMENT, 0.25), runtime_id=7),
                self.update(self.attribute(MOVEMENT, 0.75)),
            ),
        )
        self.assertEqual(self.observed(8).attributes, (
            EntityAttribute(HEALTH, 0, 5, 8, 0, 0, 0, ()),
            EntityAttribute(MOVEMENT, 0, 0.75, 0, 0, 0, 0, ()),
        ))
        self.assertEqual(self.bot.health, 14)
        await self.deliver(b.MovePlayerPacket(player_id=7, pos=b.Vec3(x=0.5, y=101.62, z=0.5),
                                              reset_position=2, on_ground=True))
        await self.bot.set_move_vector(0, 1)
        await self.clock.wake_at(50)
        frame, = [packet for packet in self.wire.sent if isinstance(packet, b.PlayerAuthInputPacket)]
        self.assertAlmostEqual(frame.pos_delta.z, 0.25, places=5)

    async def test_login_dimension_change_clears_attributes_before_runtime_reuse(self):
        b = self.b
        await self.join(before_spawn=(
            self.spawn(b.SyncedAttribute(name=HEALTH, current_value=8, max_value=8)),
            self.spawn(runtime_id=9, entity_id=110),
            self.update(self.attribute(MOVEMENT, 0.25)),
            b.ChangeDimensionPacket(dimension_id=1),
            self.update(self.attribute(ATTACK, 99)),
            self.spawn(b.SyncedAttribute(name=HEALTH, current_value=20, max_value=20), entity_id=109),
            self.update(self.attribute(HEALTH, 17, max_value=20)),
        ))
        entity, = self.bot.observed_entities
        self.assertEqual(entity.entity_id, 109)
        self.assertEqual(entity.attributes, (EntityAttribute(HEALTH, 0, 17, 20, 0, 0, 0, ()),))

    async def test_legacy_update_omits_default_bounds(self):
        # Seed the observation because protocol 712 predates packets needed for entity spawn dispatch.
        protocol = Protocol(712)
        b = protocol.binding
        wire = Wire(protocol)
        connection = PacketConnection(wire, protocol, b.NetworkSettingsPacket(
            compression_algorithm=b.PacketCompressionAlgorithm.NONE,
        ))
        self.addAsyncCleanup(connection.close)
        entities = Entities(connection)
        self.addCleanup(entities.close)
        entities._record(EntityState(108, 8, "minecraft:sheep", (1.5, 100, 1.5)))
        entities.start()
        connection.start_receiving()
        waiting = asyncio.create_task(entities.wait_for_entity_attribute_update(timeout=1))
        self.addAsyncCleanup(self.cancel, waiting)
        await asyncio.sleep(0)
        wire.push(b.UpdateAttributesPacket(runtime_id=8, tick=31, attribute_data=[b.AttributeData(
            name=HEALTH, min_value=1, current_value=5, max_value=10, default_value=6,
        )]))
        event = await waiting
        self.assertEqual((event.names, event.tick), ((HEALTH,), 31))
        self.assertEqual(event.entity.attributes, (EntityAttribute(HEALTH, 1, 5, 10, None, 6, None, ()),))
