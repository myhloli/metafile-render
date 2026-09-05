# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF gdi.wmf 内部实现。"""

from __future__ import annotations

from typing import Callable

from ..binary import BoundedReader
from ..commands import DrawPathCommand
from ..geometry import PathBuilder, arc_path, ellipse_path, rectangle_path, round_rectangle_path, transform_path
from ..limits import MAX_POINTS_PER_RECORD
from ..models import MetafileMalformedError
from ..primitives import Brush, Pen, Rect
from .bitmap import _handle_wmf_stretchdib
from .constants import _RGN_AND, _RGN_DIFF
from .objects import _make_brush, _make_pen, _parse_wmf_font
from .playback import _Playback
from .record_types import WmfRecord
from .scalars import _parse_color
from .shapes import _path_from_device_points, _read_wmf_points
from .text import _handle_wmf_text


def _handle_wmf_record(function: int, payload: BoundedReader, playback: _Playback) -> None:
    """以显式映射分派已实现记录，并保留未知记录的降级语义。"""
    if function == WmfRecord.EOF:
        return
    handler = _WMF_DISPATCH.get(function)
    if handler is not None:
        handler(function, payload, playback)
        return
    harmless_records = {
        0x0108,
        0x020A,
        0x0231,
        0x0626,
        0x0234,
        0x0035,
        0x0436,
        0x0037,
        0x0139,
    }
    if function in harmless_records:
        playback.warn("ignored_wmf_state_record", f"WMF state/control record was ignored: {function:#x}", partial=False)
        return
    playback.warn("unsupported_wmf_record", f"unsupported WMF record was skipped: {function:#x}")


def _handle_wmf_state_record(function: int, payload: BoundedReader, playback: _Playback) -> None:
    """集中处理 wmf state 类记录，保持原有字段顺序与边界检查。"""
    if function == WmfRecord.SETBKCOLOR:
        playback.state.background_color = _parse_color(payload.u32(0))
        return
    if function == WmfRecord.SETBKMODE:
        playback.state.background_mode = payload.u16(0)
        return
    if function == WmfRecord.SETMAPMODE:
        playback.state.map_mode = payload.i16(0)
        return
    if function == WmfRecord.SETROP2:
        rop2 = payload.u16(0)
        if not 1 <= rop2 <= 16:
            playback.warn("unsupported_rop2", f"invalid WMF ROP2 will use COPYPEN approximation: {rop2}")
            rop2 = 13
        playback.state.rop2 = rop2
        return
    if function == WmfRecord.SETPOLYFILLMODE:
        playback.state.polygon_fill_mode = payload.u16(0)
        return
    if function == WmfRecord.SETSTRETCHBLTMODE:
        playback.state.stretch_mode = payload.u16(0)
        return
    if function == WmfRecord.SETTEXTCOLOR:
        playback.state.text_color = _parse_color(payload.u32(0))
        return
    if function == WmfRecord.SETWINDOWORG:
        playback.state.window_origin = float(payload.i16(2)), float(payload.i16(0))
        return
    if function == WmfRecord.SETWINDOWEXT:
        playback.state.window_extent = float(payload.i16(2)), float(payload.i16(0))
        return
    if function == WmfRecord.SETVIEWPORTORG:
        playback.state.viewport_origin = float(payload.i16(2)), float(payload.i16(0))
        return
    if function == WmfRecord.SETVIEWPORTEXT:
        playback.state.viewport_extent = float(payload.i16(2)), float(payload.i16(0))
        return
    if function == WmfRecord.OFFSETWINDOWORG:
        x, y = playback.state.window_origin
        playback.state.window_origin = x + payload.i16(2), y + payload.i16(0)
        return
    if function == WmfRecord.OFFSETVIEWPORTORG:
        x, y = playback.state.viewport_origin
        playback.state.viewport_origin = x + payload.i16(2), y + payload.i16(0)
        return
    if function in {WmfRecord.SCALEWINDOWEXT, WmfRecord.SCALEVIEWPORTEXT}:
        y_num, y_den, x_num, x_den = payload.i16(0), payload.i16(2), payload.i16(4), payload.i16(6)
        if x_den == 0 or y_den == 0:
            playback.warn("zero_scale_denominator", "WMF viewport/window scale record has a zero denominator")
            return
        if function == WmfRecord.SCALEWINDOWEXT:
            width, height = playback.state.window_extent
            playback.state.window_extent = width * x_num / x_den, height * y_num / y_den
        else:
            width, height = playback.state.viewport_extent
            playback.state.viewport_extent = width * x_num / x_den, height * y_num / y_den
        return
    if function == WmfRecord.SAVEDC:
        playback.save_dc()
        return
    if function == WmfRecord.RESTOREDC:
        playback.restore_dc(payload.i16(0))
        return
    if function == WmfRecord.SETTEXTALIGN:
        playback.state.text_align = payload.u16(0)
        return


