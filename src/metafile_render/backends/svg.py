# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF backends.svg 内部实现。"""

from __future__ import annotations

import base64
from html import escape
from io import BytesIO

from ..commands import ClearCommand, DrawImageCommand, DrawPathCommand, DrawTextCommand, MetafileDocument
from ..geometry import transform_path
from ..limits import MAX_CANVAS_PIXELS, MAX_GENERATED_SVG_BYTES, MAX_RENDER_WORK_PIXELS
from ..models import MetafileResourceLimitError
from ..primitives import Brush, ClipOperation, ClipStack, Color, GraphicsPath, Matrix, Pen, Rect
from .bitmap import _crop_destination, _crop_source, _decode_dib
from .constants import _SRCCOPY, _TA_BASELINE, _TA_BOTTOM, _TA_CENTER, _TA_RIGHT
from .mapping import _document_matrix, _mapped_rect
from .raster import _png_fallback_bytes
from .text import _aligned_text_positions


def _svg_number(value: float) -> str:
    """以稳定且紧凑的形式序列化 SVG 浮点数。"""
    rounded = round(value, 4)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.4f}".rstrip("0").rstrip(".")


def _svg_path_data(path: GraphicsPath, matrix: Matrix) -> str:
    """把统一路径转换为安全 SVG path data。"""
    parts: list[str] = []
    for segment in transform_path(path, matrix).segments:
        if segment.verb == "Z":
            parts.append("Z")
            continue
        coordinates = " ".join(f"{_svg_number(point[0])} {_svg_number(point[1])}" for point in segment.points)
        parts.append(f"{segment.verb} {coordinates}")
    return " ".join(parts)


def _svg_opacity(color: Color) -> str:
    """返回仅在颜色半透明时需要的 SVG opacity 属性。"""
    return "" if color.alpha == 255 else f' opacity="{_svg_number(color.alpha / 255.0)}"'


def _svg_pen_attributes(pen: Pen, matrix: Matrix, miter_limit: float) -> str:
    """把内部 Pen 转换为 SVG stroke 属性。"""
    if pen.null:
        return 'stroke="none"'
    scale = (abs(matrix.a) + abs(matrix.d)) / 2.0
    width = pen.width if pen.cosmetic else pen.width * max(scale, 1e-9)
    attributes = [
        f'stroke="{pen.color.svg()}"',
        f'stroke-width="{_svg_number(max(width, 1.0))}"',
        f'stroke-linecap="{pen.cap}"',
        f'stroke-linejoin="{pen.join}"',
        f'stroke-miterlimit="{_svg_number(max(miter_limit, 1.0))}"',
    ]
    if pen.color.alpha != 255:
        attributes.append(f'stroke-opacity="{_svg_number(pen.color.alpha / 255.0)}"')
    if pen.dashes:
        dash_scale = 1.0 if pen.cosmetic else scale
        attributes.append(f'stroke-dasharray="{" ".join(_svg_number(value * dash_scale) for value in pen.dashes)}"')
    return " ".join(attributes)


def _svg_brush_attributes(brush: Brush) -> str:
    """把内部 Brush 转换为基础 SVG fill 属性。"""
    if brush.kind == "null":
        return 'fill="none"'
    attributes = [f'fill="{brush.color.svg()}"']
    if brush.color.alpha != 255:
        attributes.append(f'fill-opacity="{_svg_number(brush.color.alpha / 255.0)}"')
    return " ".join(attributes)


def _svg_clip_definitions(document: MetafileDocument, matrix: Matrix) -> tuple[list[str], dict[ClipOperation, str]]:
    """为只含 copy/and 的裁剪路径生成确定性 clipPath 定义。"""
    definitions: list[str] = []
    identifiers: dict[ClipOperation, str] = {}
    ordinal = 0
    for command in document.commands:
        for operation in command.clip:
            if operation in identifiers:
                continue
            ordinal += 1
            identifier = f"metafile-render-clip-{ordinal}"
            identifiers[operation] = identifier
            definitions.append(
                f'<clipPath id="{identifier}"><path d="{_svg_path_data(operation.path, matrix)}" '
                f'fill-rule="{operation.fill_rule}"/></clipPath>'
            )
    return definitions, identifiers


def _wrap_svg_clip(element: str, clip: ClipStack, identifiers: dict[ClipOperation, str]) -> str:
    """按裁剪顺序用嵌套 SVG group 包裹单个图元。"""
    wrapped = element
    active: list[ClipOperation] = []
    for operation in clip:
        if operation.mode == "copy":
            active = [operation]
        elif operation.mode == "and":
            active.append(operation)
    for operation in reversed(active):
        wrapped = f'<g clip-path="url(#{identifiers[operation]})">{wrapped}</g>'
    return wrapped


