# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF render 内部实现。"""

from __future__ import annotations

from io import BytesIO

from PIL import Image, features

from .backends.raster import _png_fallback_bytes, render_pillow
from .backends.svg import render_svg
from .commands import MetafileDocument
from .models import MetafileOutputFormat, MetafileUnsupportedError


def encode_document(document: MetafileDocument, output_format: MetafileOutputFormat) -> tuple[bytes, str]:
    """按目标格式编码统一图元文档并返回 MIME。"""
    if output_format == "svg":
        return render_svg(document), "image/svg+xml"
    if output_format == "png":
        return _png_fallback_bytes(document), "image/png"
    if output_format == "webp" and not features.check("webp"):
        raise MetafileUnsupportedError("WebP encoder is unavailable in this Pillow installation")
    image = render_pillow(document)
    output = BytesIO()
    if output_format == "webp":
        image.save(output, format="WEBP", lossless=False, quality=90, method=4)
        return output.getvalue(), "image/webp"
    background = Image.new("RGB", image.size, (255, 255, 255))
    background.paste(image.convert("RGB"), (0, 0), image.getchannel("A"))
    background.save(output, format="JPEG", quality=90)
    return output.getvalue(), "image/jpeg"


__all__ = ["encode_document"]
