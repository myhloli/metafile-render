# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""确定性的字体样式选择及替代原因。"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from PIL import ImageFont

from .index import _font_aliases, _font_file_index


@dataclass(frozen=True, slots=True)
class FontResolution:
    """保存实际字体、家族和选择路径，供所有度量及绘制阶段复用。"""

    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    family: str
    style: str
    candidate: str
    reason: str | None


def _style_candidates(alias: str, weight: int, italic: bool) -> tuple[str, ...]:
    """按粗斜体组合生成候选，避免常规字体在样式候选前抢先命中。"""
    bold = weight >= 700
    if not bold and not italic:
        return (alias,)
    suffixes = ("BoldItalic", "BoldOblique") if bold and italic else ("Bold",) if bold else ("Italic", "Oblique")
    base = alias.removesuffix("-Regular").removesuffix("Regular")
    candidates = [variant for suffix in suffixes for variant in (base + suffix, base + "-" + suffix)]
    windows = {
        "arial": ("arialbd", "ariali", "arialbi"),
        "timesnewroman": ("timesbd", "timesi", "timesbi"),
        "couriernew": ("courbd", "couri", "courbi"),
        "calibri": ("calibrib", "calibrii", "calibriz"),
        "cambria": ("cambriab", "cambriai", "cambriaz"),
    }
    short = windows.get(alias.casefold().replace(" ", ""))
    if short is not None:
        candidates.insert(0, short[2 if bold and italic else 0 if bold else 1])
    return tuple(dict.fromkeys((*candidates, alias)))


@lru_cache(maxsize=256)
def resolve_font(face_name: str, size: int, weight: int, italic: bool, charset: int) -> FontResolution:
    """保持别名优先级，在每个家族内先选所需样式再使用常规替代。"""
    size = max(1, min(size, 4096))
    index = _font_file_index()
    for alias in _font_aliases(face_name, charset):
        for variant in _style_candidates(alias, weight, italic):
            key = variant.casefold().replace(" ", "")
            candidates = (index[key], variant, f"{variant}.ttf") if key in index else (variant, f"{variant}.ttf")
            for candidate in dict.fromkeys(candidates):
                try:
                    font = ImageFont.truetype(candidate, size)
                except OSError:
                    continue
                family_value, style_value = font.getname()
                family, style = family_value or alias, style_value or "Regular"
                replaced = family.casefold().replace(" ", "") != face_name.casefold().replace(" ", "")
                style_lower = style.casefold()
                missing_style = (weight >= 700 and "bold" not in style_lower) or (
                    italic and not any(value in style_lower for value in ("italic", "oblique"))
                )
                reason = "font family substituted" if replaced else "font style substituted" if missing_style else None
                return FontResolution(font, family, style, candidate, reason)
    fallback = ImageFont.load_default(size=size)
    return FontResolution(fallback, "Pillow default", "Regular", "Pillow default", "requested font unavailable")


__all__ = ["FontResolution", "resolve_font"]
