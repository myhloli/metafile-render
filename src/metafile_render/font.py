# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""共享字体加载入口和文字度量。"""

from __future__ import annotations

from PIL import ImageFont

from .fonts.resolve import resolve_font
from .primitives import Font


def load_font(
    face_name: str, size: int, weight: int, italic: bool, charset: int
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """使用具名解析结果加载与度量阶段一致的字体。"""
    return resolve_font(face_name, size, weight, italic, charset).font


def font_metrics(font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> tuple[int, int]:
    """为 FreeType 或 Pillow 默认字体提供统一的上升与下降度量。"""
    if isinstance(font, ImageFont.FreeTypeFont):
        return font.getmetrics()
    bounds = font.getbbox("Hg")
    return max(1, bounds[3] - bounds[1]), 0


def measure_text_advance(font: Font, text: str) -> float:
    """用 renderer 同款字体回退规则估算无显式 spacing 的逻辑 advance。"""
    if not text:
        return 0.0
    font_size = max(1, round(abs(font.height or -12.0)))
    loaded = load_font(font.face_name, font_size, font.weight, font.italic, font.charset)
    advance = float(loaded.getlength(text))
    if font.width:
        natural_width = max(float(loaded.getlength("0")), 1e-9)
        advance *= abs(font.width) / natural_width
    return max(advance, 0.0)


__all__ = ["load_font", "measure_text_advance", "font_metrics"]
