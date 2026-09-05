# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""固定预算下的画布尺寸计算。"""

from __future__ import annotations

from . import limits
from .commands import DrawImageCommand, MetafileDocument
from .limits import MAX_CANVAS_DIMENSION, MAX_CANVAS_PIXELS
from .models import MetafileMalformedError, MetafileResourceLimitError


def _bounded_canvas_size(requested: tuple[int, int]) -> tuple[int, int, bool]:
    """按单边和总像素预算等比收缩画布尺寸。"""
    width, height = requested
    if width <= 0 or height <= 0:
        raise MetafileMalformedError(f"metafile canvas must be positive: {width}x{height}")
    scale = min(1.0, MAX_CANVAS_DIMENSION / width, MAX_CANVAS_DIMENSION / height)
    if width * height * scale * scale > MAX_CANVAS_PIXELS:
        scale = min(scale, (MAX_CANVAS_PIXELS / (width * height)) ** 0.5)
    bounded_width = max(1, round(width * scale))
    bounded_height = max(1, round(height * scale))
    return bounded_width, bounded_height, scale < 1.0


__all__ = ["_bounded_canvas_size", "render_work_units", "choose_raster_scale", "check_render_size"]


def render_work_units(document: MetafileDocument) -> int:
    """统一估算命令与逐层裁剪的渲染工作量，不依赖缓存命中。"""
    return max(sum(1 + len(command.clip) for command in document.commands), 1)


def choose_raster_scale(document: MetafileDocument, *, maximum: int) -> int:
    """在相同画布与工作预算下选择普通渲染或 SVG fallback 倍率。"""
    if any(isinstance(command, DrawImageCommand) for command in document.commands):
        return 1
    work = render_work_units(document)
    for factor in (4, 2):
        if factor > maximum:
            continue
        width, height = document.width * factor, document.height * factor
        if (
            max(width, height) <= limits.MAX_CANVAS_DIMENSION
            and width * height <= limits.MAX_CANVAS_PIXELS
            and width * height * work <= limits.MAX_RENDER_WORK_PIXELS
        ):
            return factor
    return 1


def check_render_size(width: int, height: int, work_units: int = 1) -> None:
    """在分配像素之前统一验证输出画布和渲染工作量。"""
    if (
        width <= 0
        or height <= 0
        or max(width, height) > limits.MAX_CANVAS_DIMENSION
        or width * height > limits.MAX_CANVAS_PIXELS
    ):
        raise MetafileResourceLimitError("metafile render dimensions exceed canvas budget")
    if width * height * work_units > limits.MAX_RENDER_WORK_PIXELS:
        raise MetafileResourceLimitError(f"metafile exceeds max_render_work_pixels={limits.MAX_RENDER_WORK_PIXELS}")
