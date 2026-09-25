import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import align  # noqa: E402
import visual  # noqa: E402


def image(width, height, color_at):
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            row += bytes(color_at(x, y)) + b"\xff"
        rows.append(bytes(row))
    return visual.Image(width, height, rows)


class ResampleTest(unittest.TestCase):
    def test_box_filter_at_one_and_a_half(self):
        # device columns alternate 0 / 255: logical pixel 0 covers device 0 and half of 1
        source = image(6, 3, lambda x, y: (255 * (x % 2),) * 3)
        plane = align.resample(source, 1.5, (0, 0), (4, 2))
        self.assertAlmostEqual(plane.pixel(0, 0)[0], 255 * 0.5 / 1.5)
        self.assertAlmostEqual(plane.pixel(1, 0)[0], 255 * 0.5 / 1.5)  # half of device 1, all of 2
        self.assertAlmostEqual(plane.pixel(2, 0)[0], 255 * 1.0 / 1.5)  # all of device 3, half of 4

    def test_half_pixel_offset_shifts_the_grid(self):
        # a hard edge at device row 3: with oy = 0.5, logical row 1 spans device rows 2..3.5
        source = image(3, 9, lambda x, y: (0, 0, 0) if y < 3 else (255, 255, 255))
        plane = align.resample(source, 1.5, (0, 0.5), (2, 4))
        self.assertAlmostEqual(plane.pixel(0, 1)[0], 255 * 0.5 / 1.5)
        self.assertEqual(plane.pixel(0, 0)[0], 0)
        self.assertEqual(plane.pixel(0, 2)[0], 255)

    def test_plane_round_trips_to_an_opaque_image(self):
        source = image(3, 2, lambda x, y: (x * 100, y * 100, 7))
        self.assertEqual(align.to_image(align.plane(source)).rows, source.rows)
        rounded = align.to_image(align.Plane(1, 1, [[(-3.0, 127.6, 300.0)]]))
        self.assertEqual(rounded.rows, [bytes((0, 128, 255, 255))])


class LandmarkTest(unittest.TestCase):
    def setUp(self):
        # white frame, grey block from x=10..30 (partially covering x=10 at 25%), y=5..15
        def color(x, y):
            if 5 <= y < 15 and 11 <= x < 30:
                return (100, 100, 100)
            if 5 <= y < 15 and x == 10:
                return (216, 216, 216)  # 25% coverage of 100 over 255
            return (255, 255, 255)
        self.plane = align.plane(image(40, 20, color))

    def test_edge_is_sub_pixel(self):
        self.assertAlmostEqual(align.edge(self.plane, "x", 8, 0, 20, 6), 10.75, places=2)
        self.assertAlmostEqual(align.edge(self.plane, "y", 20, 0, 19, 6), 5.0)

    def test_right_anchored_edge_counts_from_the_right(self):
        self.assertAlmostEqual(align.edge(self.plane, "x", 8, 0, 20, 6, anchor="right"), 10.0)

    def test_ink_box_and_rmse(self):
        white = (255.0, 255.0, 255.0)
        self.assertEqual(align.ink_box(self.plane, align.Region("all", 0, 0, 40, 20), white), (11, 5, 30, 15))
        self.assertEqual(align.ink_box(self.plane, align.Region("r", 0, 0, 40, 20, "right"), white), (10, 5, 29, 15))
        rmse, count = align.region_rmse(self.plane, self.plane, align.Region("all", 0, 0, 40, 20))
        self.assertEqual((rmse, count), (0.0, 800))


class CardTest(unittest.TestCase):
    """A shell card: a keyline ring around a flat surface, a slow backdrop gradient outside, an accent bar inside."""

    def setUp(self):
        def color(x, y):
            inside = 10 <= x < 70 and 10 <= y < 40
            if not inside:
                return (200 + x // 8, 200, 200)  # gentle gradient, no sharp edges
            if x in (10, 69) or y in (10, 39):
                return (220, 220, 220)  # keyline
            if 30 <= x < 50 and 24 <= y < 27:
                return (24, 83, 199)  # accent fill
            return (250, 250, 250)
        self.plane = align.plane(image(80, 50, color))

    def test_card_box_finds_the_keyline_from_inside(self):
        # the box is the keyline's inner edge: the surface it encloses
        box = align.card_box(self.plane, margin=10, probe=6)
        self.assertEqual({key: round(value) for key, value in box.items()},
                         {"left": 11, "top": 11, "right": 69, "bottom": 39})

    def test_color_box_and_crop(self):
        found = align.color_box(self.plane, align.Region("all", 0, 0, 80, 50), (24.0, 83.0, 199.0), 10)
        self.assertEqual(found, (30, 24, 50, 27))
        crop = align.crop(self.plane, 30, 24, 20, 3)
        self.assertEqual((crop.width, crop.height, crop.pixel(0, 0)), (20, 3, (24, 83, 199)))


if __name__ == "__main__":
    unittest.main()
