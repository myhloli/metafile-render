# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF gdi.emf 内部实现。"""

from __future__ import annotations

from dataclasses import replace
from math import isfinite
from typing import Callable

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
from .record_types import EmfRecord
from .scalars import _parse_color, _parse_rect_i32, _parse_xform
from .shapes import _emit_polybezier, _path_from_device_points, _read_emf_points
from .text import _handle_emf_text


def _handle_emf_poly(record_type: int, record: BoundedReader, playback: _Playback) -> bool:
    """处理 EMF 32/16 位折线、多边形与贝塞尔记录。"""
    simple_types = {2, 3, 4, 5, 6, 85, 86, 87, 88, 89}
    poly_types = {7, 8, 90, 91}
    if record_type not in simple_types | poly_types:
        return False
    compact = record_type >= EmfRecord.POLYBEZIER16
    if record_type in simple_types:
        count = record.u32(24)
        playback.charge_points(count)
        points = _read_emf_points(record, count, 28, compact=compact)
        if record_type in {EmfRecord.POLYBEZIER, EmfRecord.POLYBEZIERTO, EmfRecord.POLYBEZIER16, EmfRecord.POLYBEZIERTO16}:
            _emit_polybezier(playback, points, to=record_type in {EmfRecord.POLYBEZIERTO, EmfRecord.POLYBEZIERTO16})
            return True
        mapped = [playback.map_point(point) for point in points]
        if record_type in {EmfRecord.POLYLINETO, EmfRecord.POLYLINETO16}:
            playback.polyline_to(points)
            return True
        playback.emit_path(
            _path_from_device_points(mapped, close=record_type in {EmfRecord.POLYGON, EmfRecord.POLYGON16}),
            stroke=True,
            fill=record_type in {EmfRecord.POLYGON, EmfRecord.POLYGON16},
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
    close = record_type in {EmfRecord.POLYPOLYGON, EmfRecord.POLYPOLYGON16}
    for count in counts:
        mapped = [playback.map_point(point) for point in points[cursor : cursor + count]]
        builder.extend(_path_from_device_points(mapped, close=close))
        cursor += count
    playback.emit_path(builder.build(), stroke=True, fill=close)
    return True


def _handle_emf_record(record_type: int, record: BoundedReader, playback: _Playback) -> None:
    """以显式映射分派已实现记录，并保留未知记录的降级语义。"""
    if record_type in {EmfRecord.HEADER, EmfRecord.EOF, EmfRecord.GDICOMMENT}:
        return
    if _handle_emf_poly(record_type, record, playback):
        return
    handler = _EMF_DISPATCH.get(record_type)
    if handler is not None:
        handler(record_type, record, playback)
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


def _handle_emf_state_record(record_type: int, record: BoundedReader, playback: _Playback) -> None:
    """集中处理 emf state 类记录，保持原有字段顺序与边界检查。"""
    if record_type == EmfRecord.SETWINDOWEXTEX:
        playback.state.window_extent = float(record.i32(8)), float(record.i32(12))
        return
    if record_type == EmfRecord.SETWINDOWORGEX:
        playback.state.window_origin = float(record.i32(8)), float(record.i32(12))
        return
    if record_type == EmfRecord.SETVIEWPORTEXTEX:
        playback.state.viewport_extent = float(record.i32(8)), float(record.i32(12))
        return
    if record_type == EmfRecord.SETVIEWPORTORGEX:
        playback.state.viewport_origin = float(record.i32(8)), float(record.i32(12))
        return
    if record_type == EmfRecord.SETBRUSHORGEX:
        playback.state.brush_origin = float(record.i32(8)), float(record.i32(12))
        return
    if record_type == EmfRecord.SETMAPMODE:
        playback.state.map_mode = record.i32(8)
        return
    if record_type == EmfRecord.SETBKMODE:
        playback.state.background_mode = record.u32(8)
        return
    if record_type == EmfRecord.SETPOLYFILLMODE:
        playback.state.polygon_fill_mode = record.u32(8)
        return
    if record_type == EmfRecord.SETROP2:
        rop2 = record.u32(8)
        if not 1 <= rop2 <= 16:
            playback.warn("unsupported_rop2", f"invalid ROP2 will use COPYPEN approximation: {rop2}")
            rop2 = 13
        playback.state.rop2 = rop2
        return
    if record_type == EmfRecord.SETSTRETCHBLTMODE:
        playback.state.stretch_mode = record.u32(8)
        return
    if record_type == EmfRecord.SETTEXTALIGN:
        playback.state.text_align = record.u32(8)
        return
    if record_type == EmfRecord.SETTEXTCOLOR:
        playback.state.text_color = _parse_color(record.u32(8))
        return
    if record_type == EmfRecord.SETBKCOLOR:
        playback.state.background_color = _parse_color(record.u32(8))
        return
    if record_type in {EmfRecord.SCALEVIEWPORTEXTEX, EmfRecord.SCALEWINDOWEXTEX}:
        x_num, x_den = record.i32(8), record.i32(12)
        y_num, y_den = record.i32(16), record.i32(20)
        if x_den == 0 or y_den == 0:
            playback.warn("zero_scale_denominator", "viewport/window scale record has a zero denominator")
            return
        if record_type == EmfRecord.SCALEVIEWPORTEXTEX:
            width, height = playback.state.viewport_extent
            playback.state.viewport_extent = width * x_num / x_den, height * y_num / y_den
        else:
            width, height = playback.state.window_extent
            playback.state.window_extent = width * x_num / x_den, height * y_num / y_den
        return
    if record_type == EmfRecord.SAVEDC:
        playback.save_dc()
        return
    if record_type == EmfRecord.RESTOREDC:
        playback.restore_dc(record.i32(8))
        return
    if record_type == EmfRecord.SETWORLDTRANSFORM:
        playback.state.world_transform = _parse_xform(record, 8)
        return
    if record_type == EmfRecord.MODIFYWORLDTRANSFORM:
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
    if record_type == EmfRecord.SETARCDIRECTION:
        playback.state.arc_direction = record.u32(8)
        return
    if record_type == EmfRecord.SETMITERLIMIT:
        value = record.f32(8)
        if isfinite(value) and value > 0:
            playback.state.miter_limit = value
        else:
            playback.warn("invalid_miter_limit", f"invalid miter limit: {value}")
        return


def _handle_emf_clip_record(record_type: int, record: BoundedReader, playback: _Playback) -> None:
    """集中处理 emf clip 类记录，保持原有字段顺序与边界检查。"""
    if record_type == EmfRecord.OFFSETCLIPRGN:
        offset = playback.map_vector((float(record.i32(8)), float(record.i32(12))))
        translation = Matrix(e=offset[0], f=offset[1])
        playback.state.clip = tuple(
            replace(operation, path=transform_path(operation.path, translation)) for operation in playback.state.clip
        )
        return
    if record_type == EmfRecord.SETMETARGN:
        playback.reset_clip()
        return
    if record_type in {EmfRecord.EXCLUDECLIPRECT, EmfRecord.INTERSECTCLIPRECT}:
        logical = _parse_rect_i32(record, 8)
        mapped = transform_path(rectangle_path(logical), playback.logical_matrix())
        playback.add_clip(mapped, _RGN_DIFF if record_type == EmfRecord.EXCLUDECLIPRECT else _RGN_AND)
        return
    if record_type == EmfRecord.SELECTCLIPPATH:
        path = playback.consume_path()
        if path.segments:
            playback.add_clip(path, record.u32(8))
        return
    if record_type == EmfRecord.EXTSELECTCLIPRGN:
        _handle_emf_region_clip(record, playback)
        return


def _handle_emf_drawing_record(record_type: int, record: BoundedReader, playback: _Playback) -> None:
    """集中处理 emf drawing 类记录，保持原有字段顺序与边界检查。"""
    if record_type == EmfRecord.MOVETOEX:
        point = float(record.i32(8)), float(record.i32(12))
        playback.move_to(point)
        return
    if record_type == EmfRecord.SETPIXELV:
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
    if record_type == EmfRecord.LINETO:
        endpoint = float(record.i32(8)), float(record.i32(12))
        playback.line_to(endpoint)
        return
    if record_type in {EmfRecord.ELLIPSE, EmfRecord.RECTANGLE, EmfRecord.ROUNDRECT}:
        rect = _parse_rect_i32(record, 8)
        if record_type == EmfRecord.ELLIPSE:
            path = ellipse_path(rect)
        elif record_type == EmfRecord.ROUNDRECT:
            path = round_rectangle_path(rect, abs(record.i32(24)) / 2.0, abs(record.i32(28)) / 2.0)
        else:
            path = rectangle_path(rect)
        playback.emit_logical_path(path, stroke=True, fill=True)
        return
    if record_type in {EmfRecord.ARC, EmfRecord.CHORD, EmfRecord.PIE, EmfRecord.ARCTO}:
        rect = _parse_rect_i32(record, 8)
        start = float(record.i32(24)), float(record.i32(28))
        end = float(record.i32(32)), float(record.i32(36))
        close_mode = "chord" if record_type == EmfRecord.CHORD else "pie" if record_type == EmfRecord.PIE else "open"
        path = arc_path(rect, start, end, direction=playback.state.arc_direction, close_mode=close_mode)
        if record_type == EmfRecord.ARCTO:
            playback.connected_arc(path)
        else:
            playback.emit_logical_path(path, stroke=True, fill=close_mode != "open")
        return
    if record_type == EmfRecord.ANGLEARC:
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


def _handle_emf_objects_record(record_type: int, record: BoundedReader, playback: _Playback) -> None:
    """集中处理 emf objects 类记录，保持原有字段顺序与边界检查。"""
    if record_type == EmfRecord.SELECTOBJECT:
        playback.select_emf_handle(record.u32(8))
        return
    if record_type == EmfRecord.CREATEPEN:
        handle = record.u32(8)
        _validate_emf_handle(handle)
        playback.emf_objects[handle] = _make_pen(record.u32(12), float(record.i32(16)), record.u32(24))
        return
    if record_type == EmfRecord.CREATEBRUSHINDIRECT:
        handle = record.u32(8)
        _validate_emf_handle(handle)
        playback.emf_objects[handle] = _make_brush(record.u32(12), record.u32(16), record.u32(20))
        return
    if record_type == EmfRecord.DELETEOBJECT:
        playback.emf_objects.pop(record.u32(8), None)
        return
    if record_type == EmfRecord.EXTCREATEFONTINDIRECTW:
        handle = record.u32(8)
        _validate_emf_handle(handle)
        playback.emf_objects[handle] = _parse_emf_font(record)
        return
    if record_type == EmfRecord.CREATEDIBPATTERNBRUSHPT:
        handle = record.u32(8)
        _validate_emf_handle(handle)
        cb_bmi, cb_bits = record.u32(20), record.u32(28)
        pattern = b""
        if cb_bmi + cb_bits <= MAX_EMBEDDED_BITMAP_BYTES:
            pattern = record.bytes(record.u32(16), cb_bmi) + record.bytes(record.u32(24), cb_bits)
        playback.emf_objects[handle] = Brush(kind="pattern", pattern=pattern)
        playback.warn("pattern_brush_approximation", "DIB pattern brush will use a solid-color approximation")
        return
    if record_type == EmfRecord.EXTCREATEPEN:
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
    if record_type == EmfRecord.RESERVED_120:
        playback.state.brush = replace(playback.state.brush, color=_parse_color(record.u32(8)))
        return
    if record_type == EmfRecord.COLORMATCHTOTARGETW:
        playback.state.pen = replace(playback.state.pen, color=_parse_color(record.u32(8)))
        return


def _handle_emf_path_record(record_type: int, record: BoundedReader, playback: _Playback) -> None:
    """集中处理 emf path 类记录，保持原有字段顺序与边界检查。"""
    if record_type == EmfRecord.BEGINPATH:
        playback.begin_path()
        return
    if record_type == EmfRecord.ENDPATH:
        playback.end_path()
        return
    if record_type == EmfRecord.CLOSEFIGURE:
        if playback.path_active and playback.path_builder.figure_open:
            playback.path_builder.close()
        else:
            playback.warn("close_figure_without_path", "CloseFigure ignored without an active path")
        return
    if record_type in {EmfRecord.FILLPATH, EmfRecord.STROKEANDFILLPATH, EmfRecord.STROKEPATH}:
        path = playback.consume_path()
        if record_type == EmfRecord.STROKEANDFILLPATH:
            path = close_open_subpaths(path)
        playback.emit_path(
            path,
            stroke=record_type in {EmfRecord.STROKEANDFILLPATH, EmfRecord.STROKEPATH},
            fill=record_type in {EmfRecord.FILLPATH, EmfRecord.STROKEANDFILLPATH},
        )
        return
    if record_type == EmfRecord.ABORTPATH:
        playback.path_builder.clear()
        playback.path_active = False
        playback.path_ready = False
        return


def _handle_emf_bitmap_record(record_type: int, record: BoundedReader, playback: _Playback) -> None:
    """集中处理 emf bitmap 类记录，保持原有字段顺序与边界检查。"""
    if record_type in {
        EmfRecord.BITBLT,
        EmfRecord.STRETCHBLT,
        EmfRecord.SETDIBITSTODEVICE,
        EmfRecord.STRETCHDIBITS,
        EmfRecord.ALPHABLEND,
    }:
        _handle_emf_bitmap(record_type, record, playback)
        return


def _handle_emf_text_record(record_type: int, record: BoundedReader, playback: _Playback) -> None:
    """集中处理 emf text 类记录，保持原有字段顺序与边界检查。"""
    if record_type in {EmfRecord.EXTTEXTOUTA, EmfRecord.EXTTEXTOUTW}:
        _handle_emf_text(record_type, record, playback)
        return


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


_EMF_DISPATCH: dict[int, Callable[[int, BoundedReader, _Playback], None]] = {
    EmfRecord.SETWINDOWEXTEX: _handle_emf_state_record,
    EmfRecord.SETWINDOWORGEX: _handle_emf_state_record,
    EmfRecord.SETVIEWPORTEXTEX: _handle_emf_state_record,
    EmfRecord.SETVIEWPORTORGEX: _handle_emf_state_record,
    EmfRecord.SETBRUSHORGEX: _handle_emf_state_record,
    EmfRecord.SETPIXELV: _handle_emf_drawing_record,
    EmfRecord.SETMAPMODE: _handle_emf_state_record,
    EmfRecord.SETBKMODE: _handle_emf_state_record,
    EmfRecord.SETPOLYFILLMODE: _handle_emf_state_record,
    EmfRecord.SETROP2: _handle_emf_state_record,
    EmfRecord.SETSTRETCHBLTMODE: _handle_emf_state_record,
    EmfRecord.SETTEXTALIGN: _handle_emf_state_record,
    EmfRecord.SETTEXTCOLOR: _handle_emf_state_record,
    EmfRecord.SETBKCOLOR: _handle_emf_state_record,
    EmfRecord.OFFSETCLIPRGN: _handle_emf_clip_record,
    EmfRecord.MOVETOEX: _handle_emf_drawing_record,
    EmfRecord.SETMETARGN: _handle_emf_clip_record,
    EmfRecord.EXCLUDECLIPRECT: _handle_emf_clip_record,
    EmfRecord.INTERSECTCLIPRECT: _handle_emf_clip_record,
    EmfRecord.SCALEVIEWPORTEXTEX: _handle_emf_state_record,
    EmfRecord.SCALEWINDOWEXTEX: _handle_emf_state_record,
    EmfRecord.SAVEDC: _handle_emf_state_record,
    EmfRecord.RESTOREDC: _handle_emf_state_record,
    EmfRecord.SETWORLDTRANSFORM: _handle_emf_state_record,
    EmfRecord.MODIFYWORLDTRANSFORM: _handle_emf_state_record,
    EmfRecord.SELECTOBJECT: _handle_emf_objects_record,
    EmfRecord.CREATEPEN: _handle_emf_objects_record,
    EmfRecord.CREATEBRUSHINDIRECT: _handle_emf_objects_record,
    EmfRecord.DELETEOBJECT: _handle_emf_objects_record,
    EmfRecord.ANGLEARC: _handle_emf_drawing_record,
    EmfRecord.ELLIPSE: _handle_emf_drawing_record,
    EmfRecord.RECTANGLE: _handle_emf_drawing_record,
    EmfRecord.ROUNDRECT: _handle_emf_drawing_record,
    EmfRecord.ARC: _handle_emf_drawing_record,
    EmfRecord.CHORD: _handle_emf_drawing_record,
    EmfRecord.PIE: _handle_emf_drawing_record,
    EmfRecord.LINETO: _handle_emf_drawing_record,
    EmfRecord.ARCTO: _handle_emf_drawing_record,
    EmfRecord.SETARCDIRECTION: _handle_emf_state_record,
    EmfRecord.SETMITERLIMIT: _handle_emf_state_record,
    EmfRecord.BEGINPATH: _handle_emf_path_record,
    EmfRecord.ENDPATH: _handle_emf_path_record,
    EmfRecord.CLOSEFIGURE: _handle_emf_path_record,
    EmfRecord.FILLPATH: _handle_emf_path_record,
    EmfRecord.STROKEANDFILLPATH: _handle_emf_path_record,
    EmfRecord.STROKEPATH: _handle_emf_path_record,
    EmfRecord.SELECTCLIPPATH: _handle_emf_clip_record,
    EmfRecord.ABORTPATH: _handle_emf_path_record,
    EmfRecord.EXTSELECTCLIPRGN: _handle_emf_clip_record,
    EmfRecord.BITBLT: _handle_emf_bitmap_record,
    EmfRecord.STRETCHBLT: _handle_emf_bitmap_record,
    EmfRecord.SETDIBITSTODEVICE: _handle_emf_bitmap_record,
    EmfRecord.STRETCHDIBITS: _handle_emf_bitmap_record,
    EmfRecord.EXTCREATEFONTINDIRECTW: _handle_emf_objects_record,
    EmfRecord.EXTTEXTOUTA: _handle_emf_text_record,
    EmfRecord.EXTTEXTOUTW: _handle_emf_text_record,
    EmfRecord.CREATEDIBPATTERNBRUSHPT: _handle_emf_objects_record,
    EmfRecord.EXTCREATEPEN: _handle_emf_objects_record,
    EmfRecord.ALPHABLEND: _handle_emf_bitmap_record,
    EmfRecord.RESERVED_120: _handle_emf_objects_record,
    EmfRecord.COLORMATCHTOTARGETW: _handle_emf_objects_record,
}

__all__ = ["_handle_emf_poly", "_handle_emf_record", "_validate_emf_handle", "_handle_emf_region_clip"]
