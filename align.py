"""Measured alignment between native window captures and live specimens.

Pure stdlib. `script/align-check` drives it: it decodes a native capture
(`captures/<surface>-<mode>.png`, device pixels) and a screenshot of the
matching standalone specimen (logical px, device scale 1), resamples the
native crop to logical px with an exact box filter (the crop can start on a
half device pixel; the metadata says where), and reports per-region RMSE and
the positions of landmark edges so alignment is measured, not eyeballed.

All coordinates are logical px relative to the window's outer box (Hyprland
border included), which is what both the native crop and the specimen's
`.hypr-window` screenshot contain. A region or probe anchored "right" counts x
from the window's right edge, so windows of different widths still line up.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import visual


# ── Images as float RGB planes ───────────────────────────────────────────


@dataclass
class Plane:
    """An RGB image as rows of float triples (0–255)."""

    width: int
    height: int
    rows: list  # rows[y][x] = (r, g, b)

    def pixel(self, x: int, y: int) -> tuple:
        return self.rows[y][x]


def plane(image: visual.Image) -> Plane:
    rows = [[(row[i], row[i + 1], row[i + 2]) for i in range(0, len(row), 4)] for row in image.rows]
    return Plane(image.width, image.height, rows)


def to_image(image: Plane) -> visual.Image:
    """The inverse of plane(): channels rounded and clamped, opaque."""
    rows = [b"".join(bytes(max(0, min(255, round(channel))) for channel in pixel) + b"\xff" for pixel in row)
            for row in image.rows]
    return visual.Image(image.width, image.height, rows)


def _weights(count: int, scale: float, offset: float, limit: int) -> list:
    """For each output index, [(source index, weight)] covering [i*scale+offset, (i+1)*scale+offset)."""
    result = []
    for index in range(count):
        start, end = index * scale + offset, (index + 1) * scale + offset
        taps, first = [], max(0, math.floor(start))
        for source in range(first, min(limit, math.ceil(end))):
            weight = min(end, source + 1) - max(start, source)
            if weight > 1e-9:
                taps.append((source, weight))
        total = sum(weight for _, weight in taps) or 1.0
        result.append([(source, weight / total) for source, weight in taps])
    return result


def resample(image: visual.Image, scale: float, offset: tuple = (0.0, 0.0),
             size: tuple | None = None) -> Plane:
    """Box-filter `image` (device px) down to logical px.

    Logical pixel (X, Y) averages the device-pixel area
    [X*scale + ox, (X+1)*scale + ox) × [Y*scale + oy, …). `offset` is where
    the logical origin sits inside the image, in device px (a crop taken at
    a truncated coordinate starts early, so the offset is positive).
    """
    ox, oy = offset
    width = size[0] if size else int((image.width - ox) / scale)
    height = size[1] if size else int((image.height - oy) / scale)
    xs = _weights(width, scale, ox, image.width)
    ys = _weights(height, scale, oy, image.height)
    source_rows = {}

    def horizontal(y):
        if y not in source_rows:
            row = image.rows[y]
            out = []
            for taps in xs:
                r = g = b = 0.0
                for source, weight in taps:
                    base = source * 4
                    r += row[base] * weight
                    g += row[base + 1] * weight
                    b += row[base + 2] * weight
                out.append((r, g, b))
            source_rows[y] = out
        return source_rows[y]

    rows = []
    for taps in ys:
        parts = [(horizontal(source), weight) for source, weight in taps]
        rows.append([
            tuple(sum(part[x][channel] * weight for part, weight in parts) for channel in range(3))
            for x in range(width)
        ])
    return Plane(width, height, rows)


# ── Regions and RMSE ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Region:
    name: str
    x: float
    y: float
    width: float
    height: float
    anchor: str = "left"  # "right": x counts from the window's right edge to the region's right edge

    def box(self, width: int, height: int) -> tuple:
        """Integer (x0, y0, x1, y1) inside an image of this size, clipped."""
        x0 = self.x if self.anchor == "left" else width - self.x - self.width
        x1, y1 = x0 + self.width, self.y + self.height
        return (max(0, int(x0)), max(0, int(self.y)), min(width, int(x1)), min(height, int(y1)))


def region_rmse(left: Plane, right: Plane, region: Region) -> tuple:
    """RMSE (8-bit units, RGB) over `region` placed in each image; returns (rmse, pixels)."""
    a = region.box(left.width, left.height)
    b = region.box(right.width, right.height)
    width = min(a[2] - a[0], b[2] - b[0])
    height = min(a[3] - a[1], b[3] - b[1])
    if width <= 0 or height <= 0:
        return (None, 0)
    total = 0.0
    for dy in range(height):
        row_a, row_b = left.rows[a[1] + dy], right.rows[b[1] + dy]
        for dx in range(width):
            pa, pb = row_a[a[0] + dx], row_b[b[0] + dx]
            total += (pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2 + (pa[2] - pb[2]) ** 2
    count = width * height
    return (math.sqrt(total / (count * 3)), count)


# ── Landmarks ────────────────────────────────────────────────────────────


def distance(a: tuple, b: tuple) -> float:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def to_hex(color: tuple) -> str:
    return "#" + "".join(f"{max(0, min(255, round(channel))):02x}" for channel in color)


def hex_color(value: str) -> tuple:
    return tuple(float(int(value[index:index + 2], 16)) for index in (1, 3, 5))


def edge(image: Plane, axis: str, fixed: float, start: float, stop: float, threshold: float = 24,
         anchor: str = "left") -> float | None:
    """Sub-pixel position of the first colour change scanning from `start` to `stop`.

    `axis` "y" scans down a column at x=`fixed`; "x" scans along a row at
    y=`fixed`. The pixel where the change starts is treated as partially
    covered: its distance from the starting colour, relative to the settled
    colour two pixels on, gives the fraction. Returns the edge coordinate in
    the scan direction (for an "x" scan with anchor "right", counted from the
    right edge like the regions are).
    """
    step = 1 if stop >= start else -1

    def at(position):
        position = int(position)
        if axis == "y":
            x = int(fixed) if anchor == "left" else image.width - 1 - int(fixed)
            return image.pixel(x, position) if 0 <= position < image.height else None
        x = position if anchor == "left" else image.width - 1 - position
        return image.pixel(x, int(fixed)) if 0 <= x < image.width else None

    base = at(start)
    if base is None:
        return None
    position = int(start)
    while (position - stop) * step < 0:
        position += step
        current = at(position)
        if current is None:
            return None
        if distance(current, base) > threshold:
            settled = at(position + 2 * step) or current
            span = distance(settled, base) or 1.0
            coverage = max(0.0, min(1.0, distance(current, base) / span))
            # scanning forward, the edge sits `coverage` of a pixel before the far side of this pixel
            return position + 1 - coverage if step > 0 else position + coverage
    return None


def ink_box(image: Plane, region: Region, background: tuple, threshold: float = 60) -> tuple | None:
    """(left, top, right, bottom) of pixels differing from `background` inside a region, exclusive ends."""
    x0, y0, x1, y1 = region.box(image.width, image.height)
    top = bottom = left = right = None
    for y in range(y0, y1):
        row = image.rows[y]
        for x in range(x0, x1):
            if distance(row[x], background) > threshold:
                top = y if top is None else top
                bottom = y + 1
                left = x if left is None or x < left else left
                right = x + 1 if right is None or x + 1 > right else right
    if top is None:
        return None
    if region.anchor == "right":
        left, right = image.width - right, image.width - left
    return (left, top, right, bottom)


def ink_rows(image: Plane, region: Region, background: tuple, threshold: float = 60) -> list:
    """Runs (top, bottom) of rows that contain ink inside a region."""
    x0, y0, x1, y1 = region.box(image.width, image.height)
    runs, start = [], None
    for y in range(y0, y1):
        row = image.rows[y]
        inked = any(distance(row[x], background) > threshold for x in range(x0, x1))
        if inked and start is None:
            start = y
        elif not inked and start is not None:
            runs.append((start, y))
            start = None
    if start is not None:
        runs.append((start, y1))
    return runs


def mean_color(image: Plane, region: Region) -> tuple | None:
    x0, y0, x1, y1 = region.box(image.width, image.height)
    count = (x1 - x0) * (y1 - y0)
    if count <= 0:
        return None
    sums = [0.0, 0.0, 0.0]
    for y in range(y0, y1):
        for x in range(x0, x1):
            pixel = image.rows[y][x]
            for channel in range(3):
                sums[channel] += pixel[channel]
    return tuple(value / count for value in sums)


# ── Floating shell cards (OSD, notification) ─────────────────────────────


def crop(image: Plane, x: int, y: int, width: int, height: int) -> Plane:
    """A sub-plane; the box is clipped to the image."""
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(image.width, x + width), min(image.height, y + height)
    return Plane(max(0, x1 - x0), max(0, y1 - y0), [row[x0:x1] for row in image.rows[y0:y1]])


def color_box(image: Plane, region: Region, color: tuple, threshold: float = 60) -> tuple | None:
    """(left, top, right, bottom) of pixels within `threshold` of `color` inside a region, exclusive ends."""
    x0, y0, x1, y1 = region.box(image.width, image.height)
    found = None
    for y in range(y0, y1):
        row = image.rows[y]
        for x in range(x0, x1):
            if distance(row[x], color) <= threshold:
                found = (x, y, x + 1, y + 1) if found is None else (
                    min(found[0], x), found[1], max(found[2], x + 1), y + 1)
    return found


def strongest(image: Plane, region: Region, background: tuple) -> tuple | None:
    """The pixel inside a region that differs most from `background` (the ink colour of text)."""
    x0, y0, x1, y1 = region.box(image.width, image.height)
    best, best_distance = None, -1.0
    for y in range(y0, y1):
        for x in range(x0, x1):
            value = distance(image.rows[y][x], background)
            if value > best_distance:
                best, best_distance = image.rows[y][x], value
    return best


def _outward(image: Plane, axis: str, fixed: int, start: int, stop: int, threshold: float) -> float | None:
    """First sharp change scanning from `start` towards `stop`, against the previous pixel.

    Comparing locally rather than with the starting pixel ignores the slow
    gradients a blurred backdrop leaves across a translucent card.
    """
    step = 1 if stop >= start else -1

    def at(position):
        if axis == "y":
            return image.pixel(fixed, position) if 0 <= position < image.height else None
        return image.pixel(position, fixed) if 0 <= position < image.width else None

    position = start
    while (position - stop) * step < 0:
        position += step
        current, before = at(position), at(position - step)
        if current is None or before is None:
            return None
        change = distance(current, before)
        if change > threshold:
            settled = at(position + step) or current
            span = max(change, distance(settled, before)) or 1.0
            coverage = max(0.0, min(1.0, change / span))
            return position + 1 - coverage if step > 0 else position + coverage
    return None


def card_box(image: Plane, margin: float, probe: float = 8, threshold: float = 5) -> dict | None:
    """The card's inner keyline box in a shell capture with about `margin` px of backdrop around the card.

    Scans outward from inside the card, so the first sharp change is the
    card's own 0.5px keyline and the backdrop never matters. Horizontal edges
    are found on the middle row, starting `probe` px inside the expected edge;
    vertical edges on a column 12px inside the right edge (clear of the
    content and, for radius ≤ 14, of the corner curve), starting from the
    middle row.
    """
    middle = image.height // 2
    left = _outward(image, "x", middle, int(margin + probe), 0, threshold)
    right = _outward(image, "x", middle, image.width - 1 - int(margin + probe), image.width - 1, threshold)
    if left is None or right is None:
        return None
    column = int(right) - 12
    top = _outward(image, "y", column, middle, 0, threshold)
    bottom = _outward(image, "y", column, middle, image.height - 1, threshold)
    if top is None or bottom is None:
        return None
    return {"left": left, "top": top, "right": right, "bottom": bottom}
