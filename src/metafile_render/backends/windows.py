# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""通过 Pillow 在 Windows 上执行受尺寸约束的原生 GDI 渲染。"""

from __future__ import annotations

import sys
from io import BytesIO
from threading import RLock

from PIL import Image, WmfImagePlugin

from ..sizing import check_render_size

# Pillow 的内置 handler 在 open 时保存共享 bbox；锁覆盖整个 open/load 窗口。
_NATIVE_LOCK = RLock()


def is_windows() -> bool:
    """仅在调用时判断当前操作系统。"""
    return sys.platform == "win32"


def available() -> bool:
    """检测 Pillow 构建是否包含 Windows 原生 metafile 能力。"""
    return is_windows() and hasattr(Image.core, "drawwmf")


def render_native(data: bytes, size: tuple[int, int], dpi: int) -> Image.Image:
    """在任何像素分配前设定已校验尺寸，并返回独立白底 RGB 图片。"""
    check_render_size(*size)
    if not available():
        raise OSError("Windows native metafile renderer is unavailable")
    with _NATIVE_LOCK:
        with Image.open(BytesIO(data)) as image:
            if not isinstance(image, WmfImagePlugin.WmfStubImageFile):
                raise OSError("Pillow did not select its WMF/EMF loader")
            # Pillow 11.0 的 EMF load(dpi=...) 不调整尺寸；局部适配器统一设置实例尺寸。
            image._size = size
            image.info["dpi"] = (dpi, dpi)
            image.load()
            if image.size != size:
                raise OSError("native metafile renderer returned an unexpected canvas size")
            return image.convert("RGB")


__all__ = ["available", "is_windows", "render_native"]