def _svg_text_anchor(text_align: int) -> tuple[str, str]:
    """把 GDI 文字对齐转换为 SVG anchor 和 baseline。"""
    horizontal = "middle" if text_align & _TA_CENTER == _TA_CENTER else "end" if text_align & _TA_RIGHT else "start"
    vertical = (
        "alphabetic"
        if text_align & _TA_BASELINE == _TA_BASELINE
        else "text-after-edge"
        if text_align & _TA_BOTTOM
        else "hanging"
    )
    return horizontal, vertical


def _svg_text_elements(command: DrawTextCommand, matrix: Matrix) -> str:
    """把统一文字命令转换为一个或多个安全 SVG text 元素。"""
    anchor, baseline = _svg_text_anchor(command.text_align)
    scale_y = max(abs(matrix.d), 1e-9)
    font_size = max(1.0, command.font_height * scale_y)
    decorations = []
    if command.font.underline:
        decorations.append("underline")
    if command.font.strikeout:
        decorations.append("line-through")
    decoration_attr = f' text-decoration="{" ".join(decorations)}"' if decorations else ""
    elements: list[str] = []
    positions = command.positions or (command.origin,)
    texts = tuple(command.text) if command.positions else (command.text,)
    mapped_positions_list = [matrix.transform_point(position) for position in positions]
    mapped_positions_list, positioned_run = _aligned_text_positions(command, matrix, mapped_positions_list)
    if positioned_run:
        anchor = "start"
    common = (
        f'fill="{command.color.svg()}"{_svg_opacity(command.color)} '
        f'font-family="{escape(command.font.face_name, quote=True)}" font-size="{_svg_number(font_size)}" '
        f'font-weight="{command.font.weight}" font-style="{"italic" if command.font.italic else "normal"}" '
        f'text-anchor="{anchor}" dominant-baseline="{baseline}"{decoration_attr}'
    )
    mapped_positions = tuple(mapped_positions_list)
    for mapped, text in zip(mapped_positions, texts):
        transform = ""
        if abs(command.rotation) > 1e-6:
            transform = (
                f' transform="rotate({_svg_number(command.rotation)} {_svg_number(mapped[0])} {_svg_number(mapped[1])})"'
            )
        elements.append(
            f'<text x="{_svg_number(mapped[0])}" y="{_svg_number(mapped[1])}" {common}{transform}>{escape(text)}</text>'
        )
    background = ""
    if command.opaque:
        if command.bounds is not None:
            rect = _mapped_rect(command.bounds, matrix).normalized()
        else:
            approximate_widths = tuple(max(font_size * 0.6 * len(text), font_size * 0.25) for text in texts)
            left = min(position[0] for position in mapped_positions)
            right = max(position[0] + width for position, width in zip(mapped_positions, approximate_widths))
            top = min(position[1] - font_size for position in mapped_positions)
            bottom = max(position[1] + font_size * 0.25 for position in mapped_positions)
            rect = Rect(left, top, right, bottom)
        background = (
            f'<rect x="{_svg_number(rect.left)}" y="{_svg_number(rect.top)}" width="{_svg_number(rect.width)}" '
            f'height="{_svg_number(rect.height)}" fill="{command.background_color.svg()}"'
            f"{_svg_opacity(command.background_color)}/>"
        )
    return background + "".join(elements)


