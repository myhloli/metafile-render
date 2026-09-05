# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""原生和回放后端共用的 DIB 声明与未压缩像素边界校验。"""

from __future__ import annotations

from .binary import BoundedReader
from .commands import DibPayload
from .limits import MAX_CANVAS_PIXELS, MAX_EMBEDDED_BITMAP_BYTES
from .models import MetafileMalformedError, MetafileResourceLimitError


def dib_dimensions(header: bytes) -> tuple[int, int]:
    """读取 DIB 尺寸，在任何像素分配之前限制总像素数。"""
    reader = BoundedReader(header)
    size = reader.u32(0)
    if size == 12:
        width, height = reader.u16(4), reader.u16(6)
        reader.subreader(0, 12)
    elif size >= 40:
        reader.subreader(0, size)
        width, height = reader.i32(4), abs(reader.i32(8))
    else:
        raise MetafileMalformedError(f"unsupported or truncated DIB header size: {size}")
    if width <= 0 or height <= 0 or width * height > MAX_CANVAS_PIXELS:
        raise MetafileResourceLimitError(f"DIB dimensions exceed pixel budget: {width}x{height}")
    return width, height


def validate_dib_payload(payload: DibPayload) -> int:
    """验证原始 DIB 行步长与字节范围，并返回保守的解码占用。"""
    size = len(payload.header) + len(payload.bits)
    if size > MAX_EMBEDDED_BITMAP_BYTES:
        raise MetafileResourceLimitError("embedded bitmap exceeds byte budget")
    width, height = dib_dimensions(payload.header)
    reader = BoundedReader(payload.header)
    core = reader.u32(0) == 12
    bpp = reader.u16(10 if core else 14)
    compression = 0 if core else reader.u32(16)
    if compression in {0, 3, 6}:
        if bpp not in {1, 4, 8, 16, 24, 32}:
            raise MetafileMalformedError(f"invalid uncompressed DIB bit depth: {bpp}")
        required = ((width * bpp + 31) // 32) * 4 * height
        if required > len(payload.bits):
            raise MetafileMalformedError("uncompressed DIB pixel data is truncated")
    return width * height * 4 + size


__all__ = ["dib_dimensions", "validate_dib_payload"]
