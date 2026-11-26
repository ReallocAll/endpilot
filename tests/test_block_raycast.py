import unittest
from math import cos, inf, nextafter, radians

from endpilot import BlockRaycastResult, Bot
from endpilot.block_raycast import trace_blocks
from endpilot.blocks import Blocks
from endpilot.movement import Movement

from test_blocks import PacketStream


class RaycastBoundaryTests(unittest.TestCase):
    cube = (0., 0., 0., 1., 1., 1.), False
    air = (), False
    unresolved = None, False

    def cast(self, terrain, yaw, distance=5., eye=(0.5, 0.5, 0.5)):
        return trace_blocks(
            eye, (0., yaw), dimension_id=0, max_distance=distance,
            outline_at=lambda position: terrain.get(position, self.air),
        )

    def test_max_distance_includes_exact_grid_surface(self):
        target = (1, 0, 4)
        distance = 3.5 / cos(radians(10))
        terrain = {target: self.cube}
        self.assertEqual(self.cast(terrain, -10., nextafter(distance, -inf)).status, "miss")
        for limit in (distance, nextafter(distance, inf)):
            with self.subTest(limit=limit):
                hit = self.cast(terrain, -10., limit)
                self.assertEqual((hit.status, hit.position, hit.face), ("hit", target, 2))
                self.assertEqual(hit.distance, distance)

    def test_exact_corner_differs_from_a_ray_on_either_side(self):
        diagonal = (1, 0, 1)
        z_first = (0, 0, 1)
        x_first = (1, 0, 0)
        for outline in (self.cube, self.unresolved):
            terrain = {diagonal: self.cube, z_first: outline, x_first: outline}
            exact = self.cast(terrain, -45.)
            self.assertEqual((exact.status, exact.position, exact.face), ("hit", diagonal, 4))
            for yaw, position, face in ((-44.99999999998, z_first, 2), (-45.00000000002, x_first, 4)):
                with self.subTest(yaw=yaw, outline=outline):
                    hit = self.cast(terrain, yaw)
                    if outline == self.cube:
                        self.assertEqual((hit.status, hit.position, hit.face), ("hit", position, face))
                    else:
                        self.assertEqual(hit, BlockRaycastResult("unknown", 0, position))

    def test_parallel_ray_on_negative_grid_plane_uses_floor_side(self):
        terrain = {(-2, 0, 0): self.cube, (-1, 0, 1): self.cube}
        hit = self.cast(terrain, 0., eye=(-1., 0.5, -0.5))
        self.assertEqual(hit, BlockRaycastResult("hit", 0, (-1, 0, 1), 2, (0., 0.5, 0.), 1.5))


class BlockRaycastTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("Raycast", protocol=2225)
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

    def move(self, position):
        x, y, z = position
        self.deliver(self.b.MovePlayerPacket(player_id=7, pos=self.b.Vec3(x=x, y=y, z=z)))

    def air_section(self):
        b = self.b
        self.deliver(b.SubChunkPacket(
            dimension_type=0, center_pos=b.SubChunkPos(x=-1, y=13, z=-1),
            sub_chunk_data=[b.SubChunkPacket.SubChunkPacketData(
                sub_chunk_pos_offset=b.SubChunkPacket.SubChunkPosOffset(x=0, y=0, z=0),
                result=b.SubChunkPacket.SubChunkRequestResult.SUCCESS_ALL_AIR,
            )],
        ))

    def update(self, position, runtime_id):
        x, y, z = position
        self.deliver(self.b.UpdateBlockPacket(
            pos=self.b.BlockPos(x=x, y=y, z=z), runtime_id=runtime_id, layer=0,
        ))

    async def test_thin_button_or_barrel_through_the_same_cell(self):
        self.move((-14.5, 221.5, -8.5))
        self.air_section()
        button, barrel = (-13, 221, -9), (-11, 221, -9)
        self.update(button, 577382071)
        self.update(barrel, 198111737)

        await self.bot.look_at((-12.55, 221.5, -8.94))
        hit = self.bot.raycast_block()
        self.assertEqual((hit.status, hit.position, hit.face), ("hit", button, 4))
        self.assertAlmostEqual(hit.click_pos[0], 0.3125)
        self.assertAlmostEqual(hit.click_pos[1], 0.5)
        self.assertTrue(0 < hit.click_pos[2] < 0.125)

        await self.bot.look_at((-10.5, 221.5, -8.5))
        self.assertEqual(self.bot.raycast_block(max_distance=3.49), BlockRaycastResult("miss", 0))
        hit = self.bot.raycast_block(max_distance=3.5)
        self.assertEqual(hit, BlockRaycastResult("hit", 0, barrel, 4, (0., 0.5, 0.5), 3.5))

    async def test_chest_outlines_use_the_same_inset_in_both_id_modes(self):
        chest = (-13, 221, -9)
        cases = (
            ("chest", True, (741882976, 1429214429, 3162850062, 2001328343)),
            ("chest", False, (20183, 20184, 20185, 20186)),
            ("trapped_chest", True, (1235022161, 1655451298, 828777955, 3798198968)),
            ("trapped_chest", False, (14627, 14628, 14629, 14630)),
        )
        for family, hashed, orientations in cases:
            self.blocks.hashed_runtime_ids = hashed
            for runtime_id in orientations:
                with self.subTest(family=family, hashed=hashed, runtime_id=runtime_id):
                    self.air_section()
                    self.update(chest, runtime_id)
                    self.move((-14.5, 221.94, -8.5))
                    hit = self.bot.raycast_block_towards((-11.5, 221.94, -8.5))
                    self.assertEqual((hit.status, hit.position, hit.face), ("hit", chest, 4))
                    self.assertEqual(hit.click_pos[0], .025)

                    self.move((-14.5, 221.96, -8.5))
                    self.assertEqual(self.bot.raycast_block_towards((-11.5, 221.96, -8.5)).status, "miss")

    async def test_pending_pose_and_unknown_neighbors_are_conservative(self):
        self.assertEqual(self.bot.raycast_block(), BlockRaycastResult("unknown", 0))
        self.move((-14.5, 221.5, -8.5))
        self.assertEqual(self.bot.raycast_block().status, "unknown")
        self.air_section()
        await self.bot.look_at((-10.5, 221.5, -8.5))
        self.update((-13, 222, -9), 0xFFFFFFFE)
        self.assertEqual(self.bot.raycast_block().status, "unknown")
        self.air_section()
        self.assertEqual(self.bot.raycast_block(max_distance=4).status, "miss")
        self.deliver(self.b.ChangeDimensionPacket(dimension_id=1))
        self.assertEqual(self.bot.raycast_block(), BlockRaycastResult("unknown", 1))

    async def test_diagonal_corner_crosses_both_planes_at_once(self):
        self.move((-14.5, 221.5, -8.5))
        self.air_section()
        for position in ((-14, 221, -9), (-15, 221, -8), (-13, 221, -7)):
            self.update(position, 2150698529)
        await self.bot.look_at((-10.5, 221.5, -4.5))
        hit = self.bot.raycast_block()
        self.assertEqual((hit.status, hit.position), ("hit", (-13, 221, -7)))
        self.assertAlmostEqual(hit.distance, 1.5 * 2 ** 0.5)

    async def test_sequential_palette_and_vertical_face(self):
        self.blocks.hashed_runtime_ids = False
        self.move((-14.5, 222.5, -8.5))
        self.air_section()
        self.update((-15, 220, -9), 17896)
        await self.bot.look_at((-14.5, 220.5, -8.5))
        hit = self.bot.raycast_block()
        self.assertEqual(hit, BlockRaycastResult("hit", 0, (-15, 220, -9), 1, (0.5, 1., 0.5), 1.5))
        self.blocks._custom_blocks = True
        self.assertEqual(self.bot.raycast_block().status, "unknown")
        for distance in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.bot.raycast_block(max_distance=distance)

    async def test_static_slab_neighbors_and_opposing_faces_in_both_id_modes(self):
        slab, back = (-13, 221, -9), (-11, 221, -9)
        for hashed, bottom, top, cobble, pane in (
            (True, 2747170404, 1728771929, 1741778478, 1582595964),
            (False, 10165, 10166, 6797, 13146),
        ):
            with self.subTest(hashed=hashed):
                self.blocks.hashed_runtime_ids = hashed
                self.move((-14.5, 221.75, -8.5))
                self.air_section()
                self.update(slab, bottom)
                self.update(back, cobble)
                self.update((-14, 221, -8), pane)
                await self.bot.look_at((-10.5, 221.75, -8.5))
                self.assertEqual(self.bot.raycast_block().position, back)
                self.update(slab, top)
                self.assertEqual(
                    self.bot.raycast_block(),
                    BlockRaycastResult("hit", 0, slab, 4, (0., 0.75, 0.5), 1.5),
                )

                self.air_section()
                self.update(slab, top)
                self.move((-10.5, 221.75, -8.5))
                await self.bot.look_at((-14.5, 221.75, -8.5))
                self.assertEqual(
                    self.bot.raycast_block(),
                    BlockRaycastResult("hit", 0, slab, 5, (1., 0.75, 0.5), 1.5),
                )

    async def test_first_hit_can_belong_to_an_adjacent_overshooting_box(self):
        owner, back = (-13, 220, -9), (-11, 221, -9)
        for hashed, mature, shorter, cobble in (
            (True, 1670228562, 1608753739, 1741778478),
            (False, 20932, 20931, 6797),
        ):
            with self.subTest(hashed=hashed):
                self.blocks.hashed_runtime_ids = hashed
                self.move((-14.5, 221.09375, -8.5))
                self.air_section()
                self.update(owner, mature)
                self.update(back, cobble)
                await self.bot.look_at((-10.5, 221.09375, -8.5))
                self.assertEqual(
                    self.bot.raycast_block(),
                    BlockRaycastResult("hit", 0, owner, 4, (0., 1.09375, 0.5), 1.5),
                )
                self.update(owner, shorter)
                self.assertEqual(self.bot.raycast_block().position, back)

    async def test_only_an_unresolved_neighbor_that_may_spill_blocks_a_clear_ray(self):
        self.move((-14.5, 221.5, -8.5))
        self.air_section()
        await self.bot.look_at((-10.5, 221.5, -8.5))
        neighbor = (-13, 221, -8)
        self.update(neighbor, 1211861802)  # Water, with a confined but unsupported picking mode.
        self.assertEqual(self.bot.raycast_block(max_distance=4).status, "miss")
        self.update(neighbor, 2667808960)  # Azure bluet uses a position-dependent model offset.
        self.assertEqual(self.bot.raycast_block(max_distance=4),
                         BlockRaycastResult("unknown", 0, neighbor))
        self.air_section()
        self.update((-13, 221, -9), 1211861802)
        self.assertEqual(self.bot.raycast_block(max_distance=4).status, "unknown")
