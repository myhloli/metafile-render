"""按平台选择发行物，在独立环境验证必需原生安装。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import venv
from verify_release_artifacts import verify_wheel

from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename


def select_distributions(directory: Path) -> tuple[list[Path], list[Path]]:
    """核验所有原生 wheel，只安装匹配平台的产物；拒绝兼容包与源码发行物。"""
    compatible = set(sys_tags())
    selected, skipped = [], []
    for path in sorted(directory.iterdir()):
        verify_wheel(path, allow_local_linux=True)
        target = selected if parse_wheel_filename(path.name)[3] & compatible else skipped
        target.append(path)
    if not selected:
        raise ValueError("directory contains no compatible native wheel")
    return selected, skipped


def check_distribution(distribution: Path, root: Path, reference: Path | None) -> dict[str, Any]:
    """在仓库外创建独立安装环境，保留日志与实际后端结果。"""
    environment = root / "env"
    venv.EnvBuilder(with_pip=True, symlinks=os.name != "nt").create(environment)
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    with (root / "process.log").open("w", encoding="utf-8") as log:
        if reference is not None:
            versions = json.loads((reference / "capture.json").read_text())["environment"]["dependencies"]
            subprocess.run(
                [str(python), "-m", "pip", "install", f"pillow=={versions['pillow']}", f"pyclipper=={versions['pyclipper']}"],
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
            )
        subprocess.run(
            [str(python), "-m", "pip", "install", "--no-cache-dir", str(distribution.resolve())],
            cwd=root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
        subprocess.run([str(python), "-m", "pip", "check"], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        command = [
            str(python),
            str(Path(__file__).with_name("verify_installation.py").resolve()),
            "--output",
            str(root / "verification"),
        ]
        if reference is not None:
            command.extend(["--reference", str(reference.resolve())])
        subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    report = json.loads((root / "verification/report.json").read_text())
    report["distribution"] = distribution.name
    report["distribution_sha256"] = hashlib.sha256(distribution.read_bytes()).hexdigest()
    print(f"Verified {distribution.name}: {report['backend']['backend']}", flush=True)
    return report


def main() -> None:
    """为多平台产物目录逐个执行本机可用的安装验收。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args()
    distributions, skipped = select_distributions(args.directory)
    temporary = tempfile.TemporaryDirectory(prefix="metafile-install-") if args.output is None else None
    root = Path(temporary.name) if temporary is not None else args.output
    if temporary is None:
        root.mkdir(parents=True, exist_ok=False)
    report = {"python": sys.version, "skipped_incompatible": [p.name for p in skipped], "installations": []}
    try:
        for index, distribution in enumerate(distributions):
            destination = root / f"{index:02d}-{distribution.name}"
            destination.mkdir()
            report["installations"].append(check_distribution(distribution, destination, args.reference))
        (root / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    main()
