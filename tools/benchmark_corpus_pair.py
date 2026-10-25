"""冻结多来源语料，按 AB/BA 独立进程比较完整输出、分类计时与 RSS。"""

from __future__ import annotations

import argparse
from collections import defaultdict
import gc
import importlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
from typing import Any

from benchmark_corpus import verification_contract, worker as corpus_worker
from benchmark_pair import (
    contained_path,
    digest,
    first_difference,
    peak_rss_bytes,
    process_identity,
    result_signature,
    runtime_difference,
    source_identity,
    validate_destination,
    write_json,
)

GROUPS = (
    "office_runtime",
    "office_embedded",
    "library_icon",
    "upstream_regression",
    "upstream_control",
    "generated_control",
    "robustness",
)
PRIMARY_GROUPS = set(GROUPS[:4])


def case_groups(row: dict[str, Any]) -> list[str]:
    """按来源优先级独占主组；格式和实际 IR 类别另行提供可重叠统计。"""
    origins = {origin["group"] for origin in row["origins"]}
    group = next((name for name in GROUPS if name in origins), "unknown")
    categories = row["verification"].get("statistics", {}).get("categories", [])
    return ["all", "group:" + group, "format:" + row["format"], *["category:" + name for name in categories]] + (
        ["primary"] if group in PRIMARY_GROUPS else []
    )


def paired_statistics(before: list[float], after: list[float]) -> dict[str, Any]:
    """用每对比值的中位数报告变化，避免把不同批次的百分比相加。"""
    if not before or len(before) != len(after) or any(value <= 0 for value in before):
        raise ValueError("paired samples require positive before values and equal nonempty lengths")
    ratios = [right / left for left, right in zip(before, after)]
    ratio = statistics.median(ratios)
    return {
        "before": before,
        "after": after,
        "before_median": statistics.median(before),
        "after_median": statistics.median(after),
        "paired_ratios": ratios,
        "paired_ratio_median": ratio,
        "reduction_percent": (1 - ratio) * 100,
    }


def worker(config: dict[str, Any], source: str, mode: str, output: Path) -> dict[str, Any]:
    """只导入指定快照，验证与测量分进程；计时外才计算输出指纹。"""
    root = Path(source)
    sys.path.insert(0, str(root / "src"))
    meta = importlib.import_module("metafile_render")
    assert Path(meta.__file__).resolve() == (root / "src/metafile_render/__init__.py").resolve()
    if mode == "verify":
        results = {}
        for job in config["jobs"]:
            directory = output / job["variant"]
            directory.mkdir()
            result = corpus_worker(dict(job, source=source, corpus=config["corpus"]), directory, "verify")
            results[job["variant"]] = verification_contract(result)
        return {"cases": results, "identity": process_identity()}
    jobs = [job for job in config["jobs"] if "signature" in job["expected"] and "group:robustness" not in job["groups"]]
    inputs = []
    for job in jobs:
        data = contained_path(Path(config["corpus"]), job["asset"]["input_file"]).read_bytes()
        if digest(data) != job["asset"]["sha256"]:
            raise ValueError("corpus input changed")
        kwargs = dict(job["kwargs"])
        if kwargs.get("size_hint") is not None:
            kwargs["size_hint"] = tuple(kwargs["size_hint"])
        inputs.append((job, data, kwargs))
    if mode == "timing":
        for _job, data, kwargs in inputs:
            meta.render_metafile(data, **kwargs)
        gc.collect()
    results = {}
    for job, data, kwargs in inputs:
        started = time.perf_counter()
        rendered = meta.render_metafile(data, **kwargs)
        seconds = time.perf_counter() - started
        results[job["variant"]] = {"seconds": seconds, "signature": result_signature(rendered)}
    return {"cases": results, "peak_rss_bytes": peak_rss_bytes(), "identity": process_identity()}


