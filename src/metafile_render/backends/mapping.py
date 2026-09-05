# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF backends.mapping 内部实现。"""

from __future__ import annotations

from ..commands import MetafileDocument
from ..primitives import Matrix, Rect


def _document_matrix(document: MetafileDocument, *, raster_scale: int = 1) -> Matrix:
    """把统一 device 坐标映射到最终输出像素。"""
    bounds = document.bounds.normalized()
    scale_x = document.width * raster_scale / bounds.width
    scale_y = document.height * raster_scale / bounds.height
    return Matrix(a=scale_x, d=scale_y, e=-bounds.left * scale_x, f=-bounds.top * scale_y)


def _mapped_rect(rect: Rect, matrix: Matrix) -> Rect:
    """把矩形四角变换后返回轴对齐像素包围盒。"""
    corners = [
        matrix.transform_point((rect.left, rect.top)),
        matrix.transform_point((rect.right, rect.top)),
        matrix.transform_point((rect.right, rect.bottom)),
        matrix.transform_point((rect.left, rect.bottom)),
    ]
    return Rect(
        min(point[0] for point in corners),
        min(point[1] for point in corners),
        max(point[0] for point in corners),
        max(point[1] for point in corners),
    )


__all__ = ["_document_matrix", "_mapped_rect"]
