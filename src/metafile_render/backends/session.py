# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""单次转换内有界复用图片、裁剪及路径展平预算。"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Hashable
from types import TracebackType

from PIL import Image

from ..commands import DrawTextCommand
from ..context import ReplayContext
from ..fonts.resolve import FontResolution, resolve_font
from ..geometry import FlattenBudget

MAX_CACHE_BYTES = 64 * 1024 * 1024


class ImageCache:
    """按像素存储量计费并淘汰最久未使用的图片副本。"""

    def __init__(self, limit: int = MAX_CACHE_BYTES) -> None:
        """创建独立缓存，不跨文档保留载荷。"""
        self.limit = limit
        self.used = 0
        self.entries: OrderedDict[Hashable, Image.Image] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: Hashable) -> Image.Image | None:
        """返回副本，确保后续透明度修改不会污染缓存。"""
        value = self.entries.get(key)
        if value is None:
            self.misses += 1
            return None
        self.hits += 1
        self.entries.move_to_end(key)
        return value.copy()

    def put(self, key: Hashable, image: Image.Image) -> None:
        """在复制图片之前检查预算，必要时淘汰旧项。"""
        size = image.width * image.height * (1 if image.mode == "L" else 4)
        if size > self.limit:
            return
        if key in self.entries:
            self._discard(key)
        while self.used + size > self.limit and self.entries:
            self._discard(next(iter(self.entries)))
        self.entries[key] = image.copy()
        self.used += size

    def _discard(self, key: Hashable) -> None:
        """释放一项缓存及其计费空间。"""
        image = self.entries.pop(key)
        self.used -= image.width * image.height * (1 if image.mode == "L" else 4)
        image.close()

    def clear(self) -> None:
        """立即释放所有缓存图片。"""
        while self.entries:
            self._discard(next(iter(self.entries)))


class RenderSession:
    """向多个编码阶段提供共享缓存、展平预算和诊断上下文。"""

    def __init__(self, context: ReplayContext | None = None, *, cache_limit: int = MAX_CACHE_BYTES) -> None:
        """初始化一次转换的可释放资源。"""
        self.context = context
        self.cache = ImageCache(cache_limit)
        self.flatten = FlattenBudget()
        self.reported_fonts: set[tuple[str, str]] = set()

    def font(self, command: DrawTextCommand, size: int) -> FontResolution:
        """共享字体选择，并按来源位置报告一次字体替代。"""
        requested = command.font
        resolved = resolve_font(requested.face_name, size, requested.weight, requested.italic, requested.charset)
        key = (requested.face_name, resolved.family + resolved.style)
        if resolved.reason is not None and key not in self.reported_fonts:
            self.reported_fonts.add(key)
            if self.context is not None:
                self.context.report(
                    "font_substituted",
                    f"{requested.face_name} -> {resolved.family} {resolved.style}: {resolved.reason}",
                    location=command.location,
                )
        return resolved

    def __enter__(self) -> RenderSession:
        """进入单次渲染会话。"""
        return self

    def __exit__(
        self, error_type: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None
    ) -> None:
        """成功或失败后均释放缓存，不吞掉异常。"""
        self.cache.clear()


__all__ = ["ImageCache", "RenderSession", "MAX_CACHE_BYTES"]
