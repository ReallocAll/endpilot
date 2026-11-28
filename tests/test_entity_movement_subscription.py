"""Exercise movement subscriptions through the joined Bot's native packet reader."""

import asyncio
from dataclasses import FrozenInstanceError, replace
from math import isnan
import unittest
from unittest.mock import patch
from uuid import UUID

from endpilot import Bot, EntityMovementUpdate
from endpilot.entities import EntityState
from endpilot.protocol import write_batch
from test_scheduled_input import ManualClock, Wire


class EntityMovementSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("MovementWatch", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.protocol = self.bot.protocol
        self.b = self.protocol.binding

    async def join(self, *startup):
        b = self.b
        self.wire = Wire(self.protocol)
        self.wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.wire.incoming.put_nowait(write_batch(map(self.protocol.encode, (
            b.StartGamePacket(runtime_id=7, entity_id=17),
            *startup,
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        ))))
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket(time=23)
        processed = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(map(self.protocol.encode, (*packets, marker))))
        await processed

    def actor(self, entity_id=108, *, actor_type="minecraft:cow"):
        return self.b.AddActorPacket(
            entity_id=entity_id, runtime_id=8, actor_type=actor_type, pos=self.b.Vec3(x=1, y=64, z=3),
        )

    def delta(self, runtime_id=8, **fields):
        return self.b.MoveActorDeltaPacket(move_data=self.b.MoveActorDeltaData(runtime_id=runtime_id, **fields))

    async def test_each_packet_publishes_the_applied_snapshot_and_its_own_angles(self):
        await self.join()
        b = self.b
        seen, cached = [], []

        def observe(update):
            seen.append(update)
            cached.append(self.bot.observed_entities)

        self.bot.subscribe_entity_movement(observe)
        await self.deliver(self.actor(), b.AddPlayerPacket(
            uuid=UUID(int=9), runtime_id=9, name="Peer", pos=b.Vec3(x=7.25, y=80, z=11),
        ))
        self.assertEqual(seen, [])
        lifecycle = []
        self.bot.subscribe_entity_lifecycle(lifecycle.append)
        angle_only = self.delta(rot_x=0, rot_y_head=32, is_on_ground=True)
        player_position = b.Vec3(x=7.25, y=81.625, z=11)
        await self.deliver(
            b.MoveActorAbsolutePacket(move_data=b.MoveActorAbsoluteData(
                runtime_id=8, header=5, pos=b.Vec3(x=5, y=70, z=10), rot_x=-56, rot_y=64, rot_y_head=-1,
            )),
            self.delta(new_position_x=0, new_position_z=-2.25, rot_y=-64),
            angle_only,
            angle_only,
            self.delta(new_position_y=63.5),
            b.MoveActorAbsolutePacket(move_data=b.MoveActorAbsoluteData(
                runtime_id=8, header=4, pos=b.Vec3(x=0, y=63.5, z=-2.25),
            )),
            b.MovePlayerPacket(
                player_id=9, pos=player_position, rot=b.Vec2(x=-12.25, y=271.5), y_head_rot=137.75, on_ground=True,
            ),
            b.MovePlayerPacket(player_id=9, pos=player_position, y_head_rot=-45.5, on_ground=False),
        )

        absolute = EntityState(108, 8, "minecraft:cow", (5, 70, 10), pitch=281.25, yaw=90, head_yaw=358.59375)
        merged = replace(absolute, position=(0, 70, -2.25), yaw=270)
        turned = replace(merged, pitch=0, head_yaw=45)
        last_actor = replace(turned, position=(0, 63.5, -2.25))
        reset_actor = replace(last_actor, yaw=0, head_yaw=0)
        player = EntityState(0, 9, "minecraft:player", (7.25, 80, 11), name="Peer", pitch=0, yaw=0, head_yaw=0)
        moved_player = replace(player, position=(7.25, 81.625, 11), pitch=-12.25, yaw=271.5, head_yaw=137.75)
        turned_player = replace(moved_player, pitch=0, yaw=0, head_yaw=-45.5)
        rotated = EntityMovementUpdate(entity=turned, pitch=0, yaw=None, head_yaw=45, on_ground=True)
        expected = [
            EntityMovementUpdate(entity=absolute, pitch=281.25, yaw=90, head_yaw=358.59375, on_ground=True),
            EntityMovementUpdate(entity=merged, pitch=None, yaw=270, head_yaw=None, on_ground=False),
            rotated,
            rotated,
            EntityMovementUpdate(entity=last_actor, pitch=None, yaw=None, head_yaw=None, on_ground=False),
            EntityMovementUpdate(entity=reset_actor, pitch=0, yaw=0, head_yaw=0, on_ground=False),
            EntityMovementUpdate(entity=moved_player, pitch=-12.25, yaw=271.5, head_yaw=137.75, on_ground=True),
            EntityMovementUpdate(entity=turned_player, pitch=0, yaw=0, head_yaw=-45.5, on_ground=False),
        ]
        self.assertEqual(seen, expected)
        self.assertEqual(cached, [
            (absolute, player), (merged, player), (turned, player), (turned, player),
            (last_actor, player), (reset_actor, player), (reset_actor, moved_player), (reset_actor, turned_player),
        ])
        self.assertEqual(lifecycle, [])
        for update, wanted in zip(seen, expected):
            self.assertIs(update.on_ground, wanted.on_ground)
        with self.assertRaises(FrozenInstanceError):
            seen[0].yaw = 0
        with self.assertRaises(FrozenInstanceError):
            seen[0].entity.position = (0, 0, 0)

    async def test_query_tracks_finite_axes_and_keeps_history_through_reuse_and_disconnect(self):
        b = self.b
        invalid = float("nan")
        await self.join(
            b.AddActorPacket(entity_id=108, runtime_id=8, actor_type="minecraft:cow",
                             rot=b.Vec2(x=-3.5, y=400), y_head_rotation=106, y_body_rotation=200),
            b.AddPlayerPacket(uuid=UUID(int=9), runtime_id=9, name="Peer",
                              rot=b.Vec2(x=invalid, y=-93.5), y_head_rot=250.25),
            b.AddPaintingPacket(entity_id=110, runtime_id=10, dir=2, motif="Kebab"),
        )
        sent_before = list(self.wire.sent)
        actor, player, painting = self.bot.observed_entities

        def angles(actor):
            return actor.pitch, actor.yaw, actor.head_yaw

        self.assertEqual(angles(actor), (-3.5, 400, 106))
        self.assertEqual(angles(player), (None, -93.5, 250.25))
        self.assertEqual(angles(painting), (None, None, None))
        seen, lifecycle = [], []
        self.bot.subscribe_entity_movement(seen.append)
        self.bot.subscribe_entity_lifecycle(lifecycle.append)
        await self.deliver(self.delta(10, rot_y_head=64), b.MovePlayerPacket(
            player_id=9, rot=b.Vec2(x=-24.25, y=float("inf")), y_head_rot=invalid,
        ))
        _, current_player, current_painting = self.bot.observed_entities
        self.assertEqual(angles(current_player), (-24.25, -93.5, 250.25))
        self.assertEqual(angles(current_painting), (None, None, 90))
        self.assertEqual((angles(seen[0]), seen[0].entity.position), ((None, None, 90), painting.position))
        self.assertEqual(seen[1].pitch, -24.25)
        self.assertEqual(seen[1].yaw, float("inf"))
        self.assertTrue(isnan(seen[1].head_yaw))
        self.assertEqual(lifecycle, [])

        replacement = b.AddItemActorPacket(id=108, runtime_id=8)
        await self.deliver(b.RemoveActorPacket(entity_id=108), self.delta(rot_x=64), replacement)
        current = next(state for state in self.bot.observed_entities if state.runtime_id == 8)
        self.assertEqual((current.actor_type, angles(current)), ("minecraft:item", (None, None, None)))
        self.assertEqual(tuple(angles(state) for state in (actor, player, painting)),
                         ((-3.5, 400, 106), (None, -93.5, 250.25), (None, None, None)))
        with self.assertRaises(FrozenInstanceError):
            actor.yaw = 0
        before_end = self.bot.observed_entities
        await self.deliver(b.DisconnectPacket(), self.delta(rot_y=64))
        self.assertEqual(self.bot.observed_entities, before_end)
        self.assertEqual(len(seen), 2)
        self.assertEqual(self.wire.sent, sent_before)
        await self.bot.close()
        await self.join(replacement)
        fresh, = self.bot.observed_entities
        self.assertEqual(angles(fresh), (None, None, None))

    async def test_reentrant_subscription_changes_apply_to_the_next_packet(self):
        await self.join(self.actor())
        changed, duplicated, from_callback, from_predicate, predicate_states = [], [], [], [], []

        def change_subscribers(update):
            changed.append(update)
            stop_changing()
            stop_duplicate()
            stop_duplicate()
            self.bot.subscribe_entity_movement(from_callback.append)

        def matches(state):
            predicate_states.append(state)
            if state.position[0] == 5:
                self.bot.subscribe_entity_movement(from_predicate.append)
                return True
            return False

        stop_changing = self.bot.subscribe_entity_movement(change_subscribers)
        duplicate_callback = duplicated.append
        stop_duplicate = self.bot.subscribe_entity_movement(duplicate_callback)
        self.bot.subscribe_entity_movement(duplicate_callback)
        waiting = asyncio.create_task(self.bot.wait_for_entity_movement_matching(8, matches, timeout=1))
        await asyncio.sleep(0)
        await self.deliver(self.delta(new_position_x=5), self.delta(new_position_x=6))

        self.assertEqual([state.position for state in predicate_states], [(1, 64, 3), (5, 64, 3)])
        first, = changed
        second, = from_callback
        self.assertEqual(await waiting, first.entity)
        self.assertEqual(first.entity.position, (5, 64, 3))
        self.assertEqual(second.entity.position, (6, 64, 3))
        self.assertEqual(duplicated, [first, first, second])
        self.assertEqual(from_predicate, [second])

    async def test_remote_filter_and_callback_failures_preserve_raw_and_packet_waits(self):
        await self.join()
        b = self.b
        seen, raw_packets, errors = [], [], []
        loop = asyncio.get_running_loop()
        self.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
        loop.set_exception_handler(lambda _, context: errors.append(context["exception"]))

        def fail(_):
            raise ValueError("observer failed")

        def cancel(_):
            raise asyncio.CancelledError("observer cancelled")

        self.bot.subscribe_entity_movement(fail)
        self.bot.subscribe_entity_movement(cancel)
        self.bot.subscribe_entity_movement(seen.append)
        self.bot.packets.subscribe_raw(raw_packets.append)
        waiting = asyncio.create_task(self.bot.wait_for_entity_movement(99, timeout=1))
        await asyncio.sleep(0)
        packets = (
            self.actor(actor_type="minecraft:boat"),
            b.SetActorMotionPacket(runtime_id=8, motion=b.Vec3(x=1.25)),
            b.MovePlayerPacket(player_id=8, pos=b.Vec3(x=100)),
            b.MovePlayerPacket(player_id=7, pos=b.Vec3(y=64)),
            b.MoveActorAbsolutePacket(move_data=b.MoveActorAbsoluteData(runtime_id=7, header=4)),
            self.delta(7, new_position_x=11),
            self.delta(new_position_y=65),
            self.delta(99, new_position_x=1),
        )
        await self.deliver(*packets)

        update, = seen
        self.assertEqual(update.entity.position, (1, 65, 3))
        self.assertEqual(update.entity.boat_velocity, (1.25, 0, 0))
        self.assertEqual([type(error) for error in errors], [ValueError, asyncio.CancelledError])
        legacy = await waiting
        self.assertEqual((legacy.runtime_id, legacy.x, legacy.y, legacy.z), (99, 1, None, None))
        self.assertEqual(raw_packets, list(map(self.protocol.encode, (*packets, b.SetTimePacket(time=23)))))

    async def test_subscription_spans_dimensions_and_runtime_reuse_but_ends_on_disconnect(self):
        old, new = [], []
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            self.bot.subscribe_entity_movement(old.append)
        await self.join(self.actor(), self.delta(new_position_x=2))
        state, = self.bot.observed_entities
        self.assertEqual(state.position, (2, 64, 3))
        stop_old = self.bot.subscribe_entity_movement(old.append)
        self.assertEqual(old, [])

        b = self.b
        await self.deliver(
            self.delta(new_position_x=3),
            b.ChangeDimensionPacket(dimension_id=1),
            self.delta(new_position_x=4),
            self.actor(entity_id=208),
            self.delta(new_position_y=65),
            b.DisconnectPacket(),
            self.delta(new_position_x=7),
        )
        self.assertEqual([(event.entity.entity_id, event.entity.position) for event in old], [
            (108, (3, 64, 3)), (208, (1, 65, 3)),
        ])
        with self.assertRaisesRegex(ConnectionError, "entity session is closed"):
            self.bot.subscribe_entity_movement(old.append)
        await self.bot.close()

        await self.join(self.actor())
        self.bot.subscribe_entity_movement(new.append)
        stop_old()
        stop_old()
        await self.deliver(self.delta(new_position_x=8))
        self.assertEqual(len(old), 2)
        self.assertEqual([update.entity.position for update in new], [(8, 64, 3)])
