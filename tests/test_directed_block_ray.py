import unittest
from decimal import Inexact, localcontext
from fractions import Fraction
from math import cos, inf, nextafter, radians
from uuid import UUID

from endpilot import BlockRaycastResult, Bot
from endpilot.blocks import Blocks
from endpilot.entities import Entities
from endpilot.movement import Movement

from test_blocks import PacketStream


class DirectedBlockRayTests(unittest.IsolatedAsyncioTestCase):
    EYE = (-14.5, 221.5, -8.5)
    BARREL = (-11, 221, -9)

    async def asyncSetUp(self):
        self.bot = Bot("DirectedRay", protocol=2225)
        self.stream = PacketStream(self.bot.protocol)
        self.b = self.bot.protocol.binding
        self.movement = Movement(self.stream, 7)
        self.blocks = Blocks(self.stream, self.movement, None, hashed_runtime_ids=True)
        self.bot._movement = self.movement
        self.bot._blocks = self.blocks
        self.unsubscribe = self.stream.subscribe_raw(self.movement._on_packet)
        self.blocks.start()

    async def asyncTearDown(self):
        self.blocks.close()
        self.unsubscribe()
        self.assertEqual(self.stream.sent, [])

    def deliver(self, packet):
        self.stream.emit(self.bot.protocol.encode(packet))

    def prepare(self, eye=EYE):
        b = self.b
        self.deliver(b.MovePlayerPacket(player_id=7, pos=b.Vec3(x=eye[0], y=eye[1], z=eye[2])))
        self.deliver(b.SubChunkPacket(
            dimension_type=0, center_pos=b.SubChunkPos(x=-1, y=13, z=-1),
            sub_chunk_data=[b.SubChunkPacket.SubChunkPacketData(
                sub_chunk_pos_offset=b.SubChunkPacket.SubChunkPosOffset(x=0, y=0, z=0),
                result=b.SubChunkPacket.SubChunkRequestResult.SUCCESS_ALL_AIR,
            )],
        ))
        self.update(self.BARREL, 198111737)

    def update(self, position, runtime_id):
        x, y, z = position
        self.deliver(self.b.UpdateBlockPacket(
            pos=self.b.BlockPos(x=x, y=y, z=z), runtime_id=runtime_id, layer=0,
        ))

    async def test_remote_view_waits_for_ground_after_unlisted_rider_unlinks(self):
        self.prepare()
        b = self.b
        self.stream._reader_task = None
        entities = Entities(self.stream, own_runtime_id=7, own_entity_id=107)
        self.bot._entities = entities
        entities.start()
        self.addCleanup(entities.close)

        def flags():
            return b.SynchedActorData.CopyableDataList(data=[
                b.DataItemEntry(id=word, payload=b.DataItemInt64Payload(value=0)) for word in (0, 92)
            ])

        def spawn(runtime_id, entity_id, z):
            self.deliver(b.AddPlayerPacket(
                runtime_id=runtime_id, uuid=UUID(int=entity_id), name=f"Player{entity_id}",
                pos=b.Vec3(x=-14.5, y=220, z=z), rot=b.Vec2(x=0, y=-90), y_head_rot=-90,
                unpack=flags(), abilities_data=b.SerializedAbilitiesData(target_player=entity_id),
            ))

        def link(kind):
            self.deliver(b.SetActorLinkPacket(link=b.ActorLink(a=208, b=108, type=kind)))

        def move(*, mode=b.PlayerPositionModeComponent.PositionMode.NORMAL):
            self.deliver(b.MovePlayerPacket(
                player_id=8, pos=b.Vec3(x=-13.5, y=221.62, z=-8.5),
                rot=b.Vec2(x=0, y=-90), y_head_rot=-90, on_ground=True, reset_position=mode,
            ))

        other_barrel = (-11, 221, -8)
        self.update(other_barrel, 198111737)
        self.deliver(b.AddActorPacket(entity_id=208, runtime_id=18, actor_type="minecraft:boat"))
        spawn(8, 108, -8.5)
        spawn(108, 109, -7.5)
        self.assertEqual(self.bot.observed_players, ())
        self.assertEqual([entities.get(runtime_id).entity_id for runtime_id in (8, 108)], [0, 0])
        initial = self.bot.raycast_player_view(8)
        other = self.bot.raycast_player_view(108)
        self.assertEqual((initial.status, initial.position, initial.face), ("hit", self.BARREL, 4))
        self.assertEqual((other.status, other.position, other.face), ("hit", other_barrel, 4))

        link(b.ActorLinkType.RIDING)
        self.assertEqual(self.bot.raycast_player_view(8).status, "unknown")
        self.assertEqual(self.bot.raycast_player_view(108), other)
        move()  # The authoritative seat link still applies to this rider.
        self.assertEqual(self.bot.raycast_player_view(8).status, "unknown")
        link(b.ActorLinkType.NONE)
        self.assertEqual(self.bot.raycast_player_view(8).status, "unknown")
        self.deliver(b.SetActorDataPacket(id=8, packed_items=flags()))
        move(mode=b.PlayerPositionModeComponent.PositionMode.ONLY_HEAD_ROT)
        self.assertEqual(self.bot.raycast_player_view(8).status, "unknown")
        self.assertEqual(self.bot.raycast_player_view(108), other)

        move()
        recovered = self.bot.raycast_player_view(8)
        self.assertEqual((recovered.status, recovered.position, recovered.face), ("hit", self.BARREL, 4))
        self.assertEqual((initial.distance, recovered.distance), (3.5, 2.5))
        self.assertEqual(self.bot.raycast_player_view(108), other)
        self.assertEqual(self.bot.observed_players, ())

        link(b.ActorLinkType.RIDING)
        spawn(8, 110, -8.5)
        self.assertEqual(self.bot.raycast_player_view(8), initial)
        link(b.ActorLinkType.NONE)
        self.assertEqual(self.bot.raycast_player_view(8), initial)
        self.assertEqual(self.bot.raycast_player_view(108), other)

    async def test_button_and_barrel_use_the_target_ray_without_turning(self):
        self.prepare()
        button = (-13, 221, -9)
        self.update(button, 577382071)
        await self.bot.set_rotation(-21, 67)
        old_ray = self.bot.raycast_block()

        hit = self.bot.raycast_block_towards((-12.55, 221.5, -8.94))
        self.assertEqual((hit.status, hit.position, hit.face), ("hit", button, 4))
        self.assertAlmostEqual(hit.click_pos[0], 0.3125)
        self.assertAlmostEqual(hit.click_pos[1], 0.5)
        self.assertTrue(0 < hit.click_pos[2] < 0.125)

        # The target is before the button. The ray still crosses its empty space.
        point = (-14.25, 221.5, -8.5)
        expected = BlockRaycastResult("hit", 0, self.BARREL, 4, (0., 0.5, 0.5), 3.5)
        self.assertEqual(self.bot.raycast_block_towards(point), expected)
        self.assertEqual(self.bot.raycast_block_towards(point, max_distance=3.5), expected)
        self.assertEqual(self.bot.raycast_block_towards(point, max_distance=nextafter(3.5, -inf)),
                         BlockRaycastResult("miss", 0))
        self.assertEqual(self.bot.rotation, (-21, 67))
        self.assertEqual(self.bot.raycast_block(), old_ray)

    async def test_passes_above_lower_slab_and_hits_its_top_when_aimed_down(self):
        self.prepare((-14.5, 221.75, -8.5))
        slab = (-14, 221, -9)
        self.update(slab, 2747170404)
        self.assertEqual(self.bot.raycast_block_towards((-10.5, 221.75, -8.5)).position, self.BARREL)
        hit = self.bot.raycast_block_towards((-13.7, 221.4, -8.5))
        self.assertEqual((hit.status, hit.position, hit.face), ("hit", slab, 1))
        self.assertAlmostEqual(hit.click_pos[1], 0.5)

    async def test_oblique_target_ray_includes_exact_surface_distance(self):
        for eye, surface, scale, position, distance in (
            ((-14.25, 216.25, -12.75), (-14., 219.25, -9.75), 123 / 128, (-14, 219, -10), 4.25),
            ((-14.75, 216.25, -12.75), (-12., 216.75, -10.25), 49 / 64, (-12, 216, -11), 3.75),
        ):
            self.prepare(eye)
            self.update(position, 2150698529)
            expected = BlockRaycastResult("hit", 0, position, 4, (0., surface[1] % 1, surface[2] % 1), distance)
            for factor in (1, scale, 1 / 32, 3.5):
                point = tuple(origin + factor * (coord - origin) for origin, coord in zip(eye, surface, strict=True))
                with self.subTest(point=point, factor=factor, distance=distance):
                    self.assertEqual(self.bot.raycast_block_towards(point, max_distance=nextafter(distance, -inf)),
                                     BlockRaycastResult("miss", 0))
                    self.assertEqual(self.bot.raycast_block_towards(point, max_distance=distance), expected)
                    self.assertEqual(self.bot.raycast_block_towards(point, max_distance=nextafter(distance, inf)),
                                     expected)

    async def test_reported_distance_is_the_first_reaching_float(self):
        self.prepare((-14.5, 219.5, -8.5))
        position = (-13, 221, -7)
        self.update(position, 2150698529)
        point = (-13., 221., -7.)
        with localcontext() as context:
            context.traps[Inexact] = True
            hit = self.bot.raycast_block_towards(point)
        self.assertEqual((hit.status, hit.position), ("hit", position))
        shorter = nextafter(hit.distance, -inf)
        distance_squared = Fraction(27, 4)
        self.assertLess(Fraction(shorter) ** 2, distance_squared)
        self.assertGreaterEqual(Fraction(hit.distance) ** 2, distance_squared)
        self.assertEqual(self.bot.raycast_block_towards(point, max_distance=hit.distance), hit)
        self.assertEqual(self.bot.raycast_block_towards(point, max_distance=shorter), BlockRaycastResult("miss", 0))

    async def test_oblique_crosshair_includes_its_exact_surface_distance(self):
        self.prepare()
        position = (-14, 221, -5)
        self.update(position, 2150698529)
        await self.bot.set_rotation(0, -10)
        distance = 3.5 / cos(radians(10))
        self.assertEqual(self.bot.raycast_block(max_distance=nextafter(distance, -inf)),
                         BlockRaycastResult("miss", 0))
        for limit in (distance, nextafter(distance, inf)):
            hit = self.bot.raycast_block(max_distance=limit)
            self.assertEqual((hit.status, hit.position, hit.face, hit.distance), ("hit", position, 2, distance))

    async def test_nearest_occluder_unknown_shape_and_dimension_change(self):
        point = (-10.5, 221.5, -8.5)
        self.assertEqual(self.bot.raycast_block_towards(point), BlockRaycastResult("unknown", 0))
        self.prepare()
        first = (-13, 221, -9)
        self.update(first, 2150698529)
        self.assertEqual(self.bot.raycast_block_towards(point),
                         BlockRaycastResult("hit", 0, first, 4, (0., 0.5, 0.5), 1.5))
        self.update(first, 2667808960)  # Azure bluet has a position-dependent outline.
        self.assertEqual(self.bot.raycast_block_towards(point), BlockRaycastResult("unknown", 0, first))
        self.deliver(self.b.ChangeDimensionPacket(dimension_id=1))
        self.assertEqual(self.bot.raycast_block_towards(point), BlockRaycastResult("unknown", 1))

    async def test_requires_finite_point_and_distance_and_handles_no_direction(self):
        self.prepare()
        self.assertEqual(self.bot.raycast_block_towards(self.EYE), BlockRaycastResult("unknown", 0))
        for point in ((1., 2.), (1., 2., inf)):
            with self.subTest(point=point), self.assertRaises(ValueError):
                self.bot.raycast_block_towards(point)
        for distance in (-1., float("nan"), inf):
            with self.subTest(distance=distance), self.assertRaises(ValueError):
                self.bot.raycast_block_towards((0, 220, 0), max_distance=distance)
