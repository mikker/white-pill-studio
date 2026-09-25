"""Minimal PNG decode/encode and pixel diffing, pure stdlib.

Decodes 8-bit, non-interlaced truecolor PNGs (RGB or RGBA, as Chromium
writes them) into RGBA rows; anything else raises `PNGError` with the reason.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHANNELS = {2: 3, 6: 4}  # color type → channels
COLOR_NAMES = {0: "grayscale", 2: "RGB", 3: "indexed", 4: "grayscale+alpha", 6: "RGBA"}


class PNGError(ValueError):
    pass


@dataclass
class Image:
    width: int
    height: int
    rows: list  # RGBA bytes per row, width * 4 long

    def pixel(self, x: int, y: int) -> tuple:
        offset = x * 4
        return tuple(self.rows[y][offset:offset + 4])


def _chunks(data: bytes):
    if not data.startswith(SIGNATURE):
        raise PNGError("not a PNG file")
    position = len(SIGNATURE)
    while position + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[position:position + 8])
        body = data[position + 8:position + 8 + length]
        if len(body) != length:
            raise PNGError("truncated PNG chunk")
        crc = struct.unpack(">I", data[position + 8 + length:position + 12 + length])[0]
        if zlib.crc32(kind + body) & 0xFFFFFFFF != crc:
            raise PNGError(f"bad CRC in {kind.decode(errors='replace')} chunk")
        yield kind, body
        position += 12 + length
        if kind == b"IEND":
            return
    raise PNGError("PNG ends without IEND")


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(kind: int, line: bytearray, previous: bytes, bpp: int) -> bytearray:
    if kind == 0:
        return line
    size = len(line)
    if kind == 1:
        for i in range(bpp, size):
            line[i] = (line[i] + line[i - bpp]) & 0xFF
    elif kind == 2:
        for i in range(size):
            line[i] = (line[i] + previous[i]) & 0xFF
    elif kind == 3:
        for i in range(size):
            left = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
    elif kind == 4:
        for i in range(size):
            if i >= bpp:
                line[i] = (line[i] + _paeth(line[i - bpp], previous[i], previous[i - bpp])) & 0xFF
            else:
                line[i] = (line[i] + previous[i]) & 0xFF
    else:
        raise PNGError(f"unknown filter type {kind}")
    return line


def decode(data: bytes) -> Image:
    header = None
    compressed = bytearray()
    for kind, body in _chunks(data):
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            compressed += body
    if header is None:
        raise PNGError("PNG has no IHDR chunk")
    width, height, depth, color, _compression, _filter, interlace = header
    if depth != 8 or color not in CHANNELS:
        raise PNGError(f"unsupported PNG: {depth}-bit {COLOR_NAMES.get(color, f'color type {color}')} "
                       "(only 8-bit RGB/RGBA is supported)")
    if interlace:
        raise PNGError("unsupported PNG: interlaced (Adam7)")
    channels = CHANNELS[color]
    stride = width * channels
    try:
        raw = zlib.decompress(bytes(compressed))
    except zlib.error as error:
        raise PNGError(f"corrupt PNG data: {error}") from None
    if len(raw) < (stride + 1) * height:
        raise PNGError("PNG image data is truncated")
    rows = []
    previous = bytes(stride)
    for y in range(height):
        start = y * (stride + 1)
        line = _unfilter(raw[start], bytearray(raw[start + 1:start + 1 + stride]), previous, channels)
        previous = bytes(line)
        if channels == 3:
            rgba = bytearray(width * 4)
            rgba[0::4], rgba[1::4], rgba[2::4] = line[0::3], line[1::3], line[2::3]
            rgba[3::4] = b"\xff" * width
            rows.append(bytes(rgba))
        else:
            rows.append(previous)
    return Image(width, height, rows)


def read(path) -> Image:
    with open(path, "rb") as file:
        return decode(file.read())


def encode(image: Image) -> bytes:
    """An 8-bit RGBA PNG (filter 0 on every row)."""
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + row for row in image.rows)
    return (SIGNATURE
            + chunk(b"IHDR", struct.pack(">IIBBBBB", image.width, image.height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def write(path, image: Image) -> None:
    with open(path, "wb") as file:
        file.write(encode(image))


@dataclass
class Diff:
    width: int
    height: int
    differing: int  # pixels where any channel differs by more than the threshold
    max_delta: int
    size_mismatch: str | None = None
    image: Image | None = None  # baseline faded, differing pixels in red

    @property
    def total(self) -> int:
        return self.width * self.height

    @property
    def ratio(self) -> float:
        if self.size_mismatch:
            return 1.0
        return self.differing / self.total if self.total else 0.0


def compare(expected: Image, actual: Image, threshold: int = 0, with_image: bool = True) -> Diff:
    """Pixel difference between two images of the same size.

    A pixel differs when any RGBA channel differs by more than `threshold`.
    """
    if (expected.width, expected.height) != (actual.width, actual.height):
        return Diff(actual.width, actual.height, actual.width * actual.height, 255,
                    f"{expected.width}×{expected.height} baseline vs {actual.width}×{actual.height} capture")
    differing = 0
    max_delta = 0
    out_rows = [] if with_image else None
    for y in range(expected.height):
        left, right = expected.rows[y], actual.rows[y]
        same = left == right
        if same and not with_image:
            continue
        faded = bytearray(len(left))
        for x in range(0, len(left), 4):
            delta = 0 if same else max(abs(left[x] - right[x]), abs(left[x + 1] - right[x + 1]),
                                       abs(left[x + 2] - right[x + 2]), abs(left[x + 3] - right[x + 3]))
            if delta > max_delta:
                max_delta = delta
            if delta > threshold:
                differing += 1
                if with_image:
                    faded[x:x + 4] = b"\xff\x00\x40\xff"
            elif with_image:
                gray = (left[x] * 54 + left[x + 1] * 183 + left[x + 2] * 19) >> 8
                value = 170 + gray * 85 // 255
                faded[x:x + 4] = bytes((value, value, value, 255))
        if with_image:
            out_rows.append(bytes(faded))
    image = Image(expected.width, expected.height, out_rows) if with_image else None
    return Diff(expected.width, expected.height, differing, max_delta, None, image)
