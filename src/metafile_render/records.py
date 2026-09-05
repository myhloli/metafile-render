# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""预扫描与正式回放共用的外层记录迭代器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from .binary import BoundedReader
from .limits import MAX_RECORDS
from .models import MetafileMalformedError, MetafileResourceLimitError, MetafileSourceFormat


@dataclass(frozen=True, slots=True)
class Record:
    """保存完整记录视图及稳定文件位置。"""

    kind: int
    index: int
    offset: int
    data: BoundedReader


def iter_records(data: bytes, start: int, end: int, source: MetafileSourceFormat) -> Iterator[Record]:
    """验证长度、对齐、数量和 EOF，在分派前提供有界记录。"""
    reader = BoundedReader(data).subreader(0, end)
    offset, index = start, 0
    minimum = 8 if source == "emf" else 6
    eof_kind = 14 if source == "emf" else 0
    while offset < end:
        head = reader.subreader(offset, minimum)
        kind = head.u32(0) if source == "emf" else head.u16(4)
        size = head.u32(4) if source == "emf" else head.u32(0) * 2
        index += 1
        if index > MAX_RECORDS:
            raise MetafileResourceLimitError(f"{source.upper()} exceeds max_records={MAX_RECORDS}")
        if size < minimum or size > end - offset or source == "emf" and size % 4:
            raise MetafileMalformedError(f"invalid {source.upper()} record size at offset={offset}: {size}")
        record = Record(kind, index, offset, reader.subreader(offset, size))
        yield record
        offset += size
        if kind == eof_kind:
            return
    raise MetafileMalformedError(f"{source.upper()} record stream does not contain EOF")


__all__ = ["Record", "iter_records"]
