import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from weakref import ref

from endpilot import Bot
from endpilot.protocol import write_batch
from endpilot.scoreboard import (
    ScoreboardDisplayChange, ScoreboardIdentityChange, ScoreboardObjectiveChange, ScoreboardScoreChange,
)


class ScoreboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Bot("ScoreboardObserver", protocol=2225)
        self.binding = self.bot.protocol.binding
        self.addAsyncCleanup(self.bot.close)

    def batch(self, *packets):
        return write_batch(map(self.bot.protocol.encode, packets))

    async def join(self, *before_spawn):
        b = self.binding
        self.incoming = asyncio.Queue()
        transport = SimpleNamespace(recv=self.incoming.get, send=AsyncMock(), close=AsyncMock())
        self.incoming.put_nowait(self.batch(b.NetworkSettingsPacket(compression_algorithm=65535)))
        self.incoming.put_nowait(self.batch(
            b.StartGamePacket(runtime_id=7, entity_id=17),
            *before_spawn,
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        ))
        with patch("endpilot.transport.nethernet.connect", return_value=transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132")

    async def deliver(self, *packets):
        marker = self.binding.SetTimePacket()
        wait = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.incoming.put_nowait(self.batch(*packets, marker))
        await wait

    def fake_score(self, name, id, value):
        return self.binding.ChangeFakePlayerScore(
            objective_name=name, scoreboard_id=id, score_value=value, fake_player_name=f"Fake{id}",
        )

    def display(self, slot, objective, order=0):
        return self.binding.SetDisplayObjectivePacket(
            display_slot_name=slot, objective_name=objective, objective_display_name=f"{objective} points",
            criteria_name="dummy", sort_order=order,
        )

    async def test_preserves_pre_spawn_state_in_wire_order_and_resets_on_reconnect(self):
        b = self.binding
        await self.join(
            b.SetScorePacket(score_info=[self.fake_score("initial", 18, 3)]),
            self.display("sidebar", "initial"),
            b.SetScorePacket(score_info=[self.fake_score("initial", 18, -9)]),
        )
        seen = self.bot.scoreboard
        self.assertEqual(seen.objectives["initial"].display_name, "initial points")
        self.assertEqual(seen.objectives["initial"].criteria_name, "dummy")
        self.assertEqual([(entry.value, entry.fake_player_name) for entry in seen.scores_in_slot("sidebar")], [(-9, "Fake18")])

        await self.bot.close()
        with self.assertRaises(RuntimeError):
            _ = self.bot.scoreboard
        await self.join()
        self.assertFalse(self.bot.scoreboard.objectives)
        self.assertFalse(self.bot.scoreboard.display_slots)
        self.assertFalse(self.bot.scoreboard.player_identities)
        self.assertEqual(seen.scores_in_slot("sidebar")[0].value, -9)

    async def test_mixed_scores_identity_updates_and_display_sort_and_removal(self):
        b = self.binding
        await self.join()
        await self.deliver(b.SetScorePacket(score_info=[
            self.fake_score("ranking", 1, 13),
            self.fake_score("other", 1, 4),
            b.ChangePlayerScore(
                objective_name="ranking", scoreboard_id=2, score_value=10,
                player_id=b.PlayerScoreboardId(actor_unique_id=-500),
            ),
            b.RemoveScore(scoreboard_id=1, objective_name="missing"),
            b.ChangeEntityScore(objective_name="ranking", scoreboard_id=3, score_value=-5, entity_id=181),
        ]))
        initial = self.bot.scoreboard
        self.assertIsNone(initial.objectives["ranking"].display_name)
        self.assertIsNone(initial.objectives["ranking"].criteria_name)
        scores = {entry.scoreboard_id: entry for entry in initial.objectives["ranking"].scores}
        self.assertEqual((scores[1].kind, scores[1].fake_player_name), ("fake_player", "Fake1"))
        self.assertEqual((scores[2].kind, scores[2].player_unique_id), ("player", -500))
        self.assertEqual((scores[3].kind, scores[3].entity_unique_id), ("entity", 181))
        self.assertEqual([entry.value for entry in initial.objectives["other"].scores], [4])

        await self.deliver(
            self.display("list", "ranking", b.ObjectiveSortOrder.ASCENDING),
            self.display("sidebar", "ranking", b.ObjectiveSortOrder.DESCENDING),
            self.display("belowname", "other"),
            b.SetScoreboardIdentityPacket(type=b.ScoreboardIdentityPacketType.UPDATE, identity_info=[
                b.ScoreboardIdentityPacketInfo(scoreboard_id=2, player_id=-501),
            ]),
        )
        displayed = self.bot.scoreboard
        self.assertEqual([entry.value for entry in displayed.scores_in_slot("list")], [-5, 10, 13])
        self.assertEqual([entry.value for entry in displayed.scores_in_slot("sidebar")], [13, 10, -5])
        self.assertEqual(displayed.player_identities, {2: -501})

        await self.deliver(b.SetScorePacket(score_info=[
            b.RemoveScore(scoreboard_id=1, objective_name="ranking"),
            self.fake_score("ranking", 4, 2),
            self.fake_score("other", 2, 1),
            b.RemoveScore(scoreboard_id=2),
        ]))
        self.assertEqual([entry.value for entry in self.bot.scoreboard.scores_in_slot("sidebar")], [2, -5])
        self.assertEqual([entry.value for entry in self.bot.scoreboard.scores_in_slot("belowname")], [4])
        self.assertEqual([entry.value for entry in displayed.scores_in_slot("sidebar")], [13, 10, -5])

        await self.deliver(
            self.display("list", ""),
            b.SetScoreboardIdentityPacket(type=b.ScoreboardIdentityPacketType.REMOVE, identity_info=[
                b.ScoreboardIdentityPacketInfo(scoreboard_id=2),
            ]),
        )
        self.assertEqual(self.bot.scoreboard.scores_in_slot("list"), ())
        self.assertIn("ranking", self.bot.scoreboard.objectives)
        self.assertEqual(self.bot.scoreboard.player_identities, {})

        await self.deliver(
            self.display("list", "ranking"),
            b.RemoveObjectivePacket(objective_name="ranking"),
        )
        self.assertEqual(set(self.bot.scoreboard.objectives), {"other"})
        self.assertEqual(set(self.bot.scoreboard.display_slots), {"belowname"})

    async def test_observes_ordered_stable_changes_for_every_entry_in_a_packet(self):
        b = self.binding
        await self.join(b.SetScorePacket(score_info=[self.fake_score("ranking", 1, 10)]))
        received = []

        def observe(changes):
            received.append((changes, self.bot.scoreboard))

        self.bot.subscribe_scoreboard(observe)
        self.assertEqual(received, [])

        await self.deliver(b.SetScorePacket(score_info=[
            self.fake_score("ranking", 1, 20),
            b.RemoveScore(scoreboard_id=1, objective_name="ranking"),
            self.fake_score("ranking", 2, -4),
            self.fake_score("ranking", 2, -4),
        ]))
        (changes, committed), = received
        self.assertEqual([type(change) for change in changes], [ScoreboardScoreChange] * 3)
        self.assertEqual([change.objective_name for change in changes], ["ranking"] * 3)
        self.assertEqual((changes[0].before.value, changes[0].after.value), (10, 20))
        self.assertEqual(changes[1].before, changes[0].after)
        self.assertIsNone(changes[1].after)
        self.assertIsNone(changes[2].before)
        self.assertEqual(changes[2].after.value, -4)
        self.assertEqual(committed.objectives["ranking"].scores, (changes[2].after,))

        display = self.display("sidebar", "ranking")
        await self.deliver(
            display,
            b.SetScoreboardIdentityPacket(type=b.ScoreboardIdentityPacketType.UPDATE, identity_info=[
                b.ScoreboardIdentityPacketInfo(scoreboard_id=2, player_id=9),
                b.ScoreboardIdentityPacketInfo(scoreboard_id=2, player_id=11),
            ]),
            display,
            b.RemoveObjectivePacket(objective_name="ranking"),
        )
        displayed, identity, removed = (batch for batch, _ in received[1:])
        objective_change, slot_change = displayed
        self.assertIsInstance(objective_change, ScoreboardObjectiveChange)
        self.assertIsNone(objective_change.before.display_name)
        self.assertEqual(objective_change.after.display_name, "ranking points")
        self.assertIsInstance(slot_change, ScoreboardDisplayChange)
        self.assertEqual(slot_change.slot, "sidebar")
        self.assertIsNone(slot_change.before)
        self.assertEqual(identity, (
            ScoreboardIdentityChange(2, None, 9), ScoreboardIdentityChange(2, 9, 11),
        ))
        self.assertEqual(removed, (
            ScoreboardObjectiveChange("ranking", objective_change.after, None),
            ScoreboardDisplayChange("sidebar", slot_change.after, None),
        ))
        self.assertEqual(changes[0].after.value, 20)
        self.assertEqual(changes[1].before.fake_player_name, "Fake1")

    async def test_observer_unsubscribes_independently_and_ends_on_close(self):
        b = self.binding
        await self.join()
        observed = []

        def broken(_changes):
            raise ValueError("observer failure")

        self.bot.subscribe_scoreboard(broken)

        def collect(changes):
            observed.append(changes)

        collect_ref = ref(collect)
        first = self.bot.subscribe_scoreboard(collect)
        remaining = self.bot.subscribe_scoreboard(collect)
        del collect
        first()
        first()

        with patch.object(asyncio.get_running_loop(), "call_exception_handler") as report:
            await self.deliver(b.SetScorePacket(score_info=[self.fake_score("ranking", 3, 6)]))
            self.assertEqual(report.call_count, 1)
            report.reset_mock()  # The exception traceback otherwise retains the dispatch frame.
        self.assertEqual(len(observed), 1)
        self.assertEqual([type(change) for change in observed[0]], [ScoreboardObjectiveChange, ScoreboardScoreChange])
        await self.bot.close()
        self.assertIsNone(collect_ref())

        await self.join()
        fresh = []
        self.bot.subscribe_scoreboard(fresh.append)
        remaining()
        await self.deliver(b.SetScorePacket(score_info=[self.fake_score("ranking", 3, 8)]))
        self.assertEqual(len(observed), 1)
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0][-1].after.value, 8)

    async def test_cancelled_callback_leaves_packet_reader_and_batch_recipients_active(self):
        b = self.binding
        await self.join()
        loop = asyncio.get_running_loop()
        cancelled = loop.create_future()
        cancelled.cancel()
        observed = []
        added_during_delivery = []

        def cancel_observer(_changes):
            stop_original()
            self.bot.subscribe_scoreboard(added_during_delivery.append)
            cancelled.result()

        stop_cancelled = self.bot.subscribe_scoreboard(cancel_observer)
        stop_original = self.bot.subscribe_scoreboard(observed.append)
        with patch.object(loop, "call_exception_handler") as report:
            # deliver also waits for the unrelated SetTime packet after the score.
            await self.deliver(b.SetScorePacket(score_info=[self.fake_score("ranking", 3, 6)]))
            report.assert_called_once()
            self.assertIsInstance(report.call_args.args[0]["exception"], asyncio.CancelledError)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][-1].after.value, 6)
        self.assertEqual(added_during_delivery, [])

        stop_cancelled()
        await self.deliver(b.SetScorePacket(score_info=[self.fake_score("ranking", 3, 8)]))
        self.assertEqual(len(observed), 1)
        (later_changes,) = added_during_delivery
        self.assertEqual(later_changes[0].before.value, 6)
        self.assertEqual(later_changes[0].after.value, 8)
