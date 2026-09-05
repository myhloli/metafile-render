# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""有界 EMF+ 对象表与跨记录载荷重组。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeVar

from ..binary import BoundedReader
from ..context import ReplayContext
from ..limits import MAX_METAFILE_BYTES
from ..models import MetafileMalformedError, MetafileResourceLimitError
from ..primitives import GraphicsPath
from .binary import Cursor
from .objects import (
    PlusImage,
    PlusObject,
    UnsupportedObject,
    parse_object,
)

T = TypeVar("T")


@dataclass(slots=True)
class ContinuedObject:
    """累计单个跨记录对象，完成前不暴露半成品。"""

    slot: int
    kind: int
    size: int
    data: bytearray = field(default_factory=bytearray)


class ObjectTable:
    """管理对象槽的完整替换和累计载荷预算。"""

    def __init__(self, host: ReplayContext) -> None:
        """初始化独立对象槽，并复用转换级预算和诊断。"""
        self.host = host
        self.objects: dict[int, PlusObject] = {}
        self.continued: ContinuedObject | None = None

    def warn(self, code: str, message: str) -> None:
        """将对象声明及引用问题定位到当前源记录。"""
        self.host.warn(code, message)

    def get(self, slot: int, expected: type[T]) -> T | None:
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

    def define(self, flags: int, cursor: Cursor) -> None:
        """重组对象分段并原子替换槽，限制所有对象的累计字节。"""
        slot, kind, more = flags & 255, (flags >> 8) & 127, bool(flags & 0x8000)
        if slot >= 64 or kind == 0:
            raise MetafileMalformedError("invalid EMF+ object slot or type")
        total = cursor.u32() if more else None
        if total is not None and (total == 0 or total > MAX_METAFILE_BYTES):
            raise MetafileResourceLimitError("EMF+ continued object exceeds byte budget")
        chunk = cursor.take(cursor.reader.remaining(cursor.offset))
        self.host.budget.charge_object_bytes(len(chunk))
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
            self.host.budget.charge_decoded_bytes(value.width * value.height * 4 + len(value.png))
        self.objects[slot] = value


__all__ = ["ObjectTable"]
