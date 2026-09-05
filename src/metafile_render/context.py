# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""共享命令、资源预算和跨阶段诊断上下文。"""

from __future__ import annotations

from dataclasses import replace

from .budget import ResourceBudget
from .commands import DibPayload, DrawCommand, DrawImageCommand
from .dib import validate_dib_payload
from .limits import MAX_DIAGNOSTICS
from .location import RecordLocation
from .models import DiagnosticLevel, MetafileDiagnostic


class DiagnosticSink:
    """有界收集诊断，冻结操作可重复且没有副作用。"""

    def __init__(self) -> None:
        """创建空诊断列表和省略计数。"""
        self.items: list[MetafileDiagnostic] = []
        self.omitted = 0

    def add(
        self,
        code: str,
        level: DiagnosticLevel,
        message: str,
        *,
        record_type: int | None = None,
        record_index: int | None = None,
        offset: int | None = None,
    ) -> None:
        """在预算内保存诊断，超限后仅累计数量。"""
        if len(self.items) >= MAX_DIAGNOSTICS:
            self.omitted += 1
            return
        self.items.append(MetafileDiagnostic(code, level, message, record_type, record_index, offset))

    def freeze(self) -> tuple[MetafileDiagnostic, ...]:
        """返回不可变快照，并仅在快照中添加省略摘要。"""
        result = tuple(self.items)
        if self.omitted:
            result += (
                MetafileDiagnostic(
                    "diagnostics_truncated", "warning", f"omitted {self.omitted} additional metafile diagnostics"
                ),
            )
        return result


class ReplayContext:
    """向各格式回放器提供不含 GDI 状态的共享能力。"""

    def __init__(self, diagnostics: DiagnosticSink | None = None) -> None:
        """初始化单次转换所有阶段共用的命令和计费状态。"""
        self.diagnostics = diagnostics if diagnostics is not None else DiagnosticSink()
        self.budget = ResourceBudget()
        self.commands: list[DrawCommand] = []
        self.partial = False
        self.location = RecordLocation()

    def set_record_context(self, record_type: int, record_index: int, offset: int) -> None:
        """更新后续命令和诊断使用的来源记录。"""
        self.location = RecordLocation(record_type, record_index, offset)

    def warn(self, code: str, message: str, *, partial: bool = True) -> None:
        """追加可定位警告并按需标记部分输出。"""
        self.partial |= partial
        self.report(code, message, level="warning")

    def report(
        self, code: str, message: str, *, level: DiagnosticLevel = "info", location: RecordLocation | None = None
    ) -> None:
        """供解析及后端报告诊断，不隐式改变部分结果状态。"""
        where = location if location is not None else self.location
        self.diagnostics.add(
            code, level, message, record_type=where.record_type, record_index=where.record_index, offset=where.offset
        )

    def charge_points(self, count: int) -> None:
        """把几何复杂度交给共享预算计费。"""
        self.budget.charge_points(count)

    def validate_command(self, command: DrawCommand) -> None:
        """在两个后端都执行输入载荷计费，不受缓存或命令存储方式影响。"""
        self.budget.charge_command(len(command.clip))
        if isinstance(command, DrawImageCommand) and isinstance(command.image, DibPayload):
            self.budget.charge_decoded_bytes(validate_dib_payload(command.image))

    def append_command(self, command: DrawCommand) -> None:
        """检查累计预算并为新命令附加来源位置。"""
        self.validate_command(command)
        self.commands.append(replace(command, location=self.location))


__all__ = ["DiagnosticSink", "ReplayContext"]
