# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF parser 内部实现。"""

from __future__ import annotations

from .commands import MetafileDocument
from .context import ReplayContext
from .gdi.bridge import GdiBridge
from .gdi.emf import _handle_emf_record
from .gdi.playback import _Playback
from .gdi.wmf import _handle_wmf_record
from .headers import _detect_source_format, _HeaderInfo, _parse_emf_header, _parse_wmf_header
from .limits import MAX_METAFILE_BYTES
from .models import MetafileMalformedError, MetafileResourceLimitError
from .primitives import Rect
from .records import iter_records


def prepare_metafile(data: bytes, *, dpi: int, size_hint: tuple[int, int] | None) -> _HeaderInfo:
    """验证公共输入并准备两种后端共用的格式和尺寸信息。"""
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
    return header


def parse_metafile(
    data: bytes,
    *,
    dpi: int = 200,
    size_hint: tuple[int, int] | None = None,
    context: ReplayContext | None = None,
    prepared: _HeaderInfo | None = None,
) -> MetafileDocument:
    """检测并解析 WMF/EMF，返回跨后端统一图元文档。"""
    header = prepared if prepared is not None else prepare_metafile(data, dpi=dpi, size_hint=size_hint)
    playback = _Playback(header, context)
    if header.source_format == "emf":
        _play_emf(data, header, playback)
    else:
        _play_wmf(data, header, playback)
    return playback.finalize(size_hint)


def _play_emf(data: bytes, header: _HeaderInfo, playback: _Playback) -> None:
    """按文件顺序验证并回放全部 EMF records。"""
    from .emfplus.playback import EmfPlusPlayback

    plus = (
        EmfPlusPlayback(
            playback.context,
            header.bounds or Rect(0, 0, 1, 1),
            (header.device_pixels_per_mm[0] * 25.4, header.device_pixels_per_mm[1] * 25.4),
            GdiBridge(playback),
        )
        if header.emfplus_mode == "only"
        else None
    )
    for item in iter_records(data, header.record_start, header.record_end, "emf"):
        playback.set_record_context(item.kind, item.index, item.offset)
        if plus is not None and item.kind == 70:
            plus.comment(item.data)
        elif plus is None or plus.gdi_active and not plus.halted and item.kind not in {1, 14}:
            _handle_emf_record(item.kind, item.data, playback)
    if plus is not None:
        plus.finish()


def _play_wmf(data: bytes, header: _HeaderInfo, playback: _Playback) -> None:
    """按文件顺序验证并回放全部 WMF records。"""
    for item in iter_records(data, header.record_start, header.record_end, "wmf"):
        playback.set_record_context(item.kind, item.index, item.offset)
        payload = item.data.subreader(6, len(item.data) - 6)
        _handle_wmf_record(item.kind, payload, playback)


__all__ = ["prepare_metafile", "parse_metafile", "_play_emf", "_play_wmf"]
