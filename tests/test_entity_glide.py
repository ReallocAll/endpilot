"""Observe remote glide state and starts through the public Bot API."""

import asyncio
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import UUID

from endpilot import Bot
from endpilot.protocol import RawPacket, write_batch
from test_scheduled_input import ManualClock, Wire


class _GlideWireTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(ManualClock().patch_scheduler())

    async def join(self, *, runtime_id=7, entity_id=17, startup=()):
        self.bot = Bot("GlideWatch")
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)
        self.wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.wire.push(b.StartGamePacket(runtime_id=runtime_id, entity_id=entity_id))
        if startup:
            for raw in startup:
                self.wire.incoming.put_nowait(write_batch([raw]))
        else:
            self.wire.push(b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132")

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket(time=23)
        waiting = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(
            packet if isinstance(packet, RawPacket) else self.bot.protocol.encode(packet)
            for packet in (*packets, marker)
        ))
        await waiting

    def entity(self, runtime_id=8):
        return next((entity for entity in self.bot.observed_entities if entity.runtime_id == runtime_id), None)


class EntityGlideTests(_GlideWireTestCase):
    def flags(self, gliding):
        b = self.b
        # The two words can arrive independently. The same bit of word 92 is distinct.
        items = [b.DataItemEntry(id=92, payload=b.DataItemInt64Payload(value=1 << 32))]
        if gliding is not None:
            items.append(b.DataItemEntry(id=0, payload=b.DataItemInt64Payload(value=(-1 << 40) | (gliding << 32))))
        return b.SynchedActorData.CopyableDataList(data=items)

    def spawn(self, gliding, *, runtime_id=8, entity_id=108):
        b = self.b
        return b.AddPlayerPacket(
            uuid=UUID(int=entity_id), runtime_id=runtime_id, name=f"Player {entity_id}", unpack=self.flags(gliding),
            abilities_data=b.SerializedAbilitiesData(target_player=entity_id),
        )

    def update(self, gliding, runtime_id=8):
        return self.b.SetActorDataPacket(id=runtime_id, packed_items=self.flags(gliding))

    async def test_only_a_known_false_to_true_flag_emits_a_start(self):
        await self.join()
        sent = len(self.wire.sent)
        seen = []
        self.bot.subscribe_entity_glides(seen.append)
        await self.deliver(self.spawn(None), self.spawn(False, runtime_id=9, entity_id=109))
        self.assertIsNone(self.entity().gliding)
        await self.deliver(self.update(True), self.update(True), self.update(None))
        self.assertIs(self.entity().gliding, True)
        self.assertIs(self.entity(9).gliding, False)
        self.assertEqual(seen, [])

        await self.deliver(self.update(False, 7), self.update(True, 7), self.update(True, 9))
        self.assertIsNone(self.entity(7))
        self.assertEqual([event.runtime_id for event in seen], [9])
        await self.deliver(self.update(False))
        self.assertIs(self.entity().gliding, False)
        await self.deliver(self.update(True), self.update(None), self.update(True))
        self.assertEqual([event.runtime_id for event in seen], [9, 8])
        self.assertIs(seen[-1], self.entity())
        self.assertIs(self.entity().gliding, True)
        await self.deliver(self.update(False))
        self.assertIs(self.entity().gliding, False)
        self.assertIs(seen[-1].gliding, True)
        with self.assertRaises(FrozenInstanceError):
            seen[-1].gliding = False
        self.assertEqual(len(self.wire.sent), sent)

    async def test_removal_and_runtime_replacement_reset_the_instance(self):
        await self.join()
        b = self.b
        seen = []
        self.bot.subscribe_entity_glides(seen.append)
        await self.deliver(self.spawn(False), self.update(True))
        await self.deliver(b.RemoveActorPacket(entity_id=108), self.update(False), self.update(True))
        self.assertIsNone(self.entity())
        await self.deliver(self.spawn(True))
        self.assertIs(self.entity().gliding, True)
        await self.deliver(self.spawn(None, entity_id=109), b.RemoveActorPacket(entity_id=108))
        self.assertIsNone(self.entity().gliding)
        await self.deliver(self.update(True))
        self.assertEqual([event.name for event in seen], ["Player 108"])
        await self.deliver(self.update(False), self.update(True))
        self.assertEqual([event.name for event in seen], ["Player 108", "Player 109"])
        await self.deliver(b.AddActorPacket(runtime_id=8, entity_id=110, actor_type="minecraft:zombie"),
                           self.update(False), self.update(True))
        self.assertIsNone(self.entity().gliding)
        self.assertEqual(len(seen), 2)

    async def test_conflicting_roster_identity_clears_the_previous_flag(self):
        await self.join()
        b = self.b

        def roster(entity_id):
            return b.PlayerListPacket(entries=[b.PlayerListPacket.AddEntry(
                action=b.PlayerListPacketType.ADD, uuid=UUID(int=108), id=entity_id, name="Player 108",
            )])

        seen = []
        self.bot.subscribe_entity_glides(seen.append)
        await self.deliver(self.spawn(False), roster(108), self.update(True))
        self.assertEqual([event.entity_id for event in seen], [108])
        await self.deliver(roster(109), self.update(False), self.update(True))
        self.assertEqual(self.entity().entity_id, 109)
        self.assertIsNone(self.entity().gliding)
        self.assertEqual(len(seen), 1)

    async def test_subscriptions_are_session_scoped_and_isolate_errors(self):
        with self.assertRaises(RuntimeError):
            Bot("NotJoined").subscribe_entity_glides(lambda _: None)
        await self.join()
        loop = asyncio.get_running_loop()
        self.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
        errors, seen = [], []
        loop.set_exception_handler(lambda _, details: errors.append(type(details["exception"])))

        def first(entity):
            self.assertIs(entity, self.entity())
            stop_second()
            stop_second()
            raise asyncio.CancelledError

        def second(_):
            raise ValueError

        self.bot.subscribe_entity_glides(first)
        stop_second = self.bot.subscribe_entity_glides(second)
        stop = self.bot.subscribe_entity_glides(seen.append)
        await self.deliver(self.spawn(False), self.update(True), self.update(False), self.update(True))
        self.assertEqual(errors, [asyncio.CancelledError, ValueError, asyncio.CancelledError])
        self.assertEqual(len(seen), 2)
        stop()
        await self.deliver(self.update(False), self.update(True), self.b.DisconnectPacket(), self.update(False),
                           self.update(True))
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(errors), 4)
        with self.assertRaises(ConnectionError):
            self.bot.subscribe_entity_glides(seen.append)
        stop()


