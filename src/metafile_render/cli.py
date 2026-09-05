# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""单文件 WMF/EMF 转换命令行。"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path
from typing import Sequence

from .api import render_metafile
from .limits import MAX_METAFILE_BYTES
from .models import MetafileError, MetafileOutputFormat, MetafileResourceLimitError

_OUTPUT_FORMATS: dict[str, MetafileOutputFormat] = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".svg": "svg",
    ".webp": "webp",
}


def _positive_integer(value: str) -> int:
    """将命令行尺寸解析为正整数，错误交由 argparse 报告。"""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _read_input(path: Path) -> bytes:
    """最多读取输入预算加一字节，避免完整载入超大文件。"""
    with path.open("rb") as stream:
        data = stream.read(MAX_METAFILE_BYTES + 1)
    if len(data) > MAX_METAFILE_BYTES:
        raise MetafileResourceLimitError(f"metafile exceeds max_metafile_bytes={MAX_METAFILE_BYTES}")
    return data


def _write_output(path: Path, data: bytes, *, overwrite: bool) -> None:
    """在目标目录原子发布完整文件，并保证默认模式不会覆盖并发创建的文件。"""
    descriptor, filename = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    """解析转换参数并返回稳定退出码，所有运行诊断写入 stderr。"""
    parser = argparse.ArgumentParser(prog="metafile-render", description="Render WMF/EMF to SVG, PNG, JPEG or WebP.")
    parser.add_argument("input", type=Path, help="input WMF or EMF file")
    parser.add_argument("-o", "--output", type=Path, required=True, help="output .svg, .png, .jpg, .jpeg or .webp file")
    parser.add_argument("--dpi", type=_positive_integer, default=144, help="render DPI, 1–1200 (default: 144)")
    parser.add_argument("--size", type=_positive_integer, nargs=2, metavar=("WIDTH", "HEIGHT"), help="pixel size hint")
    parser.add_argument("--force", action="store_true", help="replace an existing output file")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('metafile-render')}")
    arguments = parser.parse_args(argv)
    output_format = _OUTPUT_FORMATS.get(arguments.output.suffix.lower())
    if output_format is None:
        parser.error("output extension must be .svg, .png, .jpg, .jpeg or .webp")
    if arguments.dpi > 1200:
        parser.error("--dpi must be between 1 and 1200")
    try:
        source, destination = arguments.input, arguments.output
        if source.resolve() == destination.resolve() or destination.exists() and source.samefile(destination):
            parser.error("input and output must refer to different files")
        if (destination.exists() or destination.is_symlink()) and not arguments.force:
            raise FileExistsError(f"output already exists: {destination}; use --force to replace it")
        size_hint = (arguments.size[0], arguments.size[1]) if arguments.size else None
        result = render_metafile(_read_input(source), output_format=output_format, dpi=arguments.dpi, size_hint=size_hint)
        _write_output(destination, result.data, overwrite=arguments.force)
    except (MetafileError, OSError) as exc:
        print(f"metafile-render: {exc}", file=sys.stderr)
        return 1
    if result.partial:
        print("metafile-render: partial rendering", file=sys.stderr)
    for diagnostic in result.diagnostics:
        print(f"metafile-render: {diagnostic.level} [{diagnostic.code}] {diagnostic.message}", file=sys.stderr)
    return 0


__all__ = ["main"]
