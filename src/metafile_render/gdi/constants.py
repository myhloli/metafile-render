# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF gdi.constants 内部实现。"""

from __future__ import annotations

_EMF_SIGNATURE = 0x464D4520


_EMFPLUS_SIGNATURE = 0x2B464D45


_PLACEABLE_WMF_KEY = 0x9AC6CDD7


_RGN_AND = 1


_RGN_OR = 2


_RGN_XOR = 3


_RGN_DIFF = 4


_RGN_COPY = 5


_ETO_OPAQUE = 0x0002


_ETO_CLIPPED = 0x0004


_ETO_GLYPH_INDEX = 0x0010


_ETO_PDY = 0x2000


_TA_UPDATECP = 0x0001


_TA_RIGHT = 0x0002


_TA_CENTER = 0x0006


_SRCCOPY = 0x00CC0020


_SUPPORTED_ROP3 = {
    _SRCCOPY,
    0x008800C6,
    0x00EE0086,
    0x00660046,
    0x00550009,
    0x00000042,
    0x00FF0062,
}


__all__ = [
    "_EMF_SIGNATURE",
    "_EMFPLUS_SIGNATURE",
    "_PLACEABLE_WMF_KEY",
    "_RGN_AND",
    "_RGN_OR",
    "_RGN_XOR",
    "_RGN_DIFF",
    "_RGN_COPY",
    "_ETO_OPAQUE",
    "_ETO_CLIPPED",
    "_ETO_GLYPH_INDEX",
    "_ETO_PDY",
    "_TA_UPDATECP",
    "_TA_RIGHT",
    "_TA_CENTER",
    "_SRCCOPY",
    "_SUPPORTED_ROP3",
]
