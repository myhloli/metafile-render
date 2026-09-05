# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF backends.paths 内部实现。"""

from __future__ import annotations

from math import hypot

import pyclipper
from PIL import Image, ImageChops, ImageDraw

from ..commands import DrawPathCommand
from ..geometry import FlattenBudget, flatten_path, transform_path
from ..models import MetafileMalformedError, MetafileResourceLimitError
from ..primitives import Brush, ClipStack, GraphicsPath, Matrix, Pen, Point
from .constants import _CLIPPER_COORD_LIMIT, _CLIPPER_SCALE
from .session import RenderSession


def _clipper_paths(subpaths: list[tuple[list[Point], bool]]) -> list[list[tuple[int, int]]]:
    """把浮点子路径量化为 Clipper 闭合轮廓并过滤退化输入。"""
    result: list[list[tuple[int, int]]] = []
    for points, _closed in subpaths:
        converted: list[tuple[int, int]] = []
        for x, y in points:
            if abs(x) > _CLIPPER_COORD_LIMIT or abs(y) > _CLIPPER_COORD_LIMIT:
                raise MetafileResourceLimitError("flattened path coordinate exceeds Clipper range")
            point = round(x * _CLIPPER_SCALE), round(y * _CLIPPER_SCALE)
            if not converted or converted[-1] != point:
                converted.append(point)
        if converted and converted[0] == converted[-1]:
            converted.pop()
        if len(converted) >= 3:
            result.append(converted)
    return result


def _paint_polytree(mask: Image.Image, tree: pyclipper.PyPolyNode, budget: FlattenBudget | None = None) -> None:
    """按 PolyTree 层级依次绘制外轮廓、孔洞和孔洞中的岛。"""
    draw = ImageDraw.Draw(mask)
    stack: list[pyclipper.PyPolyNode] = list(reversed(tree.Childs))
    while stack:
        child = stack.pop()
        if budget is not None:
            budget.charge(len(child.Contour))
        points = [(x / _CLIPPER_SCALE, y / _CLIPPER_SCALE) for x, y in child.Contour]
        if len(points) >= 3:
            draw.polygon(points, fill=0 if child.IsHole else 255)
        stack.extend(reversed(child.Childs))


def _path_mask(
    path: GraphicsPath,
    matrix: Matrix,
    size: tuple[int, int],
    fill_rule: str,
    budget: FlattenBudget | None = None,
) -> Image.Image:
    """按 GDI winding/alternate 规则把复合路径转换为 L 模式 mask。"""
    transformed = transform_path(path, matrix)
    subpaths = flatten_path(transformed, budget=budget)
    return _subpath_mask(subpaths, size, fill_rule, budget)


def _subpath_mask(
    subpaths: list[tuple[list[Point], bool]], size: tuple[int, int], fill_rule: str, budget: FlattenBudget | None
) -> Image.Image:
    """将已展平轮廓按填充规则转换成 mask，供填充与裁剪复用。"""
    mask = Image.new("L", size, 0)
    paths = _clipper_paths(subpaths)
    if not paths:
        return mask
    fill_type = pyclipper.PFT_EVENODD if fill_rule == "evenodd" else pyclipper.PFT_NONZERO
    try:
        clipper = pyclipper.Pyclipper()
        clipper.AddPaths(paths, pyclipper.PT_SUBJECT, True)
        tree = clipper.Execute2(pyclipper.CT_UNION, fill_type, fill_type)
    except pyclipper.ClipperException as exc:
        raise MetafileMalformedError("compound path cannot be resolved") from exc
    _paint_polytree(mask, tree, budget)
    return mask


def _clip_mask(
    clip: ClipStack,
    matrix: Matrix,
    size: tuple[int, int],
    budget: FlattenBudget | None = None,
    session: RenderSession | None = None,
) -> Image.Image | None:
    """按 GDI combine mode 顺序合成最终裁剪 mask。"""
    if not clip:
        return None
    key = ("clip", clip, matrix, size)
    cached = session.cache.get(key) if session is not None else None
    if cached is not None:
        return cached
    current = Image.new("L", size, 255)
    for operation in clip:
        incoming = _path_mask(operation.path, matrix, size, operation.fill_rule, budget)
        if operation.mode == "copy":
            current = incoming
        elif operation.mode == "and":
            current = ImageChops.multiply(current, incoming)
        elif operation.mode == "or":
            current = ImageChops.lighter(current, incoming)
        elif operation.mode == "xor":
            current = ImageChops.logical_xor(current.convert("1"), incoming.convert("1")).convert("L")
        else:
            current = ImageChops.subtract(current, incoming)
    if session is not None:
        session.cache.put(key, current)
    return current


