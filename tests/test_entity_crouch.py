"""Observe remote crouch changes on the public Bot packet stream."""

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import UUID

from endpilot import Bot
from endpilot.protocol import RawPacket, write_batch
from test_scheduled_input import ManualClock, Wire


class EntityCrouchTests(unittest.IsolatedAsyncioTestCase):
    async def join(self, *startup):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("CrouchWatch", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)
        self.wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.wire.push(b.StartGamePacket(runtime_id=7, entity_id=17))
        for raw in startup:
            self.wire.incoming.put_nowait(write_batch([raw]))
        self.wire.push(b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132")

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket(time=23)
        ready = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in (*packets, marker)))
        await ready

    def entity(self, runtime_id=8):
        return next((entity for entity in self.bot.observed_entities if entity.runtime_id == runtime_id), None)

    def metadata(self, sneaking=None, *, gliding=False, extended=None, malformed=False):
        b = self.b
        items = []
        if sneaking is not None:
            # Protocol 2225 low word has SNEAKING at bit 1 and GLIDING at bit 32.
            word = -(1 << 48) | (sneaking << 1) | (gliding << 32)
            items.append(b.DataItemEntry(id=0, payload=b.DataItemInt64Payload(value=word)))
        elif malformed:
            items.append(b.DataItemEntry(id=0, payload=b.DataItemIntPayload(value=0)))
        if extended is not None:
            items.append(b.DataItemEntry(id=92, payload=b.DataItemInt64Payload(value=extended)))
        return b.SynchedActorData.CopyableDataList(data=items)

    def spawn(self, sneaking=None, *, runtime_id=8, entity_id=108, target_player=None, malformed=False):
        b = self.b
        return b.AddPlayerPacket(
            uuid=UUID(int=entity_id), runtime_id=runtime_id, name=f"Player {entity_id}",
            unpack=self.metadata(sneaking, malformed=malformed),
            abilities_data=b.SerializedAbilitiesData(target_player=entity_id if target_player is None else target_player),
        )

    def update(self, sneaking=None, *, runtime_id=8, **flags):
        return self.b.SetActorDataPacket(id=runtime_id, packed_items=self.metadata(sneaking, **flags))

    def roster(self, entity_id):
        b = self.b
        return b.PlayerListPacket(entries=[b.PlayerListPacket.AddEntry(
            action=b.PlayerListPacketType.ADD, uuid=UUID(int=108), id=entity_id, name="Roster player",
        )])

    async def arm(self, *, runtime_id=None):
        waiting = asyncio.create_task(self.bot.wait_for_entity_crouch_change(runtime_id=runtime_id, timeout=1))
        self.addAsyncCleanup(self.cancel_wait, waiting)
        await asyncio.sleep(0)
        return waiting

    @staticmethod
    async def cancel_wait(waiting):
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)

    async def test_stock_spawn_seeds_crouch_and_metadata_publishes_a_complete_snapshot(self):
        fixture = Path(__file__).with_name("fixtures") / "remote-player-add-2225.hex"
        raw = RawPacket(12, bytes.fromhex(fixture.read_text(encoding="ascii")))
        await self.join(raw)
        initial = self.entity(1)
        self.assertIs(initial.sneaking, False)
        sent = len(self.wire.sent)
        crouch, glides = [], []

        def observe(events, entity):
            events.append((entity, self.entity(entity.runtime_id)))

        self.bot.subscribe_entity_crouch_changes(lambda entity: observe(crouch, entity))
        self.bot.subscribe_entity_glides(lambda entity: observe(glides, entity))
        rising = await self.arm(runtime_id=1)
        await self.deliver(self.spawn(True), self.update(True, runtime_id=1, gliding=True))
        first = self.entity(1)
        self.assertEqual(await rising, first)
        self.assertIs(first.sneaking, True)
        self.assertIs(first.gliding, True)
        self.assertEqual(crouch, [(first, first)])
        self.assertEqual(glides, [(first, first)])
        self.assertIs(self.entity().sneaking, True)

        falling = await self.arm()
        await self.deliver(self.update(False, runtime_id=1))
        current = self.entity(1)
        self.assertEqual(await falling, current)
        self.assertIs(current.sneaking, False)
        self.assertIs(current.gliding, False)
        self.assertEqual(crouch, [(first, first), (current, current)])
        self.assertEqual(glides, [(first, first)])
        self.assertIs(first.sneaking, True)
        self.assertIs(initial.sneaking, False)
        with self.assertRaises(FrozenInstanceError):
            first.sneaking = False
        self.assertEqual(len(self.wire.sent), sent)

    async def test_only_known_remote_player_edges_count(self):
        await self.join()
        b = self.b
        seen, scoped = [], []
        self.bot.subscribe_entity_crouch_changes(seen.append)
        await self.deliver(self.spawn(), self.spawn(malformed=True, runtime_id=9, entity_id=109),
                           self.spawn(False, runtime_id=7, entity_id=17),
                           b.AddActorPacket(runtime_id=10, entity_id=110, actor_type="minecraft:zombie"))
        for invalid in (7, 10, 99):
            with self.assertRaises(LookupError):
                self.bot.subscribe_entity_crouch_changes(scoped.append, runtime_id=invalid)
            with self.assertRaises(LookupError):
                await self.bot.wait_for_entity_crouch_change(runtime_id=invalid, timeout=0)
        self.bot.subscribe_entity_crouch_changes(scoped.append, runtime_id=8)
        waiting = await self.arm(runtime_id=8)
        self.assertIsNone(self.entity().sneaking)
        self.assertIsNone(self.entity(9).sneaking)
        await self.deliver(self.update(malformed=True), self.update(extended=1 << 1))
        self.assertIsNone(self.entity().sneaking)
        await self.deliver(self.update(False, runtime_id=7), self.update(True, runtime_id=7),
                           self.update(False, runtime_id=10), self.update(True, runtime_id=10),
                           self.update(True, runtime_id=9), self.update(False, runtime_id=9),
                           self.update(True), self.update(True), self.update(extended=0))
        self.assertIs(self.entity().sneaking, True)
        await self.deliver(self.update(malformed=True), self.update())
        self.assertIs(self.entity().sneaking, True)
        self.assertIsNone(self.entity(7))
        self.assertIsNone(self.entity(10).sneaking)
        self.assertEqual([(event.runtime_id, event.sneaking) for event in seen], [(9, False)])
        self.assertFalse(waiting.done())
        self.assertEqual(scoped, [])

        await self.deliver(self.update(False), self.update(False))
        self.assertEqual(scoped, [await waiting])
        self.assertEqual([(event.runtime_id, event.sneaking) for event in seen], [(9, False), (8, False)])

    async def test_scoped_observers_end_with_the_instance_or_dimension(self):
        await self.join()
        b = self.b
        seen, scoped = [], []
        self.bot.subscribe_entity_crouch_changes(seen.append)
        await self.deliver(self.spawn(False))
        cases = (
            (LookupError, b.RemoveActorPacket(entity_id=108), self.spawn(False, entity_id=109)),
            (LookupError, self.spawn(False, entity_id=110)),
            (RuntimeError, b.ChangeDimensionPacket(dimension_id=1), self.spawn(False, entity_id=111)),
        )
        for error, *packets in cases:
            with self.subTest(boundary=type(packets[0]).__name__):
                self.bot.subscribe_entity_crouch_changes(scoped.append, runtime_id=8)
                waiting = await self.arm(runtime_id=8)
                await self.deliver(*packets, self.update(True))
                with self.assertRaises(error):
                    await waiting
        self.assertEqual(scoped, [])
        self.assertEqual([event.name for event in seen], ["Player 109", "Player 110", "Player 111"])

    async def test_late_roster_enrichment_preserves_crouch_and_conflict_discards_it(self):
        await self.join()
        seen, scoped = [], []
        self.bot.subscribe_entity_crouch_changes(seen.append)
        await self.deliver(self.spawn(True, target_player=0))
        waiting = await self.arm(runtime_id=8)
        self.bot.subscribe_entity_crouch_changes(scoped.append, runtime_id=8)
        await self.deliver(self.roster(108))
        self.assertEqual(self.entity().entity_id, 108)
        self.assertIs(self.entity().sneaking, True)
        self.assertFalse(waiting.done())
        self.assertEqual(seen, [])
        await self.deliver(self.update(False))
        self.assertEqual(scoped, [await waiting])

        displaced = await self.arm(runtime_id=8)
        await self.deliver(self.roster(109), self.update(True), self.update(False))
        with self.assertRaises(LookupError):
            await displaced
        self.assertIsNone(self.entity().sneaking)

        await self.deliver(self.spawn(True), self.update(False), self.update(True))
        self.assertIsNone(self.entity().sneaking)
        self.assertEqual(seen, scoped)
        self.assertEqual(len(seen), 1)

    async def test_callback_failures_and_wait_cancellation_leave_raw_delivery_running(self):
        await self.join()
        await self.deliver(self.spawn(False))
        seen, errors, raw = [], [], []
        loop = asyncio.get_running_loop()
        self.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
        loop.set_exception_handler(lambda _, details: errors.append(type(details["exception"])))

        def cancel(_):
            raise asyncio.CancelledError

        def fail(_):
            raise ValueError

        unsubscribe = self.bot.subscribe_entity_crouch_changes(cancel)
        self.bot.subscribe_entity_crouch_changes(fail)
        self.bot.subscribe_entity_crouch_changes(seen.append)
        self.bot.packets.subscribe_raw(raw.append)
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_entity_crouch_change(timeout=0)
        cancelled = await self.arm()
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled

        waiting = await self.arm()
        packet = self.update(True)
        await self.deliver(packet)
        self.assertEqual(seen, [await waiting])
        self.assertIn(self.bot.protocol.encode(packet), raw)
        self.assertEqual(errors, [asyncio.CancelledError, ValueError])
        unsubscribe()
        unsubscribe()
        await self.deliver(self.update(False))
        self.assertEqual([event.sneaking for event in seen], [True, False])
        self.assertEqual(errors, [asyncio.CancelledError, ValueError, ValueError])

    async def test_disconnect_ends_scoped_and_global_waits(self):
        await self.join()
        await self.deliver(self.spawn(False))
        seen = []
        self.bot.subscribe_entity_crouch_changes(seen.append)
        waits = (await self.arm(runtime_id=8), await self.arm())
        await self.deliver(self.b.DisconnectPacket(), self.update(True))
        for waiting in waits:
            with self.assertRaises(ConnectionError):
                await waiting
        self.assertEqual(seen, [])
