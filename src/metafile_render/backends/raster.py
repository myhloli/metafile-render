# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF backends.raster 内部实现。"""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageFilter

from ..commands import ClearCommand, DrawImageCommand, DrawPathCommand, DrawTextCommand, MetafileDocument
from ..geometry import FlattenBudget
from ..limits import MAX_CANVAS_PIXELS, MAX_RENDER_WORK_PIXELS
from ..models import MetafileResourceLimitError
from .bitmap import _render_image_command
from .composite import _composite_image, _composite_path
from .mapping import _document_matrix
from .paths import _clip_mask, _render_path_command
from .text import _render_text_command


def _supersample_factor(document: MetafileDocument) -> int:
    """在画布、工作量预算内为纯矢量文档选择 1×、2× 或 4×。"""
    if any(isinstance(command, DrawImageCommand) for command in document.commands):
        return 1
    work_units = _render_work_units(document)
    for factor in (4, 2):
        width = document.width * factor
        height = document.height * factor
        if (
            width <= 8192
            and height <= 8192
            and width * height <= MAX_CANVAS_PIXELS
            and width * height * work_units <= MAX_RENDER_WORK_PIXELS
        ):
            return factor
    return 1


def _render_work_units(document: MetafileDocument) -> int:
    """按绘图命令及其逐项 clip mask 合成次数计算渲染工作单元。"""
    return max(sum(1 + len(command.clip) for command in document.commands), 1)


def _render_pillow_once(document: MetafileDocument, raster_scale: int) -> Image.Image:
    """按给定整数倍率执行一次不缩放的 Pillow 栅格化。"""
    width = document.width * raster_scale
    height = document.height * raster_scale
    render_work = width * height * _render_work_units(document)
    if render_work > MAX_RENDER_WORK_PIXELS:
        raise MetafileResourceLimitError(f"metafile exceeds max_render_work_pixels={MAX_RENDER_WORK_PIXELS}")
    matrix = _document_matrix(document, raster_scale=raster_scale)
    size = width, height
    canvas = Image.new("RGBA", size, (255, 255, 255, 0))
    budget = FlattenBudget()
    for command in document.commands:
        if isinstance(command, ClearCommand):
            mask = _clip_mask(command.clip, matrix, size, budget) if command.clip else None
            canvas.paste(command.color.rgba(), (0, 0, width, height), mask)
        elif isinstance(command, DrawPathCommand):
            _composite_path(canvas, _render_path_command(command, matrix, size, raster_scale, budget), command.rop2)
        elif isinstance(command, DrawTextCommand):
            canvas.alpha_composite(_render_text_command(command, matrix, size, budget))
        else:
            _composite_image(canvas, _render_image_command(command, matrix, size, budget), command.rop)
    return canvas


def render_pillow(document: MetafileDocument) -> Image.Image:
    """把统一图元文档以自适应超采样渲染为最终尺寸 RGBA 图片。"""
    raster_scale = _supersample_factor(document)
    canvas = _render_pillow_once(document, raster_scale)
    if raster_scale == 1:
        return canvas
    resized = canvas.resize((document.width, document.height), Image.Resampling.LANCZOS)
    return resized.filter(ImageFilter.UnsharpMask(radius=0.6, percent=100, threshold=2))


def _png_fallback_bytes(document: MetafileDocument, *, pixel_scale: int = 1) -> bytes:
    """生成指定像素密度、带对应 DPI metadata 的 PNG fallback。"""
    image = render_pillow(document) if pixel_scale == 1 else _render_pillow_once(document, pixel_scale)
    output = BytesIO()
    image.save(output, format="PNG", dpi=(96 * pixel_scale, 96 * pixel_scale))
    return output.getvalue()


__all__ = ["_supersample_factor", "_render_work_units", "_render_pillow_once", "render_pillow", "_png_fallback_bytes"]
