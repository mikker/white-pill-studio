import pathlib
import struct
import sys
import tempfile
import unittest
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import visual  # noqa: E402

BASELINES = pathlib.Path(__file__).resolve().parent / "baselines"


def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def png(width, height, rows, color=6, depth=8, interlace=0, filters=None):
    """Hand-built PNG; `filters` (one per row) applies real PNG filtering."""
    channels = {2: 3, 6: 4, 0: 1}.get(color, 1)
    lines, previous = [], bytes(len(rows[0]))
    for index, row in enumerate(rows):
        kind = (filters or [0] * height)[index]
        encoded = bytearray(row)
        for i in range(len(row)):
            left = row[i - channels] if i >= channels else 0
            up = previous[i]
            corner = previous[i - channels] if i >= channels else 0
            predictor = (0, left, up, (left + up) >> 1, visual._paeth(left, up, corner))[kind]
            encoded[i] = (row[i] - predictor) & 0xFF
        lines.append(bytes([kind]) + bytes(encoded))
        previous = row
    header = struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, interlace)
    return (visual.SIGNATURE + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(lines)))
            + chunk(b"IEND", b""))


RGBA_ROWS = [
    bytes([255, 0, 0, 255, 0, 255, 0, 128, 0, 0, 255, 0]),
    bytes([10, 20, 30, 40, 200, 100, 50, 255, 1, 2, 3, 4]),
    bytes([90, 90, 90, 255, 91, 92, 93, 94, 250, 251, 252, 253]),
]


class DecodeTest(unittest.TestCase):
    def test_every_filter_type(self):
        for filters in ([0, 0, 0], [1, 1, 1], [2, 2, 2], [3, 3, 3], [4, 4, 4], [0, 4, 3]):
            image = visual.decode(png(3, 3, RGBA_ROWS, filters=filters))
            self.assertEqual((image.width, image.height), (3, 3))
            self.assertEqual(image.rows, RGBA_ROWS, filters)
        self.assertEqual(image.pixel(1, 0), (0, 255, 0, 128))

    def test_rgb_becomes_opaque_rgba(self):
        rows = [bytes([1, 2, 3, 4, 5, 6]), bytes([7, 8, 9, 10, 11, 12])]
        image = visual.decode(png(2, 2, rows, color=2, filters=[1, 4]))
        self.assertEqual(image.rows, [bytes([1, 2, 3, 255, 4, 5, 6, 255]), bytes([7, 8, 9, 255, 10, 11, 12, 255])])

    def test_round_trip(self):
        image = visual.Image(3, 3, list(RGBA_ROWS))
        self.assertEqual(visual.decode(visual.encode(image)).rows, RGBA_ROWS)
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "x.png"
            visual.write(path, image)
            self.assertEqual(visual.read(path).rows, RGBA_ROWS)

    def test_unsupported_and_corrupt_files_fail_clearly(self):
        cases = (
            (png(1, 1, [b"\x00"], color=0), "8-bit grayscale"),
            (png(1, 1, [b"\x00" * 8], depth=16), "16-bit RGBA"),
            (png(1, 1, [b"\x00" * 4], interlace=1), "interlaced"),
            (b"GIF89a", "not a PNG"),
            (png(1, 1, [b"\x00" * 4])[:-20], "truncated|without IEND|CRC"),
        )
        for data, message in cases:
            with self.assertRaisesRegex(visual.PNGError, message):
                visual.decode(data)
        broken = bytearray(png(1, 1, [b"\x00" * 4]))
        broken[30] ^= 0xFF  # inside IHDR
        with self.assertRaisesRegex(visual.PNGError, "CRC"):
            visual.decode(bytes(broken))

    def test_committed_baselines_decode(self):
        found = sorted(BASELINES.glob("*.png"))
        if not found:
            self.skipTest("no baselines recorded")
        image = visual.read(found[0])
        self.assertGreater(image.width * image.height, 0)
        self.assertEqual(len(image.rows[0]), image.width * 4)


class CompareTest(unittest.TestCase):
    def image(self, rows):
        return visual.Image(len(rows[0]) // 4, len(rows), [bytes(row) for row in rows])

    def test_identical_images(self):
        a = self.image(RGBA_ROWS)
        result = visual.compare(a, self.image(RGBA_ROWS))
        self.assertEqual((result.differing, result.max_delta, result.ratio, result.total), (0, 0, 0.0, 9))
        self.assertEqual(len(result.image.rows), 3)

    def test_counts_pixels_and_honours_the_threshold(self):
        changed = [bytearray(row) for row in RGBA_ROWS]
        changed[0][0] = 250  # pixel (0,0): Δ5
        changed[2][9] = 151  # pixel (2,2): Δ100
        a, b = self.image(RGBA_ROWS), self.image(changed)
        result = visual.compare(a, b)
        self.assertEqual((result.differing, result.max_delta), (2, 100))
        self.assertAlmostEqual(result.ratio, 2 / 9)
        self.assertEqual(result.image.pixel(0, 0), (255, 0, 64, 255))
        self.assertEqual(result.image.pixel(2, 2), (255, 0, 64, 255))
        unchanged = result.image.pixel(1, 1)
        self.assertEqual(unchanged[0], unchanged[1])  # faded gray
        self.assertEqual(visual.compare(a, b, threshold=5).differing, 1)
        self.assertEqual(visual.compare(a, b, threshold=100).differing, 0)
        self.assertIsNone(visual.compare(a, b, with_image=False).image)

    def test_size_mismatch_is_a_full_difference(self):
        result = visual.compare(self.image(RGBA_ROWS), self.image(RGBA_ROWS[:2]))
        self.assertIn("3×3 baseline vs 3×2 capture", result.size_mismatch)
        self.assertEqual(result.ratio, 1.0)


if __name__ == "__main__":
    unittest.main()
