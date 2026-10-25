# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF backends.composite 内部实现。"""

from __future__ import annotations

from PIL import Image, ImageChops

from .._native_points import get_native, record_native_rop
from .constants import _BLACKNESS, _DSTINVERT, _SRCAND, _SRCCOPY, _SRCINVERT, _SRCPAINT, _WHITENESS


_COMPOSITE_REGION_MIN_PIXELS = 1024 * 1024


def _alpha_composite_rgba(canvas: Image.Image, layer: Image.Image) -> None:
    """大画布仅合成源 alpha 覆盖框，保留透明源下目标的全部 RGBA 字节。"""
    if (
        canvas.mode != "RGBA"
        or layer.mode != "RGBA"
        or canvas.size != layer.size
        or layer.width * layer.height < _COMPOSITE_REGION_MIN_PIXELS
    ):
        canvas.alpha_composite(layer)
        return
    bbox = layer.getbbox(alpha_only=True)
    if bbox is None:
        return
    # Pillow 局部合成同时持有源副本、目标副本和结果，三份覆盖框不应大于原整图结果。
    region_pixels = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    if region_pixels * 3 >= layer.width * layer.height:
        canvas.alpha_composite(layer)
    else:
        canvas.alpha_composite(layer, dest=bbox[:2], source=bbox)


def _bitwise_channel_bytes(destination: bytes, source: bytes, operation: str) -> bytes:
    """对等长 RGB 字节串执行精确 AND、OR、XOR 或反相。"""
    result: bytes = get_native().bitwise_channel_bytes(destination, source, operation)
    record_native_rop(len(result))
    return result


def _paste_bitwise(canvas: Image.Image, layer: Image.Image, operation: str) -> None:
    """只在 layer 覆盖框内应用精确逐通道 GDI 逻辑合成。"""
    mask = layer.getchannel("A")
    bbox = mask.getbbox()
    if bbox is None:
        return
    destination = canvas.crop(bbox).convert("RGB")
    source = layer.crop(bbox).convert("RGB")
    result = Image.frombytes(
        "RGB",
        destination.size,
        _bitwise_channel_bytes(destination.tobytes(), source.tobytes(), operation),
    ).convert("RGBA")
    local_mask = mask.crop(bbox)
    result.putalpha(ImageChops.lighter(canvas.getchannel("A").crop(bbox), local_mask))
    canvas.paste(result, bbox[:2], local_mask)


def _composite_path(canvas: Image.Image, layer: Image.Image, rop2: int) -> None:
    """按常见 ROP2 或默认 source-over 合成路径层。"""
    operations = {
        1: "black",
        2: "not_or",
        3: "and_not_source",
        4: "not_source",
        5: "source_and_not_destination",
        6: "invert",
        7: "xor",
        8: "not_and",
        9: "and",
        10: "not_xor",
        12: "destination_or_not_source",
        14: "source_or_not_destination",
        15: "or",
        16: "white",
    }
    if rop2 == 13:
        _alpha_composite_rgba(canvas, layer)
    elif rop2 != 11:
        _paste_bitwise(canvas, layer, operations.get(rop2, "xor"))


def _composite_image(canvas: Image.Image, layer: Image.Image, rop: int) -> None:
    """按常见 ROP3 或 AlphaBlend 结果合成位图层。"""
    if rop in {_SRCCOPY, 0}:
        _alpha_composite_rgba(canvas, layer)
    elif rop == _SRCAND:
        _paste_bitwise(canvas, layer, "and")
    elif rop == _SRCPAINT:
        _paste_bitwise(canvas, layer, "or")
    elif rop == _SRCINVERT:
        _paste_bitwise(canvas, layer, "xor")
    elif rop == _DSTINVERT:
        _paste_bitwise(canvas, layer, "invert")
    elif rop == _BLACKNESS:
        _paste_bitwise(canvas, layer, "black")
    elif rop == _WHITENESS:
        _paste_bitwise(canvas, layer, "white")
    else:
        _alpha_composite_rgba(canvas, layer)


__all__ = ["_bitwise_channel_bytes", "_paste_bitwise", "_composite_path", "_composite_image"]
