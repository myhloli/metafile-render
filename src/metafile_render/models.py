# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""公共结果、诊断和异常模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

MetafileSourceFormat: TypeAlias = Literal["wmf", "emf"]


MetafileBackend: TypeAlias = Literal["auto", "replay"]
MetafileOutputFormat: TypeAlias = Literal["png", "jpeg", "svg", "webp"]


EmfPlusMode: TypeAlias = Literal["none", "dual", "only"]


DiagnosticLevel: TypeAlias = Literal["info", "warning", "error"]


class MetafileError(ValueError):
    """WMF/EMF 渲染错误基类，并携带稳定错误码。"""

    code = "metafile_error"


class MetafileMalformedError(MetafileError):
    """WMF/EMF 头部或记录流不满足格式边界。"""

    code = "malformed"


class MetafileResourceLimitError(MetafileError):
    """WMF/EMF 输入或输出超过固定安全预算。"""

    code = "resource_limit"


class MetafileUnsupportedError(MetafileError):
    """输入使用无法安全降级的 WMF/EMF 能力。"""

    code = "unsupported"


@dataclass(frozen=True, slots=True)
class MetafileDiagnostic:
    """记录单个可定位、可聚合的 WMF/EMF 渲染诊断。"""

    code: str
    level: DiagnosticLevel
    message: str
    record_type: int | None = None
    record_index: int | None = None
    offset: int | None = None


@dataclass(frozen=True, slots=True)
class MetafileRenderResult:
    """保存最终图片字节、尺寸与解析诊断。"""

    data: bytes
    output_format: MetafileOutputFormat
    media_type: str
    width: int
    height: int
    source_format: MetafileSourceFormat
    emfplus_mode: EmfPlusMode
    partial: bool
    diagnostics: tuple[MetafileDiagnostic, ...]


__all__ = [
    "MetafileBackend",
    "MetafileSourceFormat",
    "MetafileOutputFormat",
    "EmfPlusMode",
    "DiagnosticLevel",
    "MetafileError",
    "MetafileMalformedError",
    "MetafileResourceLimitError",
    "MetafileUnsupportedError",
    "MetafileDiagnostic",
    "MetafileRenderResult",
]
