"""惰性加载进程内必需的点列内核并报告真实执行身份。"""

from __future__ import annotations

from functools import lru_cache
from importlib import import_module
from types import ModuleType
from typing import Any

_PROTOCOL_VERSION = 2
_CALLS = 0
_POINTS = 0
_VALIDATION_FAILURES = 0


@lru_cache(maxsize=1)
def get_native() -> ModuleType:
    """加载必需扩展并校验协议，安装错误保留原原因且不降级。"""
    try:
        native = import_module("metafile_render._metafile_points")
        if getattr(native, "PROTOCOL_VERSION", None) != _PROTOCOL_VERSION:
            raise ImportError("metafile-render native protocol mismatch; rebuild the extension")
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "metafile-render requires a compatible Rust native extension; reinstall the platform wheel or rebuild the extension"
        ) from exc
    return native


def record_native_call(count: int, *, failed: bool = False) -> None:
    """累计实际原生调用与校验失败，供测试和性能验收确认执行路径。"""
    global _CALLS, _POINTS, _VALIDATION_FAILURES
    _CALLS += 1
    _POINTS += count
    _VALIDATION_FAILURES += int(failed)


def backend_info() -> dict[str, Any]:
    """报告必需扩展的加载身份与实际调用计数，不将加载等同于执行。"""
    native = get_native()
    return {
        "backend": "rust",
        "extension": getattr(native, "__file__", None),
        "protocol": getattr(native, "PROTOCOL_VERSION", None),
        "build_profile": getattr(native, "BUILD_PROFILE", None),
        "native_calls": _CALLS,
        "native_points": _POINTS,
        "validation_failures": _VALIDATION_FAILURES,
    }
