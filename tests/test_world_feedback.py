import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bedrock_protocol import nbt

from endpilot import Bot
from endpilot.block_data import vanilla_block_state
from endpilot.protocol import write_batch


class WorldFeedbackTests(unittest.IsolatedAsyncioTestCase):
    def push(self, *packets):
        self.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in packets))

    async def asyncSetUp(self):
        self.bot = Bot("FeedbackObserver", protocol=2225)
        self.b = self.bot.protocol.binding
        self.addAsyncCleanup(self.bot.close)
        await self.join()

    async def join(self, *before_spawn, hashed=True):
        b = self.b
        self.incoming = asyncio.Queue()
        transport = SimpleNamespace(recv=self.incoming.get, send=AsyncMock(), close=AsyncMock())
        self.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.push(b.StartGamePacket(runtime_id=7, entity_id=4, block_network_ids_are_hashes=hashed),
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
        self.assertIsNone(crack.result().position)
        self.assertEqual(crack.result().packet, ended)
        for event in (ambiguous, opened, piston, played_note, crack):
            self.assertIsNone(event.result().piston_attached_blocks)

    async def test_pending_observation_ends_on_close(self):
        waiting = asyncio.create_task(self.bot.wait_for_sound())
        await asyncio.sleep(0)
        await self.bot.close()
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(waiting, 1)

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

    async def test_chest_partner_needs_two_current_reciprocal_roots_and_filters_original_position(self):
        b = self.b
        lead, partner = (3, 220, 7), (3, 220, 8)
        opened = b.BlockEventPacket(pos=self.pos(lead), b0=1, b1=1)
        closed = b.BlockEventPacket(pos=self.pos(partner), b0=1, b1=0)
        partner_wait = asyncio.create_task(self.bot.wait_for_block_event(position=partner, timeout=1))
        self.addCleanup(partner_wait.cancel)
        event = await self.block_event(
            b.UpdateBlockPacket(pos=self.pos(lead), runtime_id=0x2C383C60),
            b.UpdateBlockPacket(pos=self.pos(partner), runtime_id=0x553014DD),
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
                                        "trapped", "disconnected", "non_lid")):
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
                event = await self.block_event(
                    b.UpdateBlockPacket(pos=self.pos(pos), runtime_id=0x2C383C60),
                    b.UpdateBlockPacket(pos=self.pos(peer),
                                        runtime_id=0x499CF151 if reason == "trapped" else 0x2C383C60),
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