class EntityGlideCapture(_GlideWireTestCase):
    async def check_capture(self):
        rows = [json.loads(line) for line in self.capture.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 162)
        scenarios = (
            ("pilot", 1, 2, 196, None, {
                389: (False, 0), 936: (True, 1), 965: (False, 1), 987: (None, 1),
                992: (False, 1), 1012: (None, 1),
            }),
            ("witness", 2, 1, 248, False, {
                366: (False, 0), 479: (False, 0), 495: (False, 0), 501: (True, 1), 534: (True, 1),
                560: (False, 1), 569: (False, 1), 574: (True, 2), 596: (False, 2), 615: (False, 2),
                621: (True, 3), 623: (True, 3), 626: (False, 3), 652: (False, 3), 702: (False, 3),
                714: (True, 4), 725: (True, 4), 729: (True, 4), 812: (False, 4), 837: (False, 4),
                878: (False, 4), 883: (True, 5), 903: (None, 5), 937: (True, 5),
            }),
            ("witness_rejoin", 5, 1, 267, True, {438: (True, 0), 454: (False, 0), 494: (False, 0)}),
        )
        for connection, own_runtime, remote_runtime, spawn, initial, checkpoints in scenarios:
            captured = {row["index"]: RawPacket(row["header"], bytes.fromhex(row["body_hex"]))
                        for row in rows if row["connection"] == connection}
            self.assertLessEqual(checkpoints.keys(), captured.keys())
            await self.join(runtime_id=own_runtime, entity_id=-4294967295 if own_runtime == 1 else -4294967294,
                            startup=[raw for index, raw in captured.items() if index <= spawn])
            sent = len(self.wire.sent)
            seen = []
            self.bot.subscribe_entity_glides(seen.append)
            self.assert_capture_state(remote_runtime, initial)
            for index, raw in captured.items():
                if index > spawn:
                    await self.deliver(raw)
                if index in checkpoints:
                    with self.subTest(connection=connection, index=index):
                        gliding, starts = checkpoints[index]
                        self.assert_capture_state(remote_runtime, gliding)
                        self.assertEqual(len(seen), starts)
            if connection == "pilot":
                self.assert_capture_state(2, None)
                self.assert_capture_state(5, False)
            remote_id = -4294967294 if connection == "pilot" else -4294967295
            for event in seen:
                self.assertEqual((event.runtime_id, event.entity_id), (remote_runtime, remote_id))
                self.assertIs(event.gliding, True)
            self.assertIsNone(self.entity(own_runtime))
            self.assertEqual(len(self.wire.sent), sent)
            await self.bot.close()

    def assert_capture_state(self, runtime_id, gliding):
        state = self.entity(runtime_id)
        if gliding is None:
            self.assertIsNone(state)
        else:
            self.assertIsNotNone(state)
            self.assertIs(state.gliding, gliding)


if __name__ == "__main__":
    # Keep the external capture in the workspace archive. Run its acceptance case explicitly.
    EntityGlideCapture.capture = Path(sys.argv[1])
    unittest.main(argv=sys.argv[:1], defaultTest="EntityGlideCapture.check_capture")