def _apply_clip(
    layer: Image.Image,
    clip: ClipStack,
    matrix: Matrix,
    budget: FlattenBudget | None = None,
    session: RenderSession | None = None,
) -> None:
    """把命令级裁剪 mask 乘入 RGBA layer 的 alpha 通道。"""
    mask = _clip_mask(clip, matrix, layer.size, budget, session)
    if mask is None:
        return
    alpha = layer.getchannel("A")
    layer.putalpha(ImageChops.multiply(alpha, mask))


def _hatch_layer(size: tuple[int, int], brush: Brush) -> Image.Image:
    """为常见 GDI hatch brush 生成确定性 RGBA 图案层。"""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    color = brush.color.rgba()
    spacing = 8
    width, height = size
    if brush.hatch in {0, 4, 5}:
        for y in range(0, height + spacing, spacing):
            draw.line((0, y, width, y), fill=color)
    if brush.hatch in {1, 4, 5}:
        for x in range(0, width + spacing, spacing):
            draw.line((x, 0, x, height), fill=color)
    if brush.hatch in {2, 4}:
        for offset in range(-height, width + height, spacing):
            draw.line((offset, 0, offset + height, height), fill=color)
    if brush.hatch in {3, 5}:
        for offset in range(0, width + height * 2, spacing):
            draw.line((offset, 0, offset - height, height), fill=color)
    return layer


def _dash_polyline(
    points: list[Point],
    *,
    dashes: tuple[float, ...],
    closed: bool,
) -> list[list[Point]]:
    """按连续路径长度把折线切分为需要描绘的 dash 子段。"""
    if len(points) < 2:
        return []
    if not dashes:
        return [points]
    pattern = tuple(max(0.5, value) for value in dashes)
    pattern_index = 0
    pattern_remaining = pattern[0]
    drawing = True
    visible: list[list[Point]] = []
    current: list[Point] | None = None
    for start, end in zip(points, points[1:]):
        delta_x, delta_y = end[0] - start[0], end[1] - start[1]
        length = hypot(delta_x, delta_y)
        if length <= 1e-9:
            continue
        consumed = 0.0
        while consumed < length:
            advance = min(pattern_remaining, length - consumed)
            if drawing and advance > 0:
                first = (
                    start[0] + delta_x * consumed / length,
                    start[1] + delta_y * consumed / length,
                )
                second = (
                    start[0] + delta_x * (consumed + advance) / length,
                    start[1] + delta_y * (consumed + advance) / length,
                )
                if current is None:
                    current = [first]
                elif current[-1] != first:
                    current.append(first)
                current.append(second)
            consumed += advance
            pattern_remaining -= advance
            if pattern_remaining <= 1e-9:
                if drawing and current is not None:
                    visible.append(current)
                    current = None
                pattern_index = (pattern_index + 1) % len(pattern)
                pattern_remaining = pattern[pattern_index]
                drawing = not drawing
    if current is not None:
        visible.append(current)
    if closed and len(visible) > 1 and visible[0][0] == points[0] and visible[-1][-1] == points[-1]:
        visible[0] = [*visible[-1][:-1], *visible[0]]
        visible.pop()
    return visible


def _quantize_stroke_path(points: list[Point], *, closed: bool) -> list[tuple[int, int]]:
    """把描边折线量化为 Clipper 坐标并移除重复端点。"""
    converted: list[tuple[int, int]] = []
    for x, y in points:
        if abs(x) > _CLIPPER_COORD_LIMIT or abs(y) > _CLIPPER_COORD_LIMIT:
            raise MetafileResourceLimitError("stroke coordinate exceeds Clipper range")
        point = round(x * _CLIPPER_SCALE), round(y * _CLIPPER_SCALE)
        if not converted or converted[-1] != point:
            converted.append(point)
    if closed and len(converted) > 1 and converted[0] == converted[-1]:
        converted.pop()
    return converted