def launch(config: Path, source: Path, mode: str, output: Path, python: str, timeout: float) -> dict[str, Any]:
    """串行隔离工作进程并保留日志；超时或退出失败不能被计入性能成绩。"""
    output.mkdir(parents=True)
    with (output / "process.log").open("w") as log:
        process = subprocess.run(
            [
                python,
                str(Path(__file__).resolve()),
                "--worker",
                mode,
                "--config",
                str(config),
                "--worker-source",
                str(source),
                "--output",
                str(output),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    if process.returncode:
        raise RuntimeError(f"worker failed: {output / 'process.log'}")
    return json.loads((output / "result.json").read_text())


def validate_measurement(
    result: dict[str, Any], expected: dict[str, Any], *, require_calls: bool = True
) -> list[dict[str, Any]]:
    """正式计时也必须覆盖全部预定案例并产生冻结输出，不能以少跑案例制造加速。"""
    actual = {key: row["signature"] for key, row in result["cases"].items()}
    difference = first_difference(expected, actual)
    backend = result.get("identity", {}).get("compute_backend") or {}
    errors = [difference] if difference is not None else []
    if (
        backend.get("backend") != "rust"
        or backend.get("build_profile") != "release"
        or (require_calls and backend.get("native_calls", 0) <= 0)
    ):
        errors.append({"native_execution": backend})
    return errors


def validate_reference_matrix(manifest: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    """冻结参考必须覆盖完整格式矩阵，案例路径、载荷和参数不能被悄悄替换。"""
    cases = {case["id"]: case for case in manifest["cases"]}
    if len(cases) != len(manifest["cases"]) or any(
        len(key) != 64 or any(character not in "0123456789abcdef" for character in key) for key in cases
    ):
        raise ValueError("corpus case identities must be unique lowercase SHA256 values")
    formats = {row["format"] for row in rows}
    if not formats or not formats <= {"svg", "png", "jpeg", "webp"}:
        raise ValueError("reference formats are empty or unsupported")
    variants = set()
    for row in rows:
        case = cases.get(row["case"])
        if case is None or row["asset"] != case["asset"]:
            raise ValueError("reference case or asset does not belong to corpus")
        variant = row["case"][:20] + "-" + row["format"]
        if row["variant"] != variant or variant in variants:
            raise ValueError("reference variant path is invalid or duplicated")
        variants.add(variant)
        if first_difference(dict(case["kwargs"], output_format=row["format"]), row["kwargs"]) is not None:
            raise ValueError("reference parameters differ from corpus")
    expected = {key[:20] + "-" + fmt for key in cases for fmt in formats}
    if variants != expected:
        raise ValueError("reference is missing corpus/format variants")


def paired(args: argparse.Namespace) -> int:
    """冻结输入和两份源码，先比较完整契约，再交替顺序执行计时及独立 RSS。"""
    corpus, reference = args.corpus.resolve(), args.reference.resolve()
    sources = {"before": args.before_source.resolve(), "after": args.after_source.resolve()}
    output = args.output.resolve()
    validate_destination(output, [corpus, reference, *sources.values()])
    manifest_path = corpus / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    reference_hash = digest((reference / "summary.json").read_bytes())
    previous = json.loads((reference / "summary.json").read_text())
    manifest_hash = digest(manifest_path.read_bytes())
    if not previous.get("ok") or previous["corpus_manifest_sha256"] != manifest_hash:
        raise ValueError("reference must be successful and belong to this exact corpus")
    validate_reference_matrix(manifest, previous["cases"])
    assets = {asset["sha256"]: asset for asset in manifest["assets"]}
    for asset in assets.values():
        if digest(contained_path(corpus, asset["input_file"]).read_bytes()) != asset["sha256"]:
            raise ValueError("corpus input changed")
    variants = set()
    jobs = []
    for row in previous["cases"]:
        if args.select and row["variant"] not in args.select.split(","):
            continue
        if row["variant"] in variants:
            raise ValueError("duplicate reference variant")
        variants.add(row["variant"])
        jobs.append(
            {
                "variant": row["variant"],
                "asset": assets[row["asset"]],
                "kwargs": row["kwargs"],
                "expected": verification_contract(row["verification"]),
                "groups": case_groups(row),
            }
        )
    if not jobs or (args.select and variants != set(args.select.split(","))):
        raise ValueError("selection must name existing nonempty reference variants")
    output.mkdir(parents=True)
    config_path = output / "config.json"
    require_calls = any(
        row["variant"] in variants
        and row["verification"].get("identity", {}).get("compute_backend", {}).get("native_calls", 0) > 0
        for row in previous["cases"]
    )
    config = {"corpus": str(corpus), "jobs": jobs}
    write_json(config_path, config)
    runner_files = [
        Path(__file__),
        *[Path(__file__).with_name(name) for name in ("benchmark_corpus.py", "benchmark_pair.py", "verify_installation.py")],
    ]
    hashes = {str(path): digest(path.read_bytes()) for path in runner_files}
    expected_verify = {job["variant"]: job["expected"] for job in jobs}
    expected_timing = {
        job["variant"]: job["expected"]["signature"]
        for job in jobs
        if "signature" in job["expected"] and "group:robustness" not in job["groups"]
    }
    source_ids = {name: source_identity(root) for name, root in sources.items()}
    report: dict[str, Any] = {
        "corpus_sha256": manifest_hash,
        "sources": source_ids,
        "runner_files": hashes,
        "verification": {},
        "timing": [],
        "memory": [],
        "errors": [],
    }
    for side, source in sources.items():
        result = launch(config_path, source, "verify", output / ("verify-" + side), args.python, args.timeout)
        report["verification"][side] = result
        difference = first_difference(expected_verify, result["cases"])
        if difference is not None:
            report["errors"].append({"side": side, "verification": difference})
    difference = runtime_difference(
        report["verification"]["before"]["identity"],
        report["verification"]["after"]["identity"],
        ignore_metafile=True,
        allow_module_subset=False,
    )
    if difference is not None:
        report["errors"].append({"runtime": difference})
    if not report["errors"]:
        for mode, rounds in (("timing", args.samples), ("memory", args.rss_runs)):
            for index in range(rounds):
                pair = {}
                for side in ("before", "after") if index % 2 == 0 else ("after", "before"):
                    result = launch(
                        config_path, sources[side], mode, output / f"{mode}-{index + 1}-{side}", args.python, args.timeout
                    )
                    pair[side] = result
                    report["errors"].extend(validate_measurement(result, expected_timing, require_calls=require_calls))
                    difference = runtime_difference(
                        report["verification"][side]["identity"], result["identity"], allow_module_subset=False
                    )
                    if difference is not None:
                        report["errors"].append({"runtime": difference})
                report[mode].append(pair)
                write_json(output / "summary.json", report)
                print(json.dumps({"mode": mode, "pair": index + 1, "errors": len(report["errors"])}), flush=True)
    samples: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"before": [], "after": []})
    variants_report = {}
    for variant in expected_timing:
        variants_report[variant] = (
            paired_statistics(
                [pair["before"]["cases"][variant]["seconds"] for pair in report["timing"]],
                [pair["after"]["cases"][variant]["seconds"] for pair in report["timing"]],
            )
            if report["timing"]
            else {}
        )
    for pair in report["timing"]:
        totals: dict[str, dict[str, float]] = defaultdict(lambda: {"before": 0.0, "after": 0.0})
        for job in jobs:
            if job["variant"] not in expected_timing:
                continue
            for group in job["groups"]:
                for side in sources:
                    totals[group][side] += pair[side]["cases"][job["variant"]]["seconds"]
        for group, sides in totals.items():
            for side, value in sides.items():
                samples[group][side].append(value)
    report["groups"] = {group: paired_statistics(sides["before"], sides["after"]) for group, sides in samples.items()}
    report["variants"] = variants_report
    report["rss"] = (
        paired_statistics(
            [pair["before"]["peak_rss_bytes"] for pair in report["memory"]],
            [pair["after"]["peak_rss_bytes"] for pair in report["memory"]],
        )
        if report["memory"]
        else {}
    )
    report["sources_unchanged"] = all(
        source_identity(root)["tree_sha256"] == source_ids[side]["tree_sha256"] for side, root in sources.items()
    )
    report["runner_unchanged"] = all(digest(Path(name).read_bytes()) == value for name, value in hashes.items())
    report["corpus_unchanged"] = digest(manifest_path.read_bytes()) == manifest_hash and all(
        digest(contained_path(corpus, asset["input_file"]).read_bytes()) == asset["sha256"] for asset in assets.values()
    )
    report["reference_unchanged"] = digest((reference / "summary.json").read_bytes()) == reference_hash
    report["ok"] = not report["errors"] and all(
        report[key] for key in ("sources_unchanged", "runner_unchanged", "corpus_unchanged", "reference_unchanged")
    )
    write_json(output / "summary.json", report)
    return 0 if report["ok"] else 1


def main() -> None:
    """提供完整成对验收与内部隔离 worker，不允许覆盖冻结参考。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--before-source", type=Path)
    parser.add_argument("--after-source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--rss-runs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--select")
    parser.add_argument("--worker", choices=("verify", "timing", "memory"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--worker-source")
    args = parser.parse_args()
    if args.worker:
        write_json(
            args.output / "result.json",
            worker(json.loads(args.config.read_text()), args.worker_source, args.worker, args.output),
        )
        return
    if (
        not all((args.corpus, args.reference, args.before_source, args.after_source))
        or min(args.samples, args.rss_runs) < 1
        or args.timeout <= 0
    ):
        parser.error("corpus, reference, both sources and positive sample counts are required")
    raise SystemExit(paired(args))


if __name__ == "__main__":
    main()
