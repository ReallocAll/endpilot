import asyncio
import json
import unittest
from unittest.mock import patch

from endpilot import Bot, PlayerRespawnPoint, SleepRejected
from endpilot.compression import PacketConnection
from endpilot.login import JoinSession
from endpilot.protocol import read_batch, write_batch


FOOT = (8, 220, 9)
HEAD = (8, 220, 10)


class Transport:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    async def recv(self):
        frame = await self.incoming.get()
        if isinstance(frame, Exception):
            raise frame
        return frame

    async def send(self, frame):
        self.outgoing.put_nowait(frame)

    async def close(self):
        pass


class BedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("BedTester", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = self.bot.protocol.binding
        self.transport = Transport()
        settings = self.b.NetworkSettingsPacket(
            compression_algorithm=self.b.PacketCompressionAlgorithm.ZLIB, compression_threshold=0,
        )
        packets = PacketConnection(self.transport, self.bot.protocol, settings)
        session = JoinSession(packets, self.bot.identity, "http://127.0.0.1:19132")
        session.player_id, session.player_unique_id = 42, 7
        session.block_network_ids_are_hashes = True
        with patch.object(JoinSession, "connect_nethernet", return_value=session):
            await self.bot.join_nethernet("http://127.0.0.1:19132")
        self.deliver(
            self.b.ServerPlayerPostMovePositionPacket(pos=self.b.Vec3(x=8.5, y=221.62, z=7.5)),
            self.block(FOOT, 313457523), self.block(HEAD, 2268230312), self.metadata(42, 0),
        )
        await asyncio.sleep(0)

    def position(self, position):
        return self.b.BlockPos(x=position[0], y=position[1], z=position[2])

    def block(self, position, runtime_id):
        return self.b.UpdateBlockPacket(pos=self.position(position), runtime_id=runtime_id, layer=0, update_flags=0)

    def metadata(self, actor, flags=None, bed=None):
        b = self.b
        entries = []
        if flags is not None:
            entries.append(b.DataItemEntry(id=int(b.ActorDataIDs.PLAYER_FLAGS), payload=b.DataItemBytePayload(value=flags)))
        if bed is not None:
            entries.append(b.DataItemEntry(id=int(b.ActorDataIDs.BED_POSITION), payload=b.DataItemPosPayload(value=self.position(bed))))
        return b.SetActorDataPacket(id=actor, packed_items=b.SynchedActorData.CopyableDataList(data=entries))

    def message(self, key, *, type=None, prefix=None, localize=False):
        if type is None:
            type = self.b.TextPacketType.TEXT_OBJECT
        if prefix is None:
            prefix = () if key == "tile.bed.notValid" else ({"text": "§7"},)
        return self.b.TextPacket(localize=localize, body=self.b.TextPacket.MessageOnly(
            type=type, message=json.dumps({"rawtext": [*prefix, {"translate": key}]}),
        ))

    def spawn(self, position, block, *, personal=True):
        b = self.b
        spawn_type = b.SpawnPositionType.PLAYER_RESPAWN if personal else b.SpawnPositionType.WORLD_SPAWN
        return b.SetSpawnPositionPacket(spawn_pos_type=spawn_type, pos=self.position(position),
                                        spawn_block_pos=self.position(block), dimension_type=0)

    def deliver(self, *packets):
        self.transport.incoming.put_nowait(b"\xff" + write_batch(map(self.bot.protocol.encode, packets)))

    async def sent(self, kind):
        async with asyncio.timeout(1):
            while True:
                raw = next(read_batch((await self.transport.outgoing.get())[1:]))
                if raw.packet_id == kind.ID:
                    return self.bot.protocol.decode(raw)

    async def test_bed_events_follow_local_sleep_transitions_after_cache_updates(self):
        bot, b = self.bot, self.b
        events = []
        bot.subscribe_bed_events(lambda event: events.append((event, bot.is_sleeping, bot.sleeping_bed)))
        self.assertTrue(self.transport.outgoing.empty())
        self.deliver(self.metadata(42, 4), self.metadata(43, 2, HEAD), self.metadata(42, bed=HEAD))
        await asyncio.sleep(0)
        self.assertEqual(events, [])
        self.deliver(self.metadata(42, 2, HEAD), self.metadata(42, 6), self.metadata(42, bed=HEAD),
                     self.metadata(43, 0), self.metadata(42, 4), self.metadata(42, 0))
        await asyncio.sleep(0)
        self.assertEqual(events, [("sleep", True, HEAD), ("wake", False, None)])
        self.assertTrue(self.transport.outgoing.empty())

        dimension = b.ChangeDimensionPacket(dimension_id=1, pos=b.Vec3(x=8.5, y=221.62, z=7.5))
        self.deliver(self.metadata(42, 2), dimension, self.metadata(42, 0))
        await asyncio.sleep(0)
        self.assertEqual(events[-1:], [("sleep", True, HEAD)])
        self.assertEqual(len(events), 3)
        self.deliver(dimension, self.metadata(42, 2, HEAD))
        await asyncio.sleep(0)
        self.assertEqual(events[3:], [("sleep", True, HEAD)])

    async def test_bed_invalidation_uses_native_text_even_without_known_point(self):
        bot, b = self.bot, self.b
        events = []
        bot.subscribe_bed_events(lambda event: events.append((event, bot.player_respawn_point)))
        invalid = self.message("tile.bed.notValid")
        chat = b.TextPacket(body=b.TextPacket.AuthorAndMessage(
            type=b.TextPacketType.CHAT, author="OtherPlayer", message=invalid.body.message,
        ))
        alternate = (11, 220, 10)
        self.deliver(
            self.spawn((8, 220, 7), HEAD), self.spawn((11, 220, 7), alternate),
            self.spawn((11, 220, 7), alternate), self.spawn((1, 0, 2), HEAD, personal=False),
            self.block(HEAD, 0xDBF44120), self.message("tile.bed.respawnSet"), self.message("tile.bed.noSleep"),
            self.message("tile.bed.notValid", prefix=({"text": "Quoted: "},)),
            self.message("tile.bed.notValid", localize=True), chat,
        )
        await asyncio.sleep(0)
        self.assertEqual(events, [])
        self.assertEqual(bot.player_respawn_point, PlayerRespawnPoint((11, 220, 7), 0, alternate))
        self.deliver(invalid)
        await asyncio.sleep(0)
        self.assertEqual(events, [("respawn_point_invalidated", None)])
        self.deliver(invalid)
        await asyncio.sleep(0)
        self.assertEqual(events, [("respawn_point_invalidated", None)] * 2)
        self.assertTrue(self.transport.outgoing.empty())

    async def test_bed_subscription_removes_one_registration_and_ends_at_disconnect(self):
        bot = self.bot
        events = []
        callback = events.append
        cancel = bot.subscribe_bed_events(callback)
        bot.subscribe_bed_events(callback)
        cancel()
        cancel()
        self.deliver(self.metadata(42, 2, HEAD), self.b.DisconnectPacket(), self.metadata(42, 0),
                     self.message("tile.bed.notValid"))
        self.assertEqual((await bot.wait_for_session_end()).kind, "server_disconnect")
        self.assertEqual(events, ["sleep"])
        with self.assertRaises(ConnectionError):
            bot.subscribe_bed_events(callback)

        await bot.close()
        with self.assertRaisesRegex(RuntimeError, "has not joined"):
            bot.subscribe_bed_events(callback)
        await self.join_bed_stream(bot=bot)
        fresh = []
        bot.subscribe_bed_events(fresh.append)
        reader_transport = bot.packets.transport
        reader_transport.incoming.put_nowait(write_batch([bot.protocol.encode(self.metadata(42, 2, HEAD))]))
        await asyncio.sleep(0)
        self.assertEqual(fresh, ["sleep"])
        self.assertEqual(events, ["sleep"])

    async def test_bed_subscription_ends_when_the_reader_ends(self):
        events = []
        cancel = self.bot.subscribe_bed_events(events.append)
        self.transport.incoming.put_nowait(ConnectionError("test transport lost"))
        self.assertEqual((await self.bot.wait_for_session_end()).kind, "reader_end")
        with self.assertRaises(ConnectionError):
            self.bot.subscribe_bed_events(events.append)
        cancel()
        self.assertEqual(events, [])
        with self.assertRaisesRegex(ConnectionError, "test transport lost"):
            await self.bot.close()

    async def test_sleep_requires_own_state_and_target_bed_and_wake_accepts_delta(self):
        b = self.b
        sleeping = asyncio.create_task(self.bot.sleep(FOOT, timeout=1))
        click = (await self.sent(b.InventoryTransactionPacket)).variant_transaction
        self.assertEqual(click.face, 1)
        self.assertEqual((click.pos.x, click.pos.y, click.pos.z), FOOT)
        self.assertEqual(click.target_block_id, 313457523)
        self.deliver(self.metadata(43, 2, HEAD), self.block(HEAD, 3323536883))
        await asyncio.sleep(0)
        self.assertTrue(self.bot.observed_bed(FOOT).occupied)
        self.assertFalse(self.bot.is_sleeping)
        self.assertFalse(sleeping.done())

        self.deliver(self.metadata(42, 6, (9, 220, 10)))
        await asyncio.sleep(0)
        self.assertFalse(sleeping.done())
        self.deliver(self.metadata(42, bed=HEAD))
        self.assertEqual(await sleeping, HEAD)
        self.assertEqual(self.bot.sleeping_bed, HEAD)

        waking = asyncio.create_task(self.bot.wake(timeout=1))
        stop = await self.sent(b.PlayerActionPacket)
        self.assertEqual((stop.runtime_id, stop.action), (42, b.PlayerActionType.STOP_SLEEPING))
        self.deliver(self.metadata(43, 0))
        await asyncio.sleep(0)
        self.assertFalse(waking.done())
        self.deliver(self.metadata(42, 4))
        await waking
        self.assertFalse(self.bot.is_sleeping)
        self.assertIsNone(self.bot.sleeping_bed)

        same_bed = asyncio.create_task(self.bot.sleep(HEAD, timeout=1))
        await self.sent(b.InventoryTransactionPacket)
        self.deliver(self.metadata(42, 2))
        self.assertEqual(await same_bed, HEAD)

    async def test_sleep_rechecks_the_bed_position_after_the_matching_flag(self):
        other = (11, 220, 10)
        self.deliver(self.metadata(42, 0, HEAD))
        await asyncio.sleep(0)
        sleeping = asyncio.create_task(self.bot.sleep(FOOT, timeout=1))
        await self.sent(self.b.InventoryTransactionPacket)
        self.deliver(self.metadata(42, 2), self.metadata(42, bed=other))
        done, _ = await asyncio.wait({sleeping}, timeout=0.05)
        self.assertFalse(done)
        self.assertEqual(self.bot.sleeping_bed, other)
        self.deliver(self.metadata(42, bed=HEAD))
        self.assertEqual(await sleeping, HEAD)
        while not self.transport.outgoing.empty():
            raw = next(read_batch(self.transport.outgoing.get_nowait()[1:]))
            self.assertNotEqual(raw.packet_id, self.b.InventoryTransactionPacket.ID)

    async def test_natural_wake_during_wait_registration_sends_no_stop(self):
        self.deliver(self.metadata(42, 2, HEAD))
        await asyncio.sleep(0)
        self.assertTrue(self.bot.is_sleeping)
        waking = asyncio.create_task(self.bot.wake(timeout=0.2))
        self.deliver(self.metadata(42, 0))
        await waking
        self.assertFalse(self.bot.is_sleeping)
        while not self.transport.outgoing.empty():
            raw = next(read_batch(self.transport.outgoing.get_nowait()[1:]))
            self.assertNotEqual(raw.packet_id, self.b.PlayerActionPacket.ID)

    async def test_daylight_rejection_and_personal_respawn_are_independent(self):
        b = self.b
        sleeping = asyncio.create_task(self.bot.sleep(FOOT, timeout=1))
        await self.sent(b.InventoryTransactionPacket)
        spawn = self.spawn((8, 220, 7), HEAD)
        self.deliver(spawn, self.message("tile.bed.respawnSet"), self.message("tile.bed.occupied", type=b.TextPacketType.RAW))
        await asyncio.sleep(0)
        self.assertFalse(sleeping.done())
        self.assertEqual(self.bot.player_respawn_point, PlayerRespawnPoint((8, 220, 7), 0, HEAD))
        self.deliver(self.message("tile.bed.noSleep"))
        with self.assertRaises(SleepRejected) as error:
            await sleeping
        self.assertEqual(error.exception.translation_key, "tile.bed.noSleep")
        self.assertFalse(self.bot.is_sleeping)
        self.deliver(self.block(HEAD, 0xDBF44120))
        await asyncio.sleep(0)
        self.assertIsNone(self.bot.observed_bed(HEAD))
        self.assertIsNotNone(self.bot.player_respawn_point)
        self.deliver(self.message("tile.bed.notValid"))
        await asyncio.sleep(0)
        self.assertIsNone(self.bot.player_respawn_point)

    async def test_quoted_bed_translation_does_not_refuse_or_clear_spawn(self):
        b = self.b
        sleeping = asyncio.create_task(self.bot.sleep(FOOT, timeout=1))
        await self.sent(b.InventoryTransactionPacket)
        self.deliver(
            self.spawn((8, 220, 7), HEAD),
            self.message("tile.bed.notValid", prefix=({"text": "Quoted: "},)),
            self.message("tile.bed.notValid", localize=True),
            self.message("tile.bed.occupied", prefix=({"text": "Quoted: "},)),
            self.message("tile.bed.noSleep", localize=True),
        )
        done, _ = await asyncio.wait({sleeping}, timeout=0.05)
        self.assertFalse(done)
        self.assertEqual(self.bot.player_respawn_point, PlayerRespawnPoint((8, 220, 7), 0, HEAD))
        self.deliver(self.message("tile.bed.occupied"))
        with self.assertRaises(SleepRejected) as error:
            await sleeping
        self.assertEqual(error.exception.translation_key, "tile.bed.occupied")

    async def test_unknown_bed_head_occupation_and_timeout(self):
        self.deliver(self.block(HEAD, 0xDBF44120))
        await asyncio.sleep(0)
        self.assertIsNone(self.bot.observed_bed(HEAD))
        self.assertIsNone(self.bot.observed_bed(FOOT).occupied)
        with self.assertRaises(LookupError):
            await self.bot.sleep(HEAD)
        with self.assertRaises(TimeoutError):
            await self.bot.sleep(FOOT, timeout=0.05)
        await self.bot.stop_sleeping()
        self.assertEqual((await self.sent(self.b.PlayerActionPacket)).action, self.b.PlayerActionType.STOP_SLEEPING)

    async def test_dimension_change_after_ack_in_same_batch_invalidates_sleep(self):
        b = self.b
        sleeping = asyncio.create_task(self.bot.sleep(FOOT, timeout=1))
        await self.sent(b.InventoryTransactionPacket)
        self.deliver(self.metadata(42, 2, HEAD),
                     b.ChangeDimensionPacket(dimension_id=1, pos=b.Vec3(x=8.5, y=221.62, z=7.5)))
        with self.assertRaisesRegex(RuntimeError, "dimension changed"):
            await sleeping
        self.assertIsNone(self.bot.is_sleeping)
        self.assertIsNone(self.bot.observed_bed(FOOT))

    async def join_bed_stream(self, *, bot=None, before_start=(), before_spawn=(), after_spawn=()):
        b = self.b
        if bot is None:
            bot = Bot("BedJoinTester", protocol=2225)
            self.addAsyncCleanup(bot.close)
        transport = Transport()

        def deliver(*packets):
            transport.incoming.put_nowait(write_batch(map(bot.protocol.encode, packets)))

        deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        deliver(
            *before_start, b.StartGamePacket(runtime_id=42, entity_id=7, block_network_ids_are_hashes=True),
            *before_spawn, b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN), *after_spawn,
        )
        with patch("endpilot.transport.nethernet.connect", return_value=transport):
            await bot.join_nethernet("http://127.0.0.1:19132")
        await asyncio.sleep(0)
        return bot

    async def test_bed_subscription_starts_after_the_join_snapshot(self):
        bot = await self.join_bed_stream(before_spawn=(self.metadata(42, 2, HEAD),
                                                     self.message("tile.bed.notValid")))
        events = []
        bot.subscribe_bed_events(events.append)
        self.assertTrue(bot.is_sleeping)
        self.assertEqual(bot.sleeping_bed, HEAD)
        bot.packets.transport.incoming.put_nowait(write_batch(map(bot.protocol.encode, (
            self.metadata(42, 2, HEAD), self.metadata(42, 0),
        ))))
        await asyncio.sleep(0)
        self.assertEqual(events, ["wake"])

    async def test_join_replays_local_bed_packets_after_start_game_in_wire_order(self):
        early = (self.metadata(42, 2, HEAD), self.spawn((8, 220, 7), HEAD))
        early_only = await self.join_bed_stream(before_start=early)
        self.assertIsNone(early_only.is_sleeping)
        self.assertIsNone(early_only.player_respawn_point)

        alternate = (11, 220, 10)
        bot = await self.join_bed_stream(
            before_start=early,
            before_spawn=(
                *early, self.metadata(42, bed=alternate), self.spawn((11, 220, 7), alternate),
                self.metadata(43, 0, (13, 220, 10)), self.spawn((1, 0, 2), (13, 220, 10), personal=False),
            ),
        )
        self.assertTrue(bot.is_sleeping)
        self.assertEqual(bot.sleeping_bed, alternate)
        self.assertEqual(bot.player_respawn_point, PlayerRespawnPoint((11, 220, 7), 0, alternate))

    async def test_join_applies_bed_revocations_in_order_on_both_sides_of_spawn(self):
        b = self.b
        first = (self.metadata(42, 2, HEAD), self.spawn((8, 220, 7), HEAD))
        missing_bed = self.message("tile.bed.notValid")
        change_dimension = b.ChangeDimensionPacket(dimension_id=1, pos=b.Vec3(x=8.5, y=221.62, z=7.5))
        alternate = (11, 220, 10)
        later = (
            self.metadata(42, 2, alternate), self.spawn((11, 220, 7), alternate),
            self.message("tile.bed.notValid", prefix=({"text": "Quoted: "},)),
        )
        for events, expected in (
            ((*first, missing_bed), (True, HEAD, None)),
            ((*first, change_dimension), (None, None, PlayerRespawnPoint((8, 220, 7), 0, HEAD))),
            ((*first, missing_bed, change_dimension), (None, None, None)),
            ((*first, missing_bed, change_dimension, *later),
             (True, alternate, PlayerRespawnPoint((11, 220, 7), 0, alternate))),
        ):
            for phase in ("before_spawn", "after_spawn"):
                with self.subTest(phase=phase, expected=expected):
                    bot = await self.join_bed_stream(**{phase: events})
                    self.assertEqual((bot.is_sleeping, bot.sleeping_bed, bot.player_respawn_point), expected)
