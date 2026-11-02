# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""统一单行文字清理与可恢复的 RAQM 布局异常。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager

# 保留 TAB、格式控制符和组合字符；过滤 RAQM 段落分隔符及 XML 1.0 非法字符。
_UNSAFE_TEXT = re.compile(r"[\x00-\x08\x0a-\x1f\x85\u2029\ud800-\udfff\ufffe\uffff]")


class TextLayoutError(Exception):
    """标记可以在当前文字命令内隔离的已知布局失败。"""


@contextmanager
def text_layout_errors() -> Iterator[None]:
    """只转换 RAQM 已知布局错误，其他异常及资源错误保持传播。"""
    try:
        yield
    except RuntimeError as exc:
        if str(exc) != "raqm_layout() failed.":
            raise
        raise TextLayoutError(str(exc)) from exc


def clean_single_line_text(text: str) -> tuple[str, tuple[int, ...] | None]:
    """返回安全的单行文字与保留字符的原始索引，无变化时避免分配索引。"""
    if _UNSAFE_TEXT.search(text) is None:
        return text, None
    indices = tuple(index for index, character in enumerate(text) if _UNSAFE_TEXT.fullmatch(character) is None)
    return "".join(text[index] for index in indices), indices
