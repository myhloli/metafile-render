# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF gdi.playback 内部实现。"""

from __future__ import annotations

from dataclasses import replace
from math import isfinite

from ..commands import ClearCommand, DrawCommand, DrawPathCommand, DrawTextCommand, MetafileDocument
from ..context import _DiagnosticSink
from ..geometry import PathBuilder, path_bounds, transform_path, union_rectangles, vector_length
from ..headers import _HeaderInfo
from ..limits import (
    MAX_CLIP_OPERATIONS,
    MAX_COMMANDS,
    MAX_OBJECTS,
    MAX_POINTS_PER_RECORD,
    MAX_STATE_DEPTH,
    MAX_TOTAL_CLIP_OPERATIONS,
    MAX_TOTAL_POINTS,
)
from ..models import MetafileResourceLimitError, MetafileUnsupportedError
from ..primitives import BLACK, WHITE, Brush, ClipOperation, Color, Font, GraphicsPath, Matrix, Pen, Point, Rect
from ..sizing import _bounded_canvas_size
from .constants import _RGN_AND, _RGN_COPY, _RGN_DIFF, _RGN_OR, _RGN_XOR, _TA_CENTER, _TA_RIGHT
from .shapes import _path_from_device_points
from .state import GdiState


class _Playback:
    """把 WMF/EMF records 回放为统一绘图命令。"""

    def __init__(self, header: _HeaderInfo, diagnostics: _DiagnosticSink) -> None:
        """初始化 GDI 状态、对象表、路径与资源计数。"""
        self.header = header
        self.diagnostics = diagnostics
        self.state = GdiState(device_pixels_per_mm=header.device_pixels_per_mm)
        self.state_stack: list[GdiState] = []
        self.emf_objects: dict[int, Pen | Brush | Font] = {}
        self.wmf_objects: list[Pen | Brush | Font | None] = [None] * min(header.wmf_object_count, MAX_OBJECTS)
        self.commands: list[DrawCommand] = []
        self.path_builder = PathBuilder()
        self.path_active = False
        self.path_ready = False
        self.partial = False
        self.total_points = 0
        self.total_clip_operations = 0
        self.record_type: int | None = None
        self.record_index: int | None = None
        self.record_offset: int | None = None

    def set_record_context(self, record_type: int, record_index: int, offset: int) -> None:
        """更新后续诊断所使用的当前记录定位信息。"""
        self.record_type = record_type
        self.record_index = record_index
        self.record_offset = offset

    def warn(self, code: str, message: str, *, partial: bool = True) -> None:
        """为当前记录追加警告，并按需标记输出不完整。"""
        self.partial = self.partial or partial
        self.diagnostics.add(
            code,
            "warning",
            message,
            record_type=self.record_type,
            record_index=self.record_index,
            offset=self.record_offset,
        )

    def charge_points(self, count: int) -> None:
        """累计点数并执行单记录与全文件预算。"""
        if count < 0 or count > MAX_POINTS_PER_RECORD:
            raise MetafileResourceLimitError(f"metafile record exceeds max_points_per_record={MAX_POINTS_PER_RECORD}")
        self.total_points += count
        if self.total_points > MAX_TOTAL_POINTS:
            raise MetafileResourceLimitError(f"metafile exceeds max_total_points={MAX_TOTAL_POINTS}")

    def append_command(self, command: DrawCommand) -> None:
        """在命令预算内追加一条统一绘图命令。"""
        if len(self.commands) >= MAX_COMMANDS:
            raise MetafileResourceLimitError(f"metafile exceeds max_commands={MAX_COMMANDS}")
        self.total_clip_operations += len(command.clip)
        if self.total_clip_operations > MAX_TOTAL_CLIP_OPERATIONS:
            raise MetafileResourceLimitError(f"metafile exceeds max_total_clip_operations={MAX_TOTAL_CLIP_OPERATIONS}")
        self.commands.append(command)

    def copy_state(self) -> GdiState:
        """复制当前可由 SaveDC/RestoreDC 恢复的 GDI 状态。"""
        return replace(self.state)

    def save_dc(self) -> None:
        """把当前 GDI 状态压入有界栈。"""
        if len(self.state_stack) >= MAX_STATE_DEPTH:
            raise MetafileResourceLimitError(f"metafile exceeds max_state_depth={MAX_STATE_DEPTH}")
        self.state_stack.append(self.copy_state())

    def restore_dc(self, level: int) -> None:
        """按 GDI 相对或绝对层级恢复已保存状态。"""
        if not self.state_stack:
            self.warn("restore_dc_underflow", "RestoreDC ignored because the state stack is empty")
            return
        if level < 0:
            target = len(self.state_stack) + level
        elif level > 0:
            target = level - 1
        else:
            self.warn("restore_dc_zero", "RestoreDC level 0 is invalid")
            return
        if target < 0 or target >= len(self.state_stack):
            self.warn("restore_dc_out_of_range", f"RestoreDC level is out of range: {level}")
            return
        self.state = self.state_stack[target]
        del self.state_stack[target:]

    def _mapping_matrix(self) -> Matrix:
        """根据 mapping mode、window 和 viewport 计算 page-to-device 变换。"""
        mode = self.state.map_mode
        window_x, window_y = self.state.window_origin
        viewport_x, viewport_y = self.state.viewport_origin
        if mode in {7, 8}:
            window_width, window_height = self.state.window_extent
            viewport_width, viewport_height = self.state.viewport_extent
            if window_width == 0.0 or window_height == 0.0:
                self.warn("zero_window_extent", "zero window extent was replaced by identity mapping")
                scale_x = scale_y = 1.0
            else:
                scale_x = viewport_width / window_width
                scale_y = viewport_height / window_height
        elif mode == 2:
            scale_x = self.state.device_pixels_per_mm[0] / 10.0
            scale_y = -self.state.device_pixels_per_mm[1] / 10.0
        elif mode == 3:
            scale_x = self.state.device_pixels_per_mm[0] / 100.0
            scale_y = -self.state.device_pixels_per_mm[1] / 100.0
        elif mode == 4:
            scale_x = self.state.device_pixels_per_mm[0] * 25.4 / 100.0
            scale_y = -self.state.device_pixels_per_mm[1] * 25.4 / 100.0
        elif mode == 5:
            scale_x = self.state.device_pixels_per_mm[0] * 25.4 / 1000.0
            scale_y = -self.state.device_pixels_per_mm[1] * 25.4 / 1000.0
        elif mode == 6:
            scale_x = self.state.device_pixels_per_mm[0] * 25.4 / 1440.0
            scale_y = -self.state.device_pixels_per_mm[1] * 25.4 / 1440.0
        else:
            scale_x = scale_y = 1.0
        return Matrix(
            a=scale_x,
            d=scale_y,
            e=viewport_x - window_x * scale_x,
            f=viewport_y - window_y * scale_y,
        )

    def logical_matrix(self) -> Matrix:
        """返回 world-to-page 与 page-to-device 的组合变换。"""
        return self.state.world_transform.then(self._mapping_matrix())

    def map_point(self, point: Point) -> Point:
        """把 GDI 逻辑点转换为统一 device 坐标。"""
        mapped = self.logical_matrix().transform_point(point)
        if not all(isfinite(value) and abs(value) <= 1e12 for value in mapped):
            raise MetafileResourceLimitError("metafile coordinate is non-finite or unreasonably large")
        return mapped

    def map_vector(self, vector: Point) -> Point:
        """把不含平移的 GDI 逻辑向量转换为 device 向量。"""
        mapped = self.logical_matrix().transform_vector(vector)
        if not all(isfinite(value) and abs(value) <= 1e12 for value in mapped):
            raise MetafileResourceLimitError("metafile vector is non-finite or unreasonably large")
        return mapped

    def resolved_pen(self) -> Pen:
        """把当前画笔宽度解析为 device 单位并保留 cosmetic 语义。"""
        pen = self.state.pen
        if pen.null:
            return pen
        if pen.cosmetic:
            return replace(pen, width=max(1.0, pen.width))
        width = vector_length(self.map_vector((pen.width or 1.0, 0.0)))
        return replace(pen, width=max(width, 1.0))

    def emit_path(self, path: GraphicsPath, *, stroke: bool, fill: bool) -> None:
        """把路径追加到当前 path bracket 或直接生成绘图命令。"""
        if self.path_active:
            self.path_builder.extend(path)
            return
        if not path.segments:
            return
        self.append_command(
            DrawPathCommand(
                path=path,
                pen=self.resolved_pen(),
                brush=self.state.brush,
                stroke=stroke and not self.state.pen.null,
                fill=fill and self.state.brush.kind != "null",
                fill_rule="evenodd" if self.state.polygon_fill_mode == 1 else "nonzero",
                clip=self.state.clip,
                rop2=self.state.rop2,
                miter_limit=self.state.miter_limit,
            )
        )

    def emit_logical_path(self, path: GraphicsPath, *, stroke: bool, fill: bool) -> None:
        """把逻辑坐标路径变换后交给统一路径输出。"""
        self.emit_path(transform_path(path, self.logical_matrix()), stroke=stroke, fill=fill)

    def move_to(self, point: Point) -> None:
        """更新 GDI current position，并在活动 path 中开始新 figure。"""
        self.state.current_position = point
        if self.path_active:
            self.path_builder.move_to(self.map_point(point))

    def _ensure_active_path_current(self) -> None:
        """保证活动 path 的下一条 To 记录从 GDI current position 起笔。"""
        mapped = self.map_point(self.state.current_position)
        if self.path_builder.current != mapped:
            self.path_builder.move_to(mapped)

    def line_to(self, endpoint: Point) -> None:
        """按 LineTo 语义追加直线并更新 GDI current position。"""
        if self.path_active:
            self._ensure_active_path_current()
            self.path_builder.line_to(self.map_point(endpoint))
        else:
            self.emit_path(
                _path_from_device_points(
                    [self.map_point(self.state.current_position), self.map_point(endpoint)],
                    close=False,
                ),
                stroke=True,
                fill=False,
            )
        self.state.current_position = endpoint

    def polyline_to(self, points: list[Point]) -> None:
        """按 PolylineTo 语义连续追加折线并更新 GDI current position。"""
        if not points:
            return
        if self.path_active:
            self._ensure_active_path_current()
            for point in points:
                self.path_builder.line_to(self.map_point(point))
        else:
            mapped = [self.map_point(self.state.current_position), *(self.map_point(point) for point in points)]
            self.emit_path(_path_from_device_points(mapped, close=False), stroke=True, fill=False)
        self.state.current_position = points[-1]

    def polybezier_to(self, points: list[Point]) -> None:
        """按 PolyBezierTo 语义连续追加三次曲线并更新 GDI current position。"""
        if not points:
            return
        remaining = points
        if len(remaining) % 3:
            self.warn("invalid_bezier_points", "PolyBezierTo point count is not a multiple of three")
            remaining = remaining[: len(remaining) - len(remaining) % 3]
        if not remaining:
            return
        builder = self.path_builder if self.path_active else PathBuilder()
        if self.path_active:
            self._ensure_active_path_current()
        else:
            builder.move_to(self.map_point(self.state.current_position))
        for index in range(0, len(remaining), 3):
            builder.cubic_to(
                self.map_point(remaining[index]),
                self.map_point(remaining[index + 1]),
                self.map_point(remaining[index + 2]),
            )
        if not self.path_active:
            self.emit_path(builder.build(), stroke=True, fill=False)
        self.state.current_position = remaining[-1]

    def connected_arc(self, path: GraphicsPath) -> None:
        """绘制 current-to-projected-start 连线和圆弧，并更新到 projected endpoint。"""
        if not path.segments:
            return
        mapped_path = transform_path(path, self.logical_matrix())
        mapped_start = mapped_path.segments[0].points[0]
        tail = GraphicsPath(mapped_path.segments[1:])
        if self.path_active:
            self._ensure_active_path_current()
            self.path_builder.line_to(mapped_start)
            self.path_builder.extend(tail)
        else:
            builder = PathBuilder()
            builder.move_to(self.map_point(self.state.current_position))
            builder.line_to(mapped_start)
            builder.extend(tail)
            self.emit_path(builder.build(), stroke=True, fill=False)
        self.state.current_position = path.segments[-1].points[-1]

    def begin_path(self) -> None:
        """开始新的 GDI path bracket 并清除旧路径。"""
        if self.path_active:
            self.warn("begin_path_while_active", "BeginPath replaced an already active path bracket")
        self.path_builder.clear()
        self.path_active = True
        self.path_ready = False

    def end_path(self) -> None:
        """结束 path bracket 并保留路径供后续 fill/stroke/clip。"""
        if not self.path_active:
            self.warn("end_path_without_begin", "EndPath ignored without a matching BeginPath")
            return
        self.path_active = False
        self.path_ready = True

    def consume_path(self) -> GraphicsPath:
        """返回并清除已经结束的当前路径。"""
        if not self.path_ready:
            self.warn("path_not_ready", "path operation ignored because no completed path exists")
            return GraphicsPath(())
        path = self.path_builder.build()
        self.path_builder.clear()
        self.path_ready = False
        return path

    def add_clip(self, path: GraphicsPath, mode: int) -> None:
        """把 GDI region combine mode 转换为不可变裁剪操作。"""
        mode_name = _clip_mode_name(mode)
        if mode_name is None:
            self.warn("unsupported_clip_mode", f"unsupported clip combine mode: {mode}")
            return
        operation = ClipOperation(
            path=path,
            mode=mode_name,
            fill_rule="evenodd" if self.state.polygon_fill_mode == 1 else "nonzero",
        )
        if mode == _RGN_COPY:
            self.state.clip = (operation,)
        else:
            self.state.clip = self.clip_with_operation(operation)

    def clip_with_operation(self, operation: ClipOperation) -> tuple[ClipOperation, ...]:
        """在固定 clip 深度预算内返回追加单次操作后的不可变栈。"""
        if len(self.state.clip) >= MAX_CLIP_OPERATIONS:
            raise MetafileResourceLimitError(f"metafile exceeds max_clip_operations={MAX_CLIP_OPERATIONS}")
        return (*self.state.clip, operation)

    def reset_clip(self) -> None:
        """恢复为没有额外裁剪区域的初始状态。"""
        self.state.clip = ()

    def create_wmf_object(self, value: Pen | Brush | Font) -> None:
        """按 WMF 首个空槽规则登记图形对象。"""
        for index, existing in enumerate(self.wmf_objects):
            if existing is None:
                self.wmf_objects[index] = value
                return
        if len(self.wmf_objects) >= MAX_OBJECTS:
            raise MetafileResourceLimitError(f"WMF object table exceeds max_objects={MAX_OBJECTS}")
        self.wmf_objects.append(value)

    def select_object(self, value: Pen | Brush | Font) -> None:
        """按对象实际类型更新当前选中画笔、画刷或字体。"""
        if isinstance(value, Pen):
            self.state.pen = value
        elif isinstance(value, Brush):
            self.state.brush = value
        else:
            self.state.font = value

    def select_emf_handle(self, handle: int) -> None:
        """选择 EMF 显式 handle 或 stock object。"""
        if handle & 0x80000000:
            stock = _stock_object(handle & 0x7FFFFFFF)
            if stock is None:
                self.warn("unknown_stock_object", f"unknown EMF stock object: {handle:#x}")
                return
            self.select_object(stock)
            return
        value = self.emf_objects.get(handle)
        if value is None:
            self.warn("missing_object", f"EMF object handle does not exist: {handle}")
            return
        self.select_object(value)

    def select_wmf_handle(self, handle: int) -> None:
        """选择 WMF 对象槽或兼容的 stock object。"""
        if handle & 0x8000:
            stock = _stock_object(handle & 0x7FFF)
            if stock is None:
                self.warn("unknown_stock_object", f"unknown WMF stock object: {handle:#x}")
                return
            self.select_object(stock)
            return
        if handle >= len(self.wmf_objects) or self.wmf_objects[handle] is None:
            self.warn("missing_object", f"WMF object handle does not exist: {handle}")
            return
        self.select_object(self.wmf_objects[handle])  # type: ignore[arg-type]

    def finalize(self, size_hint: tuple[int, int] | None) -> MetafileDocument:
        """解析最终边界、输出尺寸并冻结统一图元文档。"""
        command_bounds = _commands_bounds(self.commands)
        bounds = self.header.bounds
        if bounds is None or abs(bounds.width) < 1e-9 or abs(bounds.height) < 1e-9:
            bounds = command_bounds
        if bounds is None or abs(bounds.width) < 1e-9 or abs(bounds.height) < 1e-9:
            raise MetafileUnsupportedError("metafile contains no visible output")
        bounds = bounds.normalized()
        requested = size_hint or self.header.pixel_size
        if requested is None:
            requested = max(1, round(bounds.width)), max(1, round(bounds.height))
        width, height, downscaled = _bounded_canvas_size(requested)
        if downscaled:
            self.diagnostics.add(
                "canvas_downscaled",
                "warning",
                f"metafile canvas was downscaled from {requested[0]}x{requested[1]} to {width}x{height}",
            )
        return MetafileDocument(
            source_format=self.header.source_format,
            emfplus_mode=self.header.emfplus_mode,
            bounds=bounds,
            width=width,
            height=height,
            commands=tuple(self.commands),
            diagnostics=self.diagnostics.freeze(),
            partial=self.partial,
        )


