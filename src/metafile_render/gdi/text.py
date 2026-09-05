# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF gdi.text 内部实现。"""

from __future__ import annotations

from math import atan2, cos, degrees, radians, sin

from ..binary import BoundedReader
from ..commands import DrawTextCommand, TextAlignment
from ..font import measure_text_advance
from ..geometry import rectangle_path, vector_length
from ..limits import MAX_POINTS_PER_RECORD
from ..models import MetafileMalformedError, MetafileResourceLimitError
from ..primitives import ClipOperation, Point, Rect
from .constants import _ETO_CLIPPED, _ETO_GLYPH_INDEX, _ETO_OPAQUE, _ETO_PDY, _TA_UPDATECP
from .objects import _decode_ansi_text
from .playback import _Playback
from .record_types import EmfRecord, WmfRecord
from .scalars import _parse_rect_i16, _parse_rect_i32


def _emit_text(
    playback: _Playback,
    *,
    text: str,
    reference: Point,
    options: int,
    bounds: Rect | None,
    advances: list[Point] | None,
) -> None:
    """把 GDI 文字和显式 advance 转换为统一文字命令。"""
    if not text:
        return
    if options & _ETO_GLYPH_INDEX:
        playback.warn("glyph_index_text", "glyph-index text was replaced with visible placeholder glyphs")
        text = "□" * len(text)
    update_current = bool(playback.state.text_align & _TA_UPDATECP)
    logical_origin = playback.state.current_position if update_current else reference
    origin = playback.map_point(logical_origin)
    positions: list[Point] = []
    advance_end: Point | None = None
    font = playback.state.font
    if advances:
        cursor_x, cursor_y = logical_origin
        for index, _character in enumerate(text):
            positions.append(playback.map_point((cursor_x, cursor_y)))
            if index < len(advances):
                cursor_x += advances[index][0]
                cursor_y += advances[index][1]
        advance_end = playback.map_point((cursor_x, cursor_y))
        if update_current:
            playback.state.current_position = (cursor_x, cursor_y)
    elif update_current:
        advance = measure_text_advance(font, text)
        angle = radians(font.escapement / 10.0)
        playback.state.current_position = (
            logical_origin[0] + advance * cos(angle),
            logical_origin[1] - advance * sin(angle),
        )
    mapped_height = vector_length(playback.map_vector((0.0, font.height or -12.0)))
    baseline_vector = playback.map_vector((1.0, 0.0))
    rotation = degrees(atan2(baseline_vector[1], baseline_vector[0])) + font.escapement / 10.0
    mapped_bounds: Rect | None = None
    if bounds is not None and (options & (_ETO_OPAQUE | _ETO_CLIPPED)):
        corners = [
            playback.map_point((bounds.left, bounds.top)),
            playback.map_point((bounds.right, bounds.top)),
            playback.map_point((bounds.right, bounds.bottom)),
            playback.map_point((bounds.left, bounds.bottom)),
        ]
        mapped_bounds = Rect(
            min(point[0] for point in corners),
            min(point[1] for point in corners),
            max(point[0] for point in corners),
            max(point[1] for point in corners),
        )
    clip = playback.state.clip
    if mapped_bounds is not None and options & _ETO_CLIPPED:
        clip = playback.clip_with_operation(ClipOperation(rectangle_path(mapped_bounds), "and"))
    playback.append_command(
        DrawTextCommand(
            text=text,
            origin=origin,
            positions=tuple(positions),
            font=font,
            font_height=max(mapped_height, 1.0),
            rotation=rotation,
            color=playback.state.text_color,
            background_color=playback.state.background_color,
            opaque=bool(options & _ETO_OPAQUE) or playback.state.background_mode == 2,
            bounds=mapped_bounds,
            clip=clip,
            advance_end=advance_end,
            alignment=TextAlignment.from_gdi(playback.state.text_align),
        )
    )


def _handle_emf_text(record_type: int, record: BoundedReader, playback: _Playback) -> None:
    """解析 EMR_EXTTEXTOUTA/W 文字、bounds 与显式 advance。"""
    if len(record) < 76:
        raise MetafileMalformedError("EMF ExtTextOut record is truncated")
    reference = float(record.i32(36)), float(record.i32(40))
    character_count = record.u32(44)
    if character_count > MAX_POINTS_PER_RECORD:
        raise MetafileResourceLimitError(f"EMF text exceeds max characters={MAX_POINTS_PER_RECORD}")
    string_offset = record.u32(48)
    options = record.u32(52)
    bounds = _parse_rect_i32(record, 56)
    dx_offset = record.u32(72)
    char_bytes = 2 if record_type == EmfRecord.EXTTEXTOUTW else 1
    raw = record.bytes(string_offset, character_count * char_bytes)
    text = (
        raw.decode("utf-16le", errors="replace")
        if record_type == EmfRecord.EXTTEXTOUTW
        else _decode_ansi_text(raw, playback.state.font.charset)
    )
    advances: list[Point] | None = None
    if dx_offset:
        values_per_character = 2 if options & _ETO_PDY else 1
        value_count = character_count * values_per_character
        if value_count > record.remaining(dx_offset) // 4:
            raise MetafileMalformedError("EMF ExtTextOut advance array exceeds record boundary")
        advances = []
        for index in range(character_count):
            dx = float(record.i32(dx_offset + index * values_per_character * 4))
            dy = float(record.i32(dx_offset + (index * values_per_character + 1) * 4)) if values_per_character == 2 else 0.0
            advances.append((dx, dy))
    playback.charge_points(character_count)
    _emit_text(
        playback,
        text=text,
        reference=reference,
        options=options,
        bounds=bounds,
        advances=advances,
    )


def _handle_wmf_text(function: int, payload: BoundedReader, playback: _Playback) -> None:
    """解析 WMF TextOut/ExtTextOut 字符串、矩形和 advance。"""
    if function == WmfRecord.TEXTOUT:
        count = payload.u16(0)
        if count > payload.remaining(2):
            raise MetafileMalformedError("WMF TextOut string exceeds record boundary")
        raw = payload.bytes(2, count)
        coordinate_offset = 2 + count + (count & 1)
        reference = float(payload.i16(coordinate_offset + 2)), float(payload.i16(coordinate_offset))
        playback.charge_points(count)
        _emit_text(
            playback,
            text=_decode_ansi_text(raw, playback.state.font.charset),
            reference=reference,
            options=0,
            bounds=None,
            advances=None,
        )
        return
    reference = float(payload.i16(2)), float(payload.i16(0))
    count = payload.u16(4)
    options = payload.u16(6)
    offset = 8
    bounds: Rect | None = None
    if options & (_ETO_OPAQUE | _ETO_CLIPPED):
        bounds = _parse_rect_i16(payload, offset)
        offset += 8
    if count > payload.remaining(offset):
        raise MetafileMalformedError("WMF ExtTextOut string exceeds record boundary")
    raw = payload.bytes(offset, count)
    offset += count + (count & 1)
    advances: list[Point] | None = None
    if payload.remaining(offset) >= count * 2:
        advances = [(float(payload.i16(offset + index * 2)), 0.0) for index in range(count)]
    playback.charge_points(count)
    _emit_text(
        playback,
        text=_decode_ansi_text(raw, playback.state.font.charset),
        reference=reference,
        options=options,
        bounds=bounds,
        advances=advances,
    )


__all__ = ["_emit_text", "_handle_emf_text", "_handle_wmf_text"]
