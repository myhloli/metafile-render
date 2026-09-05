# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""一次转换的累计资源预算，缓存不改变输入计费。"""

from dataclasses import dataclass

from . import limits
from .models import MetafileResourceLimitError


@dataclass(slots=True)
class ResourceBudget:
    """分别累计输入几何、图元、裁剪与图片载荷。"""

    points: int = 0
    commands: int = 0
    clip_operations: int = 0
    object_bytes: int = 0
    decoded_bytes: int = 0

    def charge_points(self, count: int) -> None:
        """先限制单次点数，再累计文档内所有几何和文字点。"""
        if count < 0 or count > limits.MAX_POINTS_PER_RECORD:
            raise MetafileResourceLimitError(f"metafile record exceeds max_points_per_record={limits.MAX_POINTS_PER_RECORD}")
        self.points += count
        if self.points > limits.MAX_TOTAL_POINTS:
            raise MetafileResourceLimitError(f"metafile exceeds max_total_points={limits.MAX_TOTAL_POINTS}")

    def charge_command(self, clip_count: int) -> None:
        """在存储命令之前检查命令数和累计裁剪操作。"""
        self.commands += 1
        self.clip_operations += clip_count
        if self.commands > limits.MAX_COMMANDS:
            raise MetafileResourceLimitError(f"metafile exceeds max_commands={limits.MAX_COMMANDS}")
        if self.clip_operations > limits.MAX_TOTAL_CLIP_OPERATIONS:
            raise MetafileResourceLimitError(f"metafile exceeds max_total_clip_operations={limits.MAX_TOTAL_CLIP_OPERATIONS}")

    def charge_object_bytes(self, count: int) -> None:
        """限制包含被覆盖对象在内的累计对象载荷。"""
        self.object_bytes += count
        if self.object_bytes > limits.MAX_METAFILE_BYTES:
            raise MetafileResourceLimitError("EMF+ cumulative objects exceed byte budget")

    def charge_decoded_bytes(self, count: int) -> None:
        """限制累计图片像素与规范化编码载荷。"""
        self.decoded_bytes += count
        if self.decoded_bytes > limits.MAX_EMBEDDED_BITMAP_BYTES:
            raise MetafileResourceLimitError("cumulative decoded images exceed byte budget")


__all__ = ["ResourceBudget"]
