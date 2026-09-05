# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""共享几何与图形样式。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

Point: TypeAlias = tuple[float, float]


@dataclass(frozen=True, slots=True)
class Color:
    """保存标准 RGBA 颜色分量。"""

    red: int
    green: int
    blue: int
    alpha: int = 255

    def rgba(self) -> tuple[int, int, int, int]:
        """返回 Pillow 可直接消费的 RGBA 元组。"""
        return self.red, self.green, self.blue, self.alpha

    def svg(self) -> str:
        """返回不含透明度的 SVG 十六进制颜色。"""
        return f"#{self.red:02x}{self.green:02x}{self.blue:02x}"


BLACK = Color(0, 0, 0)


WHITE = Color(255, 255, 255)


TRANSPARENT = Color(0, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class Rect:
    """保存规范化前后均可使用的浮点矩形。"""

    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self) -> float:
        """返回矩形的有符号宽度。"""
        return self.right - self.left

    @property
    def height(self) -> float:
        """返回矩形的有符号高度。"""
        return self.bottom - self.top

    def normalized(self) -> Rect:
        """返回左右、上下均按升序排列的矩形。"""
        return Rect(
            min(self.left, self.right),
            min(self.top, self.bottom),
            max(self.left, self.right),
            max(self.top, self.bottom),
        )


@dataclass(frozen=True, slots=True)
class Matrix:
    """保存二维仿射变换，采用 SVG/Pillow 常用的六参数表示。"""

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    def transform_point(self, point: Point) -> Point:
        """把单点应用到当前仿射变换。"""
        x, y = point
        return self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f

    def transform_vector(self, vector: Point) -> Point:
        """只应用线性部分，避免平移污染长度和方向。"""
        x, y = vector
        return self.a * x + self.c * y, self.b * x + self.d * y

    def then(self, following: Matrix) -> Matrix:
        """返回先应用当前矩阵、再应用 following 的组合矩阵。"""
        return Matrix(
            a=following.a * self.a + following.c * self.b,
            b=following.b * self.a + following.d * self.b,
            c=following.a * self.c + following.c * self.d,
            d=following.b * self.c + following.d * self.d,
            e=following.a * self.e + following.c * self.f + following.e,
            f=following.b * self.e + following.d * self.f + following.f,
        )


@dataclass(frozen=True, slots=True)
class Pen:
    """保存已经解析的 GDI 画笔。"""

    color: Color = BLACK
    width: float = 1.0
    style: int = 0
    cosmetic: bool = True
    null: bool = False
    cap: Literal["round", "square", "flat"] = "round"
    join: Literal["round", "bevel", "miter"] = "round"
    dashes: tuple[float, ...] = ()


@dataclass(frozen=True, slots=True)
class Brush:
    """保存已经解析的 GDI 画刷。"""

    kind: Literal["solid", "null", "hatch", "pattern"] = "solid"
    color: Color = WHITE
    hatch: int = 0
    pattern: bytes | None = None


@dataclass(frozen=True, slots=True)
class Font:
    """保存 GDI LOGFONT 中与跨平台绘制有关的字段。"""

    face_name: str = "Arial"
    height: float = -12.0
    width: float = 0.0
    weight: int = 400
    italic: bool = False
    underline: bool = False
    strikeout: bool = False
    charset: int = 1
    escapement: float = 0.0
    orientation: float = 0.0


@dataclass(frozen=True, slots=True)
class PathSegment:
    """保存单个 move/line/cubic/close 路径片段。"""

    verb: Literal["M", "L", "C", "Z"]
    points: tuple[Point, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphicsPath:
    """保存不可变的跨后端绘图路径。"""

    segments: tuple[PathSegment, ...]


@dataclass(frozen=True, slots=True)
class ClipOperation:
    """保存按 GDI 顺序应用的单次裁剪区域操作。"""

    path: GraphicsPath
    mode: Literal["and", "or", "xor", "diff", "copy"]
    fill_rule: Literal["evenodd", "nonzero"] = "evenodd"


ClipStack: TypeAlias = tuple[ClipOperation, ...]


__all__ = [
    "Point",
    "Color",
    "BLACK",
    "WHITE",
    "TRANSPARENT",
    "Rect",
    "Matrix",
    "Pen",
    "Brush",
    "Font",
    "PathSegment",
    "GraphicsPath",
    "ClipOperation",
    "ClipStack",
]
