import asyncio
import math
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch
from uuid import UUID

from endpilot import Bot, EntityCursorResult
from endpilot.block_raycast import BlockRaycastResult
from endpilot.blocks import Blocks
from endpilot.entities import Entities, EntityState
from endpilot.entity_cursor import trace_entities
from endpilot.movement import Movement
from endpilot.protocol import write_batch

from test_blocks import PacketStream
from test_join import _spawn_transport


class EntityCursorGeometryTests(unittest.TestCase):
    eye = ((0.5, 1.52, 0.5), (0.0, -90.0))

    def actor(self, runtime, *, kind="minecraft:player"):
        return EntityState(-runtime, runtime, kind, (0, 0, 0))

    def trace(self, targets=(), *, max_distance=8, pose=eye, unresolved=(), obstacle=None):
        def blocks(reach):
            if obstacle == "unknown":
                return BlockRaycastResult("unknown", 0)
            if obstacle is not None and obstacle <= reach:
                return BlockRaycastResult("hit", 0, distance=obstacle)
            return BlockRaycastResult("miss", 0)

        return trace_entities(pose, targets, unresolved, max_distance=max_distance, trace_blocks=blocks)

    def test_narrow_side_uses_nominal_box_for_eligibility(self):
        near = self.actor(2)
        far = self.actor(3)
        far_box = ((5.30, 0, 0.2), (5.90, 1.80, 0.80))
        for low_z, expected in ((0.52, far), (0.49, near)):
            with self.subTest(low_z=low_z):
                near_box = ((2.60, 0, low_z), (3.20, 1.80, low_z + 0.60))
                status, entity, distance = self.trace(((far, far_box), (near, near_box)))
                self.assertEqual((status, entity), ("hit", expected))
                self.assertAlmostEqual(distance, 4.70 if expected is far else 2.0)

    def test_floor_range_limits_nominal_entry_before_reported_expansion(self):
        actor = self.actor(7, kind="minecraft:cow")
        box = ((3.55, 1.0, 0), (4.45, 2.3, 0.90))
        self.assertEqual(self.trace(((actor, box),), max_distance=3.99), ("miss", None, None))
        status, entity, distance = self.trace(((actor, box),), max_distance=4)
        self.assertEqual((status, entity), ("hit", actor))
        self.assertAlmostEqual(distance, 2.95)
        boundary = ((4.5, 1.0, 0), (5.4, 2.3, 0.90))
        self.assertEqual(self.trace(((actor, boundary),), max_distance=4)[0], "hit")
        outside = ((math.nextafter(4.5, math.inf), 1.0, 0), boundary[1])
        self.assertEqual(self.trace(((actor, outside),), max_distance=4)[0], "miss")

    def test_cow_reported_distance_uses_vertical_expansion_in_both_directions(self):
        cow = self.actor(7, kind="minecraft:cow")
        box = ((0.1, 4.05, 0.1), (0.9, 5.35, 0.9))
        looking_up = ((0.5, 1.0, 0.5), (-90.0, 0.0))
        looking_down = ((0.5, 8.40, 0.5), (90.0, 0.0))
        for pose in (looking_up, looking_down):
            self.assertEqual(self.trace(((cow, box),), pose=pose, max_distance=3.99)[0], "miss")
            status, entity, distance = self.trace(((cow, box),), pose=pose, max_distance=4)
            self.assertEqual((status, entity), ("hit", cow))
            self.assertAlmostEqual(distance, 2.95)

    def test_block_between_nominal_and_reported_entry_leaves_visible_hit(self):
        target = self.actor(7)
        box = ((4.10, 1.0, 0), (4.70, 2.8, 0.60))
        candidates = ((target, box),)
        status, entity, distance = self.trace(candidates, obstacle=3.55)
        self.assertEqual((status, entity), ("hit", target))
        self.assertAlmostEqual(distance, 3.5)
        self.assertEqual(self.trace(candidates, obstacle=distance), ("unknown", None, None))
        self.assertEqual(self.trace(candidates, obstacle=math.nextafter(distance, -math.inf)), ("miss", None, None))
        self.assertEqual(self.trace(candidates, obstacle="unknown"), ("unknown", None, None))

    def test_equal_entity_entries_use_ascending_runtime_id(self):
        newer, older = self.actor(9), self.actor(4)
        box = ((2.60, 0, 0.2), (3.20, 1.8, 0.8))
        for order in ((newer, older), (older, newer)):
            status, entity, _ = self.trace(tuple((state, box) for state in order))
            self.assertEqual((status, entity), ("hit", older))

    def test_uncertainty_only_overrides_geometry_it_can_precede(self):
        target = self.actor(7)
        box = ((3.1, 1.0, 0), (3.7, 2.8, 0.6))
        candidates = ((target, box),)
        far = ((6, 0, 0), (7, 2.3, 1))
        off_ray = ((1, 0, 4), (2, 2.3, 5))
        near = ((1, 0, 0), (2, 2.3, 1))
        self.assertEqual(self.trace(candidates, unresolved=(far, off_ray))[:2], ("hit", target))
        self.assertEqual(self.trace(candidates, unresolved=(near,)), ("unknown", None, None))
        self.assertEqual(self.trace(candidates, unresolved=(near,), obstacle=0.25), ("miss", None, None))
        self.assertEqual(self.trace(candidates, pose=None), ("unknown", None, None))


class EntityCursorPacketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("Cursor", protocol=2225)
        self.b = self.bot.protocol.binding
        self.stream = PacketStream(self.bot.protocol)
        self.stream._reader_task = None
        self.entities = Entities(self.stream, own_runtime_id=7, own_entity_id=107)
        self.movement = Movement(self.stream, 7, entities=self.entities)
        self.blocks = Blocks(self.stream, self.movement, None, hashed_runtime_ids=True)
        self.bot._entities = self.entities
        self.bot._movement = self.movement
        self.bot._blocks = self.blocks
        self.addCleanup(self.entities.close)
        self.addCleanup(self.blocks.close)
        self.entities.start()
        self.addCleanup(self.stream.subscribe_raw(self.movement._on_packet))
        self.blocks.start()

    def deliver(self, packet):
        self.stream.emit(self.bot.protocol.encode(packet))

    def vec(self, x, y, z):
        return self.b.Vec3(x=x, y=y, z=z)

    def metadata(self, *, width=None, height=None, low=0, high=0):
        b = self.b
        return b.SynchedActorData.CopyableDataList(data=[
            b.DataItemEntry(id=index, payload=payload(value=value))
            for index, payload, value in (
                (0, b.DataItemInt64Payload, low), (92, b.DataItemInt64Payload, high),
                (53, b.DataItemFloatPayload, width), (54, b.DataItemFloatPayload, height),
            ) if value is not None
        ])

    def air(self, dimension=0):
        b = self.b
        self.deliver(b.SubChunkPacket(
            dimension_type=dimension, center_pos=b.SubChunkPos(x=0, y=13, z=0),
            sub_chunk_data=[b.SubChunkPacket.SubChunkPacketData(
                sub_chunk_pos_offset=b.SubChunkPacket.SubChunkPosOffset(x=0, y=0, z=0),
                result=b.SubChunkPacket.SubChunkRequestResult.SUCCESS_ALL_AIR,
            )],
        ))

    async def view(self, *, dimension=0, air=True, z=8.5, pitch=0):
        b = self.b
        self.deliver(b.MovePlayerPacket(player_id=7, pos=self.vec(4.5, 221.62, z), on_ground=True))
        self.deliver(b.SetActorDataPacket(id=7, packed_items=self.metadata()))
        if air:
            self.air(dimension)
        await self.bot.set_rotation(pitch, -90)

    def player(self, runtime_id, entity_id, *, x=7.5, z=8.5, height=1.8, listed=True):
        b = self.b
        uuid = UUID(int=runtime_id)
        if listed:
            self.deliver(b.PlayerListPacket(entries=[b.PlayerListPacket.AddEntry(
                action=b.PlayerListPacketType.ADD, uuid=uuid, id=entity_id, name="Target",
            )]))
        self.deliver(b.AddPlayerPacket(
            runtime_id=runtime_id, uuid=uuid, name="Target", pos=self.vec(x, 220, z),
            unpack=self.metadata(width=0.6, height=height), abilities_data=b.SerializedAbilitiesData(target_player=entity_id),
        ))

    def spawn_actor(self, runtime_id=8, entity_id=108, *, x=7.5, y=221, z=8.5,
            width=0.9, height=1.3, low=0, actor_type="minecraft:cow"):
        self.deliver(self.b.AddActorPacket(
            runtime_id=runtime_id, entity_id=entity_id, actor_type=actor_type, pos=self.vec(x, y, z),
            data=self.metadata(width=width, height=height, low=low),
        ))

    def put_block(self, runtime_id):
        b = self.b
        self.deliver(b.UpdateBlockPacket(pos=b.BlockPos(x=6, y=221, z=8), runtime_id=runtime_id, layer=0))

    def delta(self, runtime_id=8, *, x=None, y=None, z=None):
        b = self.b
        self.deliver(b.MoveActorDeltaPacket(move_data=b.MoveActorDeltaData(
            runtime_id=runtime_id, new_position_x=x, new_position_y=y, new_position_z=z, is_on_ground=True,
        )))

    def query(self, **options):
        sent, rotation = len(self.stream.sent), self.bot.rotation
        result = self.bot.entity_at_cursor(**options)
        self.assertEqual((len(self.stream.sent), self.bot.rotation), (sent, rotation))
        return result

    async def test_public_player_cursor_uses_confirmed_head_height_and_sparse_metadata(self):
        await self.view()
        self.player(8, 108)
        before = self.query()
        self.assertEqual((before.status, before.dimension_id, before.entity.entity_id), ("hit", 0, 108))
        self.assertAlmostEqual(before.distance, 2.6)
        with self.assertRaises(FrozenInstanceError):
            before.distance = 0
        self.deliver(self.b.MovePlayerPacket(player_id=8, pos=self.vec(7.3, 221.62, 8.5), on_ground=True))
        flags = 1 << int(self.b.ActorFlags.SNEAKING)
        self.deliver(self.b.SetActorDataPacket(id=8, packed_items=self.metadata(height=1.49, low=flags, high=None)))
        self.assertEqual(self.query(), EntityCursorResult("miss", 0))
        await self.bot.set_sneaking(True)
        self.assertEqual(self.query(), EntityCursorResult("unknown", 0))
        self.deliver(self.b.SetActorDataPacket(id=7, packed_items=self.metadata(low=flags, high=None)))
        after = self.query()
        self.assertEqual((after.status, after.entity.entity_id), ("hit", 108))
        self.assertAlmostEqual(after.distance, 2.4, places=5)
        self.assertAlmostEqual(before.distance, 2.6)

    async def test_cow_sparse_axes_and_measurements_expire_on_reappearance(self):
        await self.view()
        self.spawn_actor()
        initial = self.query()
        self.assertEqual((initial.status, initial.entity.actor_type), ("hit", "minecraft:cow"))
        self.assertAlmostEqual(initial.distance, 2.45)
        self.delta(y=220)
        self.assertEqual(self.query(), EntityCursorResult("miss", 0))
        self.delta(y=221)
        self.deliver(self.b.SetActorDataPacket(id=8, packed_items=self.metadata(width=1.0, low=None, high=None)))
        self.assertAlmostEqual(self.query().distance, 2.4)
        self.spawn_actor(entity_id=109, height=None)
        self.assertEqual(self.query(max_distance=4), EntityCursorResult("unknown", 0))
        self.deliver(self.b.SetActorDataPacket(id=8, packed_items=self.metadata(height=1.3, low=None, high=None)))
        self.assertEqual(self.query().entity.entity_id, 109)
        self.deliver(self.b.RemoveActorPacket(entity_id=109))
        self.assertEqual(self.query(), EntityCursorResult("miss", 0))
        self.assertEqual(initial.entity.entity_id, 108)

    async def test_unsupported_body_only_makes_relevant_candidate_uncertain(self):
        await self.view()
        self.spawn_actor()
        self.player(9, 109, x=9.5, listed=False)
        self.assertEqual(self.query(max_distance=8).entity.entity_id, 108)
        self.spawn_actor(10, 110, x=6.0, width=None)
        self.assertEqual(self.query(max_distance=8), EntityCursorResult("unknown", 0))
        self.deliver(self.b.RemoveActorPacket(entity_id=110))
        self.deliver(self.b.SetActorDataPacket(
            id=8, packed_items=self.metadata(low=1 << int(self.b.ActorFlags.BABY), high=None),
        ))
        self.assertEqual(self.query(max_distance=8), EntityCursorResult("unknown", 0))

    async def test_missing_width_preserves_known_height_planes_for_uncertainty(self):
        await self.view()
        self.spawn_actor(8, 108, x=7.5, width=None)
        self.spawn_actor(9, 109, x=8.0)
        self.assertEqual(self.query(max_distance=8), EntityCursorResult("unknown", 0))
        self.delta(y=219)
        self.assertEqual(self.query(max_distance=8).entity.entity_id, 109)

    async def test_measured_sheep_envelope_excludes_off_ray_actor_without_selecting_it(self):
        await self.view()
        self.spawn_actor()
        sheep = {"runtime_id": 9, "entity_id": 109, "actor_type": "minecraft:sheep"}
        self.spawn_actor(**sheep, x=10.5, z=13.5)
        self.assertEqual(self.query(max_distance=8).entity.entity_id, 108)
        self.delta(runtime_id=9)  # A packet without position axes leaves the spawn anchor intact.
        self.assertEqual(self.query(max_distance=8).entity.entity_id, 108)
        self.delta(runtime_id=9, z=13.6)
        self.assertEqual(self.query(max_distance=8), EntityCursorResult("unknown", 0))

        self.spawn_actor(**sheep, x=6.0)
        self.assertEqual(self.query(max_distance=8), EntityCursorResult("unknown", 0))
        self.deliver(self.b.RemoveActorPacket(entity_id=109))
        self.assertEqual(self.query(max_distance=8).entity.entity_id, 108)

    async def test_visible_static_block_and_gap_use_measured_entity_occlusion(self):
        for runtime_id, through, surface, pitch in (
            (1469155469, 8.5, 8.985, 0),  # Vine plane.
            (2747170404, 8.5, 8.5, 5),  # Bottom oak slab.
            (3162850062, 8.985, 8.5, 0),  # Ordinary chest outline.
        ):
            with self.subTest(runtime_id=runtime_id):
                await self.view(z=through)
                self.player(8, 108, x=7.85, z=through)
                self.put_block(runtime_id)
                self.assertEqual(self.query(max_distance=8).entity.entity_id, 108)
                await self.view(z=surface, pitch=pitch)
                self.player(8, 108, x=7.85, z=surface)
                self.put_block(runtime_id)
                self.assertEqual(self.bot.raycast_block().status, "hit")
                self.assertEqual(self.query(max_distance=8), EntityCursorResult("miss", 0))
        self.put_block(0x713BF7A9)  # Dynamic piston outline.
        self.assertEqual(self.bot.raycast_block().status, "unknown")
        self.assertEqual(self.query(max_distance=8), EntityCursorResult("unknown", 0))

    async def test_ungrounded_teleport_invalidates_the_head_until_grounded_again(self):
        await self.view()
        self.spawn_actor()
        self.assertEqual(self.query().status, "hit")
        b = self.b
        self.deliver(b.MovePlayerPacket(
            player_id=7, pos=self.vec(4.5, 221.62, 8.5), on_ground=False,
            reset_position=b.PlayerPositionModeComponent.PositionMode.TELEPORT,
            teleport_data=b.MovePlayerTeleportData(),
        ))
        self.assertEqual(self.query(max_distance=8), EntityCursorResult("unknown", 0))
        await self.view()
        self.assertEqual(self.query().status, "hit")

    async def test_missing_terrain_and_dimension_change_do_not_leak_actor_hits(self):
        await self.view(air=False)
        self.spawn_actor()
        self.assertEqual(self.query(), EntityCursorResult("unknown", 0))
        self.air()
        self.assertEqual(self.query().status, "hit")
        self.deliver(self.b.ChangeDimensionPacket(dimension_id=1, pos=self.vec(4.5, 221.62, 8.5)))
        self.assertEqual(self.query(), EntityCursorResult("unknown", 1))
        await self.view(dimension=1)
        self.assertEqual(self.query(), EntityCursorResult("miss", 1))
        self.deliver(self.b.DisconnectPacket())
        self.assertEqual(self.query(), EntityCursorResult("unknown", 1))


