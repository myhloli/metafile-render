"""构造符合规范的 EMF+ Only/Dual 与有界错误测试载荷。"""

from __future__ import annotations

import struct

from _metafile_test_utils import build_emf, emf_record

VERSION = 0xDBC01002


def plus_record(kind: int, payload: bytes = b"", flags: int = 0) -> bytes:
    """将子记录按四字节对齐，DataSize 保留真实载荷长度。"""
    padded = payload + b"\0" * (-len(payload) % 4)
    return struct.pack("<HHII", kind, flags, 12 + len(padded), len(payload)) + padded


def plus_header(*, dual: bool = False, dpi: int = 100) -> bytes:
    """构造具有图形版本、视频标志及逻辑 DPI 的真实 Header。"""
    return plus_record(0x4001, struct.pack("<IIII", VERSION, 1, dpi, dpi), int(dual))


def plus_comment(records: list[bytes]) -> bytes:
    """把多个 EMF+ 记录装入一个 EMR_COMMENT。"""
    payload = b"EMF+" + b"".join(records)
    return emf_record(70, struct.pack("<I", len(payload)) + payload)


def plus_file(records: list[bytes]) -> bytes:
    """构造逻辑与设备均为 100 DPI 的确定性 Only 文件。"""
    return build_emf([plus_comment([plus_header(), *records, plus_record(0x4002)])])


def plus_object(slot: int, kind: int, payload: bytes) -> bytes:
    """构造带类型和槽位的图形对象。"""
    return plus_record(0x4008, payload, slot | kind << 8)


def solid(color: int) -> bytes:
    """构造可用于对象或画笔内部的 ARGB 画刷。"""
    return struct.pack("<III", VERSION, 0, color)


def fill_rect(
    color: int = 0xFFFF0000, rect: tuple[float, float, float, float] = (10, 10, 30, 30), *, inline: bool = True
) -> bytes:
    """构造单个纯色矩形，可选择引用对象槽。"""
    return plus_record(0x400A, struct.pack("<II4f", color, 1, *rect), 0x8000 if inline else 0)


def font_object(slot: int = 0, name: str = "Arial", size: float = 12) -> bytes:
    """构造使用 Pixel 单位的 Unicode 字体对象。"""
    encoded = name.encode("utf-16le")
    return plus_object(slot, 6, struct.pack("<IfIIII", VERSION, size, 2, 0, 0, len(encoded) // 2) + encoded)


def string_record(text: str, *, font: int = 0, format_id: int = 0xFFFFFFFF) -> bytes:
    """构造带布局矩形的 DrawString 记录。"""
    encoded = text.encode("utf-16le")
    return plus_record(
        0x401C, struct.pack("<III4f", 0xFF000000, format_id, len(encoded) // 2, 5, 5, 90, 70) + encoded, 0x8000 | font
    )


__all__ = [
    "VERSION",
    "fill_rect",
    "font_object",
    "plus_comment",
    "plus_file",
    "plus_header",
    "plus_object",
    "plus_record",
    "solid",
    "string_record",
]
