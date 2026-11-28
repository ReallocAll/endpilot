import asyncio
from dataclasses import FrozenInstanceError
import unittest
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bedrock_protocol import ServerBlockProperty, nbt

from endpilot import BlockEvent, Bot, vanilla_note_instruments
from endpilot.block_data import vanilla_block_state
from endpilot.protocol import RawPacket, write_batch


class NoteInstrumentCatalogTests(unittest.TestCase):
    def test_catalog_is_read_only_and_requires_a_supported_protocol(self):
        catalog = vanilla_note_instruments(protocol=2225)
        self.assertIs(catalog, vanilla_note_instruments())
        self.assertEqual(set(catalog), set(range(26)))
        self.assertEqual((catalog[0], catalog[4], catalog[19], catalog[24]),
                         ("note.harp", "note.bassattack", "note.trumpet_oxidized", "note.witherskeleton"))
        self.assertIsNone(catalog.get(26))
        with self.assertRaises(TypeError):
            catalog[4] = "note.bass"
        for protocol in (2193, 2226, 2225.0, "2225", True):
            with (self.subTest(protocol=protocol),
                  self.assertRaisesRegex(ValueError, "unsupported vanilla note instrument protocol")):
                vanilla_note_instruments(protocol)

    def test_note_without_packed_data_has_no_sound_identifier(self):
        for packet in (object(), SimpleNamespace(data=None)):
            event = BlockEvent("note", 0, (1, 2, 3), packet)
            self.assertIsNone(event.instrument_sound)
            self.assertIsNone(event.instrument_id)
            self.assertIsNone(event.note_pitch)


