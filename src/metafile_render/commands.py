# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""内部绘图命令与文档。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from .location import RecordLocation
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
    location: RecordLocation | None = None


@dataclass(frozen=True, slots=True)
class TextAlignment:
    """使用具名语义隔离来源格式与绘制后端。"""

    horizontal: Literal["left", "center", "right"] = "left"
    vertical: Literal["top", "bottom", "baseline"] = "top"

    @classmethod
    def from_gdi(cls, value: int) -> TextAlignment:
        """仅在回放边界将 GDI 对齐标志转换为内部语义。"""
        horizontal: Literal["left", "center", "right"] = "center" if value & 6 == 6 else "right" if value & 2 else "left"
        vertical: Literal["top", "bottom", "baseline"] = "baseline" if value & 24 == 24 else "bottom" if value & 8 else "top"
        return cls(horizontal, vertical)


@dataclass(frozen=True, slots=True)
class DrawTextCommand:
    """保存一次 GDI 文字输出及其显式字符位置。"""

    text: str
    origin: Point
    positions: tuple[Point, ...]
    font: Font
    font_height: float
    rotation: float
    alignment: TextAlignment
    color: Color
    background_color: Color
    opaque: bool
    bounds: Rect | None
    clip: ClipStack
    advance_end: Point | None = None
    location: RecordLocation | None = None


@dataclass(frozen=True, slots=True)
class DibPayload:
    """保存 GDI 位图的头部、像素和源 alpha 语义。"""

    header: bytes
    bits: bytes
    use_source_alpha: bool = False


@dataclass(frozen=True, slots=True)
class EncodedImage:
    """保存已验证的规范 PNG 图片载荷。"""

    png: bytes


ImagePayload: TypeAlias = DibPayload | EncodedImage


@dataclass(frozen=True, slots=True)
class DrawImageCommand:
    """保存统一图片载荷及其位置、裁剪和合成参数。"""

    image: ImagePayload
    destination: tuple[Point, Point, Point, Point]
    source: Rect | None
    rop: int
    stretch_mode: int = 3
    constant_alpha: int = 255
    clip: ClipStack = ()
    location: RecordLocation | None = None


@dataclass(frozen=True, slots=True)
class ClearCommand:
    """保存设备画布清屏操作，允许清除已有透明度。"""

    color: Color
    bounds: Rect
    clip: ClipStack = ()
    location: RecordLocation | None = None


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


__all__ = [
    "DibPayload",
    "EncodedImage",
    "ImagePayload",
    "TextAlignment",
    "DrawPathCommand",
    "DrawTextCommand",
    "DrawImageCommand",
    "ClearCommand",
    "DrawCommand",
    "MetafileDocument",
]
