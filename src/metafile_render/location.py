# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""跨解析与渲染阶段传递的源记录位置。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecordLocation:
    """保存来源格式记录的类型、序号和绝对字节偏移。"""

    record_type: int | None = None
    record_index: int | None = None
    offset: int | None = None


__all__ = ["RecordLocation"]