class WorldFeedbackTests(unittest.IsolatedAsyncioTestCase):
    def push(self, *packets):
        self.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in packets))

    async def asyncSetUp(self):
        self.bot = Bot("FeedbackObserver", protocol=2225)
        self.b = self.bot.protocol.binding
        self.addAsyncCleanup(self.bot.close)
        await self.join()

    async def join(self, *before_spawn, hashed=True, block_properties=()):
        b = self.b
        self.incoming = asyncio.Queue()
        transport = SimpleNamespace(recv=self.incoming.get, send=AsyncMock(), close=AsyncMock())
        self.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.push(b.StartGamePacket(runtime_id=7, entity_id=4, block_network_ids_are_hashes=hashed,
                                   block_properties=list(block_properties)),
                  *before_spawn, b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132")

    async def test_sound_variants_keep_wire_fields_and_verified_position(self):
        b = self.b
        play = b.PlaySoundPacket(name="random.pop", pos=b.BlockPos(x=-35, y=1764, z=34),
                                 volume=0.75, pitch=1.25, loop_count=3, bypass_listener_range_check=True,
                                 server_sound_handle=39, playback_position_seconds=1.5)
        level = b.LevelSoundEventPacket(sound_event="button.click_on", pos=b.Vec3(x=-1.5, y=220.5, z=4.5),
                                        data=-123, actor_identifier="minecraft:player", actor=77,
                                        is_global=True, is_baby=True, fire_at_position=b.Vec3(x=3, y=4, z=5))
        stop = b.StopSoundPacket(name="", stop_all=True, stop_music_legacy=True)
        volume = b.ClientboundUpdateSoundDataPacket(server_sound_handle=39, event=b.SetVolume(volume=0.25))
        async with asyncio.TaskGroup() as group:
            started = group.create_task(self.bot.wait_for_sound("random.pop", kind="play", timeout=1))
            button = group.create_task(self.bot.wait_for_sound("button.click_on", timeout=1))
            stopped = group.create_task(self.bot.wait_for_sound(kind="stop", timeout=1))
            changed = group.create_task(self.bot.wait_for_sound(kind="update", timeout=1))
            await asyncio.sleep(0)
            self.push(level, play, stop, volume)

        event = started.result()
        self.assertEqual((event.dimension_id, event.position, event.volume, event.pitch),
                         (0, (-4.375, 220.5, 4.25), 0.75, 1.25))
        self.assertEqual(event.packet, play)
        self.assertEqual(button.result().packet, level)
        self.assertIsNone(button.result().volume)
        self.assertIsNone(button.result().pitch)
        self.assertEqual(stopped.result().packet, stop)
        self.assertIsNone(stopped.result().position)
        self.assertEqual(changed.result().packet, volume)
        self.assertEqual(changed.result().volume, 0.25)

    async def test_particle_keeps_wire_dimension_attachment_and_dispatch_dimension(self):
        b = self.b
        pos = b.Vec3(x=-1.375, y=221.25, z=3.75)
        particle = b.SpawnParticleEffectPacket(
            vanilla_dimension_id=2, actor_id=91, pos=pos, effect_name="minecraft:basic_flame_particle",
            molang_variables='{"variable.a":1.5}',
        )
        wait = asyncio.create_task(self.bot.wait_for_particle("minecraft:basic_flame_particle", timeout=1))
        await asyncio.sleep(0)
        self.push(b.ChangeDimensionPacket(dimension_id=1, pos=pos), particle,
                  b.ChangeDimensionPacket(dimension_id=2, pos=pos))
        event = await wait
        self.assertEqual((event.dimension_id, event.position), (1, (-1.375, 221.25, 3.75)))
        self.assertEqual(event.packet, particle)

    async def test_block_particles_are_opt_in_and_keep_wire_fields(self):
        b = self.b
        position = (-1.5, 220.5, 3.75)
        pos = b.Vec3(x=position[0], y=position[1], z=position[2])
        native = [b.LevelEventPacket(event_id=event_id, pos=pos, data=-2108756090)
                  for event_id in (3603, 3604, 3605, 3606, 3607, 3608, 2001)]
        other_position = b.LevelEventPacket(event_id=3604, pos=b.Vec3(x=-2, y=220.5, z=3.75), data=-1)
        unrelated = b.LevelEventPacket(event_id=3609, pos=pos, data=65)
        crack = b.LevelEventPacket(event_id=3602, pos=pos, data=4369)
        stop = b.LevelEventPacket(event_id=3601, pos=b.Vec3(x=0, y=0, z=0))
        flame = b.SpawnParticleEffectPacket(pos=pos, effect_name="minecraft:basic_flame_particle")
        hit = b.LevelSoundEventPacket(pos=pos, sound_event="hit", data=-2108756090)
        opted_in, ordinary, particles, sounds = [], [], [], []
        self.bot.subscribe_block_events(opted_in.append, position=position, kind="block_particle")
        self.bot.subscribe_block_events(ordinary.append)
        self.bot.subscribe_particle(particles.append)
        self.bot.subscribe_sound(sounds.append)
        async with asyncio.TaskGroup() as group:
            first = group.create_task(self.bot.wait_for_block_event(position, kind="block_particle", timeout=1))
            legacy = group.create_task(self.bot.wait_for_block_event(timeout=1))
            named = group.create_task(self.bot.wait_for_particle(flame.effect_name, timeout=1))
            await asyncio.sleep(0)
            self.push(other_position, unrelated, flame, b.ChangeDimensionPacket(dimension_id=1, pos=pos),
                      *native, crack, hit, stop, b.ChangeDimensionPacket(dimension_id=2, pos=pos))

        self.assertEqual([event.packet for event in opted_in], native)
        self.assertEqual([(event.kind, event.dimension_id, event.position) for event in opted_in],
                         [("block_particle", 1, position)] * len(native))
        self.assertEqual(first.result(), opted_in[0])
        self.assertEqual([(event.kind, event.position, event.packet) for event in ordinary],
                         [("crack_progress", position, crack), ("crack_end", None, stop)])
        self.assertEqual(legacy.result(), ordinary[0])
        self.assertEqual([event.packet for event in particles], [flame])
        self.assertEqual(named.result(), particles[0])
        self.assertEqual([event.packet for event in sounds], [hit])

    async def test_block_particle_wait_only_decodes_level_event_packets(self):
        b = self.b
        event = b.LevelEventPacket(event_id=2001, pos=b.Vec3(x=0, y=0, z=0), data=-5)
        pending = asyncio.create_task(self.bot.wait_for_block_event(kind="block_particle", timeout=1))
        await asyncio.sleep(0)
        self.incoming.put_nowait(write_batch((RawPacket(b.BlockEventPacket.ID, b""), self.bot.protocol.encode(event))))
        observed = await pending
        self.assertEqual((observed.position, observed.packet), ((0, 0, 0), event))

    async def test_note_tuning_is_opt_in_and_requires_a_current_note_block(self):
        b = self.b
        cell = (-3, 220, 17)
        pos = b.BlockPos(x=cell[0], y=cell[1], z=cell[2])
        note = b.LevelSoundEventPacket(pos=b.Vec3(x=cell[0], y=cell[1], z=cell[2]), sound_event="note", data=5385)

        def event(tuning, instrument=21):
            return b.BlockEventPacket(pos=pos, b0=instrument, b1=tuning)

        initial, later = event(1), event(2)
        minimum, maximum = event(0, 0), event(24, 26)
        packets = [initial, later, event(-1), event(25), minimum, maximum, initial, later]
        generic, sounds, tuning, explicit_blocks = [], [], [], []
        self.bot.subscribe_block_events(generic.append)
        self.bot.subscribe_block_events(sounds.append, kind="note")
        self.bot.subscribe_block_events(tuning.append, position=cell, kind="note_tuning")
        self.bot.subscribe_block_events(explicit_blocks.append, kind="block")
        async with asyncio.TaskGroup() as group:
            tuned = group.create_task(self.bot.wait_for_block_event(cell, kind="note_tuning", timeout=1))
            legacy = group.create_task(self.bot.wait_for_block_event(cell, timeout=1))
            drained = group.create_task(self.bot.packets.wait_for_packet(b.SetTimePacket.ID, timeout=1))
            await asyncio.sleep(0)
            self.push(initial, note, b.UpdateBlockPacket(pos=pos, runtime_id=0x80310E21, layer=0), later,
                      b.UpdateBlockPacket(pos=pos, runtime_id=166024317, layer=0), packets[2], packets[3],
                      minimum, maximum, initial, note, b.UpdateBlockPacket(pos=pos, runtime_id=0x80310E21, layer=0),
                      later, b.SetTimePacket(time=42))

        self.assertEqual([event.packet for event in generic], [initial, note, *packets[1:-1], note, later])
        self.assertEqual([event.packet for event in explicit_blocks], packets)
        self.assertEqual(legacy.result().kind, "block")
        self.assertIsNone(legacy.result().note_tuning)
        self.assertEqual([(event.note_tuning, event.instrument_id) for event in tuning], [(0, 0), (24, 26), (1, 21)])
        self.assertEqual([event.packet for event in tuning], [minimum, maximum, initial])
        self.assertEqual(tuned.result(), tuning[0])
        self.assertIsNone(tuning[1].instrument_sound)
        self.assertEqual(tuning[2].instrument_sound, "note.skeleton")
        self.assertTrue(all(event.note_pitch is None for event in tuning))
        self.assertEqual([(event.note_pitch, event.note_tuning) for event in sounds], [(9, None), (9, None)])
        with self.assertRaises(FrozenInstanceError):
            tuning[0].note_tuning = 5

    async def test_subscriptions_deliver_every_matching_packet_in_dispatch_order(self):
        b = self.b
        cell = (3, 220, 7)
        pos = b.Vec3(x=3, y=220, z=7)
        history = b.StopSoundPacket(name="history")
        first_sound = b.PlaySoundPacket(name="random.pop", pos=b.BlockPos(x=24, y=1760, z=56))
        other_sound = b.PlaySoundPacket(name="random.orb")
        stop = b.StopSoundPacket(name="random.pop")
        note = b.LevelSoundEventPacket(pos=pos, sound_event="note", data=257)
        particle = b.SpawnParticleEffectPacket(
            vanilla_dimension_id=2, actor_id=91, pos=pos, effect_name="minecraft:basic_flame_particle",
            molang_variables='{"variable.a":1.5}',
        )
        other_particle = b.SpawnParticleEffectPacket(effect_name="minecraft:basic_smoke_particle")
        lid = b.BlockEventPacket(pos=self.pos(cell), b0=1, b1=1)
        other_lid = b.BlockEventPacket(pos=self.pos((4, 220, 7)), b0=1, b1=1)
        self.push(history)
        self.assertEqual((await self.bot.wait_for_sound(timeout=1)).packet, history)

        seen, sounds, particles, blocks = [], [], [], []
        self.bot.subscribe_sound(lambda event: seen.append(("sound", event)))
        self.bot.subscribe_particle(lambda event: seen.append(("particle", event)))
        self.bot.subscribe_block_events(lambda event: seen.append(("block", event)))
        self.bot.subscribe_sound(sounds.append, name="random.pop", kind="play")
        self.bot.subscribe_particle(particles.append, name=particle.effect_name)
        self.bot.subscribe_block_events(blocks.append, position=cell, kind="chest_open")
        async with asyncio.TaskGroup() as group:
            sound_wait = group.create_task(self.bot.wait_for_sound("random.pop", kind="play", timeout=1))
            particle_wait = group.create_task(self.bot.wait_for_particle(particle.effect_name, timeout=1))
            block_wait = group.create_task(self.bot.wait_for_block_event(cell, kind="chest_open", timeout=1))
            cancelled = group.create_task(self.bot.wait_for_sound("random.pop", kind="play"))
            await asyncio.sleep(0)
            cancelled.cancel()
            self.push(first_sound, other_sound, stop, particle, other_particle,
                      b.ChangeDimensionPacket(dimension_id=1, pos=pos),
                      b.UpdateBlockPacket(pos=lid.pos, runtime_id=0x2C383C60),
                      b.UpdateBlockPacket(pos=other_lid.pos, runtime_id=0x2C383C60),
                      note, other_lid, lid, particle, first_sound,
                      b.ChangeDimensionPacket(dimension_id=2, pos=pos))

        self.assertTrue(cancelled.cancelled())
        self.assertEqual([(source, event.dimension_id, event.packet) for source, event in seen], [
            ("sound", 0, first_sound), ("sound", 0, other_sound), ("sound", 0, stop),
            ("particle", 0, particle), ("particle", 0, other_particle),
            ("sound", 1, note), ("block", 1, note), ("block", 1, other_lid), ("block", 1, lid),
            ("particle", 1, particle), ("sound", 1, first_sound),
        ])
        self.assertEqual([(event.dimension_id, event.position) for event in sounds], [(0, cell), (1, cell)])
        self.assertEqual([(event.dimension_id, event.position) for event in particles], [(0, cell), (1, cell)])
        self.assertEqual([(event.kind, event.position) for event in blocks], [("chest_open", cell)])
        self.assertEqual((sound_wait.result(), particle_wait.result(), block_wait.result()),
                         (sounds[0], particles[0], blocks[0]))

    async def test_unsubscribe_is_immediate_and_callback_error_is_isolated(self):
        first = self.b.StopSoundPacket(name="first")
        second = self.b.StopSoundPacket(name="second")
        removed, kept = [], []
        failure = ValueError("sound observer failed")
        cancellation = asyncio.CancelledError("observer cancelled itself")

        def remove_and_fail(_event):
            unsubscribe()
            unsubscribe()
            raise failure

        def fail_with_cancellation(_event):
            raise cancellation

        stop_failure = self.bot.subscribe_sound(remove_and_fail, name="first")
        unsubscribe = self.bot.subscribe_sound(removed.append)
        self.bot.subscribe_sound(fail_with_cancellation, name="first")
        self.bot.subscribe_sound(kept.append)
        waiting = asyncio.create_task(self.bot.wait_for_sound("second", timeout=1))
        await asyncio.sleep(0)
        with patch.object(asyncio.get_running_loop(), "call_exception_handler") as report:
            self.push(first, second)
            self.assertEqual((await waiting).packet, second)

        stop_failure()
        self.assertEqual(removed, [])
        self.assertEqual([event.packet for event in kept], [first, second])
        self.assertEqual([(call.args[0]["message"], call.args[0]["exception"]) for call in report.call_args_list],
                         [("packet observer failed", failure), ("packet observer failed", cancellation)])

    async def test_first_subscription_rejects_after_game_disconnect(self):
        waiting = asyncio.create_task(self.bot.wait_for_server_disconnect(timeout=1))
        await asyncio.sleep(0)
        self.push(self.b.DisconnectPacket())
        await waiting
        self.assertFalse(self.bot.packets._reader_end_result()[0])
        with self.assertRaises(ConnectionError):
            self.bot.subscribe_particle(lambda _: None)

    async def test_disconnect_stops_delivery_before_trailing_packets_and_rejoin(self):
        b = self.b
        old_feedback = self.bot._feedback
        seen, errors = [], []
        unsubscribe = self.bot.subscribe_sound(seen.append)
        self.bot.subscribe_particle(seen.append)
        self.bot.subscribe_block_events(seen.append)

        def inspect(raw):
            if raw.packet_id == b.DisconnectPacket.ID:
                try:
                    self.bot.subscribe_sound(seen.append)
                except ConnectionError as error:
                    errors.append(error)

        self.addCleanup(self.bot.packets.subscribe_raw(inspect))
        last = b.StopSoundPacket(name="late")
        waiting = asyncio.create_task(self.bot.wait_for_sound("late", timeout=1))
        await asyncio.sleep(0)
        self.push(b.DisconnectPacket(), b.SpawnParticleEffectPacket(effect_name="late"), b.BlockEventPacket(), last)
        self.assertEqual((await waiting).packet, last)
        self.assertEqual(len(errors), 1)
        self.assertEqual(seen, [])
        with self.assertRaises(ConnectionError):
            old_feedback.subscribe_block_events(seen.append)
        await self.bot.close()

        await self.join()
        new = []
        self.bot.subscribe_sound(new.append)
        unsubscribe()
        self.push(last)
        self.assertEqual((await self.bot.wait_for_sound(timeout=1)).packet, last)
        self.assertEqual([event.packet for event in new], [last])
        self.assertEqual(seen, [])

    async def test_reader_failure_releases_callbacks_and_rejects_new_subscriptions(self):
        def listener(_event):
            self.fail("unexpected particle callback")

        callback = weakref.ref(listener)
        unsubscribe = self.bot.subscribe_particle(listener)
        del listener
        ended = asyncio.Event()
        self.bot.packets.subscribe_reader_end(lambda _cause: ended.set())
        self.incoming.put_nowait(b"\x02\x00")
        await asyncio.wait_for(ended.wait(), timeout=1)
        self.assertIsNone(callback())
        unsubscribe()
        with self.assertRaises(ConnectionError):
            self.bot.subscribe_particle(lambda _: None)
        with self.assertRaisesRegex(ValueError, "truncated packet in batch"):
            await self.bot.close()

    async def test_sound_after_spawn_uses_the_dimension_changed_during_login(self):
        b = self.b
        stopped = b.StopSoundPacket(name="random.pop")

        async def sound():
            waiting = asyncio.create_task(self.bot.wait_for_sound("random.pop", kind="stop", timeout=1))
            await asyncio.sleep(0)
            self.push(stopped)
            return await waiting

        self.assertEqual((await sound()).dimension_id, 0)
        await self.bot.close()
        await self.join(b.SetHealthPacket(health=17), b.ChangeDimensionPacket(dimension_id=1))
        event = await sound()
        self.assertEqual(event.dimension_id, 1)
        self.assertEqual(event.packet, stopped)
        self.assertEqual(self.bot.health, 17)

    async def test_only_observed_block_feedback_gets_derived_kinds(self):
        b = self.b
        chest_pos = b.BlockPos(x=0, y=220, z=4)
        note_pos = b.Vec3(x=-1, y=220, z=4)
        piston_pos = b.BlockPos(x=1, y=220, z=5)
        generic = b.BlockEventPacket(pos=b.BlockPos(x=-1, y=220, z=4), b0=1, b1=1)
        lid = b.BlockEventPacket(pos=chest_pos, b0=1, b1=1)
        arm = b.BlockActorDataPacket(pos=piston_pos, data=nbt.CompoundTag({
            "id": nbt.StringTag("PistonArm"), "State": nbt.ByteTag(3), "Progress": nbt.FloatTag(1.0),
        }))
        note = b.LevelSoundEventPacket(pos=note_pos, sound_event="note", data=257)
        ended = b.LevelEventPacket(event_id=3601, pos=b.Vec3(x=0, y=0, z=0), data=0)
        async with asyncio.TaskGroup() as group:
            ambiguous = group.create_task(self.bot.wait_for_block_event(kind="block", timeout=1))
            opened = group.create_task(self.bot.wait_for_block_event((0, 220, 4), kind="chest_open", timeout=1))
            piston = group.create_task(self.bot.wait_for_block_event((1, 220, 5), kind="piston_retracting", timeout=1))
            played_note = group.create_task(self.bot.wait_for_block_event(kind="note", timeout=1))
            crack = group.create_task(self.bot.wait_for_block_event(kind="crack_end", timeout=1))
            await asyncio.sleep(0)
            self.push(b.LevelEventPacket(event_id=3001, pos=note_pos, data=0), generic,
                      b.UpdateBlockPacket(pos=chest_pos, runtime_id=0x2C383C60), lid, arm, note, ended)

        self.assertEqual(ambiguous.result().packet, generic)
        self.assertEqual(opened.result().packet, lid)
        self.assertEqual(piston.result().packet.data["Progress"].value, 1.0)
        self.assertIsNone(piston.result().instrument_id)
        self.assertEqual(played_note.result().packet.data, 257)
        self.assertEqual((played_note.result().note_pitch, played_note.result().instrument_id), (1, 1))
        self.assertEqual(played_note.result().instrument_sound, "note.bd")
        for event in (ambiguous, opened, piston, crack):
            self.assertIsNone(event.result().instrument_sound)
        self.assertIsNone(crack.result().position)
        self.assertEqual(crack.result().packet, ended)
        for event in (ambiguous, opened, piston, played_note, crack):
            self.assertIsNone(event.result().piston_attached_blocks)

    async def test_negative_and_unknown_note_data_keep_the_native_packet(self):
        for data, instrument_id, pitch in ((-1, None, None), (6665, 26, 9)):
            with self.subTest(data=data):
                packet = self.b.LevelSoundEventPacket(sound_event="note", data=data)
                event = await self.block_event(packet, kind="note")
                self.assertEqual(event.packet, packet)
                self.assertEqual((event.instrument_id, event.note_pitch), (instrument_id, pitch))
                self.assertIsNone(event.instrument_sound)

    async def test_pending_observation_ends_on_close(self):
        feedback = self.bot._feedback
        packets = self.bot.packets
        player_state = self.bot._player_state
        drain_player_state = player_state.aclose
        sounds = []
        self.bot.subscribe_sound(sounds.append)
        late = self.b.StopSoundPacket(name="late")

        async def drain_with_packet():
            self.push(late)
            raw = await packets.wait_for_packet(late.ID, timeout=1)
            self.assertEqual(self.bot.protocol.decode(raw), late)
            await drain_player_state()

        waiting = asyncio.create_task(self.bot.wait_for_sound("pending"))
        await asyncio.sleep(0)
        with patch.object(player_state, "aclose", side_effect=drain_with_packet):
            await self.bot.close()
        self.assertEqual(sounds, [])
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(waiting, 1)
        with self.assertRaises(ConnectionError):
            feedback.subscribe_sound(lambda _: None)
        with self.assertRaises(RuntimeError):
            self.bot.subscribe_sound(lambda _: None)

    def pos(self, cell):
        return self.b.BlockPos(x=cell[0], y=cell[1], z=cell[2])

    def chest_root(self, cell, partner, lead, **override):
        fields = dict(id=nbt.StringTag("Chest"), x=nbt.IntTag(cell[0]), y=nbt.IntTag(cell[1]), z=nbt.IntTag(cell[2]),
                      pairx=nbt.IntTag(partner[0]), pairz=nbt.IntTag(partner[2]), pairlead=nbt.ByteTag(lead))
        fields.update(override)
        return self.b.BlockActorDataPacket(pos=self.pos(cell), data=nbt.CompoundTag(fields))

    async def block_event(self, *packets, position=None, kind=None):
        pending = asyncio.create_task(self.bot.wait_for_block_event(position=position, kind=kind, timeout=1))
        await asyncio.sleep(0)
        self.push(*packets)
        return await pending

    async def test_single_trapped_chest_lid_needs_a_known_main_layer(self):
        b = self.b
        position = (5, 220, 10)
        # Lid packet bodies and the main-layer runtime ID captured on vanilla 2225.
        opened = RawPacket(b.BlockEventPacket.ID, bytes.fromhex("0ab803140202"))
        closed = RawPacket(b.BlockEventPacket.ID, bytes.fromhex("0ab803140200"))
        extra, main = (
            self.bot.protocol.encode(b.UpdateBlockPacket(pos=self.pos(position), runtime_id=0x316625E3, layer=layer))
            for layer in (1, 0)
        )
        async with asyncio.TaskGroup() as group:
            generic = group.create_task(self.bot.wait_for_block_event(position, kind="block", timeout=1))
            lid_open = group.create_task(self.bot.wait_for_block_event(position, kind="chest_open", timeout=1))
            lid_close = group.create_task(self.bot.wait_for_block_event(position, kind="chest_close", timeout=1))
            await asyncio.sleep(0)
            self.incoming.put_nowait(write_batch((extra, opened, main, opened, closed)))

        for waiter, kind, raw in ((generic, "block", opened), (lid_open, "chest_open", opened),
                                  (lid_close, "chest_close", closed)):
            event = waiter.result()
            self.assertEqual((event.kind, event.position, event.chest_partner), (kind, position, None))
            self.assertEqual(self.bot.protocol.encode(event.packet), raw)

    async def test_ender_and_shulker_lids_keep_the_packet_position_and_never_pair(self):
        b = self.b
        for index, state_id in enumerate((1239582919, 2647010207, 670752730)):
            # Main-layer runtime IDs captured for the ender, undyed and red containers.
            target, neighbor = (0, 220, index), (1, 220, index)
            opened = b.BlockEventPacket(pos=self.pos(target), b0=1, b1=1)
            closed = b.BlockEventPacket(pos=self.pos(target), b0=1, b1=0)
            next_door = b.BlockEventPacket(pos=self.pos(neighbor), b0=1, b1=1)
            events = []
            unsubscribe = self.bot.subscribe_block_events(events.append, position=target)
            try:
                event = await self.block_event(
                    b.UpdateBlockPacket(pos=self.pos(target), runtime_id=state_id),
                    b.UpdateBlockPacket(pos=self.pos(neighbor), runtime_id=state_id),
                    self.chest_root(target, neighbor, 1), self.chest_root(neighbor, target, 0),
                    next_door, opened, opened, closed, position=target, kind="chest_close",
                )
            finally:
                unsubscribe()
            self.assertEqual(event, events[-1])
            self.assertEqual([(row.kind, row.position, row.chest_partner, row.packet) for row in events], [
                ("chest_open", target, None, opened), ("chest_open", target, None, opened),
                ("chest_close", target, None, closed),
            ])

    async def test_lid_feedback_uses_observed_main_layer_and_vanilla_names(self):
        b = self.b
        target = (0, 220, 5)
        await self.bot.close()
        await self.join(block_properties=(ServerBlockProperty(name="fixture:red_shulker_box"),
                                          ServerBlockProperty(name="fixture:waxed_copper_chest")))
        event = b.BlockEventPacket(pos=self.pos(target), b0=1, b1=1)
        stone, custom_id, red = 2150698529, 0xD15C0000, 670752730
        for state_id in (None, stone, custom_id):
            with self.subTest(state_id=state_id):
                update = [] if state_id is None else [b.UpdateBlockPacket(pos=self.pos(target), runtime_id=state_id)]
                feedback = await self.block_event(
                    b.UpdateBlockPacket(pos=self.pos(target), runtime_id=red, layer=1), *update, event,
                    position=target,
                )
                self.assertEqual((feedback.kind, feedback.chest_partner, feedback.packet), ("block", None, event))
                self.assertEqual(self.bot.observed_block_state(target, layer=1).name, "minecraft:red_shulker_box")
                if state_id is None:
                    self.assertIsNone(self.bot.observed_block(target))
                else:
                    self.assertEqual(self.bot.observed_block(target).runtime_id, state_id)
                    if state_id == stone:
                        self.assertEqual(self.bot.observed_block_state(target).name, "minecraft:stone")
        self.assertIsNone(self.bot.observed_block_state(target))

    async def test_copper_chest_lids_cover_the_eight_vanilla_block_names(self):
        b = self.b
        variants = (
            ("copper_chest", 3967469775), ("exposed_copper_chest", 987437688),
            ("weathered_copper_chest", 3584681075), ("oxidized_copper_chest", 2756911751),
            ("waxed_copper_chest", 2420240159), ("waxed_exposed_copper_chest", 3794255368),
            ("waxed_weathered_copper_chest", 998383803), ("waxed_oxidized_copper_chest", 1084588487),
        )
        for index, (name, runtime_id) in enumerate(variants):
            with self.subTest(name=name):
                target = (0, 220, index)
                position = self.pos(target)
                opened = await self.block_event(
                    b.UpdateBlockPacket(pos=position, runtime_id=runtime_id),
                    b.BlockEventPacket(pos=position, b0=1, b1=1), position=target,
                )
                closed = await self.block_event(b.BlockEventPacket(pos=position, b0=1, b1=0), position=target)
                self.assertEqual(self.bot.observed_block_state(target).name, f"minecraft:{name}")
                self.assertEqual((opened.kind, closed.kind), ("chest_open", "chest_close"))
                self.assertIsNone(opened.chest_partner)
                self.assertIsNone(closed.chest_partner)

    async def test_chest_partner_needs_two_current_reciprocal_roots_and_filters_original_position(self):
        b = self.b
        lead, partner = (3, 220, 7), (3, 220, 8)
        opened = b.BlockEventPacket(pos=self.pos(lead), b0=1, b1=1)
        closed = b.BlockEventPacket(pos=self.pos(partner), b0=1, b1=0)
        for variant, lead_id, partner_id in (("ordinary", 0x2C383C60, 0x553014DD),
                                             ("trapped", 0x316625E3, 0x499CF151),
                                             ("copper", 1415168726, 1415168726)):
            with self.subTest(variant=variant):
                partner_wait = asyncio.create_task(self.bot.wait_for_block_event(position=partner, timeout=1))
                self.addCleanup(partner_wait.cancel)
                event = await self.block_event(
                    b.UpdateBlockPacket(pos=self.pos(lead), runtime_id=lead_id),
                    b.UpdateBlockPacket(pos=self.pos(partner), runtime_id=partner_id),
                    self.chest_root(lead, partner, 1), self.chest_root(partner, lead, 0), opened,
                    position=lead, kind="chest_open",
                )
                self.assertEqual((event.position, event.chest_partner, event.packet, event.piston_face),
                                 (lead, partner, opened, None))
                self.assertFalse(partner_wait.done())
                self.push(closed)
                confirmed = await partner_wait
                self.assertEqual((confirmed.position, confirmed.chest_partner, confirmed.kind, confirmed.packet),
                                 (partner, lead, "chest_close", closed))

    async def test_chest_partner_stays_unknown_without_current_matching_typed_chest_roots(self):
        b = self.b
        for index, reason in enumerate(("missing", "one_way", "same_lead", "wrong_type", "wrong_root_cell",
                                        "mixed_trapped_peer", "mixed_ordinary_peer", "mixed_copper_variant",
                                        "disconnected", "non_lid")):
            with self.subTest(reason=reason):
                pos = (5, 220, index)
                peer = (7 if reason == "disconnected" else 6, 220, index)
                sample = []
                if reason != "missing":
                    override = {}
                    if reason == "wrong_type":
                        override["pairlead"] = nbt.IntTag(0)
                    if reason == "wrong_root_cell":
                        override["y"] = nbt.IntTag(221)
                    sample.append(self.chest_root(
                        peer, (4, 220, index) if reason == "one_way" else pos,
                        1 if reason == "same_lead" else 0, **override,
                    ))
                lead_id, peer_id = {
                    "mixed_ordinary_peer": (0x316625E3, 0x2C383C60),
                    "mixed_trapped_peer": (0x2C383C60, 0x499CF151),
                    "mixed_copper_variant": (1415168726, 1084588487),
                }.get(reason, (0x2C383C60, 0x2C383C60))
                event = await self.block_event(
                    b.UpdateBlockPacket(pos=self.pos(pos), runtime_id=lead_id),
                    b.UpdateBlockPacket(pos=self.pos(peer), runtime_id=peer_id),
                    self.chest_root(pos, peer, 1), *sample,
                    b.BlockEventPacket(pos=self.pos(pos), b0=1, b1=2 if reason == "non_lid" else 1),
                )
                self.assertIsNone(event.chest_partner)
                self.assertEqual(event.kind, "block" if reason == "non_lid" else "chest_open")

        pos, peer = (7, 220, 7), (8, 220, 7)
        event = await self.block_event(
            b.UpdateBlockPacket(pos=self.pos(pos), runtime_id=0x2C383C60),
            b.UpdateBlockPacket(pos=self.pos(peer), runtime_id=0x2C383C60),
            self.chest_root(pos, peer, 1), self.chest_root(peer, pos, 0),
            b.ChangeDimensionPacket(dimension_id=1, pos=b.Vec3(x=8, y=65, z=7)),
            b.UpdateBlockPacket(pos=self.pos(pos), runtime_id=0x2C383C60),
            b.UpdateBlockPacket(pos=self.pos(peer), runtime_id=0x2C383C60),
            b.BlockEventPacket(pos=self.pos(pos), b0=1, b1=0),
        )
        self.assertEqual((event.dimension_id, event.kind, event.chest_partner), (1, "chest_close", None))

    def piston_arm(self, position, stage, **fields):
        return self.b.BlockActorDataPacket(pos=self.pos(position), data=nbt.CompoundTag({
            "id": nbt.StringTag("PistonArm"), "State": nbt.ByteTag(stage), **fields,
        }))

    async def test_piston_attached_blocks_preserve_signed_coordinates_and_packet_order(self):
        b = self.b
        position = (7, 220, -3)
        sources = ((8, -32, -6), (-4, 220, 9), (20, 221, -11))
        attached = nbt.ListTag([nbt.IntTag(axis) for cell in sources for axis in cell])
        other_actor = b.BlockActorDataPacket(pos=self.pos(position), data=nbt.CompoundTag({
            "id": nbt.StringTag("Chest"), "State": nbt.ByteTag(1), "AttachedBlocks": attached,
        }))
        for stage, kind in ((1, "piston_extending"), (9, "piston")):
            with self.subTest(stage=stage):
                arm = self.piston_arm(position, stage, AttachedBlocks=attached)
                event = await self.block_event(other_actor, arm, position=position)
                self.assertEqual((event.kind, event.position, event.piston_face, event.packet),
                                 (kind, position, None, arm))
                self.assertEqual(event.piston_attached_blocks, sources)
                event.packet.data = nbt.CompoundTag({"AttachedBlocks": nbt.ListTag([])})
                self.assertEqual(event.piston_attached_blocks, sources)

    async def test_piston_attached_blocks_distinguish_empty_reports_from_unknown(self):
        position = (1, 220, 5)
        ints = [nbt.IntTag(value) for value in (-4, 220, 8, 9)]
        for stage, kind, fields, expected in (
            (1, "piston_extending", {"AttachedBlocks": nbt.ListTag(ints[:3])}, ((-4, 220, 8),)),
            (2, "piston_extended", {"AttachedBlocks": nbt.ListTag([])}, ()),
            (0, "piston_retracted", {"AttachedBlocks": nbt.ListTag([], int(nbt.Type.INT))}, ()),
            (3, "piston_retracting", {}, None),
            (3, "piston_retracting", {"AttachedBlocks": nbt.IntArrayTag([-4, 220, 8])}, None),
            (3, "piston_retracting", {"AttachedBlocks": nbt.ListTag(ints)}, None),
            (3, "piston_retracting", {"AttachedBlocks": nbt.ListTag([nbt.ShortTag(1)] * 3)}, None),
        ):
            with self.subTest(stage=stage, fields=fields):
                arm = self.piston_arm(position, stage, **fields)
                event = await self.block_event(arm, position=position, kind=kind)
                self.assertEqual(event.packet, arm)
                self.assertEqual(event.piston_attached_blocks, expected)

    async def test_piston_face_matches_base_states_and_preserves_each_raw_phase(self):
        b = self.b
        samples = (
            (1899755433, 0), (1961230256, 1), (1776805787, 3), (1838280610, 2), (1653856141, 5), (1715330964, 4),
            (2363112454, 0), (2301637631, 1), (2486062100, 3), (2424587277, 2), (2609011746, 5), (2547536923, 4),
        )
        phases = "piston_retracted", "piston_extending", "piston_extended", "piston_retracting"
        for index, (runtime, face) in enumerate(samples):
            with self.subTest(runtime=runtime):
                cell = (0, 220, index)
                arm = self.piston_arm(cell, index % 4)
                event = await self.block_event(b.UpdateBlockPacket(pos=self.pos(cell), runtime_id=runtime), arm)
                self.assertEqual((event.position, event.piston_face, event.kind, event.packet, event.chest_partner),
                                 (cell, face, phases[index % 4], arm, None))

        # A piston head (even in the same direction) and an unobserved cell cannot resolve a face.
        for runtime in (4162255293, None):
            cell = (0, 221 if runtime is not None else 222, 0)
            observed = () if runtime is None else (b.UpdateBlockPacket(pos=self.pos(cell), runtime_id=runtime),)
            event = await self.block_event(*observed, self.piston_arm(cell, 1))
            self.assertEqual((event.kind, event.piston_face), ("piston_extending", None))
        invalid_phase = await self.block_event(self.piston_arm((0, 220, 0), 9))
        self.assertEqual((invalid_phase.kind, invalid_phase.piston_face), ("piston", None))

    async def test_sequential_piston_state_requires_a_verified_vanilla_palette(self):
        await self.bot.close()
        await self.join(hashed=False)
        position = (0, 220, 0)
        for sequence, face in ((4127, 0), (10497, 5)):
            self.assertEqual(vanilla_block_state(sequence, hashed=False).piston_face, face)
            event = await self.block_event(self.b.UpdateBlockPacket(pos=self.pos(position), runtime_id=sequence),
                                           self.piston_arm(position, 0))
            self.assertEqual((event.kind, event.piston_face), ("piston_retracted", None))
