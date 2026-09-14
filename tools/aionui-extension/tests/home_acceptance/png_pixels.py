"""Minimal PNG pixel decoder for acceptance uniqueness checks.

The harness must compare what two screenshots actually show, not how they were
encoded: the same pixels re-encoded with a different filter or compression
level produce different bytes and would otherwise look like two distinct
observations. Pillow is not available in this environment, so this module
decodes the narrow case the acceptance captures use.

Supported: 8-bit truecolour (colour type 2) and truecolour with alpha (colour
type 6), non-interlaced, filter types 0 to 4. Anything else is refused rather
than silently falling back to a byte comparison, because a byte comparison is
exactly the weakness this decoder exists to remove.
"""

from __future__ import annotations

import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {2: 3, 6: 4}


class PngDecodeError(ValueError):
    """The screenshot is not a PNG this decoder is allowed to compare."""


def _unfilter(raw: bytes, width: int, height: int, channels: int) -> bytes:
    stride = width * channels
    if len(raw) != (stride + 1) * height:
        raise PngDecodeError("PNG pixel data length does not match the header")
    out = bytearray()
    previous = bytearray(stride)
    position = 0
    for _ in range(height):
        kind = raw[position]
        position += 1
        line = bytearray(raw[position:position + stride])
        position += stride
        if kind == 1:
            for index in range(channels, stride):
                line[index] = (line[index] + line[index - channels]) & 0xFF
        elif kind == 2:
            for index in range(stride):
                line[index] = (line[index] + previous[index]) & 0xFF
        elif kind == 3:
            for index in range(stride):
                left = line[index - channels] if index >= channels else 0
                line[index] = (line[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif kind == 4:
            for index in range(stride):
                left = line[index - channels] if index >= channels else 0
                up = previous[index]
                corner = previous[index - channels] if index >= channels else 0
                estimate = left + up - corner
                distance_left = abs(estimate - left)
                distance_up = abs(estimate - up)
                distance_corner = abs(estimate - corner)
                if distance_left <= distance_up and distance_left <= distance_corner:
                    predictor = left
                elif distance_up <= distance_corner:
                    predictor = up
                else:
                    predictor = corner
                line[index] = (line[index] + predictor) & 0xFF
        elif kind != 0:
            raise PngDecodeError(f"unsupported PNG filter type {kind}")
        out += line
        previous = line
    return bytes(out)


def decode_png(data: bytes) -> tuple[int, int, int, bytes]:
    """Return width, height, channels and the raw decoded pixel bytes."""
    if not isinstance(data, (bytes, bytearray)) or data[:8] != SIGNATURE:
        raise PngDecodeError("screenshot is not a PNG")
    offset = 8
    width = height = channels = 0
    header_seen = False
    chunks: list[bytes] = []
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise PngDecodeError("PNG is truncated")
        payload = data[offset + 8:offset + 8 + length]
        if kind == b"IHDR":
            if length != 13 or header_seen:
                raise PngDecodeError("PNG header is invalid")
            width = int.from_bytes(payload[:4], "big")
            height = int.from_bytes(payload[4:8], "big")
            depth, colour, compression, filtering, interlace = payload[8:13]
            if depth != 8:
                raise PngDecodeError(f"only 8-bit PNGs are comparable, not depth {depth}")
            if colour not in _CHANNELS:
                raise PngDecodeError(
                    f"only truecolour PNGs are comparable, not colour type {colour}"
                )
            if compression != 0 or filtering != 0:
                raise PngDecodeError("PNG uses an unsupported compression or filter method")
            if interlace != 0:
                raise PngDecodeError("interlaced PNGs are refused")
            channels = _CHANNELS[colour]
            header_seen = True
        elif kind == b"IDAT":
            chunks.append(payload)
        elif kind == b"IEND":
            break
        offset = end
    if not header_seen or width <= 0 or height <= 0:
        raise PngDecodeError("PNG header is missing")
    if not chunks:
        raise PngDecodeError("PNG carries no image data")
    try:
        raw = zlib.decompress(b"".join(chunks))
    except zlib.error:
        raise PngDecodeError("PNG image data is not decompressible") from None
    return width, height, channels, _unfilter(raw, width, height, channels)