def _offset_stroke_path(
    points: list[Point],
    *,
    closed: bool,
    width: float,
    pen: Pen,
    miter_limit: float,
    size: tuple[int, int],
    budget: FlattenBudget,
) -> Image.Image:
    """把单条折线扩张成遵守 GDI cap、join 和 miter limit 的描边 mask。"""
    mask = Image.new("L", size, 0)
    converted = _quantize_stroke_path(points, closed=closed)
    if len(converted) < (3 if closed else 2):
        return mask
    join_type = {
        "round": pyclipper.JT_ROUND,
        "bevel": pyclipper.JT_SQUARE,
        "miter": pyclipper.JT_MITER,
    }[pen.join]
    end_type = (
        pyclipper.ET_CLOSEDLINE
        if closed
        else {
            "round": pyclipper.ET_OPENROUND,
            "square": pyclipper.ET_OPENSQUARE,
            "flat": pyclipper.ET_OPENBUTT,
        }[pen.cap]
    )
    try:
        offset = pyclipper.PyclipperOffset(max(miter_limit, 1.0), _CLIPPER_SCALE * 0.1)
        offset.AddPath(converted, join_type, end_type)
        tree = offset.Execute2(max(width, 1.0) * _CLIPPER_SCALE / 2.0)
    except pyclipper.ClipperException as exc:
        raise MetafileMalformedError("stroke path cannot be widened") from exc
    _paint_polytree(mask, tree, budget)
    return mask


def _stroke_mask(
    subpaths: list[tuple[list[Point], bool]],
    *,
    size: tuple[int, int],
    width: float,
    pen: Pen,
    dashes: tuple[float, ...],
    miter_limit: float,
    budget: FlattenBudget,
) -> Image.Image:
    """把全部描边子路径合成为统一 L 模式覆盖 mask。"""
    mask = Image.new("L", size, 0)
    for points, closed in subpaths:
        visible = _dash_polyline(points, dashes=dashes, closed=closed)
        for segment in visible:
            segment_closed = closed and not dashes
            current = _offset_stroke_path(
                segment,
                closed=segment_closed,
                width=width,
                pen=pen,
                miter_limit=miter_limit,
                size=size,
                budget=budget,
            )
            mask = ImageChops.lighter(mask, current)
    return mask


def _render_path_command(
    command: DrawPathCommand,
    matrix: Matrix,
    size: tuple[int, int],
    raster_scale: int,
    budget: FlattenBudget,
    session: RenderSession | None = None,
) -> Image.Image:
    """把单条路径命令绘制到独立透明 RGBA layer。"""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    transformed = transform_path(command.path, matrix)
    subpaths = flatten_path(transformed, budget=budget)
    if command.fill and command.brush.kind != "null":
        fill_mask = _subpath_mask(subpaths, size, command.fill_rule, budget)
        if command.brush.kind == "hatch":
            fill_layer = _hatch_layer(size, command.brush)
            fill_layer.putalpha(ImageChops.multiply(fill_layer.getchannel("A"), fill_mask))
        else:
            fill_layer = Image.new("RGBA", size, command.brush.color.rgba())
            fill_layer.putalpha(ImageChops.multiply(fill_layer.getchannel("A"), fill_mask))
        layer.alpha_composite(fill_layer)
    if command.stroke and not command.pen.null:
        scale = (abs(matrix.a) + abs(matrix.d)) / 2.0
        pen_width = command.pen.width * raster_scale if command.pen.cosmetic else command.pen.width * max(scale, 1e-9)
        width = max(1.0, min(pen_width, max(size) * 2.0))
        dash_scale = raster_scale if command.pen.cosmetic else scale
        stroke_mask = _stroke_mask(
            subpaths,
            size=size,
            width=width,
            pen=command.pen,
            dashes=tuple(value * dash_scale for value in command.pen.dashes),
            miter_limit=command.miter_limit,
            budget=budget,
        )
        stroke_layer = Image.new("RGBA", size, command.pen.color.rgba())
        stroke_layer.putalpha(ImageChops.multiply(stroke_layer.getchannel("A"), stroke_mask))
        layer.alpha_composite(stroke_layer)
    _apply_clip(layer, command.clip, matrix, budget, session)
    return layer


__all__ = [
    "_clipper_paths",
    "_paint_polytree",
    "_path_mask",
    "_clip_mask",
    "_apply_clip",
    "_hatch_layer",
    "_dash_polyline",
    "_quantize_stroke_path",
    "_offset_stroke_path",
    "_stroke_mask",
    "_render_path_command",
]