def _svg_image_element(command: DrawImageCommand, matrix: Matrix) -> str:
    """把 DIB 转成内嵌 PNG 并以 SVG affine matrix 放置。"""
    image, source_fraction = _crop_source(_decode_dib(command), command.source)
    if image is None:
        return ""
    if command.constant_alpha < 255:
        image.putalpha(image.getchannel("A").point(lambda value: value * command.constant_alpha // 255))
    output = BytesIO()
    image.save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    destination = _crop_destination(command.destination, source_fraction)
    first, second, _third, fourth = (matrix.transform_point(point) for point in destination)
    a = (second[0] - first[0]) / max(image.width, 1)
    b = (second[1] - first[1]) / max(image.width, 1)
    c = (fourth[0] - first[0]) / max(image.height, 1)
    d = (fourth[1] - first[1]) / max(image.height, 1)
    transform = " ".join(_svg_number(value) for value in (a, b, c, d, first[0], first[1]))
    return (
        f'<image width="{image.width}" height="{image.height}" transform="matrix({transform})" '
        f'href="data:image/png;base64,{encoded}"/>'
    )


def _svg_requires_raster(document: MetafileDocument) -> bool:
    """判断 SVG 是否需要用整图 PNG 包装保留复杂合成语义。"""
    for index, command in enumerate(document.commands):
        if isinstance(command, ClearCommand) and (index > 0 or command.clip):
            return True
        if any(operation.mode not in {"copy", "and"} for operation in command.clip):
            return True
        if isinstance(command, DrawPathCommand) and command.rop2 != 13:
            return True
        if isinstance(command, DrawImageCommand) and command.rop not in {_SRCCOPY, 0}:
            return True
    return False


def _svg_fallback_scale(document: MetafileDocument) -> int:
    """为纯矢量 SVG fallback 选择最高 2× 的安全像素密度。"""
    if any(isinstance(command, DrawImageCommand) for command in document.commands):
        return 1
    command_count = max(len(document.commands), 1)
    for factor in (2,):
        width = document.width * factor
        height = document.height * factor
        if (
            width <= 8192
            and height <= 8192
            and width * height <= MAX_CANVAS_PIXELS
            and width * height * command_count <= MAX_RENDER_WORK_PIXELS
        ):
            return factor
    return 1


def _svg_fallback_metadata(encoded_png: str) -> str:
    """生成带固定标识的不可见 PNG fallback metadata。"""
    return f'<metadata id="metafile-render-raster-fallback" data-mime="image/png">{encoded_png}</metadata>'


def _encode_svg_markup(markup: str) -> bytes:
    """编码 SVG 并保证生成结果不会超过下游安全消费者的字节上限。"""
    output = markup.encode("utf-8")
    if len(output) > MAX_GENERATED_SVG_BYTES:
        raise MetafileResourceLimitError(f"generated SVG exceeds max_generated_svg_bytes={MAX_GENERATED_SVG_BYTES}")
    return output


def _check_svg_size_budget(estimated_bytes: int) -> None:
    """在继续编码昂贵 SVG element 前执行保守的消费者字节预算。"""
    if estimated_bytes > MAX_GENERATED_SVG_BYTES:
        raise MetafileResourceLimitError(f"generated SVG exceeds max_generated_svg_bytes={MAX_GENERATED_SVG_BYTES}")


def _raster_wrapped_svg(document: MetafileDocument) -> bytes:
    """把 Pillow 结果封装为没有外部引用的单图片 SVG。"""
    encoded = base64.b64encode(_png_fallback_bytes(document)).decode("ascii")
    _check_svg_size_budget(len(encoded) * 2)
    markup = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{document.width}" height="{document.height}" '
        f'viewBox="0 0 {document.width} {document.height}" data-metafile-render="wmf-emf">'
        f"{_svg_fallback_metadata(encoded)}"
        f'<image width="{document.width}" height="{document.height}" href="data:image/png;base64,{encoded}"/>'
        "</svg>"
    )
    return _encode_svg_markup(markup)


def render_svg(document: MetafileDocument) -> bytes:
    """把统一图元文档渲染为安全、自包含 SVG 字节。"""
    if _svg_requires_raster(document):
        return _raster_wrapped_svg(document)
    matrix = _document_matrix(document)
    definitions, clip_ids = _svg_clip_definitions(document, matrix)
    fallback_scale = _svg_fallback_scale(document)
    fallback = base64.b64encode(_png_fallback_bytes(document, pixel_scale=fallback_scale)).decode("ascii")
    fallback_metadata = _svg_fallback_metadata(fallback)
    estimated_bytes = len(fallback_metadata) + 256
    _check_svg_size_budget(estimated_bytes)
    elements: list[str] = []
    for command in document.commands:
        if isinstance(command, ClearCommand):
            element = (
                f'<rect x="0" y="0" width="{document.width}" height="{document.height}" '
                f'fill="{command.color.svg()}"{_svg_opacity(command.color)}/>'
            )
        elif isinstance(command, DrawPathCommand):
            stroke = _svg_pen_attributes(command.pen, matrix, command.miter_limit) if command.stroke else 'stroke="none"'
            fill = _svg_brush_attributes(command.brush) if command.fill else 'fill="none"'
            element = f'<path d="{_svg_path_data(command.path, matrix)}" {stroke} {fill} fill-rule="{command.fill_rule}"/>'
        elif isinstance(command, DrawTextCommand):
            element = _svg_text_elements(command, matrix)
        else:
            element = _svg_image_element(command, matrix)
        elements.append(_wrap_svg_clip(element, command.clip, clip_ids))
        estimated_bytes += len(elements[-1].encode("utf-8"))
        _check_svg_size_budget(estimated_bytes)
    defs = f"<defs>{''.join(definitions)}</defs>" if definitions else ""
    markup = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{document.width}" height="{document.height}" '
        f'viewBox="0 0 {document.width} {document.height}" data-metafile-render="wmf-emf">'
        f"{fallback_metadata}{defs}{''.join(elements)}</svg>"
    )
    return _encode_svg_markup(markup)


__all__ = [
    "_svg_number",
    "_svg_path_data",
    "_svg_opacity",
    "_svg_pen_attributes",
    "_svg_brush_attributes",
    "_svg_clip_definitions",
    "_wrap_svg_clip",
    "_svg_text_anchor",
    "_svg_text_elements",
    "_svg_image_element",
    "_svg_requires_raster",
    "_svg_fallback_scale",
    "_svg_fallback_metadata",
    "_encode_svg_markup",
    "_check_svg_size_budget",
    "_raster_wrapped_svg",
    "render_svg",
]
