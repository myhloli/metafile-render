# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF gdi.wmf 内部实现。"""

from __future__ import annotations

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
from .scalars import _parse_color
from .shapes import _path_from_device_points, _read_wmf_points
from .text import _handle_wmf_text


def _handle_wmf_record(function: int, payload: BoundedReader, playback: _Playback) -> None:
    """分派并执行单条 WMF record，未知绘图记录按规范跳过。"""
    if function == 0:
        return
    if function == 0x0201:
        playback.state.background_color = _parse_color(payload.u32(0))
        return
    if function == 0x0102:
        playback.state.background_mode = payload.u16(0)
        return
    if function == 0x0103:
        playback.state.map_mode = payload.i16(0)
        return
    if function == 0x0104:
        rop2 = payload.u16(0)
        if not 1 <= rop2 <= 16:
            playback.warn("unsupported_rop2", f"invalid WMF ROP2 will use COPYPEN approximation: {rop2}")
            rop2 = 13
        playback.state.rop2 = rop2
        return
    if function == 0x0106:
        playback.state.polygon_fill_mode = payload.u16(0)
        return
    if function == 0x0107:
        playback.state.stretch_mode = payload.u16(0)
        return
    if function == 0x0209:
        playback.state.text_color = _parse_color(payload.u32(0))
        return
    if function == 0x020B:
        playback.state.window_origin = float(payload.i16(2)), float(payload.i16(0))
        return
    if function == 0x020C:
        playback.state.window_extent = float(payload.i16(2)), float(payload.i16(0))
        return
    if function == 0x020D:
        playback.state.viewport_origin = float(payload.i16(2)), float(payload.i16(0))
        return
    if function == 0x020E:
        playback.state.viewport_extent = float(payload.i16(2)), float(payload.i16(0))
        return
    if function == 0x020F:
        x, y = playback.state.window_origin
        playback.state.window_origin = x + payload.i16(2), y + payload.i16(0)
        return
    if function == 0x0211:
        x, y = playback.state.viewport_origin
        playback.state.viewport_origin = x + payload.i16(2), y + payload.i16(0)
        return
    if function in {0x0410, 0x0412}:
        y_num, y_den, x_num, x_den = payload.i16(0), payload.i16(2), payload.i16(4), payload.i16(6)
        if x_den == 0 or y_den == 0:
            playback.warn("zero_scale_denominator", "WMF viewport/window scale record has a zero denominator")
            return
        if function == 0x0410:
            width, height = playback.state.window_extent
            playback.state.window_extent = width * x_num / x_den, height * y_num / y_den
        else:
            width, height = playback.state.viewport_extent
            playback.state.viewport_extent = width * x_num / x_den, height * y_num / y_den
        return
    if function == 0x001E:
        playback.save_dc()
        return
    if function == 0x0127:
        playback.restore_dc(payload.i16(0))
        return
    if function == 0x012E:
        playback.state.text_align = payload.u16(0)
        return
    if function == 0x012D:
        playback.select_wmf_handle(payload.u16(0))
        return
    if function == 0x01F0:
        handle = payload.u16(0)
        if handle < len(playback.wmf_objects):
            playback.wmf_objects[handle] = None
        return
    if function == 0x02FA:
        playback.create_wmf_object(_make_pen(payload.u16(0), float(payload.i16(2)), payload.u32(6)))
        return
    if function == 0x02FC:
        playback.create_wmf_object(_make_brush(payload.u16(0), payload.u32(2), payload.u16(6)))
        return
    if function == 0x02FB:
        playback.create_wmf_object(_parse_wmf_font(payload))
        return

    if function == 0x0214:
        point = float(payload.i16(2)), float(payload.i16(0))
        playback.move_to(point)
        return
    if function == 0x0213:
        endpoint = float(payload.i16(2)), float(payload.i16(0))
        playback.line_to(endpoint)
        return
    if function in {0x0324, 0x0325}:
        count = payload.u16(0)
        playback.charge_points(count)
        points = _read_wmf_points(payload, count, 2)
        playback.emit_path(
            _path_from_device_points([playback.map_point(point) for point in points], close=function == 0x0324),
            stroke=True,
            fill=function == 0x0324,
        )
        return
    if function == 0x0538:
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
    if function == 0x061C:
        rect = Rect(float(payload.i16(10)), float(payload.i16(8)), float(payload.i16(6)), float(payload.i16(4)))
        path = round_rectangle_path(rect, abs(payload.i16(2)) / 2.0, abs(payload.i16(0)) / 2.0)
        playback.emit_logical_path(path, stroke=True, fill=True)
        return
    if function in {0x0418, 0x041B}:
        rect = Rect(float(payload.i16(6)), float(payload.i16(4)), float(payload.i16(2)), float(payload.i16(0)))
        if function == 0x0418:
            path = ellipse_path(rect)
        else:
            path = rectangle_path(rect)
        playback.emit_logical_path(path, stroke=True, fill=True)
        return
    if function in {0x0817, 0x081A, 0x0830}:
        end = float(payload.i16(2)), float(payload.i16(0))
        start = float(payload.i16(6)), float(payload.i16(4))
        rect = Rect(float(payload.i16(14)), float(payload.i16(12)), float(payload.i16(10)), float(payload.i16(8)))
        close_mode = "pie" if function == 0x081A else "chord" if function == 0x0830 else "open"
        playback.emit_logical_path(
            arc_path(rect, start, end, direction=2, close_mode=close_mode),
            stroke=True,
            fill=close_mode != "open",
        )
        return
    if function == 0x041F:
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
    if function in {0x0415, 0x0416}:
        rect = Rect(float(payload.i16(6)), float(payload.i16(4)), float(payload.i16(2)), float(payload.i16(0)))
        playback.add_clip(
            transform_path(rectangle_path(rect), playback.logical_matrix()),
            _RGN_DIFF if function == 0x0415 else _RGN_AND,
        )
        return

    if function in {0x0521, 0x0A32}:
        _handle_wmf_text(function, payload, playback)
        return
    if function == 0x0F43:
        _handle_wmf_stretchdib(payload, playback)
        return
    if function in {0x0940, 0x0B41}:
        playback.warn("unsupported_wmf_dib_record", f"WMF DIB record will be skipped: {function:#x}")
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


__all__ = ["_handle_wmf_record"]
