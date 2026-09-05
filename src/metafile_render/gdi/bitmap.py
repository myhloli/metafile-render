# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF gdi.bitmap 内部实现。"""

from __future__ import annotations

import struct

from ..binary import BoundedReader
from ..commands import DrawImageCommand
from ..limits import MAX_EMBEDDED_BITMAP_BYTES
from ..models import MetafileMalformedError, MetafileResourceLimitError
from ..primitives import Rect
from .constants import _SRCCOPY, _SUPPORTED_ROP3
from .playback import _Playback


def _append_image_command(
    playback: _Playback,
    record: BoundedReader,
    *,
    off_bmi: int,
    cb_bmi: int,
    off_bits: int,
    cb_bits: int,
    destination: Rect,
    source: Rect | None,
    rop: int,
    constant_alpha: int = 255,
    use_source_alpha: bool = False,
) -> None:
    """校验 DIB 子区间并追加统一位图命令。"""
    if cb_bmi <= 0 or cb_bits <= 0:
        playback.warn("empty_dib", "bitmap record does not contain a DIB header and pixel payload")
        return
    if rop not in _SUPPORTED_ROP3:
        playback.warn("unsupported_rop3", f"unsupported ROP3 will use source-over approximation: {rop:#x}")
    if (
        cb_bmi > MAX_EMBEDDED_BITMAP_BYTES
        or cb_bits > MAX_EMBEDDED_BITMAP_BYTES
        or cb_bmi + cb_bits > MAX_EMBEDDED_BITMAP_BYTES
    ):
        raise MetafileResourceLimitError(f"embedded bitmap exceeds max_embedded_bitmap_bytes={MAX_EMBEDDED_BITMAP_BYTES}")
    dib_header = record.bytes(off_bmi, cb_bmi)
    bits = record.bytes(off_bits, cb_bits)
    corners = (
        playback.map_point((destination.left, destination.top)),
        playback.map_point((destination.right, destination.top)),
        playback.map_point((destination.right, destination.bottom)),
        playback.map_point((destination.left, destination.bottom)),
    )
    playback.append_command(
        DrawImageCommand(
            dib_header=dib_header,
            bits=bits,
            destination=corners,
            source=source,
            rop=rop,
            stretch_mode=playback.state.stretch_mode,
            constant_alpha=max(0, min(constant_alpha, 255)),
            use_source_alpha=use_source_alpha,
            clip=playback.state.clip,
        )
    )


def _handle_emf_bitmap(record_type: int, record: BoundedReader, playback: _Playback) -> None:
    """解析常见 EMF DIB、BitBlt、StretchBlt 与 AlphaBlend 记录。"""
    if record_type in {76, 77}:
        destination = Rect(
            float(record.i32(24)),
            float(record.i32(28)),
            float(record.i32(24) + record.i32(32)),
            float(record.i32(28) + record.i32(36)),
        )
        source_width = record.i32(100) if record_type == 77 and len(record) >= 108 else record.i32(32)
        source_height = record.i32(104) if record_type == 77 and len(record) >= 108 else record.i32(36)
        source = Rect(
            float(record.i32(44)),
            float(record.i32(48)),
            float(record.i32(44) + source_width),
            float(record.i32(48) + source_height),
        )
        _append_image_command(
            playback,
            record,
            off_bmi=record.u32(84),
            cb_bmi=record.u32(88),
            off_bits=record.u32(92),
            cb_bits=record.u32(96),
            destination=destination,
            source=source,
            rop=record.u32(40),
        )
        return
    if record_type == 81:
        destination = Rect(
            float(record.i32(24)),
            float(record.i32(28)),
            float(record.i32(24) + record.i32(72)),
            float(record.i32(28) + record.i32(76)),
        )
        source = Rect(
            float(record.i32(32)),
            float(record.i32(36)),
            float(record.i32(32) + record.i32(40)),
            float(record.i32(36) + record.i32(44)),
        )
        _append_image_command(
            playback,
            record,
            off_bmi=record.u32(48),
            cb_bmi=record.u32(52),
            off_bits=record.u32(56),
            cb_bits=record.u32(60),
            destination=destination,
            source=source,
            rop=record.u32(68),
        )
        return
    if record_type == 80:
        width, height = record.i32(40), record.i32(44)
        destination = Rect(
            float(record.i32(24)),
            float(record.i32(28)),
            float(record.i32(24) + width),
            float(record.i32(28) + height),
        )
        source = Rect(
            float(record.i32(32)),
            float(record.i32(36)),
            float(record.i32(32) + width),
            float(record.i32(36) + height),
        )
        _append_image_command(
            playback,
            record,
            off_bmi=record.u32(48),
            cb_bmi=record.u32(52),
            off_bits=record.u32(56),
            cb_bits=record.u32(60),
            destination=destination,
            source=source,
            rop=_SRCCOPY,
        )
        return
    destination = Rect(
        float(record.i32(24)),
        float(record.i32(28)),
        float(record.i32(24) + record.i32(32)),
        float(record.i32(28) + record.i32(36)),
    )
    source = Rect(
        float(record.i32(44)),
        float(record.i32(48)),
        float(record.i32(44) + record.i32(100)),
        float(record.i32(48) + record.i32(104)),
    )
    _append_image_command(
        playback,
        record,
        off_bmi=record.u32(84),
        cb_bmi=record.u32(88),
        off_bits=record.u32(92),
        cb_bits=record.u32(96),
        destination=destination,
        source=source,
        rop=_SRCCOPY,
        constant_alpha=record.u8(42),
        use_source_alpha=bool(record.u8(43) & 1),
    )


