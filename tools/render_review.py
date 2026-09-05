"""保存 Only/Dual 四种输出及诊断，供三平台目视对照。"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path

from metafile_render import render_metafile


def main() -> None:
    """按真实样本生成自动策略结果，保留版本和诊断清单。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()
    arguments.directory.mkdir(parents=True, exist_ok=True)
    fixtures = Path(__file__).resolve().parents[1] / "tests/fixtures/gdiplus"
    manifest: dict[str, object] = {"version": version("metafile-render"), "pillow": version("Pillow")}
    for source in sorted(fixtures.glob("*.emf")):
        for fmt in ("png", "jpeg", "svg", "webp"):
            result = render_metafile(source.read_bytes(), output_format=fmt)
            name = source.stem + "." + fmt
            (arguments.directory / name).write_bytes(result.data)
            manifest[name] = {
                "width": result.width,
                "height": result.height,
                "mode": result.emfplus_mode,
                "partial": result.partial,
                "diagnostics": [asdict(item) for item in result.diagnostics],
            }
    (arguments.directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

__all__ = ["main"]
