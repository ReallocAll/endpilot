import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from endpilot import Bot, SessionEnd, Weather, WorldClock, WorldSpawn, WorldTime
from endpilot.protocol import write_batch


class WorldEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("WorldObserver", protocol=2225)
        self.binding = self.bot.protocol.binding
        self.incoming = asyncio.Queue()
        self.addAsyncCleanup(self.bot.close)

    def rules(self, **values):
        return self.binding.GameRulesChangedPacketData(rules=[
            self.binding.GameRule(name=name, value=value) for name, value in values.items()
        ])

    def clock_sync(self, *entries):
        binding = self.binding
        return binding.SyncWorldClocksPacket(data=binding.SyncWorldClocksPacket.SyncStateData(clock_data=[
            binding.SyncWorldClockStateData(clock_id=clock_id, time=ticks, is_paused=paused)
            for clock_id, ticks, paused in entries
        ]))

    def clock_registry(self, *entries):
        binding = self.binding
        return binding.SyncWorldClocksPacket(data=binding.SyncWorldClocksPacket.InitializeRegistryData(clock_data=[
            binding.WorldClockData(id=clock_id, name=name, time=ticks, is_paused=paused)
            for clock_id, name, ticks, paused in entries
        ]))

    def event(self, event_id, data):
        return self.binding.LevelEventPacket(event_id=event_id, data=data)

    def deliver(self, *packets, framed=True):
        batch = write_batch([self.bot.protocol.encode(packet) for packet in packets])
        self.incoming.put_nowait((b"\xff" if framed else b"") + batch)

    async def join(self, *, rain=0, thunder=0, before_start=(), before_spawn=(), initial_difficulty=0, initial_hardcore=False,
                   generator=0, default_spawn=(0, 32767, 0), dimension=0, clocks=(
                       (1, "minecraft:overworld", 26345, False), (2, "test:timer", 538, True),
                   )):
        b = self.binding

        async def receive():
            data = await self.incoming.get()
            if isinstance(data, Exception):
                raise data
            return data

        self.transport = SimpleNamespace(recv=receive, send=AsyncMock(), close=AsyncMock())
        self.deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.ZLIB,
                                             compression_threshold=1), framed=False)
        self.deliver(
            self.clock_sync((1, 26345, False)),
            *before_start,
            b.StartGamePacket(runtime_id=10, entity_id=11, level_current_time=253,
                              settings=b.LevelSettings(time=26345, rain_level=rain, lightning_level=thunder,
                                                       game_difficulty=b.Difficulty(initial_difficulty),
                                                       is_hardcore=initial_hardcore,
                                                       generator=generator,
                                                       default_spawn=self.block_pos(default_spawn),
                                                       spawn_settings=b.SpawnSettings(dimension=dimension),
                                                       game_rules=self.rules(doDayLightCycle=False))),
            self.clock_registry(*clocks),
            *before_spawn,
            b.PlayStatusPacket(status=3),
        )
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132")

    def block_pos(self, point):
        return self.binding.BlockPos(**dict(zip("xyz", point, strict=True)))

    def spawn(self, point, *, personal=False, dimension=0):
        b = self.binding
        kind = b.SpawnPositionType.PLAYER_RESPAWN if personal else b.SpawnPositionType.WORLD_SPAWN
        return b.SetSpawnPositionPacket(
            spawn_pos_type=kind, pos=self.block_pos(point), dimension_type=dimension,
            spawn_block_pos=self.block_pos((-2147483648,) * 3),
        )

    def difficulty(self, code):
        return self.binding.SetDifficultyPacket(difficulty=self.binding.Difficulty(code))

    async def apply(self, *packets):
        marker = self.binding.SetTimePacket(time=31)
        observed = await self.arm(self.bot.packets.wait_for_packet(type(marker).ID, timeout=1))
        self.deliver(*packets, marker)
        await observed

    async def arm(self, awaitable):
        task = asyncio.create_task(awaitable)
        await asyncio.sleep(0)
        return task

    async def test_initial_values_and_pre_spawn_updates_survive_public_join(self):
        await self.join(rain=0.75, thunder=0.25, before_spawn=(
            self.event(3001, 65535),
            self.event(3004, 0),
            self.binding.GameRulesChangedPacket(rule_data=self.rules(dodaylightcycle=True)),
        ))
        self.assertEqual(self.bot.world_time, WorldTime(26345, 253, True, (
            WorldClock(1, "minecraft:overworld", 26345, False), WorldClock(2, "test:timer", 538, True),
        )))
        self.assertEqual((self.bot.world_time.daytime, self.bot.world_time.day), (2345, 1))
        self.assertEqual(self.bot.world_time.observed_moon_phase, 1)
        self.assertEqual(self.bot.weather, Weather(True, False, 0.75, None))

    async def test_start_game_separates_public_spawn_height_and_player_dimension(self):
        self.assertIsNone(self.bot.world_difficulty)
        self.assertIsNone(self.bot.world_spawn)
        await self.join(dimension=1, default_spawn=(-14, 32767, 8), before_start=(
            self.difficulty(3), self.spawn((3, 12, 45)),
        ))
        sends = self.transport.send.await_count
        self.assertEqual(self.bot.world_difficulty, 0)
        spawn = self.bot.world_spawn
        self.assertEqual(spawn, WorldSpawn((-14, None, 8), dimension_id=0))
        self.assertEqual(self.transport.send.await_count, sends)
        with self.assertRaises(FrozenInstanceError):
            spawn.position = (0, 10, 0)
        await self.bot.close()
        self.assertIsNone(self.bot.world_difficulty)
        self.assertIsNone(self.bot.world_spawn)
        self.assertEqual(spawn.position, (-14, None, 8))

    async def test_login_updates_apply_in_order_with_personal_respawn_kept_separate(self):
        await self.join(initial_difficulty=2, default_spawn=(1, 2, 3), before_spawn=(
            self.spawn((8, 22, 5)), self.difficulty(1), self.spawn((9, 0, -2)),
            self.spawn((20, 100, 32), personal=True), self.difficulty(0),
        ))
        self.assertEqual(self.bot.world_spawn, WorldSpawn((9, 0, -2), dimension_id=0))
        self.assertEqual(self.bot.world_difficulty, 0)
        self.assertEqual(self.bot.player_respawn_point.position, (20, 100, 32))

    async def test_hardcore_is_independent_of_difficulty_and_the_players_game_mode(self):
        self.assertIsNone(self.bot.world_hardcore)
        b = self.binding
        for hardcore in (False, True):
            with self.subTest(hardcore=hardcore):
                await self.join(initial_difficulty=3, initial_hardcore=hardcore)
                sends = self.transport.send.await_count
                self.assertEqual(self.bot.world_difficulty, 3)
                self.assertIs(self.bot.world_hardcore, hardcore)
                self.assertEqual(self.transport.send.await_count, sends)
                await self.apply(b.SetPlayerGameTypePacket(player_game_type=b.GameType.SPECTATOR), self.difficulty(1))
                self.assertEqual(self.bot.world_difficulty, 1)
                self.assertIs(self.bot.world_hardcore, hardcore)
                await self.bot.close()
                self.assertIsNone(self.bot.world_hardcore)

    async def test_binding_without_hardcore_field_leaves_it_unknown(self):
        def missing(_settings):
            raise AttributeError("is_hardcore")

        with patch.object(self.binding.LevelSettings, "is_hardcore", new=property(missing)):
            await self.join(initial_difficulty=3, initial_hardcore=True)
        self.assertEqual(self.bot.world_difficulty, 3)
        self.assertIsNone(self.bot.world_hardcore)

    async def test_advertised_world_generator_preserves_members_and_unknown_ids(self):
        b = self.binding
        self.assertIsNone(self.bot.advertised_world_generator)
        for expected in (*b.GeneratorType, -17, 127):
            with self.subTest(generator=expected):
                await self.join(generator=int(expected))
                sends = self.transport.send.await_count
                actual = self.bot.advertised_world_generator
                self.assertIs(type(actual), type(expected))
                self.assertEqual(actual, expected)
                self.assertEqual(self.transport.send.await_count, sends)
                await self.bot.close()
                self.assertIsNone(self.bot.advertised_world_generator)

    async def test_advertised_world_generator_stays_at_login_value_until_disconnect(self):
        b = self.binding
        await self.join(generator=b.GeneratorType.FLAT)
        await self.apply(b.ChangeDimensionPacket(dimension_id=1, loading_screen_id=0),
                         b.PlayerActionPacket(runtime_id=10, action=b.PlayerActionType.CHANGE_DIMENSION_ACK))
        self.assertEqual(self.bot.dimension_bounds.dimension_id, 1)
        self.assertIs(self.bot.advertised_world_generator, b.GeneratorType.FLAT)
        await self.apply(b.RespawnPacket(runtime_id=10, state=b.PlayerRespawnState.SEARCHING_FOR_SPAWN),
                         b.RespawnPacket(runtime_id=10, state=b.PlayerRespawnState.READY_TO_SPAWN))
        self.assertIs(self.bot.advertised_world_generator, b.GeneratorType.FLAT)
        late = b.StartGamePacket(settings=b.LevelSettings(generator=b.GeneratorType.OVERWORLD))
        await self.apply(late)
        self.assertIs(self.bot.advertised_world_generator, b.GeneratorType.FLAT)
        await self.apply(b.DisconnectPacket(), late)
        self.assertIsNone(self.bot.advertised_world_generator)

    async def test_live_updates_leave_public_spawn_unchanged_during_personal_change_and_dimension_switch(self):
        b = self.binding
        await self.join(initial_difficulty=1, initial_hardcore=True, default_spawn=(8, 200, 8))
        initial = self.bot.world_spawn
        await self.apply(self.spawn((15, 200, 13), personal=True),
                         b.ChangeDimensionPacket(dimension_id=1, loading_screen_id=0),
                         b.PlayerActionPacket(runtime_id=10, action=b.PlayerActionType.CHANGE_DIMENSION_ACK),
                         self.difficulty(0))
        self.assertEqual(self.bot.world_difficulty, 0)
        self.assertIs(self.bot.world_hardcore, True)
        self.assertEqual(self.bot.world_spawn, initial)
        self.assertEqual(self.bot.player_respawn_point.position, (15, 200, 13))
        await self.apply(b.ChangeDimensionPacket(dimension_id=0, loading_screen_id=0),
                         b.PlayerActionPacket(runtime_id=10, action=b.PlayerActionType.CHANGE_DIMENSION_ACK))
        self.assertIs(self.bot.world_hardcore, True)
        self.assertEqual(self.bot.world_spawn, initial)
        await self.apply(self.spawn((-20, 0, 55)))
        self.assertEqual(initial, WorldSpawn((8, 200, 8), dimension_id=0))
        self.assertEqual(self.bot.world_spawn, WorldSpawn((-20, 0, 55), dimension_id=0))
        self.assertEqual(self.bot.player_respawn_point.position, (15, 200, 13))

    async def test_disconnect_invalidates_observations_before_reader_ends_and_rejoin_rebuilds(self):
        b = self.binding
        await self.join(initial_difficulty=2, initial_hardcore=True, default_spawn=(17, 200, 6))
        late = b.StartGamePacket(settings=b.LevelSettings(is_hardcore=True))
        await self.apply(b.DisconnectPacket(), late, self.difficulty(0), self.spawn((8, 200, 8)))
        self.assertIsNone(self.bot.world_difficulty)
        self.assertIsNone(self.bot.world_hardcore)
        self.assertIsNone(self.bot.world_spawn)
        self.assertEqual(self.bot.world_time.daylight_ticks, 31)
        await self.bot.close()
        await self.join(initial_difficulty=0, default_spawn=(0, 32767, 0))
        self.assertEqual(self.bot.world_difficulty, 0)
        self.assertIs(self.bot.world_hardcore, False)
        self.assertEqual(self.bot.world_spawn, WorldSpawn((0, None, 0), dimension_id=0))

    async def test_reader_end_invalidates_advertised_world_settings(self):
        await self.join(initial_hardcore=True, generator=self.binding.GeneratorType.FLAT)
        self.assertIs(self.bot.advertised_world_generator, self.binding.GeneratorType.FLAT)
        ended = asyncio.Event()
        self.bot.packets.subscribe_reader_end(lambda _cause: ended.set())
        self.incoming.put_nowait(ConnectionError("test connection lost"))
        await asyncio.wait_for(ended.wait(), timeout=1)
        self.assertIsNone(self.bot.world_difficulty)
        self.assertIsNone(self.bot.world_hardcore)
        self.assertIsNone(self.bot.advertised_world_generator)
        self.assertIsNone(self.bot.world_spawn)
        with self.assertRaisesRegex(ConnectionError, "test connection lost"):
            await self.bot.close()

    async def test_reader_cancellation_invalidates_advertised_world_settings(self):
        await self.join(initial_hardcore=True, generator=self.binding.GeneratorType.FLAT)
        self.assertEqual(self.bot.world_difficulty, 0)
        self.assertIs(self.bot.advertised_world_generator, self.binding.GeneratorType.FLAT)
        self.assertEqual(self.bot.world_spawn, WorldSpawn((0, None, 0), dimension_id=0))
        ended = asyncio.Event()
        self.bot.packets.subscribe_reader_end(lambda _cause: ended.set())
        reader = self.bot.packets.start_receiving()
        reader.cancel()
        # Resume after the reader exits and before its done callback runs.
        await asyncio.sleep(0)
        self.assertTrue(reader.cancelled())
        self.assertEqual(await self.bot.wait_for_session_end(timeout=0), SessionEnd("reader_end"))
        self.assertFalse(ended.is_set())
        self.assertIsNone(self.bot.advertised_world_generator)
        with self.assertRaises(asyncio.CancelledError):
            await reader
        await asyncio.wait_for(ended.wait(), timeout=1)
        self.assertIsNone(self.bot.world_difficulty)
        self.assertIsNone(self.bot.world_hardcore)
        self.assertIsNone(self.bot.advertised_world_generator)
        self.assertIsNone(self.bot.world_spawn)

    async def test_only_the_overworld_clock_updates_daylight_time(self):
        await self.join()
        before = self.bot.world_time
        update = await self.arm(self.bot.wait_for_world_time_update(timeout=1))
        self.deliver(self.clock_sync((97, 123, True)), self.binding.SetTimePacket(time=31000),
                     self.clock_sync((2, 812, False)))
        first = await update
        self.assertEqual(first.daylight_ticks, 31000)
        self.assertEqual(first.clocks[1], WorldClock(2, "test:timer", 538, True))
        self.assertEqual(self.bot.world_time.clocks[1], WorldClock(2, "test:timer", 812, False))
        self.assertEqual(self.bot.world_time.daylight_ticks, 31000)
        update = await self.arm(self.bot.wait_for_world_time_update(timeout=1))
        self.deliver(self.clock_sync((1, 34000, True)))
        paused = await update
        self.assertEqual(paused.clocks[0], WorldClock(1, "minecraft:overworld", 34000, True))
        self.assertEqual(paused.daylight_ticks, 34000)
        self.assertIs(paused.do_daylight_cycle, False)
        self.assertEqual(paused.game_ticks_at_join, 253)
        self.assertEqual(before.daylight_ticks, 26345)

    async def test_moon_phase_requires_a_named_overworld_sample(self):
        b = self.binding
        await self.join(clocks=((2, "minecraft:nether", 96000, False),))
        initial = self.bot.world_time
        self.assertEqual((initial.game_ticks_at_join, initial.daylight_ticks), (253, 26345))
        self.assertIsNone(initial.observed_moon_phase)

        update = await self.arm(self.bot.wait_for_world_time_update(timeout=1))
        self.deliver(self.clock_sync((1, 96000, False)), b.SetTimePacket(time=96000))
        observed = await update
        self.assertEqual(observed.daylight_ticks, 96000)
        self.assertIsNone(observed.observed_moon_phase)

        update = await self.arm(self.bot.wait_for_world_time_update(timeout=1))
        self.deliver(self.clock_registry((1, "minecraft:overworld", -1, False)))
        self.assertIsNone((await update).observed_moon_phase)
        update = await self.arm(self.bot.wait_for_world_time_update(timeout=1))
        self.deliver(self.clock_sync((1, 96000, False)))
        self.assertEqual((await update).observed_moon_phase, 4)

    async def test_moon_phase_uses_the_last_sample_across_rewinds_and_day_boundary(self):
        b = self.binding
        await self.join(clocks=((1, "minecraft:overworld", 96000, False),),
                        before_spawn=(b.GameRulesChangedPacket(rule_data=self.rules(doDaylightCycle=True)),))
        later_day = self.bot.world_time
        self.assertEqual(later_day.observed_moon_phase, 4)

        update = await self.arm(self.bot.wait_for_world_time_update(timeout=1))
        self.deliver(self.clock_sync((1, 23999, False)))
        before_boundary = await update
        self.assertEqual(before_boundary.observed_moon_phase, 0)
        self.assertEqual(later_day.observed_moon_phase, 4)
        self.assertTrue(before_boundary.do_daylight_cycle)
        await self.bot.wait_ticks(3)
        self.assertEqual(self.bot.world_time, before_boundary)
        self.assertEqual(self.bot.world_time.observed_moon_phase, 0)

        update = await self.arm(self.bot.wait_for_world_time_update(timeout=1))
        self.deliver(b.SetTimePacket(time=24000))
        without_named_update = await update
        self.assertEqual(without_named_update.day, 1)
        self.assertEqual(without_named_update.observed_moon_phase, 0)
        update = await self.arm(self.bot.wait_for_world_time_update(timeout=1))
        self.deliver(self.clock_sync((1, 24000, False)))
        self.assertEqual((await update).observed_moon_phase, 1)

    async def test_moon_phase_observes_overworld_in_nether_and_resets_on_rejoin(self):
        b = self.binding
        await self.join()
        transition = await self.arm(self.bot.wait_for_dimension_change(timeout=1))
        self.deliver(b.ChangeDimensionPacket(dimension_id=1, loading_screen_id=0),
                     b.PlayerActionPacket(runtime_id=10, action=b.PlayerActionType.CHANGE_DIMENSION_ACK))
        self.assertEqual((await transition).dimension_id, 1)
        self.assertEqual(self.bot.world_time.observed_moon_phase, 1)

        update = await self.arm(self.bot.wait_for_world_time_update(timeout=1))
        self.deliver(self.clock_sync((1, 192000, False), (2, 96000, False)))
        retained = await update
        self.assertEqual(retained.observed_moon_phase, 0)
        await self.bot.close()
        await self.join(clocks=())
        self.assertIsNone(self.bot.world_time.observed_moon_phase)
        self.assertEqual(retained.observed_moon_phase, 0)

    async def test_weather_wait_returns_first_change_and_uses_no_integer_conversion(self):
        await self.join()
        initial = self.bot.weather
        update = await self.arm(self.bot.wait_for_weather_update(timeout=1))
        self.deliver(self.event(3003, 0), self.event(2001, 42), self.event(3001, 17), self.event(3002, 65535))
        self.assertEqual(await update, Weather(True, False, None, 0.0))
        self.assertEqual(initial, Weather(False, False, 0.0, 0.0))
        self.assertEqual(self.bot.weather, Weather(True, True))
        update = await self.arm(self.bot.wait_for_weather_update(timeout=1))
        await self.bot.close()
        with self.assertRaises(ConnectionError):
            await update