def _handle_wmf_stretchdib(payload: BoundedReader, playback: _Playback) -> None:
    """解析 META_STRETCHDIB 的内联 DIB 与源/目标矩形。"""
    if len(payload) < 22:
        raise MetafileMalformedError("WMF StretchDIB record is truncated")
    rop = payload.u32(0)
    source_height, source_width = payload.i16(6), payload.i16(8)
    source_y, source_x = payload.i16(10), payload.i16(12)
    destination_height, destination_width = payload.i16(14), payload.i16(16)
    destination_y, destination_x = payload.i16(18), payload.i16(20)
    dib = payload.bytes(22, payload.remaining(22))
    if len(dib) < 12:
        raise MetafileMalformedError("WMF StretchDIB payload does not contain a DIB header")
    header_size = struct.unpack_from("<I", dib, 0)[0]
    if header_size < 12 or header_size > len(dib):
        raise MetafileMalformedError(f"invalid WMF DIB header size: {header_size}")
    bits_offset = _dib_bits_offset(dib)
    if bits_offset > len(dib):
        raise MetafileMalformedError("WMF DIB pixel offset exceeds record boundary")
    header = dib[:bits_offset]
    bits = dib[bits_offset:]
    corners = (
        playback.map_point((float(destination_x), float(destination_y))),
        playback.map_point((float(destination_x + destination_width), float(destination_y))),
        playback.map_point((float(destination_x + destination_width), float(destination_y + destination_height))),
        playback.map_point((float(destination_x), float(destination_y + destination_height))),
    )
    playback.append_command(
        DrawImageCommand(
            dib_header=header,
            bits=bits,
            destination=corners,
            source=Rect(
                float(source_x),
                float(source_y),
                float(source_x + source_width),
                float(source_y + source_height),
            ),
            rop=rop,
            stretch_mode=playback.state.stretch_mode,
            clip=playback.state.clip,
        )
    )


def _dib_bits_offset(dib: bytes) -> int:
    """根据 DIB header、bit depth 和 palette 推导像素起始偏移。"""
    reader = BoundedReader(dib)
    header_size = reader.u32(0)
    if header_size == 12:
        bit_count = reader.u16(10)
        palette_entries = 1 << bit_count if bit_count <= 8 else 0
        return header_size + palette_entries * 3
    if header_size < 40:
        return header_size
    bit_count = reader.u16(14)
    compression = reader.u32(16)
    colors_used = reader.u32(32)
    palette_entries = colors_used or ((1 << bit_count) if bit_count <= 8 else 0)
    bitfields = 12 if compression == 3 and header_size == 40 else 16 if compression == 6 and header_size == 40 else 0
    return header_size + bitfields + palette_entries * 4


__all__ = ["_append_image_command", "_handle_emf_bitmap", "_handle_wmf_stretchdib", "_dib_bits_offset"]