def _handle_wmf_objects_record(function: int, payload: BoundedReader, playback: _Playback) -> None:
    """集中处理 wmf objects 类记录，保持原有字段顺序与边界检查。"""
    if function == WmfRecord.SELECTOBJECT:
        playback.select_wmf_handle(payload.u16(0))
        return
    if function == WmfRecord.DELETEOBJECT:
        handle = payload.u16(0)
        if handle < len(playback.wmf_objects):
            playback.wmf_objects[handle] = None
        return
    if function == WmfRecord.CREATEPENINDIRECT:
        playback.create_wmf_object(_make_pen(payload.u16(0), float(payload.i16(2)), payload.u32(6)))
        return
    if function == WmfRecord.CREATEBRUSHINDIRECT:
        playback.create_wmf_object(_make_brush(payload.u16(0), payload.u32(2), payload.u16(6)))
        return
    if function == WmfRecord.CREATEFONTINDIRECT:
        playback.create_wmf_object(_parse_wmf_font(payload))
        return


def _handle_wmf_drawing_record(function: int, payload: BoundedReader, playback: _Playback) -> None:
    """集中处理 wmf drawing 类记录，保持原有字段顺序与边界检查。"""
    if function == WmfRecord.MOVETO:
        point = float(payload.i16(2)), float(payload.i16(0))
        playback.move_to(point)
        return
    if function == WmfRecord.LINETO:
        endpoint = float(payload.i16(2)), float(payload.i16(0))
        playback.line_to(endpoint)
        return
    if function in {WmfRecord.POLYGON, WmfRecord.POLYLINE}:
        count = payload.u16(0)
        playback.charge_points(count)
        points = _read_wmf_points(payload, count, 2)
        playback.emit_path(
            _path_from_device_points([playback.map_point(point) for point in points], close=function == WmfRecord.POLYGON),
            stroke=True,
            fill=function == WmfRecord.POLYGON,
        )
        return
    if function == WmfRecord.POLYPOLYGON:
        polygon_count = payload.u16(0)
        if polygon_count > MAX_POINTS_PER_RECORD or polygon_count > payload.remaining(2) // 2:
            raise MetafileMalformedError("WMF PolyPolygon count array exceeds record boundary")
        counts = [payload.u16(2 + index * 2) for index in range(polygon_count)]
        total_count = sum(counts)
        playback.charge_points(total_count)
        points = _read_wmf_points(payload, total_count, 2 + polygon_count * 2)
        builder = PathBuilder()
        cursor = 0
        for count in counts:
            builder.extend(
                _path_from_device_points(
                    [playback.map_point(point) for point in points[cursor : cursor + count]],
                    close=True,
                )
            )
            cursor += count
        playback.emit_path(builder.build(), stroke=True, fill=True)
        return
    if function == WmfRecord.ROUNDRECT:
        rect = Rect(float(payload.i16(10)), float(payload.i16(8)), float(payload.i16(6)), float(payload.i16(4)))
        path = round_rectangle_path(rect, abs(payload.i16(2)) / 2.0, abs(payload.i16(0)) / 2.0)
        playback.emit_logical_path(path, stroke=True, fill=True)
        return
    if function in {WmfRecord.ELLIPSE, WmfRecord.RECTANGLE}:
        rect = Rect(float(payload.i16(6)), float(payload.i16(4)), float(payload.i16(2)), float(payload.i16(0)))
        if function == WmfRecord.ELLIPSE:
            path = ellipse_path(rect)
        else:
            path = rectangle_path(rect)
        playback.emit_logical_path(path, stroke=True, fill=True)
        return
    if function in {WmfRecord.ARC, WmfRecord.PIE, WmfRecord.CHORD}:
        end = float(payload.i16(2)), float(payload.i16(0))
        start = float(payload.i16(6)), float(payload.i16(4))
        rect = Rect(float(payload.i16(14)), float(payload.i16(12)), float(payload.i16(10)), float(payload.i16(8)))
        close_mode = "pie" if function == WmfRecord.PIE else "chord" if function == WmfRecord.CHORD else "open"
        playback.emit_logical_path(
            arc_path(rect, start, end, direction=2, close_mode=close_mode),
            stroke=True,
            fill=close_mode != "open",
        )
        return
    if function == WmfRecord.SETPIXEL:
        point = playback.map_point((float(payload.i16(6)), float(payload.i16(4))))
        color = _parse_color(payload.u32(0))
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


