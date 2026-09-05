# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF 公共渲染入口及确定性的后端选择。"""

from __future__ import annotations

from PIL import Image

from .backends import windows
from .context import ReplayContext
from .models import MetafileBackend, MetafileError, MetafileOutputFormat, MetafileRenderResult
from .parser import parse_metafile, prepare_metafile
from .preflight import validate_for_native
from .render import encode_document, encode_raster, validate_encoder
from .sizing import _bounded_canvas_size, check_render_size


def render_metafile(
    data: bytes,
    *,
    output_format: MetafileOutputFormat = "png",
    dpi: int | None = None,
    size_hint: tuple[int, int] | None = None,
    backend: MetafileBackend = "auto",
) -> MetafileRenderResult:
    """默认以 200 DPI 渲染；Windows 栅格输出原生优先，SVG 始终走解析回放。"""
    if output_format not in {"png", "jpeg", "svg", "webp"}:
        raise ValueError(f"unsupported metafile output format: {output_format}")
    if backend not in {"auto", "replay"}:
        raise ValueError(f"unsupported metafile backend: {backend}")
    resolved_dpi = 200 if dpi is None else dpi
    header = prepare_metafile(data, dpi=resolved_dpi, size_hint=size_hint)
    validate_encoder(output_format)
    context = ReplayContext()
    native_candidate = (
        backend == "auto"
        and output_format != "svg"
        and windows.is_windows()
        and header.emfplus_mode != "only"
        and (header.source_format == "emf" or header.bounds is not None)
    )
    if native_candidate:
        work_units = validate_for_native(data, header)
        if header.pixel_size is not None:
            width, height, downscaled = _bounded_canvas_size(header.pixel_size)
            check_render_size(width, height, work_units)
            try:
                image = windows.render_native(data, (width, height), resolved_dpi)
            except MetafileError:
                raise
            except (OSError, SyntaxError, ValueError, ZeroDivisionError, Image.DecompressionBombError) as exc:
                context.report("native_backend_fallback", f"native rendering unavailable or failed: {exc}; using replay")
            else:
                try:
                    output, media_type = encode_raster(image, output_format, dpi=resolved_dpi)
                finally:
                    image.close()
                if downscaled:
                    context.report(
                        "canvas_downscaled",
                        f"metafile canvas was downscaled from {header.pixel_size[0]}x{header.pixel_size[1]} "
                        f"to {width}x{height}",
                        level="warning",
                    )
                return MetafileRenderResult(
                    output,
                    output_format,
                    media_type,
                    width,
                    height,
                    header.source_format,
                    header.emfplus_mode,
                    False,
                    context.diagnostics.freeze(),
                )
        else:
            context.report("native_backend_fallback", "native canvas size is unavailable; using replay")
    document = parse_metafile(data, dpi=resolved_dpi, size_hint=size_hint, context=context, prepared=header)
    output, media_type = encode_document(document, output_format, dpi=resolved_dpi, context=context)
    return MetafileRenderResult(
        output,
        output_format,
        media_type,
        document.width,
        document.height,
        document.source_format,
        document.emfplus_mode,
        context.partial,
        context.diagnostics.freeze(),
    )


__all__ = ["render_metafile"]
