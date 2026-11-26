"""Packet-driven tests of the public observed-entity query."""

import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch
from uuid import UUID

from endpilot import Bot
from endpilot.protocol import write_batch

from test_join import _spawn_transport


class NearestEntityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("Observer", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.protocol = self.bot.protocol
        self.b = self.protocol.binding

    async def join(self, *startup):
        self.transport = _spawn_transport(self.protocol, 7, *startup)
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        processed = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.transport.received.put_nowait(write_batch(map(self.protocol.encode, (*packets, marker))))
        await processed

    def vec(self, position):
        x, y, z = position
        return self.b.Vec3(x=x, y=y, z=z)

    def move(self, runtime_id=7, position=(0, 101.62, 0)):
        return self.b.MovePlayerPacket(player_id=runtime_id, pos=self.vec(position))

    def cow(self, unique, runtime, position):
        return self.b.AddActorPacket(entity_id=unique, runtime_id=runtime, actor_type="minecraft:cow",
                                     pos=self.vec(position))

    def drop(self, unique, runtime, position):
        b = self.b
        return b.AddItemActorPacket(id=unique, runtime_id=runtime, pos=self.vec(position),
                                    item=b.SerializedNetworkItemStackDescriptor(id=4, stack_size=3))

    def roster(self, unique):
        b = self.b
        return b.PlayerListPacket(entries=[b.PlayerListPacket.AddEntry(
            action=b.PlayerListPacketType.ADD, uuid=UUID(int=unique), id=unique, name="Remote",
        )])

    def nearest(self, match=None):
        sent = len(self.transport.sent)
        try:
            return self.bot.nearest_entity(match)
        finally:
            self.assertEqual(len(self.transport.sent), sent)

    def observe_entity_packet(self, packet):
        self.bot._entities._on_packet(self.protocol.encode(packet))

    async def test_ranks_player_feet_and_non_player_anchors_and_preserves_snapshots(self):
        b = self.b
        painting = b.AddPaintingPacket(entity_id=-30, runtime_id=30, pos=self.vec((0, 102.25, 0)),
                                        dir=2, motif="Pigscene")
        spawn = b.AddPlayerPacket(uuid=UUID(int=101), runtime_id=11, name="Remote",
                                  pos=self.vec((1.5, 100, 0)))
        await self.join(painting)
        self.assertIsNone(self.nearest())
        await self.deliver(self.move(), self.roster(101), spawn, self.move(11, (1.5, 101.62, 0)),
                           self.cow(-20, 20, (0, 98, 0)), self.drop(-40, 40, (2.5, 100, 0)))

        nearest = self.nearest()
        self.assertIs(nearest, self.bot.nearest_player())
        self.assertEqual(nearest.entity_id, 101)
        self.assertAlmostEqual(nearest.position[1], 101.62, places=4)
        self.assertEqual(self.nearest(lambda actor: actor.actor_type != "minecraft:player").entity_id, -20)
        picture = self.nearest(lambda actor: actor.painting is not None)
        self.assertEqual((picture.position, picture.painting.motif), ((0, 102.25, 0), "Pigscene"))
        item = self.nearest(lambda actor: actor.actor_type == "minecraft:item")
        self.assertEqual((item.entity_id, item.item_network_id, item.item_count), (-40, 4, 3))
        self.assertTrue(any(actor is item for actor in self.bot.observed_entities))
        with self.assertRaises(FrozenInstanceError):
            item.item_count = 8
        self.assertIsNone(self.nearest(lambda _: False))

        failure = RuntimeError("caller predicate")

        def reject(_):
            raise failure

        with self.assertRaises(RuntimeError) as captured:
            self.nearest(reject)
        self.assertIs(captured.exception, failure)

    async def test_checks_identity_and_finite_anchors_and_breaks_ties_by_unique_id(self):
        b = self.b
        await self.join()
        await self.deliver(
            self.move(), self.cow(901, 7, (0, 100, 0)), self.cow(7, 902, (0, 100, 0)),
            self.cow(0, 50, (0, 100, 0)), self.cow(-51, 51, (float("nan"), 100, 0)),
            self.cow(-52, 52, (0, float("inf"), 0)),
            self.cow(-10, 12, (2, 100, 0)), self.drop(-20, 24, (2, 100, 0)),
        )
        seen = []

        def allow(actor):
            seen.append(actor.entity_id)
            return True

        self.assertEqual(self.nearest(allow).runtime_id, 24)
        self.assertCountEqual(seen, [-10, -20])
        await self.deliver(b.MoveActorDeltaPacket(move_data=b.MoveActorDeltaData(
            runtime_id=51, new_position_z=1,
        )))
        self.assertEqual(self.nearest().entity_id, -20)
        await self.deliver(b.MoveActorDeltaPacket(move_data=b.MoveActorDeltaData(
            runtime_id=51, new_position_x=0,
        )))
        self.assertEqual(self.nearest().position, (0, 100, 1))

    async def test_waits_for_player_identity_and_does_not_rank_unknown_feet(self):
        b = self.b
        player = b.AddPlayerPacket(uuid=UUID(int=101), runtime_id=11, name="Remote",
                                   pos=self.vec((0.25, 100, 0)))
        await self.join(self.cow(-20, 20, (2, 100, 0)))
        await self.deliver(self.move(), player)
        self.assertEqual(self.nearest().entity_id, -20)
        await self.deliver(self.roster(101))
        self.assertEqual(self.nearest().entity_id, 101)

        await self.deliver(b.MoveActorAbsolutePacket(move_data=b.MoveActorAbsoluteData(
            runtime_id=11, pos=self.vec((0.25, 100, 0)),
        )))
        self.assertEqual(self.nearest().entity_id, -20)
        self.assertIsNone(self.nearest(lambda actor: actor.entity_id == 101))
        self.assertIsNone(self.bot.nearest_player())
        await self.deliver(self.move(11, (0.25, 101.62, 0)))
        self.assertEqual(self.nearest().entity_id, 101)

    async def test_reuses_neither_runtime_nor_unique_id_state_and_invalidates_on_lifecycle(self):
        b = self.b
        self.assertIsNone(self.bot.nearest_entity())
        await self.join()
        await self.deliver(self.move(), self.drop(-20, 12, (0.5, 100, 0)))
        previous = self.nearest()
        await self.deliver(self.cow(-40, 12, (2, 100, 0)), b.RemoveActorPacket(entity_id=-20))
        replacement = self.nearest()
        self.assertEqual((replacement.entity_id, replacement.item_count, replacement.painting), (-40, None, None))
        self.assertEqual((previous.entity_id, previous.item_count, previous.position), (-20, 3, (0.5, 100, 0)))
        await self.deliver(self.cow(-40, 24, (1, 100, 0)))
        self.assertEqual(self.nearest().runtime_id, 24)
        self.assertIsNone(self.nearest(lambda actor: actor.runtime_id == 12))
        await self.deliver(b.RemoveActorPacket(entity_id=-40))
        self.assertIsNone(self.nearest())

        await self.deliver(self.drop(-20, 12, (0.5, 100, 0)),
                           b.TakeItemActorPacket(item_id=12, actor_id=7))
        self.assertIsNone(self.nearest())
        await self.deliver(self.cow(-30, 15, (1, 100, 0)))
        self.assertEqual(self.nearest().entity_id, -30)
        await self.deliver(b.ChangeDimensionPacket(dimension_id=1, loading_screen_id=0),
                           self.cow(-50, 12, (40, 100, 0)), self.move(position=(40, 101.62, 0)))
        self.assertIsNone(self.nearest())
        await self.deliver(b.PlayerActionPacket(runtime_id=7, action=b.PlayerActionType.CHANGE_DIMENSION_ACK))
        self.assertEqual(self.nearest().entity_id, -50)
        self.assertIsNone(self.nearest(lambda actor: actor.entity_id == -30))

        reader = self.bot.packets.start_receiving()
        reader.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reader
        self.assertIsNone(self.nearest())
        await self.bot.close()
        self.assertIsNone(self.nearest())

    async def test_predicate_eager_close_stops_both_queries_even_when_it_rejects_first(self):
        b = self.b
        for players_only in (False, True):
            with self.subTest(players_only=players_only):
                await self.join()
                if players_only:
                    await self.deliver(
                        self.move(), self.roster(101), self.roster(102),
                        b.AddPlayerPacket(uuid=UUID(int=101), runtime_id=11, pos=self.vec((1, 100, 0))),
                        b.AddPlayerPacket(uuid=UUID(int=102), runtime_id=12, pos=self.vec((2, 100, 0))),
                    )
                else:
                    await self.deliver(self.move(), self.cow(101, 11, (1, 100, 0)), self.cow(102, 12, (2, 100, 0)))
                query = self.bot.nearest_player if players_only else self.nearest
                called = []
                close_task = None

                def match(actor):
                    nonlocal close_task
                    called.append(actor.entity_id)
                    if close_task is None:
                        close_task = asyncio.eager_task_factory(asyncio.get_running_loop(), self.bot.close())
                        return False
                    return True

                try:
                    self.assertIsNone(query(match))
                    self.assertEqual(called, [101])
                    self.assertIsNone(self.bot.nearest_entity())
                finally:
                    if close_task is not None:
                        await close_task

    async def test_entity_predicate_drops_replaced_and_later_removed_matches(self):
        b = self.b
        await self.join()
        await self.deliver(self.move(), self.drop(-11, 11, (0.25, 100, 0)), self.cow(-12, 12, (1, 100, 0)),
                           self.cow(-13, 13, (2, 100, 0)), self.cow(-14, 14, (3, 100, 0)))
        called = []

        def match(actor):
            called.append(actor.entity_id)
            if actor.runtime_id == 11:
                self.observe_entity_packet(self.cow(-11, 11, (0.5, 100, 0)))
            if actor.runtime_id == 14:
                self.observe_entity_packet(b.RemoveActorPacket(entity_id=-12))
                return False
            return True

        self.assertEqual(self.nearest(match).entity_id, -13)
        self.assertEqual(called, [-11, -12, -13, -14])
        self.assertEqual(self.nearest().actor_type, "minecraft:cow")
        self.assertEqual(self.nearest().entity_id, -11)

    async def test_player_predicate_cannot_return_an_earlier_match_whose_feet_became_unknown(self):
        b = self.b
        await self.join()
        await self.deliver(
            self.move(), self.roster(101), self.roster(102),
            b.AddPlayerPacket(uuid=UUID(int=101), runtime_id=11, pos=self.vec((1, 100, 0))),
            b.AddPlayerPacket(uuid=UUID(int=102), runtime_id=12, pos=self.vec((2, 100, 0))),
        )

        def match(player):
            if player.entity_id == 102:
                self.observe_entity_packet(b.SetActorLinkPacket(link=b.ActorLink(
                    a=-200, b=101, type=b.ActorLinkType.PASSENGER,
                )))
            return True

        self.assertEqual(self.bot.nearest_player(match).entity_id, 102)
        self.assertEqual(self.bot.nearest_player().entity_id, 102)

    async def test_dimension_change_during_predicate_aborts_the_current_scan(self):
        b = self.b
        await self.join()
        await self.deliver(self.move(), self.cow(-11, 11, (1, 100, 0)), self.cow(-12, 12, (2, 100, 0)))
        called = []

        def match(actor):
            called.append(actor.entity_id)
            self.observe_entity_packet(b.ChangeDimensionPacket(dimension_id=1))
            return True

        self.assertIsNone(self.nearest(match))
        self.assertEqual(called, [-11])
