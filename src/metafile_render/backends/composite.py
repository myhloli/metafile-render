# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF backends.composite 内部实现。"""

from __future__ import annotations

from PIL import Image, ImageChops

from .constants import _BLACKNESS, _DSTINVERT, _SRCAND, _SRCCOPY, _SRCINVERT, _SRCPAINT, _WHITENESS


def _bitwise_channel_bytes(destination: bytes, source: bytes, operation: str) -> bytes:
    """对等长 RGB 字节串执行精确 AND、OR、XOR 或反相。"""
    if operation == "xor":
        return bytes(left ^ right for left, right in zip(destination, source))
    if operation == "and":
        return bytes(left & right for left, right in zip(destination, source))
    if operation == "or":
        return bytes(left | right for left, right in zip(destination, source))
    if operation == "not_or":
        return bytes((~(left | right)) & 0xFF for left, right in zip(destination, source))
    if operation == "and_not_source":
        return bytes(left & (~right & 0xFF) for left, right in zip(destination, source))
    if operation == "not_source":
        return bytes(255 - value for value in source)
    if operation == "source_and_not_destination":
        return bytes(right & (~left & 0xFF) for left, right in zip(destination, source))
    if operation == "not_and":
        return bytes((~(left & right)) & 0xFF for left, right in zip(destination, source))
    if operation == "not_xor":
        return bytes((~(left ^ right)) & 0xFF for left, right in zip(destination, source))
    if operation == "destination_or_not_source":
        return bytes(left | (~right & 0xFF) for left, right in zip(destination, source))
    if operation == "source_or_not_destination":
        return bytes(right | (~left & 0xFF) for left, right in zip(destination, source))
    if operation == "invert":
        return bytes(255 - value for value in destination)
    fill = 0 if operation == "black" else 255
    return bytes([fill]) * len(destination)


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
        canvas.alpha_composite(layer)
    elif rop2 != 11:
        _paste_bitwise(canvas, layer, operations.get(rop2, "xor"))


def _composite_image(canvas: Image.Image, layer: Image.Image, rop: int) -> None:
    """按常见 ROP3 或 AlphaBlend 结果合成位图层。"""
    if rop in {_SRCCOPY, 0}:
        canvas.alpha_composite(layer)
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
        canvas.alpha_composite(layer)


__all__ = ["_bitwise_channel_bytes", "_paste_bitwise", "_composite_path", "_composite_image"]
