# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF gdi.state 内部实现。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..primitives import BLACK, WHITE, Brush, ClipStack, Color, Font, Matrix, Pen, Point


@dataclass(slots=True)
class GdiState:
    """保存 WMF/EMF 当前 playback device context。"""

    pen: Pen = field(default_factory=Pen)
    brush: Brush = field(default_factory=Brush)
    font: Font = field(default_factory=Font)
    text_color: Color = BLACK
    background_color: Color = WHITE
    background_mode: int = 2
    text_align: int = 0
    map_mode: int = 1
    window_origin: Point = (0.0, 0.0)
    window_extent: Point = (1.0, 1.0)
    viewport_origin: Point = (0.0, 0.0)
    viewport_extent: Point = (1.0, 1.0)
    world_transform: Matrix = field(default_factory=Matrix)
    current_position: Point = (0.0, 0.0)
    polygon_fill_mode: int = 1
    rop2: int = 13
    stretch_mode: int = 1
    arc_direction: int = 2
    miter_limit: float = 10.0
    brush_origin: Point = (0.0, 0.0)
    clip: ClipStack = ()
    device_pixels_per_mm: Point = (96.0 / 25.4, 96.0 / 25.4)


__all__ = ["GdiState"]
