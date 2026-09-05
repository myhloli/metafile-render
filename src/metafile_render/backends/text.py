# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF backends.text 内部实现。"""

from __future__ import annotations

from math import ceil, hypot

from PIL import Image, ImageDraw, ImageFont

from ..commands import DrawTextCommand
from ..font import load_font
from ..geometry import FlattenBudget
from ..primitives import Matrix, Point
from .constants import _TA_BASELINE, _TA_BOTTOM, _TA_CENTER, _TA_RIGHT
from .mapping import _mapped_rect
from .paths import _apply_clip


def _text_anchor(text_align: int) -> str:
    """把 GDI TextAlignmentMode 转换为 Pillow anchor。"""
    horizontal = "r" if text_align & _TA_CENTER == _TA_CENTER else "r" if text_align & _TA_RIGHT else "l"
    if text_align & _TA_CENTER == _TA_CENTER:
        horizontal = "m"
    vertical = "s" if text_align & _TA_BASELINE == _TA_BASELINE else "d" if text_align & _TA_BOTTOM else "a"
    return horizontal + vertical


def _aligned_text_positions(
    command: DrawTextCommand,
    matrix: Matrix,
    positions: list[Point],
) -> tuple[list[Point], bool]:
    """把显式字符位置按整串 CENTER/RIGHT alignment 平移一次。"""
    if not command.positions or command.advance_end is None:
        return positions, False
    factor = 0.5 if command.text_align & _TA_CENTER == _TA_CENTER else 1.0 if command.text_align & _TA_RIGHT else 0.0
    mapped_origin = matrix.transform_point(command.origin)
    mapped_end = matrix.transform_point(command.advance_end)
    offset_x = (mapped_end[0] - mapped_origin[0]) * factor
    offset_y = (mapped_end[1] - mapped_origin[1]) * factor
    return [(position[0] - offset_x, position[1] - offset_y) for position in positions], True


def _draw_rotated_text(
    layer: Image.Image,
    position: Point,
    text: str,
    *,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
    anchor: str,
    rotation: float,
    underline: bool,
    strikeout: bool,
) -> tuple[int, int]:
    """在指定 anchor 绘制可旋转文字，并返回未旋转文字尺寸。"""
    draw = ImageDraw.Draw(layer)
    bbox = draw.textbbox((0, 0), text, font=font, anchor=anchor)
    width = max(1, bbox[2] - bbox[0])
    height = max(1, bbox[3] - bbox[1])
    line_width = max(1, round(height / 14.0))

    def draw_decorations(target_draw: ImageDraw.ImageDraw, reference: Point) -> None:
        """围绕同一文字 reference 绘制下划线和删除线。"""
        left = reference[0] + bbox[0]
        right = reference[0] + bbox[2]
        if underline:
            y = reference[1] + bbox[3] + line_width
            target_draw.line((left, y, right, y), fill=fill, width=line_width)
        if strikeout:
            y = reference[1] + (bbox[1] + bbox[3]) / 2.0
            target_draw.line((left, y, right, y), fill=fill, width=line_width)

    if abs(rotation) < 1e-6:
        draw.text(position, text, font=font, fill=fill, anchor=anchor)
        draw_decorations(draw, position)
        return width, height
    padding = max(4, ceil(max(width, height) * 0.1))
    bottom = bbox[3] + line_width * 2 if underline else bbox[3]
    radius = ceil(max(hypot(x, y) for x in (bbox[0], bbox[2]) for y in (bbox[1], bottom))) + padding
    patch = Image.new("RGBA", (radius * 2 + 1, radius * 2 + 1), (0, 0, 0, 0))
    patch_draw = ImageDraw.Draw(patch)
    reference = float(radius), float(radius)
    patch_draw.text(reference, text, font=font, fill=fill, anchor=anchor)
    draw_decorations(patch_draw, reference)
    rotated = patch.rotate(-rotation, expand=False, resample=Image.Resampling.BICUBIC, center=reference)
    target = round(position[0] - radius), round(position[1] - radius)
    layer.alpha_composite(rotated, target)
    return width, height


def _render_text_command(
    command: DrawTextCommand,
    matrix: Matrix,
    size: tuple[int, int],
    budget: FlattenBudget,
) -> Image.Image:
    """把单条文字命令绘制到独立透明 RGBA layer。"""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    scale_y = max(abs(matrix.d), 1e-9)
    font_size = max(1, round(command.font_height * scale_y))
    font = load_font(command.font.face_name, font_size, command.font.weight, command.font.italic, command.font.charset)
    anchor = _text_anchor(command.text_align)
    positions = (
        [matrix.transform_point(position) for position in command.positions]
        if command.positions
        else [matrix.transform_point(command.origin)]
    )
    positions, positioned_run = _aligned_text_positions(command, matrix, positions)
    if positioned_run:
        anchor = "l" + anchor[1]
    texts = tuple(command.text) if command.positions else (command.text,)
    if command.opaque:
        draw = ImageDraw.Draw(layer)
        if command.bounds is not None:
            rect = _mapped_rect(command.bounds, matrix).normalized()
            background_bounds = rect.left, rect.top, rect.right, rect.bottom
        else:
            text_bounds = [draw.textbbox(position, text, font=font, anchor=anchor) for position, text in zip(positions, texts)]
            background_bounds = (
                min(bounds[0] for bounds in text_bounds),
                min(bounds[1] for bounds in text_bounds),
                max(bounds[2] for bounds in text_bounds),
                max(bounds[3] for bounds in text_bounds),
            )
        draw.rectangle(background_bounds, fill=command.background_color.rgba())
    if command.positions:
        for position, character in zip(positions, command.text):
            _draw_rotated_text(
                layer,
                position,
                character,
                font=font,
                fill=command.color.rgba(),
                anchor=anchor,
                rotation=command.rotation,
                underline=command.font.underline,
                strikeout=command.font.strikeout,
            )
    else:
        position = positions[0]
        _draw_rotated_text(
            layer,
            position,
            command.text,
            font=font,
            fill=command.color.rgba(),
            anchor=anchor,
            rotation=command.rotation,
            underline=command.font.underline,
            strikeout=command.font.strikeout,
        )
    _apply_clip(layer, command.clip, matrix, budget)
    return layer


__all__ = ["_text_anchor", "_aligned_text_positions", "_draw_rotated_text", "_render_text_command"]