def _handle_wmf_clip_record(function: int, payload: BoundedReader, playback: _Playback) -> None:
    """集中处理 wmf clip 类记录，保持原有字段顺序与边界检查。"""
    if function in {WmfRecord.EXCLUDECLIPRECT, WmfRecord.INTERSECTCLIPRECT}:
        rect = Rect(float(payload.i16(6)), float(payload.i16(4)), float(payload.i16(2)), float(payload.i16(0)))
        playback.add_clip(
            transform_path(rectangle_path(rect), playback.logical_matrix()),
            _RGN_DIFF if function == WmfRecord.EXCLUDECLIPRECT else _RGN_AND,
        )
        return


def _handle_wmf_text_record(function: int, payload: BoundedReader, playback: _Playback) -> None:
    """集中处理 wmf text 类记录，保持原有字段顺序与边界检查。"""
    if function in {WmfRecord.TEXTOUT, WmfRecord.EXTTEXTOUT}:
        _handle_wmf_text(function, payload, playback)
        return


def _handle_wmf_bitmap_record(function: int, payload: BoundedReader, playback: _Playback) -> None:
    """集中处理 wmf bitmap 类记录，保持原有字段顺序与边界检查。"""
    if function == WmfRecord.STRETCHDIB:
        _handle_wmf_stretchdib(payload, playback)
        return
    if function in {WmfRecord.DIBBITBLT, WmfRecord.DIBSTRETCHBLT}:
        playback.warn("unsupported_wmf_dib_record", f"WMF DIB record will be skipped: {function:#x}")
        return


_WMF_DISPATCH: dict[int, Callable[[int, BoundedReader, _Playback], None]] = {
    WmfRecord.SAVEDC: _handle_wmf_state_record,
    WmfRecord.SETBKMODE: _handle_wmf_state_record,
    WmfRecord.SETMAPMODE: _handle_wmf_state_record,
    WmfRecord.SETROP2: _handle_wmf_state_record,
    WmfRecord.SETPOLYFILLMODE: _handle_wmf_state_record,
    WmfRecord.SETSTRETCHBLTMODE: _handle_wmf_state_record,
    WmfRecord.RESTOREDC: _handle_wmf_state_record,
    WmfRecord.SELECTOBJECT: _handle_wmf_objects_record,
    WmfRecord.SETTEXTALIGN: _handle_wmf_state_record,
    WmfRecord.DELETEOBJECT: _handle_wmf_objects_record,
    WmfRecord.SETBKCOLOR: _handle_wmf_state_record,
    WmfRecord.SETTEXTCOLOR: _handle_wmf_state_record,
    WmfRecord.SETWINDOWORG: _handle_wmf_state_record,
    WmfRecord.SETWINDOWEXT: _handle_wmf_state_record,
    WmfRecord.SETVIEWPORTORG: _handle_wmf_state_record,
    WmfRecord.SETVIEWPORTEXT: _handle_wmf_state_record,
    WmfRecord.OFFSETWINDOWORG: _handle_wmf_state_record,
    WmfRecord.OFFSETVIEWPORTORG: _handle_wmf_state_record,
    WmfRecord.LINETO: _handle_wmf_drawing_record,
    WmfRecord.MOVETO: _handle_wmf_drawing_record,
    WmfRecord.CREATEPENINDIRECT: _handle_wmf_objects_record,
    WmfRecord.CREATEFONTINDIRECT: _handle_wmf_objects_record,
    WmfRecord.CREATEBRUSHINDIRECT: _handle_wmf_objects_record,
    WmfRecord.POLYGON: _handle_wmf_drawing_record,
    WmfRecord.POLYLINE: _handle_wmf_drawing_record,
    WmfRecord.SCALEWINDOWEXT: _handle_wmf_state_record,
    WmfRecord.SCALEVIEWPORTEXT: _handle_wmf_state_record,
    WmfRecord.EXCLUDECLIPRECT: _handle_wmf_clip_record,
    WmfRecord.INTERSECTCLIPRECT: _handle_wmf_clip_record,
    WmfRecord.ELLIPSE: _handle_wmf_drawing_record,
    WmfRecord.RECTANGLE: _handle_wmf_drawing_record,
    WmfRecord.SETPIXEL: _handle_wmf_drawing_record,
    WmfRecord.TEXTOUT: _handle_wmf_text_record,
    WmfRecord.POLYPOLYGON: _handle_wmf_drawing_record,
    WmfRecord.ROUNDRECT: _handle_wmf_drawing_record,
    WmfRecord.ARC: _handle_wmf_drawing_record,
    WmfRecord.PIE: _handle_wmf_drawing_record,
    WmfRecord.CHORD: _handle_wmf_drawing_record,
    WmfRecord.DIBBITBLT: _handle_wmf_bitmap_record,
    WmfRecord.EXTTEXTOUT: _handle_wmf_text_record,
    WmfRecord.DIBSTRETCHBLT: _handle_wmf_bitmap_record,
    WmfRecord.STRETCHDIB: _handle_wmf_bitmap_record,
}

__all__ = ["_handle_wmf_record"]
