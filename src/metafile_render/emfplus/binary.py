# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""EMF+ 有界标量、坐标和记录读取。"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterator

from ..binary import BoundedReader
from ..limits import MAX_POINTS_PER_RECORD
from ..models import MetafileMalformedError, MetafileResourceLimitError
from ..primitives import Color, Matrix, Point, Rect


def finite(value: float) -> float:
    """拒绝非有限值与无法安全回放的极端坐标。"""
    if not isfinite(value) or abs(value) > 1e12:
        raise MetafileMalformedError("EMF+ contains non-finite or excessive coordinates")
    return value


def argb(value: int) -> Color:
    """将 EMF+ ARGB 标量转换成统一 RGBA 颜色。"""
    return Color((value >> 16) & 255, (value >> 8) & 255, value & 255, value >> 24)


class Cursor:
    """为变长对象维护单调前进的有界游标。"""

    def __init__(self, reader: BoundedReader) -> None:
        """保存不可越过的对象或记录边界。"""
        self.reader = reader
        self.offset = 0

    def take(self, size: int) -> bytes:
        """读取定长片段并推进游标。"""
        value = self.reader.bytes(self.offset, size)
        self.offset += size
        return value

    def u8(self) -> int:
        """读取单字节。"""
        value = self.reader.u8(self.offset)
        self.offset += 1
        return value

    def u32(self) -> int:
        """读取无符号 DWORD。"""
        value = self.reader.u32(self.offset)
        self.offset += 4
        return value

    def i32(self) -> int:
        """读取有符号 DWORD。"""
        value = self.reader.i32(self.offset)
        self.offset += 4
        return value

    def f32(self) -> float:
        """读取并验证浮点数。"""
        value = finite(self.reader.f32(self.offset))
        self.offset += 4
        return value

    def count(self) -> int:
        """在分配数组之前校验元素数量。"""
        count = self.u32()
        if count > MAX_POINTS_PER_RECORD:
            raise MetafileResourceLimitError("EMF+ count exceeds max_points_per_record")
        return count

    def relative(self) -> int:
        """解码以高位区分的七位或十五位有符号相对坐标。"""
        first = self.u8()
        if first & 128:
            value = ((first & 127) << 8) | self.u8()
            return value - 32768 if value & 16384 else value
        return first - 128 if first & 64 else first

    def point(self, compressed: bool = False) -> Point:
        """读取绝对整数或浮点坐标。"""
        if compressed:
            point = self.reader.i16(self.offset), self.reader.i16(self.offset + 2)
            self.offset += 4
            return float(point[0]), float(point[1])
        return self.f32(), self.f32()

    def points(self, count: int, flags: int) -> list[Point]:
        """解码浮点、压缩整数或累加相对坐标数组。"""
        if count > MAX_POINTS_PER_RECORD:
            raise MetafileResourceLimitError("EMF+ point array exceeds max_points_per_record")
        if not flags & 0x0800:
            stride = 4 if flags & 0x4000 else 8
            self.reader.subreader(self.offset, count * stride)
            return [self.point(bool(flags & 0x4000)) for _ in range(count)]
        points: list[Point] = []
        x = y = 0.0
        for _ in range(count):
            x, y = finite(x + self.relative()), finite(y + self.relative())
            points.append((x, y))
        return points

    def rect(self, compressed: bool = False) -> Rect:
        """读取以位置和宽高表达的矩形。"""
        x, y = self.point(compressed)
        w, h = self.point(compressed)
        return Rect(x, y, finite(x + w), finite(y + h))

    def matrix(self) -> Matrix:
        """读取六分量仿射矩阵。"""
        return Matrix(*(self.f32() for _ in range(6)))

    def version(self) -> None:
        """验证 EMF+ 图形对象的固定签名。"""
        if self.u32() >> 12 != 0xDBC01:
            raise MetafileMalformedError("invalid EMF+ graphics version signature")

    def text(self, count: int) -> str:
        """读取 UTF-16 文本并将非法编码转为稳定格式错误。"""
        if count > MAX_POINTS_PER_RECORD:
            raise MetafileResourceLimitError("EMF+ text exceeds character budget")
        try:
            return self.take(count * 2).decode("utf-16le")
        except UnicodeDecodeError as exc:
            raise MetafileMalformedError("invalid EMF+ UTF-16 text") from exc

    def finish(self) -> None:
        """完整消费后仅允许最多三个对齐字节。"""
        if self.reader.remaining(self.offset) > 3:
            raise MetafileMalformedError("unexpected trailing EMF+ record data")


@dataclass(frozen=True, slots=True)
class PlusRecord:
    """保留 EMF+ 子记录和原文件中的诊断偏移。"""

    kind: int
    flags: int
    payload: BoundedReader
    offset: int


def comment_records(record: BoundedReader) -> Iterator[PlusRecord]:
    """验证 EMR_COMMENT 内的多条 EMF+ 子记录，普通注释返回空序列。"""
    size = record.u32(8)
    comment = record.subreader(12, size)
    if len(comment) < 4 or comment.u32(0) != 0x2B464D45:
        return
    offset = 4
    while offset < len(comment):
        header = comment.subreader(offset, 12)
        kind, flags, length, data_size = header.u16(0), header.u16(2), header.u32(4), header.u32(8)
        if length < 12 or length % 4 or data_size > length - 12 or length - 12 - data_size > 3:
            raise MetafileMalformedError("invalid EMF+ record size")
        entry = comment.subreader(offset, length)
        yield PlusRecord(kind, flags, entry.subreader(12, data_size), entry.base_offset)
        offset += length


__all__ = ["Cursor", "PlusRecord", "argb", "comment_records", "finite"]
