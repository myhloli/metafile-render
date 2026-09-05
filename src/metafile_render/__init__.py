# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""WMF/EMF 的跨平台渲染 API。"""

from .api import render_metafile
from .models import (
    MetafileDiagnostic,
    MetafileError,
    MetafileMalformedError,
    MetafileOutputFormat,
    MetafileRenderResult,
    MetafileResourceLimitError,
    MetafileUnsupportedError,
)

__all__ = [
    "MetafileDiagnostic",
    "MetafileError",
    "MetafileMalformedError",
    "MetafileOutputFormat",
    "MetafileRenderResult",
    "MetafileResourceLimitError",
    "MetafileUnsupportedError",
    "render_metafile",
]
