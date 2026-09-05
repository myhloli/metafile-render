# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""隔离 EMF+ GetDC 窗口对 GDI 状态的临时修改。"""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from ..primitives import ClipStack, Matrix
from .state import GdiState


class GdiHost(Protocol):
    """桥接器所需的最小 GDI 状态接口。"""

    state: GdiState
    state_stack: list[GdiState]


class GdiBridge:
    """保存并恢复一次 GetDC 窗口的状态及嵌套栈。"""

    def __init__(self, host: GdiHost) -> None:
        """绑定 GDI 主机，不向 EMF+ 暴露其可变字段。"""
        self.host = host
        self.saved_state: GdiState | None = None
        self.saved_stack: list[GdiState] = []

    def enter(self, matrix: Matrix, clip: ClipStack) -> None:
        """使用 EMF+ 坐标和裁剪开始新的原生 EMF 绘制窗口。"""
        self.leave()
        self.saved_state = replace(self.host.state)
        self.saved_stack = list(self.host.state_stack)
        self.host.state_stack = []
        self.host.state = replace(self.host.state, world_transform=matrix, clip=clip)

    def leave(self) -> None:
        """关闭窗口并精确恢复窗口前的 GDI 状态。"""
        if self.saved_state is not None:
            self.host.state = self.saved_state
            self.host.state_stack = self.saved_stack
            self.saved_state = None
            self.saved_stack = []


__all__ = ["GdiBridge", "GdiHost"]
