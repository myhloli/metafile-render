"""对多来源图元建立分类性能与输出基线，保留部分渲染、拒绝和超时结果。"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import gc
import importlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
from typing import Any

from benchmark_pair import (
    contained_path,
    digest,
    first_difference,
    peak_rss_bytes,
    process_identity,
    result_signature,
    source_identity,
    validate_destination,
    write_json,
)
from verify_installation import fallback_signature


def command_statistics(document: Any) -> dict[str, Any]:
    """按实际 IR 记录文字、位图、曲线和 ROP 覆盖，不从文件名猜测热点。"""
    from metafile_render.commands import DrawImageCommand, DrawPathCommand, DrawTextCommand

    paths = [command for command in document.commands if isinstance(command, DrawPathCommand)]
    images = [command for command in document.commands if isinstance(command, DrawImageCommand)]
    texts = [command for command in document.commands if isinstance(command, DrawTextCommand)]
    points = sum(len(segment.points) for command in paths for segment in command.path.segments)
    cubics = sum(segment.verb == "C" for command in paths for segment in command.path.segments)
    rop2 = Counter(str(command.rop2) for command in paths)
    rop3 = Counter(str(command.rop) for command in images)
    categories = [document.source_format, "emfplus_" + document.emfplus_mode]
    if texts:
        categories.append("text")
    if images:
        categories.append("bitmap")
    if cubics:
        categories.append("curves")
    if points >= 1000:
        categories.append("dense_path")
    if any(command.rop2 != 13 for command in paths) or any(command.rop not in {0, 0x00CC0020} for command in images):
        categories.append("rop")
    if any(command.clip for command in document.commands):
        categories.append("clip")
    return {
        "commands": dict(Counter(type(command).__name__ for command in document.commands)),
        "path_points": points,
        "cubic_segments": cubics,
        "text_characters": sum(len(command.text) for command in texts),
        "rop2": dict(rop2),
        "rop3": dict(rop3),
        "categories": categories,
    }


def worker(config: dict[str, Any], output: Path, mode: str) -> dict[str, Any]:
    """在唯一新进程中执行验证、无插桩计时或独立 RSS；身份散列放在测量之后。"""
    source = Path(config["source"])
    sys.path.insert(0, str(source / "src"))
    meta = importlib.import_module("metafile_render")
    assert Path(meta.__file__).resolve() == (source / "src/metafile_render/__init__.py").resolve()
    if mode == "identity":
        from metafile_render._native_points import get_native

        get_native()
        return {"identity": process_identity()}
    data = contained_path(Path(config["corpus"]), config["asset"]["input_file"]).read_bytes()
    if digest(data) != config["asset"]["sha256"]:
        raise ValueError("corpus asset changed")
    kwargs = dict(config["kwargs"])
    if kwargs.get("size_hint") is not None:
        kwargs["size_hint"] = tuple(kwargs["size_hint"])

    def convert() -> Any:
        """用清单参数调用公开渲染入口，不在 stopwatch 内插桩、散列或写文件。"""
        return meta.render_metafile(data, **kwargs)

    result: dict[str, Any] = {}
    try:
        if mode == "verify":
            from metafile_render.parser import parse_metafile

            document = parse_metafile(data, dpi=kwargs["dpi"], size_hint=kwargs.get("size_hint"))
            result["statistics"] = command_statistics(document)
            result["ir_sha256"] = digest(json.dumps(asdict(document), sort_keys=True, default=str).encode())
            rendered = convert()
            result["signature"] = result_signature(rendered)
            (output / ("output." + rendered.output_format)).write_bytes(rendered.data)
            if rendered.output_format == "svg":
                result["fallback"] = fallback_signature(rendered.data)
        elif mode == "timing":
            warmup = convert()
            del warmup
            gc.collect()
            times = []
            for _ in range(config["repeats"]):
                started = time.perf_counter()
                rendered = convert()
                times.append(time.perf_counter() - started)
            result.update(
                {"seconds": times, "median_seconds": statistics.median(times), "signature": result_signature(rendered)}
            )
        else:
            before = peak_rss_bytes()
            rendered = convert()
            peak = peak_rss_bytes()
            result.update(
                {"pre_process_peak_rss_bytes": before, "process_peak_rss_bytes": peak, "signature": result_signature(rendered)}
            )
    except Exception as exc:
        from metafile_render import MetafileError

        result["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "expected_rejection": isinstance(exc, MetafileError),
        }
    result["identity"] = process_identity()
    return result


def launch(config_path: Path, output: Path, mode: str, python: str, timeout: float) -> dict[str, Any]:
    """串行启动工作进程，限制长尾输入，日志与实际输出逐案例保存。"""
    output.mkdir(parents=True, exist_ok=False)
    with (output / "process.log").open("w", encoding="utf-8") as log:
        try:
            process = subprocess.run(
                [
                    python,
                    str(Path(__file__).resolve()),
                    "--worker",
                    mode,
                    "--config",
                    str(config_path),
                    "--output",
                    str(output),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"timeout_seconds": timeout}
    result = output / "result.json"
    if not result.exists():
        return {"worker_error": process.returncode}
    return json.loads(result.read_text())


def verification_contract(result: dict[str, Any]) -> dict[str, Any]:
    """只比较语义、输出与拒绝结果，运行路径和进程身份不混入输出契约。"""
    return {key: result[key] for key in ("statistics", "ir_sha256", "signature", "fallback", "error") if key in result}


def baseline(args: argparse.Namespace) -> int:
    """建立新输出参考与各案例基线，任何拒绝或超时都留在清单而不静默丢弃。"""
    corpus, source, output = args.corpus.resolve(), args.source.resolve(), args.output.resolve()
    protected = [corpus, source]
    if args.reference is not None:
        protected.append(args.reference.resolve())
    validate_destination(output, protected)
    manifest = json.loads((corpus / "manifest.json").read_text())
    assets = {item["sha256"]: item for item in manifest["assets"]}
    if len(assets) != len(manifest["assets"]) or len({c["id"] for c in manifest["cases"]}) != len(manifest["cases"]):
        raise ValueError("duplicate corpus identities")
    for case in manifest["cases"]:
        if len(case["id"]) != 64 or any(ch not in "0123456789abcdef" for ch in case["id"]):
            raise ValueError("case identity must be a lowercase SHA256")
    for asset in assets.values():
        if digest(contained_path(corpus, asset["input_file"]).read_bytes()) != asset["sha256"]:
            raise ValueError("corpus input hash differs")
    manifest_hash = digest((corpus / "manifest.json").read_bytes())
    references = {}
    if args.reference is not None:
        previous = json.loads((args.reference / "summary.json").read_text())
        if previous["corpus_manifest_sha256"] != manifest_hash:
            raise ValueError("reference belongs to a different corpus manifest")
        references = {row["variant"]: row["verification"] for row in previous["cases"]}
    output.mkdir(parents=True)
    write_json(output / "preflight.json", {"source": str(source)})
    preflight = launch(output / "preflight.json", output / "preflight", "identity", args.python, args.timeout)
    if "identity" not in preflight:
        write_json(output / "summary.json", {"ok": False, "preflight": preflight})
        return 1
    report: dict[str, Any] = {
        "schema_version": 1,
        "corpus_manifest_sha256": manifest_hash,
        "runner_sha256": digest(Path(__file__).read_bytes()),
        "source": source_identity(source),
        "repeats": args.repeats,
        "cases": [],
        "notes": [
            "Inputs retain their original DPI and dimensions; no fixed size_hint is injected.",
            "PNG is an additional benchmark format, not necessarily a document caller request.",
            "Timings are baselines, not paired speedup measurements.",
            "Groups separate document calls, upstream regressions, generated controls and robustness inputs.",
        ],
    }
    for case in manifest["cases"]:
        for fmt in args.formats.split(","):
            key = case["id"][:20] + "-" + fmt
            directory = output / key
            directory.mkdir()
            kwargs = dict(case["kwargs"], output_format=fmt)
            config = {
                "source": str(source),
                "corpus": str(corpus),
                "asset": assets[case["asset"]],
                "kwargs": kwargs,
                "repeats": args.repeats,
            }
            write_json(directory / "config.json", config)
            row: dict[str, Any] = {
                "case": case["id"],
                "variant": key,
                "asset": case["asset"],
                "format": fmt,
                "kwargs": kwargs,
                "origins": case["origins"],
            }
            verification = launch(directory / "config.json", directory / "verification", "verify", args.python, args.timeout)
            row["verification"] = verification
            if args.reference is not None:
                if key not in references:
                    raise ValueError(f"reference case is missing: {key}")
                row["reference_difference"] = first_difference(
                    verification_contract(references[key]), verification_contract(verification)
                )
            if args.verify_only:
                row["stable_output"] = "signature" in verification and row["reference_difference"] is None
                row["stable_rejection"] = (
                    verification.get("error", {}).get("expected_rejection", False) and row["reference_difference"] is None
                )
            elif "signature" in verification and not any(origin["group"] == "robustness" for origin in case["origins"]):
                timing = launch(directory / "config.json", directory / "timing", "timing", args.python, args.timeout)
                row["timing"] = timing
                if "signature" in timing:
                    row["stable_output"] = first_difference(verification["signature"], timing["signature"]) is None
                    row["memory"] = launch(directory / "config.json", directory / "memory", "memory", args.python, args.timeout)
                    if "signature" in row["memory"]:
                        row["stable_output"] &= first_difference(verification["signature"], row["memory"]["signature"]) is None
            else:
                repeated = launch(
                    directory / "config.json", directory / "verification-repeat", "verify", args.python, args.timeout
                )
                row["repeated_verification"] = repeated
                if "signature" in verification:
                    row["stable_output"] = first_difference(verification.get("signature"), repeated.get("signature")) is None
                else:
                    row["stable_rejection"] = first_difference(
                        verification.get("error"), repeated.get("error")
                    ) is None and verification.get("error", {}).get("expected_rejection", False)
            report["cases"].append(row)
            write_json(output / "summary.json", report)
            if len(report["cases"]) % 10 == 0:
                print(json.dumps({"completed": len(report["cases"]), "last": key}), flush=True)
    report["source_unchanged"] = source_identity(source)["tree_sha256"] == report["source"]["tree_sha256"]
    report["runner_unchanged"] = digest(Path(__file__).read_bytes()) == report["runner_sha256"]
    report["corpus_unchanged"] = digest((corpus / "manifest.json").read_bytes()) == manifest_hash and all(
        digest(contained_path(corpus, asset["input_file"]).read_bytes()) == asset["sha256"] for asset in assets.values()
    )
    report["ok"] = (
        report["source_unchanged"]
        and report["runner_unchanged"]
        and report["corpus_unchanged"]
        and all(
            (row.get("stable_output", False) or row.get("stable_rejection", False)) and row.get("reference_difference") is None
            for row in report["cases"]
        )
    )
    write_json(output / "summary.json", report)
    return 0 if report["ok"] else 1


def main() -> None:
    """提供语料基线入口，内部 worker 与外部控制进程使用相同脚本身份。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--formats", default="svg,png")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--worker", choices=("verify", "timing", "memory", "identity"))
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.worker:
        result = worker(json.loads(args.config.read_text()), args.output, args.worker)
        write_json(args.output / "result.json", result)
        return
    if (
        args.corpus is None
        or args.repeats < 1
        or args.timeout <= 0
        or not set(args.formats.split(",")) <= {"svg", "png", "jpeg", "webp"}
    ):
        parser.error("corpus, positive repeats/timeout and supported formats are required")
    if args.verify_only and args.reference is None:
        parser.error("--verify-only requires a frozen --reference")
    raise SystemExit(baseline(args))


if __name__ == "__main__":
    main()
