# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""EMF+ 图形对象解码与有界位图规范化。"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Callable, TypeAlias

from PIL import Image, UnidentifiedImageError

from ..binary import BoundedReader
from ..geometry import PathBuilder
from ..limits import MAX_CANVAS_DIMENSION, MAX_CANVAS_PIXELS, MAX_EMBEDDED_BITMAP_BYTES
from ..models import Color, Font, GraphicsPath, MetafileMalformedError, MetafileResourceLimitError, Pen
from .binary import Cursor, argb

Warn: TypeAlias = Callable[[str, str], None]


@dataclass(frozen=True, slots=True)
class UnsupportedObject:
    """占据已定义但未实现的对象槽，防止误用被覆盖的旧对象。"""

    kind: int


@dataclass(frozen=True, slots=True)
class PlusBrush:
    """保存纯色或可诊断的不可用填充。"""

    color: Color | None


@dataclass(frozen=True, slots=True)
class PlusPen:
    """保存画笔及其计量单位。"""

    pen: Pen
    unit: int
    miter_limit: float = 10.0


@dataclass(frozen=True, slots=True)
class PlusFont:
    """保存字体及 em 尺寸所使用的单位。"""

    font: Font
    unit: int


@dataclass(frozen=True, slots=True)
class PlusImage:
    """保存经过尺寸校验的标准 PNG 位图。"""

    png: bytes
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class StringFormat:
    """保存首版支持的水平与垂直对齐，以及格式限制。"""

    flags: int = 0
    alignment: int = 0
    line_alignment: int = 0


@dataclass(frozen=True, slots=True)
class ImageAttributes:
    """标记已验证的图片属性对象，颜色处理按首版策略近似。"""

    wrap_mode: int


PlusObject: TypeAlias = (
    PlusBrush | PlusPen | PlusFont | PlusImage | GraphicsPath | StringFormat | ImageAttributes | UnsupportedObject
)


def brush(cursor: Cursor, warn: Warn) -> PlusBrush:
    """解码纯色画刷，渐变和阴影线仅取明确的代表颜色。"""
    cursor.version()
    kind = cursor.u32()
    if kind == 0:
        return PlusBrush(argb(cursor.u32()))
    if kind == 1:
        cursor.u32()
        color = argb(cursor.u32())
        cursor.u32()
    elif kind == 4:
        cursor.u32()
        cursor.u32()
        cursor.rect()
        color = argb(cursor.u32())
        cursor.take(12)
    elif kind == 3:
        cursor.u32()
        cursor.u32()
        color = argb(cursor.u32())
        cursor.point()
        cursor.take(cursor.count() * 4)
    else:
        warn("emfplus_unsupported_brush", f"brush type {kind} has no supported representative color; drawing skipped")
        return PlusBrush(None)
    warn("emfplus_brush_approximation", f"brush type {kind} uses a representative solid color")
    return PlusBrush(color)


def path(cursor: Cursor) -> GraphicsPath:
    """将绝对或相对路径及类型数组解码为统一路径。"""
    count, flags = cursor.count(), cursor.u32()
    points = cursor.points(count, flags)
    types: list[int] = []
    if flags & 0x0800:
        while len(types) < count:
            run, point_type = cursor.u8(), cursor.u8()
            repeat = run & 63
            if not repeat or len(types) + repeat > count or not run & 64:
                raise MetafileMalformedError("invalid EMF+ path type run")
            types.extend([point_type] * repeat)
    else:
        types = list(cursor.take(count))
    builder = PathBuilder()
    index = 0
    while index < count:
        kind = types[index] & 7
        close = bool(types[index] & 128)
        if kind == 0:
            builder.move_to(points[index])
        elif kind == 1 and builder.current is not None:
            builder.line_to(points[index])
        elif kind == 3 and builder.current is not None:
            if index + 2 >= count or any(types[i] & 7 != 3 for i in range(index, index + 3)):
                raise MetafileMalformedError("incomplete EMF+ Bezier path segment")
            builder.cubic_to(*points[index : index + 3])
            close = bool(types[index + 2] & 128)
            index += 2
        else:
            raise MetafileMalformedError("invalid EMF+ path point type or missing start")
        if close:
            builder.close()
        index += 1
    cursor.finish()
    return builder.build()