def _clip_mode_name(mode: int) -> str | None:
    """把 GDI RegionMode 数值转换为内部裁剪操作名称。"""
    return {
        _RGN_AND: "and",
        _RGN_OR: "or",
        _RGN_XOR: "xor",
        _RGN_DIFF: "diff",
        _RGN_COPY: "copy",
    }.get(mode)


def _stock_object(index: int) -> Pen | Brush | Font | None:
    """返回常见 GDI stock object 的确定性跨平台表示。"""
    brushes: dict[int, Brush] = {
        0: Brush(color=WHITE),
        1: Brush(color=Color(192, 192, 192)),
        2: Brush(color=Color(128, 128, 128)),
        3: Brush(color=Color(64, 64, 64)),
        4: Brush(color=BLACK),
        5: Brush(kind="null"),
        6: Brush(kind="null"),
        18: Brush(color=WHITE),
    }
    if index in brushes:
        return brushes[index]
    pens: dict[int, Pen] = {
        7: Pen(color=WHITE),
        8: Pen(color=BLACK),
        9: Pen(null=True),
        19: Pen(color=BLACK),
    }
    if index in pens:
        return pens[index]
    if 10 <= index <= 17:
        return Font(face_name="Courier New" if index in {10, 11, 16} else "Arial")
    return None


