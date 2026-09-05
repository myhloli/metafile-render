# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF parser 内部实现。"""

from __future__ import annotations

import struct

from .binary import BoundedReader
from .commands import MetafileDocument
from .context import _DiagnosticSink
from .gdi.emf import _handle_emf_record
from .gdi.playback import _Playback
from .gdi.wmf import _handle_wmf_record
from .headers import _detect_source_format, _HeaderInfo, _parse_emf_header, _parse_wmf_header
from .limits import MAX_METAFILE_BYTES, MAX_RECORDS
from .models import MetafileMalformedError, MetafileResourceLimitError
from .primitives import Rect


def parse_metafile(
    data: bytes,
    *,
    dpi: int = 144,
    size_hint: tuple[int, int] | None = None,
) -> MetafileDocument:
    """检测并解析 WMF/EMF，返回跨后端统一图元文档。"""
    if not isinstance(data, bytes):
        raise TypeError("metafile data must be bytes")
    if not data:
        raise MetafileMalformedError("metafile data must not be empty")
    if len(data) > MAX_METAFILE_BYTES:
        raise MetafileResourceLimitError(f"metafile exceeds max_metafile_bytes={MAX_METAFILE_BYTES}")
    if not isinstance(dpi, int) or dpi < 1 or dpi > 1200:
        raise ValueError("dpi must be an integer between 1 and 1200")
    if size_hint is not None and (
        len(size_hint) != 2 or not all(isinstance(value, int) for value in size_hint) or size_hint[0] <= 0 or size_hint[1] <= 0
    ):
        raise ValueError("size_hint must contain two positive integers")
    source_format = _detect_source_format(data)
    header = _parse_emf_header(data, dpi, size_hint) if source_format == "emf" else _parse_wmf_header(data, dpi, size_hint)
    diagnostics = _DiagnosticSink()
    playback = _Playback(header, diagnostics)
    if source_format == "emf":
        _play_emf(data, header, playback)
    else:
        _play_wmf(data, header, playback)
    return playback.finalize(size_hint)


def _play_emf(data: bytes, header: _HeaderInfo, playback: _Playback) -> None:
    """按文件顺序验证并回放全部 EMF records。"""
    from .emfplus.playback import EmfPlusPlayback

    plus = (
        EmfPlusPlayback(
            playback,
            header.bounds or Rect(0, 0, 1, 1),
            (header.device_pixels_per_mm[0] * 25.4, header.device_pixels_per_mm[1] * 25.4),
        )
        if header.emfplus_mode == "only"
        else None
    )
    offset = header.record_start
    record_index = 0
    saw_eof = False
    while offset < header.record_end:
        if offset + 8 > header.record_end:
            raise MetafileMalformedError(f"truncated EMF record header at offset={offset}")
        record_type, record_size = struct.unpack_from("<II", data, offset)
        record_index += 1
        if record_index > MAX_RECORDS:
            raise MetafileResourceLimitError(f"EMF exceeds max_records={MAX_RECORDS}")
        if record_size < 8 or record_size % 4 or record_size > header.record_end - offset:
            raise MetafileMalformedError(f"invalid EMF record size at offset={offset}: {record_size}")
        playback.set_record_context(record_type, record_index, offset)
        record = BoundedReader(memoryview(data)[offset : offset + record_size], base_offset=offset)
        if plus is not None and record_type == 70:
            plus.comment(record)
        elif plus is None or plus.gdi_active and not plus.halted and record_type not in {1, 14}:
            _handle_emf_record(record_type, record, playback)
        offset += record_size
        if record_type == 14:
            saw_eof = True
            break
    if not saw_eof:
        raise MetafileMalformedError("EMF record stream does not contain EMR_EOF")
    if plus is not None:
        plus.finish()


def _play_wmf(data: bytes, header: _HeaderInfo, playback: _Playback) -> None:
    """按文件顺序验证并回放全部 WMF records。"""
    offset = header.record_start
    record_index = 0
    saw_eof = False
    while offset < header.record_end:
        if offset + 6 > header.record_end:
            raise MetafileMalformedError(f"truncated WMF record header at offset={offset}")
        size_words = struct.unpack_from("<I", data, offset)[0]
        function = struct.unpack_from("<H", data, offset + 4)[0]
        record_size = size_words * 2
        record_index += 1
        if record_index > MAX_RECORDS:
            raise MetafileResourceLimitError(f"WMF exceeds max_records={MAX_RECORDS}")
        if size_words < 3 or record_size > header.record_end - offset:
            raise MetafileMalformedError(f"invalid WMF record size at offset={offset}: words={size_words}")
        playback.set_record_context(function, record_index, offset)
        payload = BoundedReader(memoryview(data)[offset + 6 : offset + record_size], base_offset=offset + 6)
        _handle_wmf_record(function, payload, playback)
        offset += record_size
        if function == 0:
            saw_eof = True
            break
    if not saw_eof:
        raise MetafileMalformedError("WMF record stream does not contain META_EOF")


__all__ = ["parse_metafile", "_play_emf", "_play_wmf"]
