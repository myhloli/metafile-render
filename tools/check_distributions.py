"""在不安装 MinerU 的隔离环境中验证 wheel 与 sdist 的 API 和 CLI。"""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import venv
from pathlib import Path


def check_distribution(distribution: Path) -> None:
    """逐个安装发行物，并在包目录之外检查真实入口。"""
    with tempfile.TemporaryDirectory(prefix="metafile-dist-") as directory:
        root = Path(directory)
        environment = root / "env"
        venv.EnvBuilder(with_pip=True).create(environment)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        subprocess.run([str(python), "-m", "pip", "install", str(distribution.resolve())], check=True)
        subprocess.run([str(python), "-m", "pip", "check"], check=True)
        payload = Path(__file__).resolve().parents[1] / "tests/fixtures/gdiplus/geometry-only.emf"
        source = root / "input.emf"
        source.write_bytes(payload.read_bytes())
        smoke = """from pathlib import Path
from importlib.util import find_spec
from metafile_render import render_metafile
assert find_spec("mineru") is None
for kind in ("svg", "png", "jpeg", "webp"):
    result = render_metafile(Path("input.emf").read_bytes(), output_format=kind)
    assert result.width > 400 and result.height > 400
    assert result.source_format == "emf" and result.emfplus_mode == "only"
    assert len(result.data) > 100
"""
        subprocess.run([str(python), "-c", smoke], cwd=root, check=True)
        command = scripts / ("metafile-render.exe" if os.name == "nt" else "metafile-render")
        subprocess.run([str(command), str(source), "-o", str(root / "output.webp"), "--backend", "replay"], check=True)
        subprocess.run([str(python), "-m", "metafile_render", "--version"], check=True)
        print(f"Verified {distribution.name}", flush=True)


def main() -> None:
    """从指定目录选择当前版本的 wheel 和源码包。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()
    distributions = sorted(arguments.directory.glob("*.whl")) + sorted(arguments.directory.glob("*.tar.gz"))
    if len(distributions) != 2:
        parser.error("directory must contain exactly one wheel and one sdist")
    for distribution in distributions:
        check_distribution(distribution)


if __name__ == "__main__":
    main()

__all__ = ["main"]
