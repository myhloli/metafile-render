# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF gdi.emf 内部实现。"""

from __future__ import annotations

from dataclasses import replace
from math import isfinite

from ..binary import BoundedReader
from ..commands import DrawPathCommand
from ..geometry import (
    PathBuilder,
    arc_path,
    close_open_subpaths,
    ellipse_path,
    rectangle_path,
    round_rectangle_path,
    transform_path,
)
from ..limits import MAX_EMBEDDED_BITMAP_BYTES, MAX_OBJECTS, MAX_POINTS_PER_RECORD
from ..models import MetafileMalformedError, MetafileResourceLimitError
from ..primitives import Brush, Matrix, Pen, Rect
from .bitmap import _handle_emf_bitmap
from .constants import _RGN_AND, _RGN_COPY, _RGN_DIFF
from .objects import _make_brush, _make_pen, _parse_emf_font
from .playback import _Playback
from .scalars import _parse_color, _parse_rect_i32, _parse_xform
from .shapes import _emit_polybezier, _path_from_device_points, _read_emf_points
from .text import _handle_emf_text


def _handle_emf_poly(record_type: int, record: BoundedReader, playback: _Playback) -> bool:
    """处理 EMF 32/16 位折线、多边形与贝塞尔记录。"""
    simple_types = {2, 3, 4, 5, 6, 85, 86, 87, 88, 89}
    poly_types = {7, 8, 90, 91}
    if record_type not in simple_types | poly_types:
        return False
    compact = record_type >= 85
    if record_type in simple_types:
        count = record.u32(24)
        playback.charge_points(count)
        points = _read_emf_points(record, count, 28, compact=compact)
        if record_type in {2, 5, 85, 88}:
            _emit_polybezier(playback, points, to=record_type in {5, 88})
            return True
        mapped = [playback.map_point(point) for point in points]
        if record_type in {6, 89}:
            playback.polyline_to(points)
            return True
        playback.emit_path(
            _path_from_device_points(mapped, close=record_type in {3, 86}),
            stroke=True,
            fill=record_type in {3, 86},
        )
        return True

    polygon_count = record.u32(24)
    total_count = record.u32(28)
    if polygon_count > MAX_POINTS_PER_RECORD or polygon_count > record.remaining(32) // 4:
        raise MetafileMalformedError(f"EMF polygon count array exceeds record boundary: count={polygon_count}")
    counts = [record.u32(32 + index * 4) for index in range(polygon_count)]
    if sum(counts) != total_count:
        raise MetafileMalformedError("EMF poly-polygon counts do not equal total point count")
    playback.charge_points(total_count)
    points_offset = 32 + polygon_count * 4
    points = _read_emf_points(record, total_count, points_offset, compact=compact)
    builder = PathBuilder()
    cursor = 0
    close = record_type in {8, 91}
    for count in counts:
        mapped = [playback.map_point(point) for point in points[cursor : cursor + count]]
        builder.extend(_path_from_device_points(mapped, close=close))
        cursor += count
    playback.emit_path(builder.build(), stroke=True, fill=close)
    return True


