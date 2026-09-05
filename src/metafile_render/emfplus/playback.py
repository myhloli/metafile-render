# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""把 EMF+ Only 记录转换为共享绘图命令。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import atan2, cos, degrees, hypot, radians, sin
from typing import Protocol

from ..binary import BoundedReader
from ..commands import ClearCommand, DrawCommand, DrawImageCommand, DrawPathCommand, DrawTextCommand
from ..font import load_font, measure_text_advance
from ..gdi.state import GdiState
from ..geometry import PathBuilder, arc_path, ellipse_path, rectangle_path, transform_path
from ..limits import MAX_CLIP_OPERATIONS, MAX_EMBEDDED_BITMAP_BYTES, MAX_METAFILE_BYTES, MAX_RECORDS, MAX_STATE_DEPTH
from ..models import MetafileMalformedError, MetafileResourceLimitError, MetafileUnsupportedError
from ..primitives import Brush, ClipOperation, ClipStack, GraphicsPath, Matrix, Pen, Point, Rect
from .binary import Cursor, PlusRecord, argb, comment_records, finite
from .objects import (
    ImageAttributes,
    PlusBrush,
    PlusFont,
    PlusImage,
    PlusObject,
    PlusPen,
    StringFormat,
    UnsupportedObject,
    parse_object,
)


class PlaybackHost(Protocol):
    """共享编排层提供的显式接口，不依赖 GDI 私有解析实现。"""

    commands: list[DrawCommand]
    state: GdiState
    state_stack: list[GdiState]

    def warn(self, code: str, message: str, *, partial: bool = True) -> None:
        """追加当前记录的诊断。"""
        ...

    def set_record_context(self, record_type: int, record_index: int, offset: int) -> None:
        """更新诊断位置。"""
        ...

    def append_command(self, command: DrawCommand) -> None:
        """追加并检查绘图预算。"""
        ...

    def charge_points(self, count: int) -> None:
        """累计几何和文字的点数预算。"""
        ...


@dataclass(slots=True)
class PlusState:
    """保存与 GDI 对象选择无关的 EMF+ 图形状态。"""

    world: Matrix = field(default_factory=Matrix)
    container: Matrix = field(default_factory=Matrix)
    unit: int = 1
    page_scale: float = 1.0
    clip: ClipStack = ()


@dataclass(slots=True)
class ContinuedObject:
    """累计单个跨记录对象，完成前不暴露半成品。"""

    slot: int
    kind: int
    size: int
    data: bytearray = field(default_factory=bytearray)


