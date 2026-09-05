# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF gdi.shapes 内部实现。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..binary import BoundedReader
from ..geometry import PathBuilder
from ..limits import MAX_POINTS_PER_RECORD
from ..models import MetafileMalformedError
from ..primitives import GraphicsPath, Point

if TYPE_CHECKING:
    from .playback import _Playback


def _path_from_device_points(points: list[Point], *, close: bool) -> GraphicsPath:
    """从 device 坐标点列构造单个折线路径。"""
    if not points:
        return GraphicsPath(())
    builder = PathBuilder()
    builder.move_to(points[0])
    for point in points[1:]:
        builder.line_to(point)
    if close:
        builder.close()
    return builder.build()


def _read_emf_points(record: BoundedReader, count: int, offset: int, *, compact: bool) -> list[Point]:
    """读取 EMF PointL/PointS 数组并保留逻辑坐标。"""
    stride = 4 if compact else 8
    if count < 0 or count > MAX_POINTS_PER_RECORD or count > record.remaining(offset) // stride:
        raise MetafileMalformedError(f"EMF point array exceeds record boundary: count={count}")
    if compact:
        return [(float(record.i16(offset + index * 4)), float(record.i16(offset + index * 4 + 2))) for index in range(count)]
    return [(float(record.i32(offset + index * 8)), float(record.i32(offset + index * 8 + 4))) for index in range(count)]


def _read_wmf_points(payload: BoundedReader, count: int, offset: int) -> list[Point]:
    """读取 WMF PointS 数组并保留逻辑坐标。"""
    if count < 0 or count > MAX_POINTS_PER_RECORD or count > payload.remaining(offset) // 4:
        raise MetafileMalformedError(f"WMF point array exceeds record boundary: count={count}")
    return [(float(payload.i16(offset + index * 4)), float(payload.i16(offset + index * 4 + 2))) for index in range(count)]


def _emit_polybezier(playback: _Playback, points: list[Point], *, to: bool) -> None:
    """把 PolyBezier/PolyBezierTo 点列追加为三次路径。"""
    if not points:
        return
    if to:
        playback.polybezier_to(points)
        return
    builder = PathBuilder()
    builder.move_to(playback.map_point(points[0]))
    remaining = points[1:]
    if len(remaining) % 3:
        playback.warn("invalid_bezier_points", "PolyBezier point count is not a multiple of three")
        remaining = remaining[: len(remaining) - len(remaining) % 3]
    for index in range(0, len(remaining), 3):
        builder.cubic_to(
            playback.map_point(remaining[index]),
            playback.map_point(remaining[index + 1]),
            playback.map_point(remaining[index + 2]),
        )
    playback.emit_path(builder.build(), stroke=True, fill=False)


__all__ = ["_path_from_device_points", "_read_emf_points", "_read_wmf_points", "_emit_polybezier"]