def _handle_emf_record(record_type: int, record: BoundedReader, playback: _Playback) -> None:
    """分派并执行单条 EMF record，未知绘图记录按规范跳过。"""
    if record_type in {1, 14, 70}:
        return
    if _handle_emf_poly(record_type, record, playback):
        return

    if record_type == 9:
        playback.state.window_extent = float(record.i32(8)), float(record.i32(12))
        return
    if record_type == 10:
        playback.state.window_origin = float(record.i32(8)), float(record.i32(12))
        return
    if record_type == 11:
        playback.state.viewport_extent = float(record.i32(8)), float(record.i32(12))
        return
    if record_type == 12:
        playback.state.viewport_origin = float(record.i32(8)), float(record.i32(12))
        return
    if record_type == 13:
        playback.state.brush_origin = float(record.i32(8)), float(record.i32(12))
        return
    if record_type == 17:
        playback.state.map_mode = record.i32(8)
        return
    if record_type == 18:
        playback.state.background_mode = record.u32(8)
        return
    if record_type == 19:
        playback.state.polygon_fill_mode = record.u32(8)
        return
    if record_type == 20:
        rop2 = record.u32(8)
        if not 1 <= rop2 <= 16:
            playback.warn("unsupported_rop2", f"invalid ROP2 will use COPYPEN approximation: {rop2}")
            rop2 = 13
        playback.state.rop2 = rop2
        return
    if record_type == 21:
        playback.state.stretch_mode = record.u32(8)
        return
    if record_type == 22:
        playback.state.text_align = record.u32(8)
        return
    if record_type == 24:
        playback.state.text_color = _parse_color(record.u32(8))
        return
    if record_type == 25:
        playback.state.background_color = _parse_color(record.u32(8))
        return
    if record_type == 26:
        offset = playback.map_vector((float(record.i32(8)), float(record.i32(12))))
        translation = Matrix(e=offset[0], f=offset[1])
        playback.state.clip = tuple(
            replace(operation, path=transform_path(operation.path, translation)) for operation in playback.state.clip
        )
        return
    if record_type == 27:
        point = float(record.i32(8)), float(record.i32(12))
        playback.move_to(point)
        return
    if record_type == 28:
        playback.reset_clip()
        return
    if record_type in {29, 30}:
        logical = _parse_rect_i32(record, 8)
        mapped = transform_path(rectangle_path(logical), playback.logical_matrix())
        playback.add_clip(mapped, _RGN_DIFF if record_type == 29 else _RGN_AND)
        return
    if record_type in {31, 32}:
        x_num, x_den = record.i32(8), record.i32(12)
        y_num, y_den = record.i32(16), record.i32(20)
        if x_den == 0 or y_den == 0:
            playback.warn("zero_scale_denominator", "viewport/window scale record has a zero denominator")
            return
        if record_type == 31:
            width, height = playback.state.viewport_extent
            playback.state.viewport_extent = width * x_num / x_den, height * y_num / y_den
        else:
            width, height = playback.state.window_extent
            playback.state.window_extent = width * x_num / x_den, height * y_num / y_den
        return
    if record_type == 33:
        playback.save_dc()
        return
    if record_type == 34:
        playback.restore_dc(record.i32(8))
        return
    if record_type == 35:
        playback.state.world_transform = _parse_xform(record, 8)
        return
    if record_type == 36:
        transform = _parse_xform(record, 8)
        mode = record.u32(32)
        if mode == 1:
            playback.state.world_transform = Matrix()
        elif mode == 2:
            playback.state.world_transform = playback.state.world_transform.then(transform)
        elif mode == 3:
            playback.state.world_transform = transform.then(playback.state.world_transform)
        elif mode == 4:
            playback.state.world_transform = transform
        else:
            playback.warn("unsupported_transform_mode", f"unsupported ModifyWorldTransform mode: {mode}")
        return
    if record_type == 37:
        playback.select_emf_handle(record.u32(8))
        return
    if record_type == 38:
        handle = record.u32(8)
        _validate_emf_handle(handle)
        playback.emf_objects[handle] = _make_pen(record.u32(12), float(record.i32(16)), record.u32(24))
        return
    if record_type == 39:
        handle = record.u32(8)
        _validate_emf_handle(handle)
        playback.emf_objects[handle] = _make_brush(record.u32(12), record.u32(16), record.u32(20))
        return
    if record_type == 40:
        playback.emf_objects.pop(record.u32(8), None)
        return
    if record_type == 57:
        playback.state.arc_direction = record.u32(8)
        return
    if record_type == 58:
        value = record.f32(8)
        if isfinite(value) and value > 0:
            playback.state.miter_limit = value
        else:
            playback.warn("invalid_miter_limit", f"invalid miter limit: {value}")
        return

    if record_type == 15:
        point = playback.map_point((float(record.i32(8)), float(record.i32(12))))
        color = _parse_color(record.u32(16))
        playback.append_command(
            DrawPathCommand(
                path=rectangle_path(Rect(point[0], point[1], point[0] + 1.0, point[1] + 1.0)),
                pen=Pen(null=True),
                brush=Brush(color=color),
                stroke=False,
                fill=True,
                fill_rule="evenodd",
                clip=playback.state.clip,
                rop2=13,
            )
        )
        return
    if record_type == 54:
        endpoint = float(record.i32(8)), float(record.i32(12))
        playback.line_to(endpoint)
        return
    if record_type in {42, 43, 44}:
        rect = _parse_rect_i32(record, 8)
        if record_type == 42:
            path = ellipse_path(rect)
        elif record_type == 44:
            path = round_rectangle_path(rect, abs(record.i32(24)) / 2.0, abs(record.i32(28)) / 2.0)
        else:
            path = rectangle_path(rect)
        playback.emit_logical_path(path, stroke=True, fill=True)
        return
    if record_type in {45, 46, 47, 55}:
        rect = _parse_rect_i32(record, 8)
        start = float(record.i32(24)), float(record.i32(28))
        end = float(record.i32(32)), float(record.i32(36))
        close_mode = "chord" if record_type == 46 else "pie" if record_type == 47 else "open"
        path = arc_path(rect, start, end, direction=playback.state.arc_direction, close_mode=close_mode)
        if record_type == 55:
            playback.connected_arc(path)
        else:
            playback.emit_logical_path(path, stroke=True, fill=close_mode != "open")
        return
    if record_type == 41:
        center = float(record.i32(8)), float(record.i32(12))
        radius = abs(float(record.u32(16)))
        start_angle = record.f32(20)
        sweep_angle = record.f32(24)
        from math import cos, radians, sin

        start = (
            center[0] + radius * cos(radians(start_angle)),
            center[1] - radius * sin(radians(start_angle)),
        )
        end_angle = start_angle + sweep_angle
        end = (
            center[0] + radius * cos(radians(end_angle)),
            center[1] - radius * sin(radians(end_angle)),
        )
        direction = 1 if sweep_angle >= 0 else 2
        playback.connected_arc(
            arc_path(
                Rect(center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius),
                start,
                end,
                direction=direction,
            ),
        )
        return

    if record_type == 59:
        playback.begin_path()
        return
    if record_type == 60:
        playback.end_path()
        return
    if record_type == 61:
        if playback.path_active and playback.path_builder.figure_open:
            playback.path_builder.close()
        else:
            playback.warn("close_figure_without_path", "CloseFigure ignored without an active path")
        return
    if record_type in {62, 63, 64}:
        path = playback.consume_path()
        if record_type == 63:
            path = close_open_subpaths(path)
        playback.emit_path(path, stroke=record_type in {63, 64}, fill=record_type in {62, 63})
        return
    if record_type == 67:
        path = playback.consume_path()
        if path.segments:
            playback.add_clip(path, record.u32(8))
        return
    if record_type == 68:
        playback.path_builder.clear()
        playback.path_active = False
        playback.path_ready = False
        return

    if record_type == 75:
        _handle_emf_region_clip(record, playback)
        return
    if record_type in {76, 77, 80, 81, 114}:
        _handle_emf_bitmap(record_type, record, playback)
        return
    if record_type == 82:
        handle = record.u32(8)
        _validate_emf_handle(handle)
        playback.emf_objects[handle] = _parse_emf_font(record)
        return
    if record_type in {83, 84}:
        _handle_emf_text(record_type, record, playback)
        return
    if record_type == 94:
        handle = record.u32(8)
        _validate_emf_handle(handle)
        cb_bmi, cb_bits = record.u32(20), record.u32(28)
        pattern = b""
        if cb_bmi + cb_bits <= MAX_EMBEDDED_BITMAP_BYTES:
            pattern = record.bytes(record.u32(16), cb_bmi) + record.bytes(record.u32(24), cb_bits)
        playback.emf_objects[handle] = Brush(kind="pattern", pattern=pattern)
        playback.warn("pattern_brush_approximation", "DIB pattern brush will use a solid-color approximation")
        return
    if record_type == 95:
        handle = record.u32(8)
        _validate_emf_handle(handle)
        style = record.u32(28)
        width = float(record.u32(32))
        pen = _make_pen(style, width, record.u32(40), extended=True)
        style_count = record.u32(48)
        if style_count and style_count <= record.remaining(52) // 4:
            dashes = tuple(max(1.0, float(record.u32(52 + index * 4))) for index in range(style_count))
            pen = replace(pen, dashes=dashes)
        playback.emf_objects[handle] = pen
        return
    if record_type == 120:
        playback.state.brush = replace(playback.state.brush, color=_parse_color(record.u32(8)))
        return
    if record_type == 121:
        playback.state.pen = replace(playback.state.pen, color=_parse_color(record.u32(8)))
        return

    harmless_state_records = {
        16,
        23,
        48,
        49,
        50,
        51,
        52,
        98,
        99,
        100,
        101,
        104,
        107,
        109,
        110,
        111,
        112,
        113,
        115,
        117,
        119,
    }
    if record_type in harmless_state_records:
        playback.warn("ignored_state_record", f"EMF state/control record was ignored: {record_type}", partial=False)
        return
    playback.warn("unsupported_emf_record", f"unsupported EMF record was skipped: {record_type}")


