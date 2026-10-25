"""在干净临时源码中构建必需原生 发行物，避免残留二进制混入。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def build_distributions(output: Path, *, no_isolation: bool = False) -> None:
    """使用独立源码和 build 目录构建原生 wheel，不复用旧二进制。"""
    root = Path(__file__).resolve().parents[1]
    output = output.absolute()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    output.mkdir(parents=True)
    ignored = shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info", "*.so", "*.pyd", "target")
    with tempfile.TemporaryDirectory(prefix="metafile-build-") as directory:
        stage = Path(directory)
        for name in ("pyproject.toml", "setup.py", "MANIFEST.in", "README.md", "README_zh-CN.md", "LICENSE"):
            shutil.copy2(root / name, stage / name)
        for name in ("src", "rust", "tests", "tools"):
            shutil.copytree(root / name, stage / name, ignore=ignored)
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        environment.setdefault("CARGO_TARGET_DIR", str(stage / "cargo-target"))
        if sys.platform == "darwin":
            environment["MACOSX_DEPLOYMENT_TARGET"] = "14.0"
        command = [sys.executable, "-m", "build", "--wheel", "--outdir", str(output)]
        if no_isolation:
            command.append("--no-isolation")
        subprocess.run(command, cwd=stage, env=environment, check=True)


def main() -> None:
    """为 CI 和本地验收构建唯一的原生发行类型。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--no-isolation", action="store_true")
    args = parser.parse_args()
    build_distributions(args.output, no_isolation=args.no_isolation)


if __name__ == "__main__":
    main()