def pen(cursor: Cursor, warn: Warn) -> PlusPen:
    """按标志顺序读取变长画笔选项，保留常见线型并诊断近似。"""
    if cursor.u32() != 0:
        raise MetafileMalformedError("invalid EMF+ pen type")
    flags, unit, width = cursor.u32(), cursor.u32(), cursor.f32()
    if flags & ~0x1FFF or width < 0 or unit > 6:
        raise MetafileMalformedError("invalid EMF+ pen flags, unit or width")
    start = end = join = style = 0
    miter = 10.0
    dashes: tuple[float, ...] = ()
    if flags & 1:
        cursor.matrix()
        warn("emfplus_pen_approximation", "pen-specific transform ignored")
    if flags & 2:
        start = cursor.u32()
    if flags & 4:
        end = cursor.u32()
    if flags & 8:
        join = cursor.u32()
    if flags & 16:
        miter = max(1.0, cursor.f32())
    if flags & 32:
        style = cursor.u32()
    if flags & 64 and cursor.u32():
        warn("emfplus_pen_approximation", "dash caps use the standard stroke caps")
    if flags & 128 and cursor.f32():
        warn("emfplus_pen_approximation", "dash offset ignored")
    if flags & 256:
        dashes = tuple(cursor.f32() for _ in range(cursor.count()))
        if any(value <= 0 for value in dashes):
            raise MetafileMalformedError("EMF+ dash lengths must be positive")
    if flags & 512 and cursor.u32():
        warn("emfplus_pen_approximation", "pen alignment uses centered strokes")
    if flags & 1024:
        cursor.take(cursor.count() * 4)
        warn("emfplus_pen_approximation", "compound pen uses a single stroke")
    for flag in (2048, 4096):
        if flags & flag:
            cursor.take(cursor.u32())
            warn("emfplus_pen_approximation", "custom line cap ignored")
    fill = brush(cursor, warn)
    if start != end or start not in {0, 1, 2} or join > 2:
        warn("emfplus_pen_approximation", "unsupported or asymmetric caps/joins use standard strokes")
    if not dashes:
        patterns = {0: (), 1: (3.0, 1.0), 2: (1.0, 1.0), 3: (3.0, 1.0, 1.0, 1.0), 4: (3.0, 1.0, 1.0, 1.0, 1.0, 1.0)}
        dashes = patterns.get(style, ())
        if style not in patterns:
            warn("emfplus_pen_approximation", "unknown dash style uses a solid stroke")
    return PlusPen(
        Pen(
            color=fill.color or Color(0, 0, 0, 0),
            width=width,
            cosmetic=False,
            null=fill.color is None,
            cap={0: "flat", 1: "square", 2: "round"}.get(start, "flat"),
            join={0: "miter", 1: "bevel", 2: "round"}.get(join, "miter"),
            dashes=dashes,
        ),
        unit,
        miter,
    )


def image_size(width: int, height: int) -> None:
    """在像素分配之前执行单边与总像素预算。"""
    if width <= 0 or height <= 0:
        raise MetafileMalformedError("invalid EMF+ bitmap dimensions")
    if max(width, height) > MAX_CANVAS_DIMENSION or width * height > MAX_CANVAS_PIXELS:
        raise MetafileResourceLimitError("EMF+ bitmap dimensions exceed pixel budget")


