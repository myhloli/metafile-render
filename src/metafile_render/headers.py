# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""格式识别、头部与 EMF+ 模式预扫描。"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .binary import BoundedReader
from .emfplus.record_types import PlusRecordType
from .gdi.constants import _EMF_SIGNATURE, _PLACEABLE_WMF_KEY
from .gdi.scalars import _parse_rect_i32
from .limits import MAX_OBJECTS, MAX_RECORDS
from .models import EmfPlusMode, MetafileMalformedError, MetafileResourceLimitError, MetafileSourceFormat
from .primitives import Point, Rect
from .records import iter_records


@dataclass(frozen=True, slots=True)
class _HeaderInfo:
    """保存格式头部解析出的记录边界、坐标边界和输出尺寸。"""

    source_format: MetafileSourceFormat
    record_start: int
    record_end: int
    bounds: Rect | None
    pixel_size: tuple[int, int] | None
    emfplus_mode: EmfPlusMode = "none"
    device_pixels_per_mm: Point = (96.0 / 25.4, 96.0 / 25.4)
    wmf_object_count: int = 0


def _detect_source_format(data: bytes) -> MetafileSourceFormat:
    """只依据实际文件签名区分 WMF 与 EMF。"""
    if (
        len(data) >= 44
        and struct.unpack_from("<I", data, 0)[0] == 1
        and struct.unpack_from("<I", data, 40)[0] == _EMF_SIGNATURE
    ):
        return "emf"
    wmf_offset = 22 if len(data) >= 22 and struct.unpack_from("<I", data, 0)[0] == _PLACEABLE_WMF_KEY else 0
    if len(data) >= wmf_offset + 18:
        file_type, header_size = struct.unpack_from("<HH", data, wmf_offset)
        if file_type in {1, 2} and header_size == 9:
            return "wmf"
    raise MetafileMalformedError("input does not contain a valid WMF or EMF signature")


def _scan_emfplus_mode(data: bytes, start: int, end: int) -> EmfPlusMode:
    """严格扫描子记录边界及真实 Header，避免坏 EMF+ 被误判为普通 EMF。"""
    from .emfplus.binary import Cursor, comment_records

    mode: EmfPlusMode = "none"
    plus_count = 0
    eof = False
    for record in iter_records(data, start, end, "emf"):
        if record.kind != 70:
            continue
        for item in comment_records(record.data):
            plus_count += 1
            if plus_count > MAX_RECORDS:
                raise MetafileResourceLimitError("EMF+ exceeds record budget")
            if eof or mode == "none" and item.kind != PlusRecordType.HEADER:
                raise MetafileMalformedError("EMF+ record outside Header/EOF boundaries")
            if item.kind == PlusRecordType.HEADER:
                if mode != "none" or len(item.payload) != 16:
                    raise MetafileMalformedError("invalid or duplicate EMF+ Header")
                cursor = Cursor(item.payload)
                cursor.version()
                cursor.u32()
                dx, dy = cursor.u32(), cursor.u32()
                if not dx or not dy or max(dx, dy) > 100000:
                    raise MetafileMalformedError("invalid EMF+ logical DPI")
                mode = "dual" if item.flags & 1 else "only"
            elif item.kind == PlusRecordType.END_OF_FILE:
                if len(item.payload):
                    raise MetafileMalformedError("EMF+ EndOfFile must be empty")
                eof = True
    if mode != "none" and not eof:
        raise MetafileMalformedError("EMF+ stream has no EndOfFile")
    return mode


