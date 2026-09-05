# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""有界诊断收集。"""

from __future__ import annotations

from .limits import MAX_DIAGNOSTICS
from .models import MetafileDiagnostic


class _DiagnosticSink:
    """收集有界诊断并聚合超出预算的重复项目。"""

    def __init__(self) -> None:
        """创建空诊断列表和截断计数器。"""
        self.items: list[MetafileDiagnostic] = []
        self.omitted = 0

    def add(
        self,
        code: str,
        level: str,
        message: str,
        *,
        record_type: int | None = None,
        record_index: int | None = None,
        offset: int | None = None,
    ) -> None:
        """在预算内追加诊断，超出时只累计省略数量。"""
        if len(self.items) >= MAX_DIAGNOSTICS:
            self.omitted += 1
            return
        normalized_level = level if level in {"info", "warning", "error"} else "warning"
        self.items.append(
            MetafileDiagnostic(
                code=code,
                level=normalized_level,  # type: ignore[arg-type]
                message=message,
                record_type=record_type,
                record_index=record_index,
                offset=offset,
            )
        )

    def freeze(self) -> tuple[MetafileDiagnostic, ...]:
        """冻结诊断，并在需要时追加一条截断摘要。"""
        if self.omitted:
            self.items.append(
                MetafileDiagnostic(
                    code="diagnostics_truncated",
                    level="warning",
                    message=f"omitted {self.omitted} additional metafile diagnostics",
                )
            )
        return tuple(self.items)


__all__ = ["_DiagnosticSink"]