class EmfPlusPlayback:
    """管理 EMF+ 独立对象表、状态栈和 GetDC 回放窗口。"""

    def __init__(self, host: PlaybackHost, bounds: Rect, dpi: Point) -> None:
        """初始化首版支持范围内的回放状态与累计资源计数。"""
        self.host = host
        self.bounds = bounds
        self.dpi = dpi
        self.state = PlusState()
        self.stack: list[tuple[int, str, PlusState]] = []
        self.objects: dict[int, PlusObject] = {}
        self.continued: ContinuedObject | None = None
        self.object_bytes = 0
        self.decoded_bytes = 0
        self.record_count = 0
        self.saw_header = False
        self.saw_eof = False
        self.gdi_active = False
        self.halted = False
        self.saved_gdi_state: GdiState | None = None
        self.saved_gdi_stack: list[GdiState] = []

    def warn(self, code: str, message: str) -> None:
        """保留全部可定位的降级诊断。"""
        self.host.warn(code, message)

    def stop(self, message: str) -> None:
        """无法确定后续状态时停止绘制，但继续验证子记录边界。"""
        self.halted = True
        self.gdi_active = False
        self.warn("emfplus_state_unsupported", message)

    def units(self, unit: int) -> Point:
        """把 EMF+ 计量单位换算为录制设备像素。"""
        if unit in {0, 1, 2}:
            return 1.0, 1.0
        divisors = {3: 72.0, 4: 1.0, 5: 300.0, 6: 25.4}
        if unit not in divisors:
            raise MetafileMalformedError(f"invalid EMF+ unit: {unit}")
        return self.dpi[0] / divisors[unit], self.dpi[1] / divisors[unit]

    def matrix(self) -> Matrix:
        """按世界、页面、容器的顺序组合坐标变换。"""
        x, y = self.units(self.state.unit)
        result = self.state.world.then(Matrix(a=x * self.state.page_scale, d=y * self.state.page_scale)).then(
            self.state.container
        )
        for value in (result.a, result.b, result.c, result.d, result.e, result.f):
            finite(value)
        return result

    def point(self, point: Point, matrix: Matrix | None = None) -> Point:
        """映射单点并限制变换后的坐标。"""
        x, y = (matrix or self.matrix()).transform_point(point)
        return finite(x), finite(y)

    def get(self, slot: int, expected: type) -> PlusObject | None:
        """验证引用类型，已声明的不支持对象只跳过对应绘制。"""
        if slot not in range(64) or slot not in self.objects:
            raise MetafileMalformedError(f"undefined EMF+ object: {slot}")
        value = self.objects[slot]
        if isinstance(value, UnsupportedObject):
            self.warn("emfplus_object_skipped", f"drawing references unsupported object {slot}")
            return None
        if not isinstance(value, expected):
            raise MetafileMalformedError(f"EMF+ object {slot} has unexpected type")
        return value

    def brush(self, value: int, flags: int) -> PlusBrush:
        """将内联 ARGB 或画刷引用解析成统一纯色。"""
        if flags & 0x8000:
            return PlusBrush(argb(value))
        result = self.get(value, PlusBrush)
        return result if isinstance(result, PlusBrush) else PlusBrush(None)

    def object(self, flags: int, cursor: Cursor) -> None:
        """重组对象分段并原子替换槽，限制所有对象的累计字节。"""
        slot, kind, more = flags & 255, (flags >> 8) & 127, bool(flags & 0x8000)
        if slot >= 64 or kind == 0:
            raise MetafileMalformedError("invalid EMF+ object slot or type")
        total = cursor.u32() if more else None
        if total is not None and (total == 0 or total > MAX_METAFILE_BYTES):
            raise MetafileResourceLimitError("EMF+ continued object exceeds byte budget")
        chunk = cursor.take(cursor.reader.remaining(cursor.offset))
        self.object_bytes += len(chunk)
        if self.object_bytes > MAX_METAFILE_BYTES:
            raise MetafileResourceLimitError("EMF+ cumulative objects exceed byte budget")
        if self.continued is not None:
            current = self.continued
            if (slot, kind) != (current.slot, current.kind) or total is not None and total != current.size:
                raise MetafileMalformedError("mismatched EMF+ continued object")
        elif more:
            current = ContinuedObject(slot, kind, total or 0)
            self.continued = current
            self.objects[slot] = UnsupportedObject(kind)
        else:
            current = None
            self.objects[slot] = UnsupportedObject(kind)
        if current is not None:
            remaining = current.size - len(current.data)
            if len(chunk) > remaining:
                if more or len(chunk) - remaining > 3:
                    raise MetafileMalformedError("EMF+ continued object exceeds declared length")
                chunk = chunk[:remaining]
            current.data.extend(chunk)
            if more:
                return
            if len(current.data) != current.size:
                raise MetafileMalformedError("incomplete EMF+ object data")
            chunk = bytes(current.data)
            self.continued = None
        value = parse_object(kind, BoundedReader(chunk, base_offset=cursor.reader.base_offset), self.warn)
        if isinstance(value, GraphicsPath):
            self.host.charge_points(sum(len(segment.points) for segment in value.segments))
        if isinstance(value, PlusImage):
            self.decoded_bytes += value.width * value.height * 4 + len(value.png)
            if self.decoded_bytes > MAX_EMBEDDED_BITMAP_BYTES:
                raise MetafileResourceLimitError("EMF+ cumulative decoded images exceed byte budget")
        self.objects[slot] = value

    def emit_path(
        self, path: GraphicsPath, *, brush: PlusBrush | None = None, pen: PlusPen | None = None, winding: bool = False
    ) -> None:
        """将图形转换到设备坐标并共用既有路径、裁剪与描边后端。"""
        if not path.segments:
            return
        matrix = self.matrix()
        transformed = transform_path(path, matrix)
        for segment in transformed.segments:
            for x, y in segment.points:
                finite(x)
                finite(y)
        fill = brush is not None and brush.color is not None
        stroke = pen is not None and not pen.pen.null
        if not fill and not stroke:
            return
        self.host.charge_points(sum(len(segment.points) for segment in path.segments))
        resolved = Pen(null=True)
        if pen is not None:
            scale = (hypot(matrix.a, matrix.b) + hypot(matrix.c, matrix.d)) / 2
            if pen.unit:
                unit_scale = sum(self.units(pen.unit)) / 2
                page_unit = sum(self.units(self.state.unit)) / 2
                scale *= unit_scale / page_unit
            width = finite(pen.pen.width * scale)
            resolved = replace(
                pen.pen, width=max(width, 1.0), dashes=tuple(value * max(width, 1.0) for value in pen.pen.dashes)
            )
        self.host.append_command(
            DrawPathCommand(
                transformed,
                resolved,
                Brush(color=brush.color) if fill and brush is not None and brush.color is not None else Brush(kind="null"),
                stroke,
                fill,
                "nonzero" if winding else "evenodd",
                self.state.clip,
                13,
                pen.miter_limit if pen else 10.0,
            )
        )

    def save(self, index: int, kind: str) -> None:
        """按记录索引保存图形状态，容器与 Save 索引可分别重复。"""
        if len(self.stack) >= MAX_STATE_DEPTH:
            raise MetafileResourceLimitError("EMF+ state stack exceeds depth budget")
        self.stack.append((index, kind, replace(self.state)))

    def restore(self, index: int, kind: str) -> None:
        """恢复匹配的保存状态并丢弃其后的嵌套状态。"""
        for position in range(len(self.stack) - 1, -1, -1):
            key, saved_kind, state = self.stack[position]
            if (key, saved_kind) == (index, kind):
                self.state = state
                del self.stack[position:]
                return
        raise MetafileMalformedError(f"EMF+ {kind} state index is not saved: {index}")

    def clip(self, path: GraphicsPath, mode: int) -> None:
        """执行矩形或路径的有界裁剪组合。"""
        modes = {0: "copy", 1: "and", 2: "or", 3: "xor", 4: "diff"}
        if mode not in modes:
            self.stop(f"clip combine mode {mode} cannot be approximated safely")
            return
        transformed = transform_path(path, self.matrix())
        self.host.charge_points(sum(len(segment.points) for segment in path.segments))
        if mode == 0:
            self.state.clip = ()
        if len(self.state.clip) >= MAX_CLIP_OPERATIONS:
            raise MetafileResourceLimitError("EMF+ clip operations exceed budget")
        self.state.clip += (ClipOperation(transformed, modes[mode]),)  # type: ignore[arg-type]

    def comment(self, record: BoundedReader) -> None:
        """逐一处理注释中的 EMF+ 记录，关闭上一段 GetDC 窗口。"""
        for item in comment_records(record):
            self.gdi_active = False
            if self.saved_gdi_state is not None:
                self.host.state = self.saved_gdi_state
                self.saved_gdi_state = None
                self.host.state_stack = self.saved_gdi_stack
            self.record_count += 1
            if self.record_count > MAX_RECORDS:
                raise MetafileResourceLimitError("EMF+ records exceed record budget")
            self.host.set_record_context(item.kind, self.record_count, item.offset)
            if self.saw_eof:
                raise MetafileMalformedError("EMF+ record follows EndOfFile")
            if not self.saw_header and item.kind != 0x4001:
                raise MetafileMalformedError("EMF+ Header must be the first record")
            if self.continued is not None and item.kind != 0x4008:
                raise MetafileMalformedError("EMF+ object continuation interrupted")
            self.record(item)

    def finish(self) -> None:
        """验证真实 Header/EOF 与完整对象，并拒绝没有支持内容的 Only 文件。"""
        if not self.saw_header or not self.saw_eof or self.continued is not None:
            raise MetafileMalformedError("EMF+ stream is missing Header/EOF or has unfinished objects")
        if not self.host.commands:
            raise MetafileUnsupportedError("EMF+ Only contains no supported drawing operations")

    def record(self, record: PlusRecord) -> None:
        """静态分派控制、状态与绘图记录，终止后仅验证结构边界。"""
        kind, flags = record.kind, record.flags
        cursor = Cursor(record.payload)
        if kind == 0x4001:
            if self.saw_header:
                raise MetafileMalformedError("duplicate EMF+ Header")
            cursor.version()
            cursor.u32()
            dx, dy = cursor.u32(), cursor.u32()
            if not dx or not dy or max(dx, dy) > 100000:
                raise MetafileMalformedError("invalid EMF+ logical DPI")
            self.dpi = float(dx), float(dy)
            self.saw_header = True
            cursor.finish()
            return
        if kind == 0x4002:
            cursor.finish()
            self.saw_eof = True
            return
        if self.halted:
            return
        if kind == 0x4003:
            return
        if kind == 0x4004:
            cursor.finish()
            self.saved_gdi_state = replace(self.host.state)
            self.saved_gdi_stack = list(self.host.state_stack)
            self.host.state_stack = []
            self.host.state = replace(self.host.state, world_transform=self.matrix(), clip=self.state.clip)
            self.gdi_active = True
            return
        if kind == 0x4008:
            self.object(flags, cursor)
            return
        if self.control(kind, flags, cursor):
            cursor.finish()
            return
        if kind == 0x401C:
            self.draw_string(flags, cursor)
        elif kind == 0x4036:
            self.driver_string(flags, cursor)
        elif kind in {0x401A, 0x401B}:
            self.draw_image(kind, flags, cursor)
        elif 0x4009 <= kind <= 0x4019 or kind in {0x4037}:
            self.draw_shape(kind, flags, cursor)
        elif kind in {0x4038}:
            self.warn("emfplus_effect_ignored", "image effects are not implemented")
        else:
            self.stop(f"unknown EMF+ record {kind:#x} may change graphics state")

    def control(self, kind: int, flags: int, cursor: Cursor) -> bool:
        """处理状态栈、变换、裁剪以及可明确近似的质量设置。"""
        if kind in {0x4025, 0x4028}:
            self.save(cursor.u32(), "save" if kind == 0x4025 else "container")
        elif kind in {0x4026, 0x4029}:
            self.restore(cursor.u32(), "save" if kind == 0x4026 else "container")
        elif kind == 0x4027:
            destination, source, index = cursor.rect(), cursor.rect(), cursor.u32()
            if not source.width or not source.height:
                raise MetafileMalformedError("zero EMF+ container source extent")
            self.units(flags & 255)
            parent = self.matrix()
            self.save(index, "container")
            mapping = Matrix(
                a=destination.width / source.width,
                d=destination.height / source.height,
                e=destination.left - source.left * destination.width / source.width,
                f=destination.top - source.top * destination.height / source.height,
            )
            self.state = PlusState(container=mapping.then(parent), unit=2, clip=self.state.clip)
        elif kind == 0x402B:
            self.state.world = Matrix()
        elif kind in {0x402A, 0x402C, 0x402D, 0x402E, 0x402F}:
            if kind in {0x402A, 0x402C}:
                matrix = cursor.matrix()
            elif kind == 0x402D:
                matrix = Matrix(e=cursor.f32(), f=cursor.f32())
            elif kind == 0x402E:
                matrix = Matrix(a=cursor.f32(), d=cursor.f32())
            else:
                angle = radians(cursor.f32())
                matrix = Matrix(a=cos(angle), b=sin(angle), c=-sin(angle), d=cos(angle))
            if kind == 0x402A:
                self.state.world = matrix
            else:
                self.state.world = self.state.world.then(matrix) if flags & 0x2000 else matrix.then(self.state.world)
            self.matrix()
        elif kind == 0x4030:
            self.units(flags & 255)
            scale = cursor.f32()
            if scale <= 0:
                raise MetafileMalformedError("EMF+ page scale must be positive")
            self.state.unit, self.state.page_scale = flags & 255, scale
        elif kind == 0x4031:
            self.state.clip = ()
        elif kind == 0x4032:
            self.clip(rectangle_path(cursor.rect()), (flags >> 8) & 15)
        elif kind == 0x4033:
            path = self.get(flags & 255, GraphicsPath)
            if isinstance(path, GraphicsPath):
                self.clip(path, (flags >> 8) & 15)
            else:
                self.stop("clip path object is unsupported")
        elif kind == 0x4034:
            self.get(flags & 255, UnsupportedObject)
            self.stop("complex region clipping is unsupported")
        elif kind == 0x4035:
            x, y = self.matrix().transform_vector(cursor.point())
            translation = Matrix(e=finite(x), f=finite(y))
            self.state.clip = tuple(replace(op, path=transform_path(op.path, translation)) for op in self.state.clip)
        elif kind == 0x401D:
            x, y = cursor.i32(), cursor.i32()
            if x or y:
                self.warn("emfplus_rendering_approximation", "rendering origin ignored")
        elif 0x401E <= kind <= 0x4024:
            defaults = {0x401E: {0, 1, 2}, 0x401F: {0}, 0x4020: {4}, 0x4021: {0, 1}, 0x4022: {0, 1}, 0x4023: {0}, 0x4024: {0}}
            value = flags & 255
            if value not in defaults[kind]:
                self.warn("emfplus_rendering_approximation", f"rendering setting {kind:#x}={value} uses the standard renderer")
        else:
            return False
        return True

    def draw_shape(self, kind: int, flags: int, cursor: Cursor) -> None:
        """解码基础图形并转换成共享路径，未实现的曲线或区域只跳过局部。"""
        if kind == 0x4009:
            self.host.append_command(ClearCommand(argb(cursor.u32()), self.bounds, self.state.clip))
            cursor.finish()
            return
        if kind in {0x4013, 0x4016, 0x4017, 0x4018, 0x4037}:
            self.warn("emfplus_drawing_skipped", f"drawing record {kind:#x} is not implemented")
            return
        fill = kind in {0x400A, 0x400C, 0x400E, 0x4010, 0x4014}
        brush = self.brush(cursor.u32(), flags) if fill else None
        if kind == 0x4015:
            pen = self.get(cursor.u32(), PlusPen)
        else:
            pen = None if fill else self.get(flags & 255, PlusPen)
        if kind in {0x4014, 0x4015}:
            path = self.get(flags & 255, GraphicsPath)
            paths = [path] if isinstance(path, GraphicsPath) else []
        elif kind in {0x400A, 0x400B}:
            count = cursor.count()
            self.host.charge_points(count * 4)
            paths = [rectangle_path(cursor.rect(bool(flags & 0x4000))) for _ in range(count)]
        elif kind in {0x400E, 0x400F}:
            paths = [ellipse_path(cursor.rect(bool(flags & 0x4000)))]
        elif kind in {0x4010, 0x4011, 0x4012}:
            start, sweep = cursor.f32(), cursor.f32()
            rect = cursor.rect(bool(flags & 0x4000))
            if abs(sweep) >= 360:
                paths = [ellipse_path(rect)]
            elif sweep == 0:
                paths = []
            else:
                center = ((rect.left + rect.right) / 2, (rect.top + rect.bottom) / 2)
                first = (center[0] + rect.width / 2 * cos(radians(start)), center[1] + rect.height / 2 * sin(radians(start)))
                last = (
                    center[0] + rect.width / 2 * cos(radians(start + sweep)),
                    center[1] + rect.height / 2 * sin(radians(start + sweep)),
                )
                paths = [
                    arc_path(
                        rect,
                        first,
                        last,
                        direction=2 if sweep > 0 else 1,
                        close_mode="pie" if kind in {0x4010, 0x4011} else "open",
                    )
                ]
        else:
            count = cursor.count()
            minimum = 4 if kind == 0x4019 else 3 if kind == 0x400C else 2
            if count < minimum:
                raise MetafileMalformedError("EMF+ shape has too few points")
            points = cursor.points(count, flags)
            builder = PathBuilder()
            if points:
                builder.move_to(points[0])
                if kind == 0x4019:
                    if (len(points) - 1) % 3:
                        raise MetafileMalformedError("invalid EMF+ Bezier point count")
                    for index in range(1, len(points), 3):
                        builder.cubic_to(*points[index : index + 3])
                else:
                    for point in points[1:]:
                        builder.line_to(point)
                    if kind == 0x400C or flags & 0x2000:
                        builder.close()
            paths = [builder.build()]
        cursor.finish()
        for path in paths:
            self.emit_path(path, brush=brush, pen=pen if isinstance(pen, PlusPen) else None)

    def draw_image(self, kind: int, flags: int, cursor: Cursor) -> None:
        """读取源裁剪与目标平行四边形，图片颜色处理使用明确降级。"""
        image = self.get(flags & 255, PlusImage)
        attributes, unit, source = cursor.u32(), cursor.u32(), cursor.rect()
        if unit != 2:
            raise MetafileMalformedError("EMF+ image source unit must be Pixel")
        if attributes != 0xFFFFFFFF:
            self.get(attributes, ImageAttributes)
            self.warn("emfplus_image_approximation", "image attributes use standard bitmap sampling")
        if kind == 0x401A:
            rect = cursor.rect(bool(flags & 0x4000))
            points = [(rect.left, rect.top), (rect.right, rect.top), (rect.left, rect.bottom)]
        else:
            count = cursor.count()
            if count != 3:
                raise MetafileMalformedError("EMF+ DrawImagePoints requires three points")
            points = cursor.points(count, flags)
        cursor.finish()
        if flags & 0x2000:
            self.warn("emfplus_effect_ignored", "image effect ignored")
        if not isinstance(image, PlusImage):
            return
        first, second, fourth = (self.point(p) for p in points)
        third = (finite(second[0] + fourth[0] - first[0]), finite(second[1] + fourth[1] - first[1]))
        self.host.append_command(
            DrawImageCommand(b"", b"", (first, second, third, fourth), source, 0, clip=self.state.clip, encoded_png=image.png)
        )

    def text_command(
        self,
        text: str,
        font: PlusFont,
        brush: PlusBrush,
        origin: Point,
        *,
        positions: tuple[Point, ...] = (),
        alignment: int = 0,
        matrix: Matrix | None = None,
        clip: ClipStack | None = None,
    ) -> None:
        """将 Unicode 文字映射为现有基线定位命令，并诊断字形仿射近似。"""
        if not text or brush.color is None:
            return
        matrix = matrix or self.matrix()
        logical_size = abs(font.font.height) * self.units(font.unit)[1] / self.units(self.state.unit)[1]
        height = finite(logical_size * hypot(matrix.c, matrix.d))
        if height <= 0:
            return
        if (
            abs(hypot(matrix.a, matrix.b) - hypot(matrix.c, matrix.d)) > 1e-5
            or abs(matrix.a * matrix.c + matrix.b * matrix.d) > 1e-5
        ):
            self.warn("emfplus_text_approximation", "nonuniform text transform uses rotated glyphs")
        self.host.charge_points(len(text))
        self.host.append_command(
            DrawTextCommand(
                text,
                self.point(origin, matrix),
                tuple(self.point(p, matrix) for p in positions),
                font.font,
                height,
                degrees(atan2(matrix.b, matrix.a)),
                24 | alignment,
                brush.color,
                brush.color,
                False,
                None,
                self.state.clip if clip is None else clip,
            )
        )

    def draw_string(self, flags: int, cursor: Cursor) -> None:
        """支持水平字符串、显式换行与布局矩形内的基本对齐。"""
        font = self.get(flags & 255, PlusFont)
        brush = self.brush(cursor.u32(), flags)
        format_id, length = cursor.u32(), cursor.count()
        rect, text = cursor.rect(), cursor.text(length)
        cursor.finish()
        format_value = self.get(format_id, StringFormat) if format_id != 0xFFFFFFFF else StringFormat()
        if not isinstance(font, PlusFont) or not isinstance(format_value, StringFormat):
            return
        if format_value.flags & 3 or "\t" in text:
            self.warn("emfplus_text_skipped", "vertical, right-to-left or tabbed text is not supported")
            return
        em = abs(font.font.height) * self.units(font.unit)[1] / self.units(self.state.unit)[1]
        loaded = load_font(font.font.face_name, max(1, round(em)), font.font.weight, font.font.italic, font.font.charset)
        ascent, descent = loaded.getmetrics()
        line_height = (ascent + descent) * em / max(1, round(em))
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        offset = max(0.0, rect.height - line_height * len(lines)) * (format_value.line_alignment / 2)
        baseline = rect.top + offset + ascent * em / max(1, round(em))
        alignment = {0: 0, 1: 6, 2: 2}[format_value.alignment]
        x = rect.left + rect.width * (format_value.alignment / 2)
        clip = self.state.clip
        if not format_value.flags & 0x4000 and rect.width > 0 and rect.height > 0:
            if len(clip) >= MAX_CLIP_OPERATIONS:
                raise MetafileResourceLimitError("EMF+ text clip exceeds budget")
            clip += (ClipOperation(transform_path(rectangle_path(rect), self.matrix()), "and"),)
        for line in lines:
            if (
                not format_value.flags & 0x1000
                and rect.width > 0
                and measure_text_advance(replace(font.font, height=-em), line) > rect.width
            ):
                self.warn("emfplus_text_approximation", "automatic line wrapping uses explicit line breaks only")
            self.text_command(line, font, brush, (x, baseline), alignment=alignment, clip=clip)
            baseline += line_height

    def driver_string(self, flags: int, cursor: Cursor) -> None:
        """解码 Unicode 模式的逐字位置；glyph 索引仅诊断并跳过。"""
        font = self.get(flags & 255, PlusFont)
        brush = self.brush(cursor.u32(), flags)
        options, has_matrix, count = cursor.u32(), cursor.u32(), cursor.count()
        if has_matrix not in {0, 1}:
            raise MetafileMalformedError("invalid EMF+ driver string matrix flag")
        raw = cursor.take(count * 2)
        points = cursor.points(1 if options & 4 and count else count, 0)
        extra = cursor.matrix() if has_matrix else Matrix()
        cursor.finish()
        if not options & 1 or options & 2:
            self.warn("emfplus_text_skipped", "glyph-index or vertical driver strings are not supported")
            return
        if not isinstance(font, PlusFont):
            return
        try:
            text = raw.decode("utf-16le")
        except UnicodeDecodeError as exc:
            raise MetafileMalformedError("invalid EMF+ driver string UTF-16") from exc
        if len(text) != count:
            self.warn("emfplus_text_skipped", "surrogate-pair driver positioning is not supported")
            return
        if options & 4:
            if not points:
                return
            x, y = points[0]
            points = []
            em = abs(font.font.height) * self.units(font.unit)[1] / self.units(self.state.unit)[1]
            for character in text:
                points.append((x, y))
                x += measure_text_advance(replace(font.font, height=-em), character)
            self.warn("emfplus_text_approximation", "driver advance uses installed font metrics")
        if points:
            self.text_command(text, font, brush, points[0], positions=tuple(points), matrix=extra.then(self.matrix()))


__all__ = ["EmfPlusPlayback"]
