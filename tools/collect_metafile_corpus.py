"""收集真实 Office 调用、独立图元与上游回归输入，按内容去重并保留来源。"""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import sys
import time
from typing import Any
from zipfile import is_zipfile, ZipFile


def digest(data: bytes) -> str:
    """为来源文件、图片及渲染参数生成稳定身份。"""
    return hashlib.sha256(data).hexdigest()


def vector_format(data: bytes) -> str | None:
    """按文件签名识别图元，不根据扩展名推断真实格式。"""
    if len(data) >= 44 and data[:4] == b"\x01\0\0\0" and data[40:44] == b" EMF":
        return "emf"
    if data.startswith(b"\xd7\xcd\xc6\x9a") or data[:4] in {b"\x01\0\x09\0", b"\x02\0\x09\0"}:
        return "wmf"
    return None


class Corpus:
    """按图片内容及实际渲染参数分别去重，来源和出现次数保持可追溯。"""

    def __init__(self, output: Path) -> None:
        """创建全新语料目录，不覆盖既有参考数据。"""
        output.mkdir(parents=True, exist_ok=False)
        (output / "inputs").mkdir()
        self.output = output
        self.assets: dict[str, dict[str, Any]] = {}
        self.cases: dict[str, dict[str, Any]] = {}
        self.documents: list[dict[str, Any]] = []

    def add(self, data: bytes, kwargs: dict[str, Any], origin: dict[str, Any], *, declared: str | None = None) -> str | None:
        """登记内容与参数；已知来源的损坏图元仍可归入鲁棒性组。"""
        kind = vector_format(data) or declared
        if kind not in {"emf", "wmf"}:
            return None
        sha = digest(data)
        relative = f"inputs/{sha}.{kind}"
        if sha not in self.assets:
            (self.output / relative).write_bytes(data)
            self.assets[sha] = {"sha256": sha, "input_file": relative, "bytes": len(data), "format": kind}
        identity = digest(json.dumps({"sha256": sha, "kwargs": kwargs}, sort_keys=True).encode())
        case = self.cases.setdefault(identity, {"id": identity, "asset": sha, "kwargs": kwargs, "origins": []})
        case["origins"].append(origin)
        return identity

    def save(self) -> None:
        """保存完整清单，保持输入和调用清单独立于后续输出基线。"""
        manifest = {
            "schema_version": 1,
            "assets": list(self.assets.values()),
            "cases": list(self.cases.values()),
            "documents": self.documents,
        }
        (self.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def collect_office(corpus: Corpus, directory: Path, doc_source: Path, meta_source: Path) -> None:
    """通过真实解析调用提取所用图片，并单独记录容器内嵌但未渲染的图元。"""
    sys.path[:0] = [str(meta_source / "src"), str(doc_source / "src")]
    import docvortex
    import metafile_render
    from docvortex.analyzers.native.office import image
    from docvortex.analyzers.native.office.legacy import officeart
    from docvortex.analyzers.native.office.ppt import parser as ppt_parser

    assert Path(metafile_render.__file__).resolve() == (meta_source / "src/metafile_render/__init__.py").resolve()
    assert Path(docvortex.__file__).resolve() == (doc_source / "src/docvortex/__init__.py").resolve()
    original_render, original_blip, original_ppt_blip = (
        image.render_metafile,
        officeart.decode_blip,
        ppt_parser.decode_officeart_blip,
    )
    current: dict[str, Any] = {}
    calls: list[dict[str, Any]] = []

    def render(data: bytes, **kwargs: Any) -> Any:
        """原样转发真实调用和异常，不改变源文档的图像选择策略。"""
        origin = {**current, "group": "office_runtime", "call_index": len(calls) + 1}
        case = corpus.add(data, kwargs, origin)
        row = {"case": case, "kwargs": kwargs}
        calls.append(row)
        try:
            result = original_render(data, **kwargs)
        except Exception as exc:
            row["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        row.update({"output_format": result.output_format, "partial": result.partial})
        return result

    def decode_blip(record: Any) -> Any:
        """保留真实 OfficeArt 解压结果，避免把公式转 LaTeX 后绕过的图元漏掉。"""
        payload = original_blip(record)
        if payload is not None and vector_format(payload.data):
            hint = image._render_size_from_emu(payload.render_size_emu, dpi=200)
            corpus.add(
                payload.data,
                {"output_format": "svg", "dpi": 200, "size_hint": hint, "backend": "auto"},
                {**current, "group": "office_embedded", "record_offset": record.offset},
            )
        return payload

    image.render_metafile, officeart.decode_blip, ppt_parser.decode_officeart_blip = render, decode_blip, decode_blip
    try:
        suffixes = {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".rtf", ".odt", ".odp", ".ods"}
        for path in sorted(directory.rglob("*")):
            if path.suffix.lower() not in suffixes:
                continue
            current = {"source": str(path), "source_sha256": digest(path.read_bytes()), "source_kind": "office_demo"}
            calls = []
            row: dict[str, Any] = dict(current)
            if is_zipfile(path):
                with ZipFile(path) as archive:
                    for item in archive.infolist():
                        if (
                            "/media/" in item.filename or item.filename.startswith("Pictures/")
                        ) and item.file_size <= 128 * 1024 * 1024:
                            with archive.open(item) as stream:
                                data = stream.read(128 * 1024 * 1024 + 1)
                            if len(data) > 128 * 1024 * 1024:
                                continue
                            if vector_format(data):
                                corpus.add(
                                    data,
                                    {"output_format": "svg", "dpi": 200, "size_hint": None, "backend": "auto"},
                                    {**current, "group": "office_embedded", "member": item.filename},
                                )
            started = time.perf_counter()
            try:
                result = docvortex.parse(path, keep_model_json=True)
                row.update({"pages": len(result.middle_json.pages), "assets": len(result.assets)})
            except Exception as exc:
                row["error"] = {"type": type(exc).__name__, "message": str(exc)}
            row["discovery_seconds"] = time.perf_counter() - started
            row["calls"] = calls
            corpus.documents.append(row)
            corpus.save()
            print(json.dumps({"document": path.name, "actual_calls": len(calls), "error": row.get("error")}), flush=True)
    finally:
        image.render_metafile, officeart.decode_blip, ppt_parser.decode_officeart_blip = (
            original_render,
            original_blip,
            original_ppt_blip,
        )


def main() -> None:
    """按明确来源收集语料，所有输出与之前的冻结基线分开保存。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--upstream", type=Path)
    parser.add_argument("--office-root", type=Path)
    parser.add_argument("--docvortex-source", type=Path)
    parser.add_argument("--template-icons", action="store_true")
    parser.add_argument("--controls", type=Path)
    args = parser.parse_args()
    corpus = Corpus(args.output)
    if args.office_root is not None:
        if args.docvortex_source is None:
            parser.error("--office-root requires --docvortex-source")
        collect_office(corpus, args.office_root, args.docvortex_source, Path(__file__).resolve().parents[1])
    if args.upstream is not None:
        for source in json.loads(args.upstream.read_text()):
            data = Path(source["path"]).read_bytes()
            assert digest(data) == source["sha256"]
            origin = {**source["origin"], "group": source["group"], "source_kind": "upstream_regression"}
            declared = Path(source["path"]).suffix.removeprefix(".")
            corpus.add(
                data, {"output_format": "svg", "dpi": 200, "size_hint": None, "backend": "replay"}, origin, declared=declared
            )
    if args.template_icons:
        import pptx

        for path in sorted((Path(pptx.__file__).parent / "templates").glob("*.emf")):
            corpus.add(
                path.read_bytes(),
                {"output_format": "svg", "dpi": 200, "size_hint": None, "backend": "replay"},
                {
                    "group": "library_icon",
                    "source_kind": "office_icon",
                    "source": str(path),
                    "package_version": metadata.version("pypptx-with-oxml"),
                },
            )
    if args.controls is not None:
        for path in sorted(args.controls.glob("*.emf")):
            corpus.add(
                path.read_bytes(),
                {"output_format": "svg", "dpi": 200, "size_hint": None, "backend": "replay"},
                {"group": "generated_control", "source_kind": "project_fixture", "source": str(path)},
            )
    corpus.save()
    print(
        json.dumps(
            {"assets": len(corpus.assets), "unique_parameter_cases": len(corpus.cases), "documents": len(corpus.documents)}
        )
    )


if __name__ == "__main__":
    main()
