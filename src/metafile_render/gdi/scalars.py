# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF gdi.scalars 内部实现。"""

from __future__ import annotations

from math import isfinite

from ..binary import BoundedReader
from ..geometry import colorref_to_rgb
from ..models import MetafileMalformedError
from ..primitives import Color, Matrix, Rect


def _parse_rect_i32(reader: BoundedReader, offset: int) -> Rect:
    """读取四个有符号 32 位坐标组成的 RectL。"""
    return Rect(reader.i32(offset), reader.i32(offset + 4), reader.i32(offset + 8), reader.i32(offset + 12))


def _parse_rect_i16(reader: BoundedReader, offset: int) -> Rect:
    """读取四个有符号 16 位坐标组成的 WMF 矩形。"""
    return Rect(reader.i16(offset), reader.i16(offset + 2), reader.i16(offset + 4), reader.i16(offset + 6))


def _parse_xform(reader: BoundedReader, offset: int) -> Matrix:
    """读取 EMF XForm 六个浮点参数并拒绝非有限值。"""
    values = tuple(reader.f32(offset + index * 4) for index in range(6))
    if not all(isfinite(value) and abs(value) <= 1e12 for value in values):
        raise MetafileMalformedError("EMF XForm contains non-finite or unreasonably large values")
    return Matrix(a=values[0], b=values[1], c=values[2], d=values[3], e=values[4], f=values[5])


def _parse_color(value: int, *, alpha: int = 255) -> Color:
    """把 GDI COLORREF 转换为带指定透明度的内部颜色。"""
    red, green, blue = colorref_to_rgb(value)
    return Color(red, green, blue, alpha)


__all__ = ["_parse_rect_i32", "_parse_rect_i16", "_parse_xform", "_parse_color"]
