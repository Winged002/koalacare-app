"""Generated brand icons: the browser favicon and the iOS home-screen tile.

The mark is the same one as ``static/favicon.svg`` — a rounded eucalyptus tile
with a ring and three dots — rasterised here in pure Python, so the repository
carries no binary assets and the image needs no imaging dependency. Each size
is rendered once per worker process and then cached.
"""

from __future__ import annotations

import struct
import zlib
from functools import lru_cache

from flask import Blueprint, Response

bp = Blueprint("icons", __name__)

# Geometry mirrors static/favicon.svg, in its 32x32 design space.
_TILE_RGB = (13, 92, 83)     # #0d5c53
_MARK_RGB = (223, 240, 235)  # #dff0eb
_RADIUS = 8.0                # corner radius of the tile
_RING = (16.0, 16.0, 9.4, 0.9)  # cx, cy, radius, half stroke width
_DOTS = ((16.0, 12.6, 2.0), (12.3, 19.2, 2.0), (19.7, 19.2, 2.0))
_SUPERSAMPLE = 3


def _inside_tile(x: float, y: float) -> bool:
    """Rounded-rectangle hit test in the 32x32 design space."""
    dx = max(_RADIUS - x, x - (32.0 - _RADIUS), 0.0)
    dy = max(_RADIUS - y, y - (32.0 - _RADIUS), 0.0)
    return dx * dx + dy * dy <= _RADIUS * _RADIUS


def _on_mark(x: float, y: float) -> bool:
    cx, cy, radius, half = _RING
    if abs(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 - radius) <= half:
        return True
    for dx, dy, dr in _DOTS:
        if (x - dx) ** 2 + (y - dy) ** 2 <= dr * dr:
            return True
    return False


def _pixel(x: int, y: int, size: int) -> tuple:
    """One RGBA pixel, supersampled so the curves come out smooth."""
    step = 1.0 / _SUPERSAMPLE
    samples = _SUPERSAMPLE * _SUPERSAMPLE
    inside = 0
    marked = 0
    for sy in range(_SUPERSAMPLE):
        for sx in range(_SUPERSAMPLE):
            ux = (x + (sx + 0.5) * step) / size * 32.0
            uy = (y + (sy + 0.5) * step) / size * 32.0
            if not _inside_tile(ux, uy):
                continue
            inside += 1
            if _on_mark(ux, uy):
                marked += 1
    if not inside:
        return (0, 0, 0, 0)
    mix = marked / inside
    red = round(_TILE_RGB[0] + (_MARK_RGB[0] - _TILE_RGB[0]) * mix)
    green = round(_TILE_RGB[1] + (_MARK_RGB[1] - _TILE_RGB[1]) * mix)
    blue = round(_TILE_RGB[2] + (_MARK_RGB[2] - _TILE_RGB[2]) * mix)
    return (red, green, blue, round(255 * inside / samples))


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def _png(size: int) -> bytes:
    """An 8-bit RGBA PNG built with the standard library alone."""
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # per-row filter: none
        for x in range(size):
            rows.extend(_pixel(x, y, size))
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _chunk(b"IEND", b"")
    )


def _ico(sizes=(16, 32, 48)) -> bytes:
    """A PNG-compressed .ico, which every browser in use reads."""
    images = [(size, _png(size)) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = bytearray()
    for size, image in images:
        entries.extend(
            struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(image), offset)
        )
        offset += len(image)
    return bytes(header) + bytes(entries) + b"".join(image for _, image in images)


@lru_cache(maxsize=1)
def apple_touch_icon_png() -> bytes:
    return _png(180)


@lru_cache(maxsize=1)
def favicon_ico() -> bytes:
    return _ico()


def _image_response(body: bytes, mimetype: str) -> Response:
    response = Response(body, mimetype=mimetype)
    response.headers["Cache-Control"] = "public, max-age=604800"
    return response


@bp.get("/apple-touch-icon.png")
def apple_touch_icon():
    return _image_response(apple_touch_icon_png(), "image/png")


@bp.get("/favicon.ico")
def favicon():
    return _image_response(favicon_ico(), "image/x-icon")
