# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF backends.bitmap 内部实现。"""

from __future__ import annotations

from io import BytesIO
from math import ceil, floor

from PIL import Image, UnidentifiedImageError

from ..commands import DibPayload, DrawImageCommand, EncodedImage
from ..dib import dib_dimensions, validate_dib_payload
from ..geometry import FlattenBudget
from ..limits import MAX_CANVAS_PIXELS, MAX_EMBEDDED_BITMAP_BYTES
from ..models import MetafileMalformedError, MetafileResourceLimitError
from ..primitives import Matrix, Point, Rect
from .paths import _apply_clip
from .session import RenderSession


def _dib_to_bmp(command: DrawImageCommand) -> bytes:
    """为分离的 DIB header 和像素数据补齐 BMP 文件头。"""
    if not isinstance(command.image, DibPayload):
        raise MetafileMalformedError("expected DIB payload")
    if len(command.image.header) < 4:
        raise MetafileMalformedError("DIB header is truncated")
    pixel_offset = 14 + len(command.image.header)
    total_size = pixel_offset + len(command.image.bits)
    if total_size > MAX_EMBEDDED_BITMAP_BYTES + 14:
        raise MetafileResourceLimitError("decoded BMP exceeds the embedded bitmap byte budget")
    import struct

    return b"BM" + struct.pack("<IHHI", total_size, 0, 0, pixel_offset) + command.image.header + command.image.bits


def _decode_raw_alpha_dib(command: DrawImageCommand) -> Image.Image | None:
    """对 AC_SRC_ALPHA 的常见 32 位 DIB 保留原始 alpha 字节。"""
    if not isinstance(command.image, DibPayload):
        return None
    header = command.image.header
    if len(header) < 40:
        return None
    import struct

    header_size, width, signed_height = struct.unpack_from("<Iii", header, 0)
    bit_count = struct.unpack_from("<H", header, 14)[0]
    compression = struct.unpack_from("<I", header, 16)[0]
    if header_size < 40 or width <= 0 or signed_height == 0 or bit_count != 32 or compression not in {0, 3, 6}:
        return None
    height = abs(signed_height)
    if width * height > MAX_CANVAS_PIXELS or len(command.image.bits) < width * height * 4:
        raise MetafileResourceLimitError("32-bit DIB exceeds pixel budget or is truncated")
    orientation = -1 if signed_height > 0 else 1
    try:
        premultiplied = Image.frombytes(
            "RGBa",
            (width, height),
            command.image.bits[: width * height * 4],
            "raw",
            "BGRa",
            width * 4,
            orientation,
        )
        return premultiplied.convert("RGBA")
    except (OSError, ValueError) as exc:
        raise MetafileMalformedError("32-bit DIB alpha payload cannot be decoded") from exc


def _validate_dib_dimensions(header: bytes) -> None:
    """在进入 Pillow decoder 前验证 DIB 声明尺寸与固定像素预算。"""
    dib_dimensions(header)


def _decode_dib(command: DrawImageCommand, session: RenderSession | None = None) -> Image.Image:
    """按不可变图片载荷复用解码结果，对调用方只暴露独立副本。"""
    key = ("image", command.image)
    cached = session.cache.get(key) if session is not None else None
    if cached is not None:
        return cached
    image = _decode_image_payload(command)
    if session is not None:
        session.cache.put(key, image)
    return image


def _decode_image_payload(command: DrawImageCommand) -> Image.Image:
    """严格解码 DIB，并在需要时恢复源 alpha。"""
    if isinstance(command.image, EncodedImage):
        if len(command.image.png) > MAX_EMBEDDED_BITMAP_BYTES:
            raise MetafileResourceLimitError("normalized image exceeds byte budget")
        try:
            with Image.open(BytesIO(command.image.png)) as image:
                if image.format != "PNG" or image.width * image.height > MAX_CANVAS_PIXELS:
                    raise MetafileMalformedError("invalid normalized EMF+ bitmap")
                image.load()
                return image.convert("RGBA")
        except (OSError, ValueError) as exc:
            raise MetafileMalformedError("normalized EMF+ bitmap cannot be decoded") from exc
    validate_dib_payload(command.image)
    if command.image.use_source_alpha:
        raw_alpha = _decode_raw_alpha_dib(command)
        if raw_alpha is not None:
            return raw_alpha
    try:
        with Image.open(BytesIO(_dib_to_bmp(command))) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_CANVAS_PIXELS:
                raise MetafileResourceLimitError(f"DIB dimensions exceed pixel budget: {width}x{height}")
            image.load()
            decoded = image.convert("RGBA")
    except MetafileResourceLimitError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise MetafileMalformedError("embedded DIB cannot be decoded by Pillow") from exc
    if not command.image.use_source_alpha:
        decoded.putalpha(255)
    return decoded


