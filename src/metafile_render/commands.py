# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""内部绘图命令与文档。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from .models import EmfPlusMode, MetafileDiagnostic, MetafileSourceFormat
from .primitives import Brush, ClipStack, Color, Font, GraphicsPath, Pen, Point, Rect


@dataclass(frozen=True, slots=True)
class DrawPathCommand:
    """保存一次路径填充和描边操作。"""

    path: GraphicsPath
    pen: Pen
    brush: Brush
    stroke: bool
    fill: bool
    fill_rule: Literal["evenodd", "nonzero"]
    clip: ClipStack
    rop2: int
    miter_limit: float = 10.0


@dataclass(frozen=True, slots=True)
class DrawTextCommand:
    """保存一次 GDI 文字输出及其显式字符位置。"""

    text: str
    origin: Point
    positions: tuple[Point, ...]
    font: Font
    font_height: float
    rotation: float
    text_align: int
    color: Color
    background_color: Color
    opaque: bool
    bounds: Rect | None
    clip: ClipStack
    advance_end: Point | None = None


@dataclass(frozen=True, slots=True)
class DrawImageCommand:
    """保存一次 DIB/位图绘制及其 GDI 合成参数。"""

    dib_header: bytes
    bits: bytes
    destination: tuple[Point, Point, Point, Point]
    source: Rect | None
    rop: int
    stretch_mode: int = 3
    constant_alpha: int = 255
    use_source_alpha: bool = False
    clip: ClipStack = ()
    encoded_png: bytes | None = None


@dataclass(frozen=True, slots=True)
class ClearCommand:
    """保存设备画布清屏操作，允许清除已有透明度。"""

    color: Color
    bounds: Rect
    clip: ClipStack = ()


DrawCommand: TypeAlias = DrawPathCommand | DrawTextCommand | DrawImageCommand | ClearCommand


@dataclass(frozen=True, slots=True)
class MetafileDocument:
    """保存解析完成、可被多个后端消费的统一图元文档。"""

    source_format: MetafileSourceFormat
    emfplus_mode: EmfPlusMode
    bounds: Rect
    width: int
    height: int
    commands: tuple[DrawCommand, ...]
    diagnostics: tuple[MetafileDiagnostic, ...]
    partial: bool


__all__ = ["DrawPathCommand", "DrawTextCommand", "DrawImageCommand", "ClearCommand", "DrawCommand", "MetafileDocument"]
