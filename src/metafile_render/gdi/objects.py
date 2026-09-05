# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF gdi.objects 内部实现。"""

from __future__ import annotations

from ..binary import BoundedReader
from ..models import MetafileMalformedError
from ..primitives import Brush, Font, Pen
from .scalars import _parse_color


def _pen_dash_pattern(style: int, width: float) -> tuple[float, ...]:
    """把常见 GDI pen style 转换为 device 单位虚线模式。"""
    unit = max(width, 1.0)
    return {
        1: (6.0 * unit, 3.0 * unit),
        2: (unit, 2.0 * unit),
        3: (6.0 * unit, 2.0 * unit, unit, 2.0 * unit),
        4: (6.0 * unit, 2.0 * unit, unit, 2.0 * unit, unit, 2.0 * unit),
        8: (unit, unit),
    }.get(style & 0xF, ())


def _make_pen(style: int, width: float, color_value: int, *, extended: bool = False) -> Pen:
    """从 LOGPEN/EXTLOGPEN 字段构造统一画笔。"""
    basic_style = style & 0xF
    cap = "square" if style & 0x100 else "flat" if style & 0x200 else "round"
    join = "bevel" if style & 0x1000 else "miter" if style & 0x2000 else "round"
    geometric = bool(style & 0x10000) if extended else abs(width) > 1.0
    normalized_width = max(abs(width), 1.0)
    return Pen(
        color=_parse_color(color_value),
        width=normalized_width,
        style=style,
        cosmetic=not geometric,
        null=basic_style == 5,
        cap=cap,
        join=join,
        dashes=_pen_dash_pattern(style, normalized_width),
    )


def _make_brush(style: int, color_value: int, hatch: int = 0, pattern: bytes | None = None) -> Brush:
    """从 LOGBRUSH 字段构造统一画刷。"""
    if style == 1:
        return Brush(kind="null")
    if style == 2:
        return Brush(kind="hatch", color=_parse_color(color_value), hatch=hatch)
    if style in {3, 5, 6, 7, 8}:
        return Brush(kind="pattern", color=_parse_color(color_value), hatch=hatch, pattern=pattern)
    return Brush(kind="solid", color=_parse_color(color_value), hatch=hatch)


def _decode_font_name(raw: bytes, *, wide: bool) -> str:
    """解码 LOGFONT face name 并移除结尾 NUL。"""
    if wide:
        if len(raw) % 2:
            raw = raw[:-1]
        text = raw.decode("utf-16le", errors="replace")
    else:
        text = raw.decode("cp1252", errors="replace")
    return text.split("\x00", 1)[0].strip() or "Arial"


def _parse_emf_font(record: BoundedReader) -> Font:
    """解析 EMR_EXTCREATEFONTINDIRECTW 的 LOGFONTW 首部。"""
    return Font(
        face_name=_decode_font_name(record.bytes(40, 64), wide=True),
        height=float(record.i32(12)),
        width=float(record.i32(16)),
        escapement=float(record.i32(20)),
        orientation=float(record.i32(24)),
        weight=record.i32(28),
        italic=bool(record.u8(32)),
        underline=bool(record.u8(33)),
        strikeout=bool(record.u8(34)),
        charset=record.u8(35),
    )


def _parse_wmf_font(payload: BoundedReader) -> Font:
    """解析 META_CREATEFONTINDIRECT 的 16 位 LOGFONT。"""
    if len(payload) < 18:
        raise MetafileMalformedError("WMF LOGFONT is truncated")
    face_size = min(32, max(0, len(payload) - 18))
    return Font(
        face_name=_decode_font_name(payload.bytes(18, face_size), wide=False),
        height=float(payload.i16(0)),
        width=float(payload.i16(2)),
        escapement=float(payload.i16(4)),
        orientation=float(payload.i16(6)),
        weight=payload.i16(8),
        italic=bool(payload.u8(10)),
        underline=bool(payload.u8(11)),
        strikeout=bool(payload.u8(12)),
        charset=payload.u8(13),
    )


def _charset_codec(charset: int) -> str:
    """把常见 Windows LOGFONT charset 映射为 Python codec。"""
    return {
        0: "cp1252",
        1: "cp1252",
        2: "cp1252",
        128: "shift_jis",
        129: "cp949",
        134: "gb18030",
        136: "big5",
        161: "cp1253",
        162: "cp1254",
        163: "cp1258",
        177: "cp1255",
        178: "cp1256",
        186: "cp1257",
        204: "cp1251",
        222: "cp874",
        238: "cp1250",
        255: "cp437",
    }.get(charset, "cp1252")


def _decode_ansi_text(raw: bytes, charset: int) -> str:
    """按当前 LOGFONT charset 尽力解码 GDI ANSI 文本。"""
    return raw.decode(_charset_codec(charset), errors="replace")


__all__ = [
    "_pen_dash_pattern",
    "_make_pen",
    "_make_brush",
    "_decode_font_name",
    "_parse_emf_font",
    "_parse_wmf_font",
    "_charset_codec",
    "_decode_ansi_text",
]
