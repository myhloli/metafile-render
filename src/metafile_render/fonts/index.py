# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""平台字体目录索引和固定别名顺序。"""

from __future__ import annotations

import platform
from functools import lru_cache
from pathlib import Path


def _font_search_roots() -> tuple[Path, ...]:
    """返回当前平台常见字体目录，不在模块导入时访问文件系统。"""
    system = platform.system()
    roots: list[Path] = []
    if system == "Darwin":
        roots.extend((Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library/Fonts"))
    elif system == "Windows":
        import os

        windows_dir = Path(os.environ.get("WINDIR", "C:/Windows"))
        roots.append(windows_dir / "Fonts")
    else:
        roots.extend(
            (
                Path("/usr/share/fonts"),
                Path("/usr/local/share/fonts"),
                Path.home() / ".fonts",
                Path.home() / ".local/share/fonts",
            )
        )
    return tuple(root for root in roots if root.is_dir())


@lru_cache(maxsize=1)
def _font_file_index() -> dict[str, str]:
    """惰性建立字体文件名索引，避免 import 时扫描系统目录。"""
    index: dict[str, str] = {}
    for root in _font_search_roots():
        try:
            candidates = sorted(root.rglob("*"))
            for candidate in candidates:
                if candidate.suffix.lower() not in {".ttf", ".ttc", ".otf"}:
                    continue
                index.setdefault(candidate.stem.casefold().replace(" ", ""), str(candidate))
        except OSError:
            continue
    return index


def _font_aliases(face_name: str, charset: int) -> tuple[str, ...]:
    """返回 Windows 字体名在 Linux/macOS 上的固定替代顺序。"""
    normalized = face_name.casefold().replace(" ", "")
    alias_map = {
        "arial": ("Arial", "LiberationSans-Regular", "DejaVuSans"),
        "calibri": ("Calibri", "Carlito-Regular", "Arial", "DejaVuSans"),
        "cambria": ("Cambria", "Caladea-Regular", "DejaVuSerif"),
        "timesnewroman": ("Times New Roman", "LiberationSerif-Regular", "DejaVuSerif"),
        "couriernew": ("Courier New", "LiberationMono-Regular", "DejaVuSansMono"),
        "simsun": ("SimSun", "Songti SC", "NotoSerifCJKsc-Regular", "DejaVuSans"),
        "microsoftyahei": ("Microsoft YaHei", "PingFang SC", "NotoSansCJKsc-Regular", "DejaVuSans"),
    }
    aliases = alias_map.get(normalized, (face_name,))
    if charset in {128, 129, 134, 136}:
        aliases = (*aliases, "PingFang SC", "NotoSansCJKsc-Regular", "NotoSansCJK-Regular", "DejaVuSans")
    return tuple(dict.fromkeys((*aliases, "DejaVuSans")))


__all__ = ["_font_file_index", "_font_aliases"]