def _horizontal_text_align_factor(text_align: int) -> float:
    """返回 LEFT/CENTER/RIGHT 文本 bounds 相对 origin 的左移比例。"""
    if text_align & _TA_CENTER == _TA_CENTER:
        return 0.5
    if text_align & _TA_RIGHT:
        return 1.0
    return 0.0


def _commands_bounds(commands: list[DrawCommand]) -> Rect | None:
    """计算全部绘图命令的保守可见包围盒。"""
    rectangles: list[Rect | None] = []
    for command in commands:
        if isinstance(command, DrawPathCommand):
            rectangles.append(path_bounds(command.path))
        elif isinstance(command, DrawTextCommand):
            if command.bounds is not None and abs(command.bounds.width) > 0 and abs(command.bounds.height) > 0:
                rectangles.append(command.bounds)
            else:
                estimated_width = max(command.font_height * 0.6 * len(command.text), command.font_height)
                if command.advance_end is not None:
                    estimated_width = max(estimated_width, abs(command.advance_end[0] - command.origin[0]))
                align_factor = _horizontal_text_align_factor(command.text_align)
                left = command.origin[0] - estimated_width * align_factor
                rectangles.append(
                    Rect(
                        left,
                        command.origin[1] - command.font_height,
                        left + estimated_width,
                        command.origin[1] + command.font_height * 0.25,
                    )
                )
        elif isinstance(command, ClearCommand):
            rectangles.append(command.bounds)
        else:
            xs = [point[0] for point in command.destination]
            ys = [point[1] for point in command.destination]
            rectangles.append(Rect(min(xs), min(ys), max(xs), max(ys)))
    return union_rectangles(rectangles)


__all__ = ["_Playback", "_clip_mode_name", "_stock_object", "_horizontal_text_align_factor", "_commands_bounds"]
