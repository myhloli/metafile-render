# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF backends.raster 内部实现。"""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageFilter

from ..commands import ClearCommand, DrawPathCommand, DrawTextCommand, MetafileDocument
from ..sizing import check_render_size, choose_raster_scale, render_work_units
from .bitmap import _render_image_command
from .composite import _composite_image, _composite_path
from .mapping import _document_matrix
from .paths import _clip_mask, _render_path_command
from .session import RenderSession
from .text import _render_text_command


def _supersample_factor(document: MetafileDocument) -> int:
    """在画布、工作量预算内为纯矢量文档选择 1×、2× 或 4×。"""
    return choose_raster_scale(document, maximum=4)


def _render_work_units(document: MetafileDocument) -> int:
    """返回与倍率选择一致的保守工作量估计。"""
    return render_work_units(document)


def _render_pillow_once(document: MetafileDocument, raster_scale: int, session: RenderSession | None = None) -> Image.Image:
    """按给定整数倍率执行一次不缩放的 Pillow 栅格化。"""
    width = document.width * raster_scale
    height = document.height * raster_scale
    check_render_size(width, height, render_work_units(document))
    session = session if session is not None else RenderSession()
    matrix = _document_matrix(document, raster_scale=raster_scale)
    size = width, height
    canvas = Image.new("RGBA", size, (255, 255, 255, 0))
    budget = session.flatten
    for command in document.commands:
        if isinstance(command, ClearCommand):
            mask = _clip_mask(command.clip, matrix, size, budget, session) if command.clip else None
            canvas.paste(command.color.rgba(), (0, 0, width, height), mask)
        elif isinstance(command, DrawPathCommand):
            _composite_path(canvas, _render_path_command(command, matrix, size, raster_scale, budget, session), command.rop2)
        elif isinstance(command, DrawTextCommand):
            canvas.alpha_composite(_render_text_command(command, matrix, size, budget, session))
        else:
            _composite_image(canvas, _render_image_command(command, matrix, size, budget, session), command.rop)
    return canvas


def render_pillow(document: MetafileDocument, session: RenderSession | None = None) -> Image.Image:
    """把统一图元文档以自适应超采样渲染为最终尺寸 RGBA 图片。"""
    raster_scale = _supersample_factor(document)
    canvas = _render_pillow_once(document, raster_scale, session)
    if raster_scale == 1:
        return canvas
    resized = canvas.resize((document.width, document.height), Image.Resampling.LANCZOS)
    return resized.filter(ImageFilter.UnsharpMask(radius=0.6, percent=100, threshold=2))


def _png_fallback_bytes(document: MetafileDocument, *, pixel_scale: int = 1, session: RenderSession | None = None) -> bytes:
    """生成指定像素密度、带对应 DPI metadata 的 PNG fallback。"""
    image = render_pillow(document, session) if pixel_scale == 1 else _render_pillow_once(document, pixel_scale, session)
    output = BytesIO()
    image.save(output, format="PNG", dpi=(96 * pixel_scale, 96 * pixel_scale))
    return output.getvalue()


__all__ = ["_supersample_factor", "_render_work_units", "_render_pillow_once", "render_pillow", "_png_fallback_bytes"]