def _crop_source(image: Image.Image, source: Rect | None) -> tuple[Image.Image | None, Rect]:
    """裁剪 GDI source rect，并返回交集在请求矩形中的归一化位置。"""
    if source is None:
        return image, Rect(0.0, 0.0, 1.0, 1.0)
    if abs(source.width) <= 1e-9 or abs(source.height) <= 1e-9:
        return None, Rect(0.0, 0.0, 0.0, 0.0)
    flip_x = source.width < 0
    flip_y = source.height < 0
    normalized = source.normalized()
    left = max(0, floor(normalized.left))
    top = max(0, floor(normalized.top))
    right = min(image.width, ceil(normalized.right))
    bottom = min(image.height, ceil(normalized.bottom))
    if right <= left or bottom <= top:
        return None, Rect(0.0, 0.0, 0.0, 0.0)
    horizontal = sorted(((left - source.left) / source.width, (right - source.left) / source.width))
    vertical = sorted(((top - source.top) / source.height, (bottom - source.top) / source.height))
    fraction = Rect(
        max(0.0, horizontal[0]),
        max(0.0, vertical[0]),
        min(1.0, horizontal[1]),
        min(1.0, vertical[1]),
    )
    cropped = image.crop((left, top, right, bottom))
    if flip_x:
        cropped = cropped.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if flip_y:
        cropped = cropped.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    return cropped, fraction


def _crop_destination(
    destination: tuple[Point, Point, Point, Point],
    fraction: Rect,
) -> tuple[Point, Point, Point, Point]:
    """把 source 可见交集的归一化位置映射到目标平行四边形。"""
    first, second, _third, fourth = destination
    axis_x = second[0] - first[0], second[1] - first[1]
    axis_y = fourth[0] - first[0], fourth[1] - first[1]

    def mapped(u: float, v: float) -> Point:
        """把 source 归一化坐标映射到 destination。"""
        return first[0] + axis_x[0] * u + axis_y[0] * v, first[1] + axis_x[1] * u + axis_y[1] * v

    return (
        mapped(fraction.left, fraction.top),
        mapped(fraction.right, fraction.top),
        mapped(fraction.right, fraction.bottom),
        mapped(fraction.left, fraction.bottom),
    )


def _affine_image_layer(
    image: Image.Image,
    destination: tuple[Point, Point, Point, Point],
    matrix: Matrix,
    size: tuple[int, int],
    stretch_mode: int,
) -> Image.Image:
    """把源图片仿射映射到目标平行四边形。"""
    first, second, _third, fourth = (matrix.transform_point(point) for point in destination)
    axis_x = second[0] - first[0], second[1] - first[1]
    axis_y = fourth[0] - first[0], fourth[1] - first[1]
    determinant = axis_x[0] * axis_y[1] - axis_x[1] * axis_y[0]
    if abs(determinant) < 1e-9:
        return Image.new("RGBA", size, (0, 0, 0, 0))
    inverse_00 = axis_y[1] / determinant
    inverse_01 = -axis_y[0] / determinant
    inverse_10 = -axis_x[1] / determinant
    inverse_11 = axis_x[0] / determinant
    scale_x = image.width
    scale_y = image.height
    a = inverse_00 * scale_x
    b = inverse_01 * scale_x
    c = -(inverse_00 * first[0] + inverse_01 * first[1]) * scale_x
    d = inverse_10 * scale_y
    e = inverse_11 * scale_y
    f = -(inverse_10 * first[0] + inverse_11 * first[1]) * scale_y
    resample = Image.Resampling.BICUBIC if stretch_mode == 4 else Image.Resampling.NEAREST
    return image.transform(
        size,
        Image.Transform.AFFINE,
        (a, b, c, d, e, f),
        resample=resample,
        fillcolor=(0, 0, 0, 0),
    )


def _render_image_command(
    command: DrawImageCommand,
    matrix: Matrix,
    size: tuple[int, int],
    budget: FlattenBudget,
    session: RenderSession | None = None,
) -> Image.Image:
    """解码、裁剪并仿射放置单条位图命令。"""
    image, source_fraction = _crop_source(_decode_dib(command, session), command.source)
    if image is None:
        return Image.new("RGBA", size, (0, 0, 0, 0))
    if command.constant_alpha < 255:
        alpha = image.getchannel("A").point(lambda value: value * command.constant_alpha // 255)
        image.putalpha(alpha)
    destination = _crop_destination(command.destination, source_fraction)
    layer = _affine_image_layer(image, destination, matrix, size, command.stretch_mode)
    _apply_clip(layer, command.clip, matrix, budget, session)
    return layer


__all__ = [
    "_dib_to_bmp",
    "_decode_raw_alpha_dib",
    "_validate_dib_dimensions",
    "_decode_dib",
    "_crop_source",
    "_crop_destination",
    "_affine_image_layer",
    "_render_image_command",
]
