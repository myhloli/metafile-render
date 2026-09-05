# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF backends.constants 内部实现。"""

from __future__ import annotations

_SRCCOPY = 0x00CC0020


_SRCAND = 0x008800C6


_SRCPAINT = 0x00EE0086


_SRCINVERT = 0x00660046


_DSTINVERT = 0x00550009


_BLACKNESS = 0x00000042


_WHITENESS = 0x00FF0062


_TA_RIGHT = 0x0002


_TA_CENTER = 0x0006


_TA_BOTTOM = 0x0008


_TA_BASELINE = 0x0018


_CLIPPER_SCALE = 256


_CLIPPER_COORD_LIMIT = (1 << 61) / _CLIPPER_SCALE


__all__ = [
    "_SRCCOPY",
    "_SRCAND",
    "_SRCPAINT",
    "_SRCINVERT",
    "_DSTINVERT",
    "_BLACKNESS",
    "_WHITENESS",
    "_TA_RIGHT",
    "_TA_CENTER",
    "_TA_BOTTOM",
    "_TA_BASELINE",
    "_CLIPPER_SCALE",
    "_CLIPPER_COORD_LIMIT",
]