class EntityCursorLoginTests(unittest.IsolatedAsyncioTestCase):
    def flags(self, b, *, actor=7, low=844459290394624, extended=None):
        words = [(0, low)]
        if extended is not None:
            words.append((92, extended))
        return b.SetActorDataPacket(id=actor, packed_items=b.SynchedActorData.CopyableDataList(
            data=[b.DataItemEntry(id=key, payload=b.DataItemInt64Payload(value=value)) for key, value in words],
        ))

    async def join(self, *, extended):
        bot = Bot("CursorLogin", protocol=2225)
        self.addAsyncCleanup(bot.close)
        b = bot.protocol.binding
        respawn = b.PlayerRespawnState
        # Vanilla's initial position exchange emits SEARCHING, both actor
        # flag words, then another SEARCHING and READY.
        transport = _spawn_transport(bot.protocol, 7,
                                     b.RespawnPacket(runtime_id=0, state=respawn.SEARCHING_FOR_SPAWN),
                                     self.flags(b, low=844424930656256, extended=extended),
                                     b.RespawnPacket(runtime_id=0, state=respawn.SEARCHING_FOR_SPAWN),
                                     b.RespawnPacket(runtime_id=0, state=respawn.READY_TO_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=transport):
            await bot.join_nethernet("http://127.0.0.1:19132")
        await self.observe(bot, transport, self.flags(b))
        await bot.set_rotation(0, -90)
        return bot, transport

    async def observe(self, bot, transport, *events):
        b = bot.protocol.binding
        terrain = b.SubChunkPacket(
            dimension_type=0, center_pos=b.SubChunkPos(x=0, y=13, z=0),
            sub_chunk_data=[b.SubChunkPacket.SubChunkPacketData(
                sub_chunk_pos_offset=b.SubChunkPacket.SubChunkPosOffset(x=0, y=0, z=0),
                result=b.SubChunkPacket.SubChunkRequestResult.SUCCESS_ALL_AIR,
            )],
        )
        metadata = b.SynchedActorData.CopyableDataList(data=[
            b.DataItemEntry(id=index, payload=payload(value=value))
            for index, payload, value in ((0, b.DataItemInt64Payload, 0), (92, b.DataItemInt64Payload, 0),
                                          (53, b.DataItemFloatPayload, 0.9), (54, b.DataItemFloatPayload, 1.3))
        ])
        cow = b.AddActorPacket(runtime_id=8, entity_id=108, actor_type="minecraft:cow",
                               pos=b.Vec3(x=7.5, y=221, z=8.5), data=metadata)
        grounded = b.MovePlayerPacket(player_id=7, on_ground=True, pos=b.Vec3(x=4.5, y=221.62, z=8.5))
        marker = b.SetTimePacket(time=31)
        waiter = asyncio.create_task(bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        transport.received.put_nowait(write_batch(map(bot.protocol.encode, (*events, terrain, cow, grounded, marker))))
        await waiter

    async def test_initial_flags_survive_spawn_position_exchange_and_sparse_low_word(self):
        bot, transport = await self.join(extended=35184405643264)
        self.assertIs(bot.movement_state.crawling, False)
        before = len(transport.sent)
        self.assertEqual(bot.entity_at_cursor().entity.entity_id, 108)
        self.assertEqual(len(transport.sent), before)

        b = bot.protocol.binding
        respawn = b.PlayerRespawnState
        await self.observe(bot, transport,
                           b.RespawnPacket(runtime_id=7, state=respawn.SEARCHING_FOR_SPAWN),
                           b.RespawnPacket(runtime_id=7, state=respawn.READY_TO_SPAWN), self.flags(b))
        self.assertIsNone(bot.movement_state.crawling)
        self.assertEqual(bot.entity_at_cursor(), EntityCursorResult("unknown", 0))

    async def test_initial_pose_without_an_extended_word_remains_unknown_until_observed(self):
        bot, transport = await self.join(extended=None)
        self.assertIsNone(bot.movement_state.crawling)
        self.assertEqual(bot.entity_at_cursor(), EntityCursorResult("unknown", 0))
        await self.observe(bot, transport, self.flags(bot.protocol.binding, extended=35184405643264))
        self.assertEqual(bot.entity_at_cursor().entity.entity_id, 108)