def _validate_emf_handle(handle: int) -> None:
    """拒绝保留 handle、stock handle 和超出固定对象预算的值。"""
    if handle == 0 or handle & 0x80000000 or handle > MAX_OBJECTS:
        raise MetafileResourceLimitError(f"invalid or excessive EMF object handle: {handle}")


def _handle_emf_region_clip(record: BoundedReader, playback: _Playback) -> None:
    """解析 EMR_EXTSELECTCLIPRGN 的矩形 region 数据。"""
    data_size = record.u32(8)
    mode = record.u32(12)
    if data_size == 0:
        if mode == _RGN_COPY:
            playback.reset_clip()
        else:
            playback.warn("empty_clip_region", f"empty clip region used with combine mode {mode}")
        return
    if data_size > record.remaining(16) or data_size < 32:
        raise MetafileMalformedError(f"EMF region data exceeds record boundary: size={data_size}")
    region = record.subreader(16, data_size)
    header_size = region.u32(0)
    region_type = region.u32(4)
    rectangle_count = region.u32(8)
    region_bytes = region.u32(12)
    if header_size < 32 or region_type != 1:
        raise MetafileMalformedError("EMF region header is invalid")
    if rectangle_count > MAX_POINTS_PER_RECORD or region_bytes > region.remaining(header_size):
        raise MetafileMalformedError("EMF region rectangle data exceeds its declared boundary")
    if rectangle_count > region.remaining(header_size) // 16:
        raise MetafileMalformedError("EMF region rectangle count exceeds record boundary")
    playback.charge_points(rectangle_count * 4)
    builder = PathBuilder()
    for index in range(rectangle_count):
        rect = _parse_rect_i32(region, header_size + index * 16)
        builder.extend(transform_path(rectangle_path(rect), playback.logical_matrix()))
    path = builder.build()
    if path.segments:
        playback.add_clip(path, mode)


__all__ = ["_handle_emf_poly", "_handle_emf_record", "_validate_emf_handle", "_handle_emf_region_clip"]