def _parse_emf_header(data: bytes, dpi: int, size_hint: tuple[int, int] | None) -> _HeaderInfo:
    """解析 EMR_HEADER、声明文件边界和物理输出尺寸。"""
    reader = BoundedReader(data)
    if len(reader) < 88 or reader.u32(0) != 1:
        raise MetafileMalformedError("EMF header is truncated or has the wrong record type")
    header_size = reader.u32(4)
    if header_size < 88 or header_size % 4 or header_size > len(reader):
        raise MetafileMalformedError(f"invalid EMF header size: {header_size}")
    if reader.u32(40) != _EMF_SIGNATURE:
        raise MetafileMalformedError("invalid EMF signature")
    declared_bytes = reader.u32(48)
    declared_records = reader.u32(52)
    if declared_bytes < header_size or declared_bytes > len(reader):
        raise MetafileMalformedError(f"invalid EMF declared byte size: {declared_bytes}")
    if declared_records == 0 or declared_records > MAX_RECORDS:
        raise MetafileResourceLimitError(f"invalid or excessive EMF record count: {declared_records}")
    bounds = _parse_rect_i32(reader, 8)
    frame = _parse_rect_i32(reader, 24)
    device_x, device_y = reader.i32(72), reader.i32(76)
    millimeter_x, millimeter_y = reader.i32(80), reader.i32(84)
    if millimeter_x > 0 and millimeter_y > 0 and device_x > 0 and device_y > 0:
        device_scale = device_x / millimeter_x, device_y / millimeter_y
    else:
        device_scale = dpi / 25.4, dpi / 25.4
    pixel_size = size_hint
    if pixel_size is None and frame.width != 0 and frame.height != 0:
        pixel_size = max(1, round(abs(frame.width) * dpi / 2540.0)), max(1, round(abs(frame.height) * dpi / 2540.0))
    if pixel_size is None and bounds.width != 0 and bounds.height != 0:
        pixel_size = max(1, round(abs(bounds.width))), max(1, round(abs(bounds.height)))
    emfplus_mode = _scan_emfplus_mode(data, 0, declared_bytes)
    if emfplus_mode == "only" and frame.width > 0 and frame.height > 0:
        bounds = Rect(
            frame.left * device_scale[0] / 100,
            frame.top * device_scale[1] / 100,
            frame.right * device_scale[0] / 100,
            frame.bottom * device_scale[1] / 100,
        )
    return _HeaderInfo(
        source_format="emf",
        record_start=0,
        record_end=declared_bytes,
        bounds=bounds,
        pixel_size=pixel_size,
        emfplus_mode=emfplus_mode,
        device_pixels_per_mm=device_scale,
    )


def _parse_wmf_header(data: bytes, dpi: int, size_hint: tuple[int, int] | None) -> _HeaderInfo:
    """解析 placeable/standard WMF 头部、对象表大小与输出尺寸。"""
    reader = BoundedReader(data)
    record_start = 0
    bounds: Rect | None = None
    pixel_size = size_hint
    if len(reader) >= 22 and reader.u32(0) == _PLACEABLE_WMF_KEY:
        checksum = 0
        for offset in range(0, 20, 2):
            checksum ^= reader.u16(offset)
        if checksum != reader.u16(20):
            raise MetafileMalformedError("placeable WMF checksum does not match")
        bounds = Rect(reader.i16(6), reader.i16(8), reader.i16(10), reader.i16(12))
        units_per_inch = reader.u16(14)
        if units_per_inch == 0:
            raise MetafileMalformedError("placeable WMF units-per-inch must be nonzero")
        if pixel_size is None:
            pixel_size = (
                max(1, round(abs(bounds.width) * dpi / units_per_inch)),
                max(1, round(abs(bounds.height) * dpi / units_per_inch)),
            )
        record_start = 22
    if len(reader) < record_start + 18:
        raise MetafileMalformedError("standard WMF header is truncated")
    header = reader.subreader(record_start, 18)
    if header.u16(0) not in {1, 2} or header.u16(2) != 9:
        raise MetafileMalformedError("invalid standard WMF header")
    declared_bytes = header.u32(6) * 2
    if declared_bytes < 18 or declared_bytes > len(reader) - record_start:
        raise MetafileMalformedError(f"invalid WMF declared byte size: {declared_bytes}")
    object_count = header.u16(10)
    if object_count > MAX_OBJECTS:
        raise MetafileResourceLimitError(f"WMF object table exceeds max_objects={MAX_OBJECTS}")
    return _HeaderInfo(
        source_format="wmf",
        record_start=record_start + 18,
        record_end=record_start + declared_bytes,
        bounds=bounds,
        pixel_size=pixel_size,
        device_pixels_per_mm=(dpi / 25.4, dpi / 25.4),
        wmf_object_count=object_count,
    )


__all__ = ["_HeaderInfo", "_detect_source_format", "_scan_emfplus_mode", "_parse_emf_header", "_parse_wmf_header"]
