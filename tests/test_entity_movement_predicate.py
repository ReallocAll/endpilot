"""Replay native movement batches through the joined Bot's live packet reader."""

import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch
from uuid import UUID

from endpilot import Bot
from endpilot.protocol import write_batch
from test_scheduled_input import ManualClock, Wire


class EntityMovementPredicateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("MovementWatch")
        self.addAsyncCleanup(self.bot.close)
        self.protocol = self.bot.protocol
        self.b = b = self.protocol.binding
        self.wire = Wire(self.protocol)
        self.wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.wire.push(b.StartGamePacket(runtime_id=7, entity_id=17))
        self.wire.push(b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)
        self.entities = self.bot._entities

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        processed = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(map(self.protocol.encode, (*packets, marker))))
        await processed

    def spawn(self, runtime_id=8, entity_id=108):
        return self.b.AddActorPacket(
            runtime_id=runtime_id, entity_id=entity_id, actor_type="minecraft:cow",
            pos=self.b.Vec3(y=64, z=3),
        )

    def delta(self, x, *, runtime_id=8):
        return self.b.MoveActorDeltaPacket(move_data=self.b.MoveActorDeltaData(
            runtime_id=runtime_id, new_position_x=x,
        ))

    def player(self, uuid, entity_id, *, runtime_id=8):
        b = self.b
        return b.AddPlayerPacket(
            uuid=uuid, runtime_id=runtime_id, name="Observed",
            abilities_data=b.SerializedAbilitiesData(target_player=entity_id),
        )

    def roster(self, uuid, entity_id):
        b = self.b
        return b.PlayerListPacket(entries=[b.PlayerListPacket.AddEntry(
            action=b.PlayerListPacketType.ADD, uuid=uuid, id=entity_id, name="Observed",
        )])

    def player_move(self, x, *, runtime_id=8):
        return self.b.MovePlayerPacket(player_id=runtime_id, pos=self.b.Vec3(x=x))

    async def arm(self, runtime_id, predicate, **kwargs):
        waiting = asyncio.create_task(self.bot.wait_for_entity_movement_matching(
            runtime_id, predicate, timeout=1, **kwargs,
        ))
        await asyncio.sleep(0)
        return waiting

    async def test_matches_in_batch_order_and_returns_current_state_without_another_packet(self):
        await self.deliver(self.spawn(), self.spawn(9, 109))
        sent = len(self.wire.sent)
        seen = []

        def at_target(state):
            seen.append(state)
            return state.position[0] == 20

        waiting = await self.arm(8, at_target, entity_id=108)
        await self.deliver(self.delta(5), self.delta(20, runtime_id=9), self.delta(20))

        result = await waiting
        self.assertEqual([state.position for state in seen], [(0, 64, 3), (5, 64, 3), (20, 64, 3)])
        self.assertEqual((result.runtime_id, result.entity_id), (8, 108))
        self.assertIs(result, await self.bot.wait_for_entity_movement_matching(8, at_target, timeout=0))
        await self.deliver(self.delta(40))
        self.assertEqual(result.position, (20, 64, 3))
        with self.assertRaises(FrozenInstanceError):
            result.position = (40, 64, 3)
        self.assertEqual(len(self.wire.sent), sent)
        self.assertFalse(self.entities._movement_waiters)

    async def test_native_absolute_and_player_packets_match_the_first_satisfying_snapshot(self):
        b = self.b
        await self.deliver(self.spawn(), b.AddPlayerPacket(uuid=UUID(int=109), runtime_id=9))
        actor = await self.arm(8, lambda state: state.position[1] == 67)
        player = await self.arm(9, lambda state: state.position[1] == 68)
        await self.deliver(
            b.MovePlayerPacket(player_id=8, pos=b.Vec3(y=67)),
            b.MoveActorAbsolutePacket(move_data=b.MoveActorAbsoluteData(
                runtime_id=8, pos=b.Vec3(x=6, y=67, z=10),
            )),
            b.MovePlayerPacket(player_id=9, pos=b.Vec3(x=7, y=68, z=11)),
            b.MovePlayerPacket(player_id=9, pos=b.Vec3(x=70, y=69, z=110)),
        )
        self.assertEqual((await actor).position, (6, 67, 10))
        self.assertEqual((await player).position, (7, 68, 11))

    async def test_event_wait_keeps_next_packet_and_omitted_axis_behavior(self):
        await self.deliver(self.spawn(), self.delta(20))
        event_wait = asyncio.create_task(self.bot.wait_for_entity_movement(8, timeout=1))
        unobserved_wait = asyncio.create_task(self.bot.wait_for_entity_movement(99, timeout=1))
        await asyncio.sleep(0)
        self.assertFalse(event_wait.done())
        await self.deliver(self.delta(25, runtime_id=99), self.delta(30))
        move = await event_wait
        self.assertEqual((move.runtime_id, move.x, move.y, move.z), (8, 30, None, None))
        self.assertEqual((await unobserved_wait).x, 25)

    async def test_requires_current_runtime_and_checks_optional_unique_id_before_matching(self):
        await self.deliver(self.spawn())

        def unexpected(_):
            self.fail("predicate ran for the wrong actor")

        for runtime_id, entity_id in ((108, None), (8, 109), (7, 17), (8, 0)):
            with self.subTest(runtime_id=runtime_id, entity_id=entity_id):
                with self.assertRaisesRegex(LookupError, "not currently observed"):
                    await self.bot.wait_for_entity_movement_matching(runtime_id, unexpected, entity_id=entity_id)

    async def test_wait_requires_a_joined_bot(self):
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            await Bot("Unjoined").wait_for_entity_movement_matching(8, lambda _: True)

    async def test_conflicting_player_ids_cannot_bind_a_movement_wait(self):
        uuid = UUID(int=108)
        await self.deliver(self.roster(uuid, 108), self.player(uuid, 109))

        def unexpected(_):
            self.fail("predicate ran against a player with contradictory identities")

        for entity_id in (None, 108, 109):
            with self.subTest(entity_id=entity_id):
                with self.assertRaisesRegex(LookupError, "not currently observed"):
                    await self.bot.wait_for_entity_movement_matching(8, unexpected, entity_id=entity_id)

    async def test_matching_roster_identity_preserves_the_awaited_player(self):
        uuid = UUID(int=108)
        await self.deliver(self.player(uuid, 108))
        waiting = await self.arm(8, lambda state: state.position[0] == 20)

        await self.deliver(self.roster(uuid, 108), self.player_move(20))

        self.assertEqual((await waiting).entity_id, 108)

    async def test_late_conflicting_roster_ends_old_wait_and_cannot_bind_another(self):
        uuid = UUID(int=108)
        await self.deliver(self.player(uuid, 108))
        waiting = await self.arm(8, lambda state: state.position[0] == 20)

        await self.deliver(self.roster(uuid, 109), self.player_move(20))

        with self.assertRaisesRegex(LookupError, "no longer observed"):
            await waiting
        with self.assertRaisesRegex(LookupError, "not currently observed"):
            await self.bot.wait_for_entity_movement_matching(8, lambda _: True, entity_id=109)

    async def assert_conflicting_roster_preserves_other_player(self, *, roster_first):
        first_uuid, other_uuid = UUID(int=108), UUID(int=109)
        await self.deliver(self.player(other_uuid, 109, runtime_id=9), self.player_move(7, runtime_id=9))
        equipment = self.bot.observed_player_equipment(9)
        self.assertEqual(equipment.entity_id, 109)
        other_wait = await self.arm(9, lambda state: state.position[0] == 20)
        if roster_first:
            await self.deliver(self.roster(first_uuid, 109), self.player(first_uuid, 108))
        else:
            await self.deliver(self.player(first_uuid, 108))
            first_wait = await self.arm(8, lambda state: state.position[0] == 20)
            await self.deliver(self.roster(first_uuid, 109), self.player_move(20))
            with self.assertRaisesRegex(LookupError, "no longer observed"):
                await first_wait

        await self.deliver(self.roster(other_uuid, 109), self.roster(first_uuid, 109))
        with self.assertRaisesRegex(LookupError, "not currently observed"):
            await self.bot.wait_for_entity_movement_matching(8, lambda _: True, entity_id=109)

        b = self.b
        await self.deliver(b.PlayerListPacket(entries=[b.PlayerListPacket.RemoveEntry(
            action=b.PlayerListPacketType.REMOVE, uuid=first_uuid,
        )]), self.player_move(20, runtime_id=9))

        other = await other_wait
        self.assertEqual((other.runtime_id, other.entity_id, other.position), (9, 109, (20, 0, 0)))
        self.assertIs(other, await self.bot.wait_for_entity_movement_matching(9, lambda _: True, entity_id=109))
        self.assertIs(self.bot.observed_player_equipment(9), equipment)

    async def test_conflicting_roster_before_add_player_preserves_the_other_player(self):
        await self.assert_conflicting_roster_preserves_other_player(roster_first=True)

    async def test_conflicting_roster_after_add_player_preserves_the_other_player(self):
        await self.assert_conflicting_roster_preserves_other_player(roster_first=False)

    async def test_reused_unlisted_player_id_ends_wait_before_the_next_move(self):
        uuid = UUID(int=108)
        replacements = {
            "actor": self.spawn(runtime_id=9, entity_id=108),
            "unlisted player": self.player(UUID(int=109), 108, runtime_id=9),
        }
        for name, replacement in replacements.items():
            with self.subTest(replacement=name):
                await self.deliver(self.player(uuid, 108))
                waiting = await self.arm(8, lambda state: state.position[0] == 20)

                await self.deliver(replacement, self.b.RemoveActorPacket(entity_id=108), self.player_move(20))

                with self.assertRaisesRegex(LookupError, "no longer observed"):
                    await waiting
                self.assertFalse(self.entities._movement_waiters)
                with self.assertRaisesRegex(LookupError, "not currently observed"):
                    await self.bot.wait_for_entity_movement_matching(8, lambda _: True)

    async def test_same_id_reuse_removal_and_replacement_end_the_old_wait(self):
        b = self.b
        seen = []
        for transition, next_runtime in (
            ((self.spawn(),), 8),
            ((self.spawn(entity_id=109),), 8),
            ((self.spawn(runtime_id=9),), 9),
            ((b.RemoveActorPacket(entity_id=108), self.spawn()), 8),
        ):
            with self.subTest(transition=transition, runtime_id=next_runtime):
                await self.deliver(self.spawn())
                waiting = await self.arm(8, lambda state: seen.append(state.position[0]) or state.position[0] == 20)
                await self.deliver(*transition, self.delta(20, runtime_id=next_runtime))
                with self.assertRaisesRegex(LookupError, "no longer observed"):
                    await waiting
                self.assertFalse(self.entities._movement_waiters)
        self.assertEqual(seen, [0, 0, 0, 0])

    async def test_dimension_change_ends_the_wait_before_reused_ids(self):
        await self.deliver(self.spawn())
        waiting = await self.arm(8, lambda state: state.position[0] == 20)
        await self.deliver(self.b.ChangeDimensionPacket(dimension_id=1), self.spawn(), self.delta(20))
        with self.assertRaisesRegex(RuntimeError, "dimension changed"):
            await waiting
        self.assertFalse(self.entities._movement_waiters)

    async def test_cancellation_and_timeout_unsubscribe(self):
        await self.deliver(self.spawn())
        seen = []
        waiting = await self.arm(8, seen.append)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_entity_movement_matching(8, seen.append, timeout=0)
        self.assertFalse(self.entities._movement_waiters)
        await self.deliver(self.delta(20))
        self.assertEqual([state.position[0] for state in seen], [0, 0])

    async def test_predicate_failures_only_reach_their_own_waiter(self):
        await self.deliver(self.spawn())
        error = ValueError("predicate failed")
        exhausted = StopIteration("predicate exhausted")

        def raise_after_movement(failure):
            def predicate(state):
                if state.position[0] == 5:
                    raise failure
                return False
            return predicate

        failed = await self.arm(8, raise_after_movement(error))
        cancelled = await self.arm(8, raise_after_movement(asyncio.CancelledError()))
        stopped = await self.arm(8, raise_after_movement(exhausted))
        successful = await self.arm(8, lambda state: state.position[0] == 20)
        await self.deliver(self.delta(5), self.delta(20))
        with self.assertRaises(ValueError) as caught:
            await failed
        self.assertIs(caught.exception, error)
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        with self.assertRaisesRegex(RuntimeError, "coroutine raised StopIteration") as caught:
            await stopped
        self.assertIs(caught.exception.__cause__, exhausted)
        self.assertEqual((await successful).position[0], 20)
        self.assertFalse(self.entities._movement_waiters)

    async def test_disconnect_ends_wait_before_later_movement(self):
        await self.deliver(self.spawn())
        waiting = await self.arm(8, lambda state: state.position[0] == 20)
        await self.deliver(self.b.DisconnectPacket(), self.delta(20))
        with self.assertRaises(ConnectionError):
            await waiting
        with self.assertRaises(ConnectionError):
            await self.bot.wait_for_entity_movement_matching(8, lambda _: True)
        self.assertFalse(self.entities._movement_waiters)

    async def test_initial_predicate_can_close_the_bot_before_an_unbounded_wait(self):
        await self.deliver(self.spawn())
        loop = asyncio.get_running_loop()
        close_task = None

        def close_bot(_):
            nonlocal close_task
            close_task = asyncio.eager_task_factory(loop, self.bot.close())
            return False

        try:
            with self.assertRaises(ConnectionError):
                async with asyncio.timeout(1):
                    await self.bot.wait_for_entity_movement_matching(8, close_bot, timeout=None)
            self.assertFalse(self.entities._movement_waiters)
        finally:
            if close_task is not None:
                await close_task

    async def test_reader_failure_ends_wait_and_retains_cause(self):
        await self.deliver(self.spawn())
        waiting = await self.arm(8, lambda _: False)
        error = OSError("in-memory transport closed")
        self.wire.incoming.put_nowait(error)
        with self.assertRaises(ConnectionError) as caught:
            await waiting
        self.assertIs(caught.exception.__cause__, error)
        self.assertFalse(self.entities._movement_waiters)
        with self.assertRaises(OSError):
            await self.bot.close()