def image(cursor: Cursor, warn: Warn) -> PlusImage | UnsupportedObject:
    """将压缩或常见原始位图转换为有界 PNG，保持直通与预乘 alpha。"""
    kind = cursor.u32()
    if kind != 1:
        warn("emfplus_unsupported_image", "nested metafile images are not supported")
        return UnsupportedObject(5)
    width, height, stride = cursor.i32(), cursor.i32(), cursor.i32()
    pixel_format, encoding = cursor.u32(), cursor.u32()
    payload = cursor.take(cursor.reader.remaining(cursor.offset))
    if len(payload) > MAX_EMBEDDED_BITMAP_BYTES:
        raise MetafileResourceLimitError("EMF+ image payload exceeds byte budget")
    try:
        if encoding == 1:
            with Image.open(BytesIO(payload)) as source:
                if source.format not in {"PNG", "JPEG"}:
                    warn("emfplus_unsupported_image", f"compressed image format {source.format} skipped")
                    return UnsupportedObject(5)
                image_size(*source.size)
                source.load()
                bitmap = source.convert("RGBA")
        elif encoding == 0:
            image_size(width, height)
            bpp = (pixel_format >> 8) & 255
            if pixel_format & 0x10000 or bpp not in {24, 32}:
                warn("emfplus_unsupported_image", f"raw pixel format {pixel_format:#x} skipped")
                return UnsupportedObject(5)
            if abs(stride) < width * (bpp // 8) or abs(stride) * height > len(payload):
                raise MetafileMalformedError("EMF+ bitmap stride or pixel data is invalid")
            if abs(stride) * height > MAX_EMBEDDED_BITMAP_BYTES:
                raise MetafileResourceLimitError("EMF+ raw bitmap exceeds byte budget")
            mode, rawmode = ("RGB", "BGR") if bpp == 24 else ("RGB", "BGRX")
            if bpp == 32 and pixel_format & 0x40000:
                mode, rawmode = ("RGBa", "BGRa") if pixel_format & 0x80000 else ("RGBA", "BGRA")
            bitmap = Image.frombytes(
                mode, (width, height), payload, "raw", rawmode, abs(stride), 1 if stride > 0 else -1
            ).convert("RGBA")
        else:
            raise MetafileMalformedError("invalid EMF+ bitmap encoding")
        output = BytesIO()
        bitmap.save(output, format="PNG")
        if output.tell() > MAX_EMBEDDED_BITMAP_BYTES:
            raise MetafileResourceLimitError("EMF+ normalized bitmap exceeds byte budget")
        return PlusImage(output.getvalue(), *bitmap.size)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        if isinstance(exc, (MetafileMalformedError, MetafileResourceLimitError)):
            raise
        raise MetafileMalformedError("EMF+ bitmap cannot be decoded") from exc


def parse_object(kind: int, data: BoundedReader, warn: Warn) -> PlusObject:
    """按显式对象类型解码，未知对象保留不可用占位。"""
    cursor = Cursor(data)
    if kind == 1:
        return brush(cursor, warn)
    cursor.version()
    if kind == 2:
        return pen(cursor, warn)
    if kind == 3:
        return path(cursor)
    if kind == 5:
        return image(cursor, warn)
    if kind == 6:
        size, unit, style = cursor.f32(), cursor.u32(), cursor.u32()
        cursor.u32()
        name = cursor.text(cursor.count()).rstrip("\x00")
        if size <= 0 or unit > 6:
            raise MetafileMalformedError("invalid EMF+ font size or unit")
        cursor.finish()
        return PlusFont(
            Font(
                face_name=name,
                height=-size,
                weight=700 if style & 1 else 400,
                italic=bool(style & 2),
                underline=bool(style & 4),
                strikeout=bool(style & 8),
            ),
            unit,
        )
    if kind == 7:
        flags = cursor.u32()
        cursor.u32()
        alignment, vertical = cursor.u32(), cursor.u32()
        cursor.take(12)
        hotkey = cursor.u32()
        cursor.f32()
        cursor.f32()
        tracking, trimming = cursor.f32(), cursor.u32()
        if hotkey or abs(tracking - 1) > 1e-6 or trimming:
            warn("emfplus_text_approximation", "advanced string formatting uses basic alignment and clipping")
        tabs, ranges = cursor.count(), cursor.count()
        cursor.take(tabs * 4 + ranges * 8)
        cursor.finish()
        if alignment > 2 or vertical > 2:
            raise MetafileMalformedError("invalid EMF+ string alignment")
        if tabs or ranges:
            warn("emfplus_text_approximation", "tab stops and character ranges use basic text layout")
        return StringFormat(flags, alignment, vertical)
    if kind == 8:
        cursor.u32()
        wrap = cursor.u32()
        cursor.take(12)
        cursor.finish()
        return ImageAttributes(wrap)
    warn("emfplus_unsupported_object", f"object type {kind} is not implemented")
    return UnsupportedObject(kind)


__all__ = [
    "ImageAttributes",
    "PlusBrush",
    "PlusFont",
    "PlusImage",
    "PlusObject",
    "PlusPen",
    "StringFormat",
    "UnsupportedObject",
    "image_size",
    "parse_object",
]
