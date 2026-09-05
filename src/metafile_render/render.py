# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""共享输出编码入口，后端只负责生成像素或安全 SVG。"""

from __future__ import annotations

from io import BytesIO

from PIL import Image, features

from .backends.raster import render_pillow
from .backends.session import RenderSession
from .backends.svg import render_svg
from .commands import MetafileDocument
from .context import ReplayContext
from .models import MetafileOutputFormat, MetafileUnsupportedError


def validate_encoder(output_format: MetafileOutputFormat) -> None:
    """在昂贵渲染之前确认所需编码器可用。"""
    if output_format == "webp" and not features.check("webp"):
        raise MetafileUnsupportedError("WebP encoder is unavailable in this Pillow installation")


def encode_raster(image: Image.Image, output_format: MetafileOutputFormat, *, dpi: int) -> tuple[bytes, str]:
    """为原生与回放图片应用相同质量、透明度和分辨率元数据。"""
    validate_encoder(output_format)
    output = BytesIO()
    if output_format == "png":
        image.save(output, format="PNG", dpi=(dpi, dpi))
        return output.getvalue(), "image/png"
    if output_format == "webp":
        image.save(output, format="WEBP", lossless=False, quality=90, method=4)
        return output.getvalue(), "image/webp"
    if output_format != "jpeg":
        raise ValueError(f"not a raster output format: {output_format}")
    rgba = image.convert("RGBA")
    background = Image.new("RGB", image.size, (255, 255, 255))
    background.paste(rgba.convert("RGB"), (0, 0), rgba.getchannel("A"))
    background.save(output, format="JPEG", quality=90, dpi=(dpi, dpi))
    return output.getvalue(), "image/jpeg"


def encode_document(
    document: MetafileDocument,
    output_format: MetafileOutputFormat,
    *,
    dpi: int = 200,
    context: ReplayContext | None = None,
) -> tuple[bytes, str]:
    """在同一可释放会话内完成回放编码及 SVG fallback。"""
    validate_encoder(output_format)
    with RenderSession(context) as session:
        if output_format == "svg":
            return render_svg(document, session), "image/svg+xml"
        return encode_raster(render_pillow(document, session), output_format, dpi=dpi)


__all__ = ["encode_document", "encode_raster", "validate_encoder"]
