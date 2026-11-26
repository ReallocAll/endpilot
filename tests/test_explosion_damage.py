import unittest
from math import inf, nan

from endpilot.explosion_damage import _estimate_game_base_damage


UNIT_BOX = (0, 0, 0, 1, 1, 1)
STONE = ((UNIT_BOX,), None)
AIR = ((), None)
UNKNOWN = ((), (-1, -1, -1, 2, 2, 2))
TOP_SLAB = (((0, 0.5, 0, 1, 1, 1),), None)
MEASURED_HALF_EXTENTS = (0.3000001907348633, 0.9000015258789062, 0.3000001907348633)


def measured_damage(feet, origin, blocks, radius=2.25):
    # The pre-trigger game AABB and feet are independent observations.
    box_center = (feet[0], 220.89999389648438, feet[2])
    return _estimate_game_base_damage(feet, box_center, MEASURED_HALF_EXTENTS, origin, radius, blocks)


class GameExplosionGeometryTests(unittest.TestCase):
    def test_collision_and_sampling_match_native_cover_windows(self):
        # The original and held-out BDS 1.26.66.1 windows each repeated these layouts.
        row = {(10, 221, z): STONE for z in range(6, 11)}
        column = {(10, y, 8): STONE for y in (220, 221)}
        wall = {(10, y, z): STONE for y in range(220, 223) for z in range(6, 11)}
        cases = [
            ("vertical air", 220.375, 8.5, 11.5, {}, 14.35453415),
            ("vertical row", 220.375, 8.5, 11.5, row, 8.85268593),
            ("vertical wall", 220.375, 8.5, 11.5, wall, 1.0),
            ("low Z air", 220.5, 7.8125, 11.5, {}, 14.15925121),
            ("low Z column", 220.5, 7.8125, 11.5, column, 8.74512672),
            ("low Z wall", 220.5, 7.8125, 11.5, wall, 1.0),
            ("high Z air", 220.5, 9.1875, 11.5, {}, 14.15925121),
            ("high Z column", 220.5, 9.1875, 11.5, column, 10.43564415),
            ("high Z wall", 220.5, 9.1875, 11.5, wall, 1.0),
            ("shape air", 220.0625, 8.5, 12.0, {}, 11.10594749),
            ("floor button", 220.0625, 8.5, 12.0, {(10, 220, 8): AIR}, 11.10594749),
            ("top slab", 220.0625, 8.5, 12.0, {(10, 220, 8): TOP_SLAB}, 4.29629517),
            ("single stone", 220.0625, 8.5, 12.0, {(10, 220, 8): STONE}, 1.0),
            ("held-out row air", 220.1875, 8.5, 11.5, {}, 14.54637241),
            ("held-out row", 220.1875, 8.5, 11.5, row, 9.99004745),
            ("held-out low Z air", 220.5, 7.8125, 11.5, {}, 14.15925121),
            ("held-out upper stone", 220.5, 7.8125, 11.5, {(10, 221, 8): STONE}, 11.87027645),
        ]
        for label, y, z, x, changes, expected in cases:
            with self.subTest(layout=label):
                def collision_at(position):
                    px, py, pz = position
                    if 5 <= px <= 15 and 219 <= py <= 225 and 4 <= pz <= 13:
                        return changes.get(position, STONE if py == 219 else AIR)
                    return UNKNOWN

                damage = measured_damage((x, 220, z), (9.5, y, z), collision_at)
                self.assertIsNotNone(damage)
                self.assertAlmostEqual(damage, expected, delta=0.00002)

    def test_stone_floor_endpoint_and_inclusive_three_dimensional_range(self):
        origin = (9.5, 220.0, 8.5)
        for feet, expected in (
            ((11.875, 220.0, 9.125), 7.09263325),
            ((13.21875, 220.0, 8.5), 1.11028671),
            ((13.25, 220.0, 8.5), 1.0),
            ((13.28125, 220.0, 8.5), 0.0),
        ):
            with self.subTest(feet=feet):
                def floor_at(position):
                    return STONE if position[1] == 219 else AIR

                damage = measured_damage(feet, origin, floor_at, radius=1.875)
                self.assertIsNotNone(damage)
                self.assertAlmostEqual(damage, expected, delta=0.00002)

        # Horizontal distance and distance to the body would include this archived miss.
        self.assertEqual(
            measured_damage((13.9765625, 220, 8.5), (9.5, 220.5, 8.5), lambda _: UNKNOWN),
            0.0,
        )

    def test_range_and_center_in_box_skip_terrain(self):
        def unexpected(_):
            self.fail("terrain lookup for a range or containment shortcut")

        self.assertEqual(
            measured_damage((13.25, 220, 8.5), (9.5, 220, 8.5), unexpected, radius=1.875),
            1.0,
        )
        # A nonzero vertical difference moves the real-valued distance beyond 2R,
        # but the game distance rounds to 2R in float32.
        self.assertEqual(
            measured_damage((13.25, 220 + 2 ** -12, 8.5), (9.5, 220, 8.5), unexpected, radius=1.875),
            1.0,
        )
        self.assertEqual(
            measured_damage((11.5, 220, 8.5), (11.5, 220, 8.5), unexpected),
            32.5,
        )

    def test_known_cover_settles_rays_with_other_unknown_candidates(self):
        feet, center = (11.5, 220, 8.5), (9.5, 220.375, 8.5)

        def collision_at(position):
            x, y, z = position
            return STONE if x == 10 and y in range(220, 223) and z == 8 else UNKNOWN

        self.assertEqual(measured_damage(feet, center, collision_at), 1.0)
        self.assertIsNone(measured_damage(feet, center, lambda _: UNKNOWN))

        # The voxel is in the broad phase, past all sampled positions.
        def unknown_beyond_ray(position):
            return ((), UNIT_BOX) if position == (12, 220, 8) else AIR

        self.assertAlmostEqual(
            measured_damage(feet, center, unknown_beyond_ray), 14.35453415, delta=0.00002,
        )

    def test_surface_only_hit_and_neighbor_overhang_are_unresolved(self):
        def estimate(feet, center, changes):
            x, y, z = feet
            return _estimate_game_base_damage(
                feet, (x, y + 0.25, z), (0.25, 0.25, 0.25), center, 2.25, lambda p: changes.get(p, AIR),
            )

        # One set of samples stays on the wall's side plane.
        wall = {(1, 0, -1): STONE}
        self.assertIsNone(estimate((2.5, 0, 0.25), (0.5, 0.375, 0), wall))
        self.assertIsNotNone(estimate((2.5, 0, 0.3125), (0.5, 0.375, 0.0625), wall))

        # The segment can cross a shape from the floor cell without entering
        # that cell. Neither a known protrusion nor an unknown one proves a hit.
        overhang = (0, 0, 0, 1, 1.5, 1)
        for collision in (((overhang,), None), ((), overhang)):
            with self.subTest(collision=collision):
                self.assertIsNone(estimate((2.5, 1, 0.5), (0.5, 1.25, 0.5), {(1, 0, 0): collision}))

    def test_invalid_inputs_and_work_caps_return_none_without_querying_terrain(self):
        def unexpected(_):
            self.fail("terrain lookup for an unsupported input")

        for radius in (0.0, -1.0, nan, inf, 1e-300, 1e300):
            with self.subTest(radius=radius):
                self.assertIsNone(measured_damage((11.5, 220, 8.5), (9.5, 220.5, 8.5), unexpected, radius))
        self.assertIsNone(measured_damage((nan, 220, 8.5), (9.5, 220.5, 8.5), unexpected))
        self.assertIsNone(
            _estimate_game_base_damage((2, 0, 0), (2, 0.9, 0), (0.3, 0.9, 10), (0, 0.5, 0), 2.25, unexpected),
        )
        self.assertIsNone(measured_damage((10_000, 220, 8.5), (9.5, 220.5, 8.5), unexpected, 6_000))
        self.assertEqual(measured_damage((10_000, 220, 8.5), (9.5, 220.5, 8.5), unexpected), 0.0)


if __name__ == "__main__":
    unittest.main()
