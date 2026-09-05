# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""固定预算下的画布尺寸计算。"""

from __future__ import annotations

from .limits import MAX_CANVAS_DIMENSION, MAX_CANVAS_PIXELS
from .models import MetafileMalformedError


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


__all__ = ["_bounded_canvas_size"]
