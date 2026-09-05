# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""原生调用前复用已实现记录的校验，而不保存完整绘图命令。"""

from __future__ import annotations

from .commands import DrawCommand
from .context import ReplayContext
from .gdi.emf import _handle_emf_record
from .gdi.playback import _Playback
from .gdi.wmf import _handle_wmf_record
from .headers import _HeaderInfo
from .records import iter_records


class ValidationPlayback(_Playback):
    """复用记录解码和状态校验，仅保留预算与当前状态。"""

    def append_command(self, command: DrawCommand) -> None:
        """检查图元及图片载荷预算后丢弃临时命令，避免创建完整 IR。"""
        self.context.validate_command(command)


def validate_for_native(data: bytes, header: _HeaderInfo) -> int:
    """原生渲染也必须消费至 EOF 并验证已知记录，未知合法记录留给系统处理。"""
    playback = ValidationPlayback(header, ReplayContext())
    for record in iter_records(data, header.record_start, header.record_end, header.source_format):
        playback.set_record_context(record.kind, record.index, record.offset)
        if header.source_format == "emf":
            _handle_emf_record(record.kind, record.data, playback)
        else:
            _handle_wmf_record(record.kind, record.data.subreader(6, len(record.data) - 6), playback)
    return max(playback.context.budget.commands + playback.context.budget.clip_operations, 1)


__all__ = ["validate_for_native"]
