"""在发行物安装环境中验证真实导入、四种输出和可选 DOC 图元回放。"""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict
import hashlib
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from typing import Any
import xml.etree.ElementTree as ET


def sha256(data: bytes) -> str:
    """为安装后的实际字节生成稳定指纹。"""
    return hashlib.sha256(data).hexdigest()


def signature(result: Any) -> dict[str, Any]:
    """按冻结参考的公开字段生成可严格比较的结果。"""
    return {
        "sha256": sha256(result.data),
        "bytes": len(result.data),
        "width": result.width,
        "height": result.height,
        "output_format": result.output_format,
        "media_type": result.media_type,
        "source_format": result.source_format,
        "emfplus_mode": result.emfplus_mode,
        "partial": result.partial,
        "diagnostics": [asdict(x) for x in result.diagnostics],
    }


def fallback_signature(svg: bytes) -> dict[str, Any]:
    """从本次实际 SVG 解出 PNG，分别比较容器字节和全部 RGBA 通道。"""
    from PIL import Image

    root = ET.fromstring(svg)
    metadata = root.find("{http://www.w3.org/2000/svg}metadata")
    if metadata is None or not metadata.text:
        raise ValueError("generated SVG has no PNG fallback")
    png = base64.b64decode(metadata.text, validate=True)
    with Image.open(BytesIO(png)) as image:
        return {"png": sha256(png), "size": list(image.size), "rgba": sha256(image.convert("RGBA").tobytes())}


def verify(output: Path, reference: Path | None) -> dict[str, Any]:
    """核验安装环境中的 API、CLI、原生执行身份及完整参考图元。"""
    import metafile_render
    from metafile_render._native_points import backend_info, get_native

    path = Path(metafile_render.__file__).resolve()
    assert path.is_relative_to(Path(sys.prefix).resolve()), path
    assert importlib.util.find_spec("mineru") is None
    get_native()
    assert backend_info()["backend"] == "rust", backend_info()
    assert backend_info()["build_profile"] == "release", backend_info()
    helper_path = Path(__file__).resolve().parents[1] / "tests/_metafile_test_utils.py"
    spec = importlib.util.spec_from_file_location("metafile_install_fixture", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    data = helper.build_emf([helper.emf_move_to(0, 0), helper.emf_polyline_to([(10, 20), (30, 10), (80, 80)])])
    source = output / "input.emf"
    source.write_bytes(data)
    report: dict[str, Any] = {"python": sys.version, "executable": sys.executable, "import_path": str(path), "formats": {}}
    for kind in ("svg", "png", "jpeg", "webp"):
        result = metafile_render.render_metafile(data, output_format=kind, dpi=144, backend="replay")
        target = output / ("cli." + kind)
        subprocess.run(
            [sys.executable, "-m", "metafile_render", str(source), "-o", str(target), "--dpi", "144", "--backend", "replay"],
            check=True,
            capture_output=True,
        )
        assert target.read_bytes() == result.data, kind
        report["formats"][kind] = signature(result)
    assert backend_info()["native_calls"] >= 4, backend_info()
    if reference is not None:
        capture = json.loads((reference / "capture.json").read_text())
        rows = []
        for entry in capture["calls"]:
            data = (reference / entry["input_file"]).read_bytes()
            assert sha256(data) == entry["input_sha256"]
            kwargs = dict(entry["kwargs"])
            if kwargs.get("size_hint") is not None:
                kwargs["size_hint"] = tuple(kwargs["size_hint"])
            result = metafile_render.render_metafile(data, **kwargs)
            assert signature(result) == entry["result"], entry["index"]
            assert fallback_signature(result.data) == fallback_signature((reference / entry["output_file"]).read_bytes())
            (output / f"{entry['index']:03d}.svg").write_bytes(result.data)
            rows.append({"index": entry["index"], "ok": True, "sha256": sha256(result.data)})
        report["reference_replay"] = rows
    report["backend"] = backend_info()
    report["ok"] = True
    return report


def main() -> None:
    """把独立安装验证结果写入明确的新目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = verify(args.output, args.reference)
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "backend": report["backend"], "reference_images": len(report.get("reference_replay", []))}))


if __name__ == "__main__":
    main()
