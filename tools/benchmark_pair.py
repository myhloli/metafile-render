"""冻结真实 DOC 参考产物，以独立进程完成成对计时、像素比较与 RSS 验收。"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict
import gc
import hashlib
import importlib
from importlib import machinery, metadata
from io import BytesIO
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
from typing import Any


def digest(data: bytes) -> str:
    """计算原始输入、实际产物与构建物的内容指纹。"""
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: Any) -> bytes:
    """稳定编码结构化清单，避免字典插入顺序影响身份。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def read_json(path: Path) -> Any:
    """读取参考或工作进程的 UTF-8 结构化报告。"""
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    """只向调用方已分配的结果目录写出可审阅报告。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def contained_path(root: Path, name: str) -> Path:
    """限制清单中的相对路径，拒绝绝对路径、上跳和逃逸符号链接。"""
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe manifest path: {name}")
    resolved = (root / relative).resolve()
    if resolved == root.resolve() or not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"manifest path escapes its directory: {name}")
    return resolved


def paths_overlap(first: Path, second: Path) -> bool:
    """将符号链接解析后的目录相等与相互包含都视为重叠。"""
    left, right = first.resolve(), second.resolve()
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def validate_destination(output: Path, protected: Sequence[Path]) -> None:
    """拒绝覆盖结果以及向参考或源码树内部写入基准产物。"""
    if output.exists() or output.is_symlink():
        raise ValueError(f"output already exists: {output}")
    for path in protected:
        if paths_overlap(output, path):
            raise ValueError(f"output overlaps protected path: {path}")


def first_difference(expected: Any, actual: Any, path: str = "$") -> dict[str, Any] | None:
    """返回首个严格结构差异，保持布尔、整数和字段缺失可区分。"""
    if type(expected) is not type(actual):
        return {"path": path, "expected": expected, "actual": actual}
    if isinstance(expected, dict):
        if set(expected) != set(actual):
            return {"path": path, "expected_keys": sorted(expected), "actual_keys": sorted(actual)}
        for key in sorted(expected):
            difference = first_difference(expected[key], actual[key], f"{path}.{key}")
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return {"path": path, "expected_length": len(expected), "actual_length": len(actual)}
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = first_difference(left, right, f"{path}[{index}]")
            if difference is not None:
                return difference
        return None
    return None if expected == actual else {"path": path, "expected": expected, "actual": actual}


def reference_manifest(reference: Path, document: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """核验捕获清单及全部参考字节，并冻结本轮依赖的参考文件指纹。"""
    capture = read_json(reference / "capture.json")
    signature = read_json(reference / "document/signature.json")
    if digest(document.read_bytes()) != capture["environment"]["source_sha256"]:
        raise ValueError("DOC source hash differs from reference")
    files = {"capture.json", "document/signature.json", "document/middle.json", "document/model.json"}
    calls = capture["calls"]
    if not calls:
        raise ValueError("reference contains no metafile calls")
    for index, call in enumerate(calls, 1):
        if call["index"] != index or "error" in call or "result" not in call:
            raise ValueError("reference must contain ordered successful calls numbered from one")
        for key, expected_hash in (("input_file", call["input_sha256"]), ("output_file", call["result"]["sha256"])):
            payload = contained_path(reference, call[key]).read_bytes()
            if digest(payload) != expected_hash:
                raise ValueError(f"reference hash differs: {call[key]}")
            files.add(call[key])
        if call["kwargs"].get("output_format") != call["result"]["output_format"]:
            raise ValueError("reference output format differs from original arguments")
    for name, expected_hash in signature["assets"].items():
        relative = "document/" + name
        if digest(contained_path(reference, relative).read_bytes()) != expected_hash:
            raise ValueError(f"reference asset hash differs: {name}")
        files.add(relative)
    if digest(json_bytes(read_json(reference / "document/middle.json"))) != signature["middle_sha256"]:
        raise ValueError("reference MiddleJSON hash differs")
    if digest((reference / "document/model.json").read_bytes()) != signature["model_sha256"]:
        raise ValueError("reference ModelJSON hash differs")
    hashes = {name: digest(contained_path(reference, name).read_bytes()) for name in sorted(files)}
    return capture, hashes


def source_identity(root: Path) -> dict[str, Any]:
    """同时支持 Git checkout 和归档源码，按运行源码及原生库计算树指纹。"""
    files: dict[str, str] = {}
    for directory in ("src", "rust"):
        base = root / directory
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                if path.is_file() and path.suffix != ".pyc" and "__pycache__" not in path.parts and "target" not in path.parts:
                    files[str(path.relative_to(root))] = digest(path.read_bytes())
    for name in ("pyproject.toml", "Cargo.toml", "Cargo.lock", "build.rs"):
        if (root / name).is_file():
            files[name] = digest((root / name).read_bytes())
    identity: dict[str, Any] = {"root": str(root), "tree_sha256": digest(json_bytes(files)), "files": files}
    snapshot = root / "snapshot.json"
    if snapshot.is_file():
        identity["snapshot"] = {"sha256": digest(snapshot.read_bytes()), "content": read_json(snapshot)}
    probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if probe.returncode == 0 and Path(probe.stdout.strip()).resolve() == root.resolve():
        identity["git"] = {
            "commit": subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(),
            "status": subprocess.check_output(["git", "-C", str(root), "status", "--short"], text=True).strip(),
        }
    return identity


def process_identity() -> dict[str, Any]:
    """记录工作负载实际加载的包及原生模块，未加载的扩展不冒充已执行。"""
    packages = {}
    binaries = {}
    prefixes = ("metafile_render", "docvortex", "PIL", "pyclipper")
    for name, module in sorted(sys.modules.items()):
        if not any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
            continue
        filename = getattr(module, "__file__", None)
        if filename is None:
            continue
        if name in prefixes:
            packages[name] = str(Path(filename).resolve())
        if any(filename.endswith(suffix) for suffix in machinery.EXTENSION_SUFFIXES):
            path = Path(filename).resolve()
            binaries[name] = {
                "path": str(path),
                "sha256": digest(path.read_bytes()),
                "protocol": getattr(module, "PROTOCOL_VERSION", None),
                "build_profile": getattr(module, "BUILD_PROFILE", None),
            }
    versions = {}
    for name in ("Pillow", "pyclipper", "olefile"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return {
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "packages": packages,
        "loaded_native_modules": binaries,
        "dependencies": versions,
        "hash_seed": os.environ.get("PYTHONHASHSEED"),
        "compute_backend": (
            sys.modules["metafile_render._native_points"].backend_info()
            if "metafile_render._native_points" in sys.modules
            else None
        ),
    }


def require_native_execution(result: dict[str, Any]) -> None:
    """强制原生比较必须实际执行 release 内核，防止缺扩展或未命中被算作 Rust 成绩。"""
    backend = result.get("identity", {}).get("compute_backend") or {}
    if backend.get("backend") != "rust" or backend.get("build_profile") != "release" or backend.get("native_calls", 0) <= 0:
        result["ok"] = False
        result["native_execution_error"] = "after variant did not execute a release Rust point kernel"


def runtime_difference(
    expected: dict[str, Any],
    actual: dict[str, Any],
    *,
    ignore_metafile: bool = False,
    allow_module_subset: bool = True,
) -> dict[str, Any] | None:
    """相同工作量严格核对模块集合；较小计时工作量可显式保留子集兼容。"""
    fields = ("python", "executable", "platform", "machine", "dependencies", "hash_seed")
    difference = first_difference({key: expected.get(key) for key in fields}, {key: actual.get(key) for key in fields})
    if difference is not None:
        return difference
    for group in ("packages", "loaded_native_modules"):
        expected_modules, actual_modules = (
            {
                name: value
                for name, value in identity.get(group, {}).items()
                if not (ignore_metafile and (name == "metafile_render" or name.startswith("metafile_render.")))
            }
            for identity in (expected, actual)
        )
        if not allow_module_subset:
            difference = first_difference(expected_modules, actual_modules, f"$.{group}")
            if difference is not None:
                return difference
        else:
            for name, value in actual_modules.items():
                difference = first_difference(expected_modules.get(name), value, f"$.{group}.{name}")
                if difference is not None:
                    return difference
    return None


def load_runtime(config: dict[str, Any], variant: str) -> tuple[Any, Any]:
    """只从指定的两份源码导入运行时，并拒绝被已安装包遮蔽。"""
    meta_root = Path(config[variant + "_source"])
    doc_root = Path(config["docvortex_source"])
    sys.path[:0] = [str(meta_root / "src"), str(doc_root / "src")]
    meta = importlib.import_module("metafile_render")
    doc = importlib.import_module("docvortex")
    for module, root in ((meta, meta_root), (doc, doc_root)):
        expected = root / "src" / module.__name__ / "__init__.py"
        if Path(module.__file__).resolve() != expected.resolve():
            raise ValueError(f"unexpected imported package: {module.__file__}; expected {expected}")
    return meta, doc


def load_cases(config: dict[str, Any]) -> list[tuple[dict[str, Any], bytes]]:
    """在计时前加载冻结输入，保持原清单顺序和全部真实调用参数。"""
    root = Path(config["reference"])
    return [(entry, contained_path(root, entry["input_file"]).read_bytes()) for entry in config["calls"]]


def original_kwargs(entry: dict[str, Any]) -> dict[str, Any]:
    """恢复 JSON 记录中的 tuple 参数，保留其余原始调用值。"""
    kwargs = dict(entry["kwargs"])
    if kwargs.get("size_hint") is not None:
        kwargs["size_hint"] = tuple(kwargs["size_hint"])
    return kwargs


def workload_runner(config: dict[str, Any], workload: str, meta: Any, doc: Any) -> Callable[[], Any]:
    """构造不插桩、不散列、不写文件的完整公开入口调用。"""
    if workload == "doc":
        document = Path(config["document"])

        def parse_document() -> Any:
            """通过实际路径执行完整 DOC 分析及后处理。"""
            return doc.parse(document, keep_model_json=True)

        return parse_document
    items = [(data, original_kwargs(entry)) for entry, data in load_cases(config)]

    def replay_metafiles() -> Any:
        """顺序重放全部图片，只保留末张结果以保持自然的释放边界。"""
        result = None
        for data, kwargs in items:
            result = meta.render_metafile(data, **kwargs)
        return result

    return replay_metafiles


def peak_rss_bytes() -> int:
    """读取平台统一的进程 RSS 高水位，明确其不是累计分配量。"""
    if sys.platform != "win32":
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value if sys.platform == "darwin" else value * 1024)
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        """描述 Windows 进程内存计数结构中的峰值工作集。"""

        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in (
                "PeakWorkingSetSize",
                "WorkingSetSize",
                "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage",
                "PagefileUsage",
                "PeakPagefileUsage",
            )
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(counters.PeakWorkingSetSize)


def result_signature(result: Any) -> dict[str, Any]:
    """按初始基线格式记录字节、尺寸及全部公开诊断。"""
    return {
        "sha256": digest(result.data),
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


def document_signature(result: Any) -> dict[str, Any]:
    """复用初始语义和素材指纹格式，环境路径不混入比较。"""
    model = result.model_json.to_json(skip_defaults=False).encode()
    return {
        "middle_sha256": digest(json_bytes(result.to_dict())),
        "model_sha256": digest(model),
        "pages": len(result.middle_json.pages),
        "model_block_counts": dict(Counter(str(block.get("type")) for page in result.model_json.pages for block in page)),
        "asset_count": len(result.assets),
        "assets": {name: digest(result.assets[name]) for name in result.assets},
        "diagnostics": [asdict(x) for x in result.diagnostics],
    }


def image_signature(payload: bytes, output_format: str) -> tuple[dict[str, Any], bytes]:
    """从实际 SVG 提取 fallback，并独立记录容器与 RGBA 像素指纹。"""
    from PIL import Image

    logical_size = None
    if output_format == "svg":
        from docvortex.foundation._image_payload import extract_generated_svg_fallback

        png, width, height = extract_generated_svg_fallback(payload)
        logical_size = [width, height]
    else:
        png = payload
    with Image.open(BytesIO(png)) as image:
        signature = {
            "logical_size": logical_size,
            "pixel_size": list(image.size),
            "encoded_sha256": digest(png),
            "rgba_sha256": digest(image.convert("RGBA").tobytes()),
        }
    return signature, png


def save_pixel_difference(expected: bytes, actual: bytes, path: Path) -> None:
    """用四通道差异的最大值生成可见差异图，避免透明 RGB 差异被隐藏。"""
    from PIL import Image, ImageChops

    with Image.open(BytesIO(expected)) as left, Image.open(BytesIO(actual)) as right:
        if left.size != right.size:
            return
        channels = ImageChops.difference(left.convert("RGBA"), right.convert("RGBA")).split()
        difference = channels[0]
        for channel in channels[1:]:
            difference = ImageChops.lighter(difference, channel)
        path.parent.mkdir(parents=True, exist_ok=True)
        difference.save(path)


def verify_metafiles(config: dict[str, Any], meta: Any, output: Path) -> list[dict[str, Any]]:
    """生成独立候选 SVG/PNG，再与不可修改的参考结果严格比较。"""
    rows = []
    reference = Path(config["reference"])
    for entry, data in load_cases(config):
        result = meta.render_metafile(data, **original_kwargs(entry))
        actual = result_signature(result)
        number = f"{entry['index']:03d}"
        path = output / "rendered" / (number + "." + result.output_format)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(result.data)
        expected_bytes = contained_path(reference, entry["output_file"]).read_bytes()
        expected_image, expected_png = image_signature(expected_bytes, entry["result"]["output_format"])
        actual_image, actual_png = image_signature(result.data, result.output_format)
        fallback = output / "fallback_png" / (number + ".png")
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_bytes(actual_png)
        if entry["index"] in {22, 32, 35} or expected_image != actual_image:
            save_pixel_difference(expected_png, actual_png, output / "pixel_differences" / (number + ".png"))
        differences = [
            item
            for item in (first_difference(entry["result"], actual), first_difference(expected_image, actual_image))
            if item is not None
        ]
        rows.append(
            {
                "index": entry["index"],
                "input_sha256": digest(data),
                "kwargs": entry["kwargs"],
                "result": actual,
                "fallback": actual_image,
                "differences": differences,
                "ok": not differences,
            }
        )
    return rows


def verify_document(config: dict[str, Any], doc: Any, output: Path) -> dict[str, Any]:
    """单独插桩完整 DOC 验证实际调用清单，并保存语义树与所有候选素材。"""
    office_image = importlib.import_module("docvortex.analyzers.native.office.image")
    original = office_image.render_metafile
    calls: list[dict[str, Any]] = []

    def wrapped(data: bytes, **kwargs: Any) -> Any:
        """原样执行真实调用并记录输入、参数、结果，不参与性能计时。"""
        result = original(data, **kwargs)
        calls.append(
            {
                "index": len(calls) + 1,
                "input_sha256": digest(data),
                "kwargs": json.loads(json_bytes(kwargs)),
                "result": result_signature(result),
            }
        )
        return result

    office_image.render_metafile = wrapped
    try:
        document = doc.parse(Path(config["document"]), keep_model_json=True)
    finally:
        office_image.render_metafile = original
    actual = document_signature(document)
    expected = read_json(Path(config["reference"]) / "document/signature.json")
    expected_calls = [{key: entry[key] for key in ("index", "input_sha256", "kwargs", "result")} for entry in config["calls"]]
    differences = [
        item for item in (first_difference(expected, actual), first_difference(expected_calls, calls)) if item is not None
    ]
    destination = output / "document"
    write_json(destination / "middle.json", document.to_dict())
    (destination / "model.json").write_text(document.model_json.to_json(skip_defaults=False), encoding="utf-8")
    write_json(destination / "signature.json", actual)
    write_json(destination / "calls.json", calls)
    for name in document.assets:
        path = contained_path(destination, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(document.assets[name])
    return {"signature": actual, "calls": calls, "differences": differences, "ok": not differences}


def worker(config: dict[str, Any], variant: str, mode: str, workload: str, output: Path) -> dict[str, Any]:
    """在唯一子进程执行一种测量，身份散列始终在计时或 RSS 采集之后。"""
    meta, doc = load_runtime(config, variant)
    if mode == "verify":
        images = verify_metafiles(config, meta, output)
        document = verify_document(config, doc, output)
        result = {"ok": all(row["ok"] for row in images) and document["ok"], "metafiles": images, "document": document}
    else:
        run = workload_runner(config, workload, meta, doc)
        if mode == "timing":
            started = time.perf_counter()
            value = run()
            first_seconds = time.perf_counter() - started
            del value
            gc.collect()
            started = time.perf_counter()
            value = run()
            elapsed = time.perf_counter() - started
            result = {"ok": True, "first_seconds_excluding_import": first_seconds, "seconds": elapsed}
        else:
            pre_rss = peak_rss_bytes()
            value = run()
            post_rss = peak_rss_bytes()
            result = {"ok": True, "pre_workload_peak_rss_bytes": pre_rss, "peak_rss_bytes": post_rss}
        del value
    result.update({"variant": variant, "mode": mode, "workload": workload, "identity": process_identity()})
    return result


def paired_order(samples: int) -> list[tuple[int, str]]:
    """交替 AB 与 BA 的样本顺序，形成可计算配对比值的独立进程序列。"""
    return [
        (index, variant)
        for index in range(samples)
        for variant in (("before", "after") if index % 2 == 0 else ("after", "before"))
    ]


def launch(
    config_path: Path, config: dict[str, Any], variant: str, mode: str, workload: str, destination: Path
) -> dict[str, Any]:
    """串行启动指定解释器，把日志和结果保存在该次唯一子目录。"""
    destination.mkdir(parents=True, exist_ok=False)
    command = [
        config["python"],
        str(Path(__file__).resolve()),
        "--worker",
        mode,
        "--config",
        str(config_path),
        "--variant",
        variant,
        "--workload",
        workload,
        "--task-output",
        str(destination),
    ]
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
    with (destination / "process.log").open("w", encoding="utf-8") as log:
        completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, env=environment, cwd=destination)
    result_path = destination / "result.json"
    result = read_json(result_path) if result_path.exists() else {"ok": False, "error": "worker did not write result.json"}
    result["returncode"] = completed.returncode
    result["ok"] = result["ok"] and completed.returncode == 0
    result["artifact"] = str(destination.relative_to(Path(config["output"])))
    return result


def summarize_pairs(rows: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    """报告每对原始比值、中位收益和离散程度，不自动把噪声当作优化。"""
    pairs: dict[int, dict[str, float]] = {}
    for row in rows:
        pairs.setdefault(row["pair"], {})[row["variant"]] = row[metric]
    ratios = [pair["after"] / pair["before"] for _, pair in sorted(pairs.items())]
    before = [pair["before"] for pair in pairs.values()]
    after = [pair["after"] for pair in pairs.values()]
    return {
        "before_median": statistics.median(before),
        "after_median": statistics.median(after),
        "paired_ratios": ratios,
        "median_after_before_ratio": statistics.median(ratios),
        "median_fraction_reduction": 1 - statistics.median(ratios),
        "max_absolute_ratio_deviation_from_one": max(abs(ratio - 1) for ratio in ratios),
        "ratio_min": min(ratios),
        "ratio_max": max(ratios),
    }


def configuration_paths(args: argparse.Namespace) -> dict[str, str]:
    """解析源码路径但保留虚拟环境解释器的符号链接入口，避免误用基础 Python。"""
    names = ("reference", "before_source", "after_source", "docvortex_source", "python", "output")
    if any(getattr(args, name) is None for name in names):
        raise ValueError("reference, before-source, after-source, docvortex-source, python and output are required")
    return {name: str(getattr(args, name).absolute() if name == "python" else getattr(args, name).resolve()) for name in names}


def orchestrate(args: argparse.Namespace) -> int:
    """先核验参考与输出，再逐阶段启动工作进程并生成可失败的汇总。"""
    if args.samples < 5 or args.rss_runs < 3:
        raise ValueError("at least five timing pairs and three RSS pairs are required")
    config = configuration_paths(args)
    output = Path(config["output"])
    protected = [Path(config[name]) for name in ("reference", "before_source", "after_source", "docvortex_source")]
    validate_destination(output, protected)
    for name, package in (
        ("before_source", "metafile_render"),
        ("after_source", "metafile_render"),
        ("docvortex_source", "docvortex"),
    ):
        if not (Path(config[name]) / "src" / package / "__init__.py").is_file():
            raise ValueError(f"source tree has no {package} package: {config[name]}")
    if not Path(config["python"]).is_file():
        raise ValueError("python executable does not exist")
    reference = Path(config["reference"])
    captured = read_json(reference / "capture.json")
    document = (args.document or Path(captured["environment"]["source"])).resolve()
    capture, reference_hashes = reference_manifest(reference, document)
    identities = {name: source_identity(Path(config[name])) for name in ("before_source", "after_source", "docvortex_source")}
    config.update({"document": str(document), "calls": capture["calls"], "samples": args.samples, "rss_runs": args.rss_runs})
    output.mkdir(parents=True, exist_ok=False)
    config_path = output / "config.json"
    write_json(config_path, config)
    write_json(output / "reference_manifest.json", reference_hashes)
    write_json(output / "sources.json", identities)
    same_source = identities["before_source"]["tree_sha256"] == identities["after_source"]["tree_sha256"]
    report: dict[str, Any] = {
        "ok": True,
        "comparison_kind": "A/A" if same_source else "A/B",
        "reference": str(reference),
        "runner_sha256": digest(Path(__file__).read_bytes()),
        "reference_manifest_sha256": digest(json_bytes(reference_hashes)),
        "input_doc_sha256": capture["environment"]["source_sha256"],
        "metafile_call_count": len(config["calls"]),
        "notes": [
            "Each timing sample uses a fresh process: imports, one first call, gc.collect(), then one measured call.",
            "The first call excludes imports and is not a cold disk-cache measurement.",
            "No correctness hashing, export, instrumentation or tracing runs between warmup and measurement.",
            "Metafile timing covers the complete preloaded replay loop, including normal result disposal.",
            "RSS uses separate fresh processes before hashing; high-water marks are not allocation totals.",
            "Performance ratios are observations; A/A noise and review determine acceptance.",
        ],
        "verification": {},
        "timing": {},
        "memory": {},
    }
    for variant in ("before", "after"):
        result = launch(config_path, config, variant, "verify", "all", output / "verification" / variant)
        if variant == "after" and getattr(args, "require_native_after", False):
            require_native_execution(result)
        report["verification"][variant] = result
        report["ok"] = report["ok"] and result["ok"]
    report["shared_runtime_difference"] = runtime_difference(
        report["verification"]["before"].get("identity", {}),
        report["verification"]["after"].get("identity", {}),
        ignore_metafile=True,
        allow_module_subset=False,
    )
    report["ok"] = report["ok"] and report["shared_runtime_difference"] is None
    if report["ok"]:
        for mode, count, metric in (("timing", args.samples, "seconds"), ("memory", args.rss_runs, "peak_rss_bytes")):
            for workload in ("doc", "metafiles"):
                rows = []
                for pair, variant in paired_order(count):
                    destination = output / mode / workload / f"{pair:02d}-{variant}"
                    result = launch(config_path, config, variant, mode, workload, destination)
                    if variant == "after" and getattr(args, "require_native_after", False):
                        require_native_execution(result)
                    result["runtime_difference"] = runtime_difference(
                        report["verification"][variant].get("identity", {}), result.get("identity", {})
                    )
                    result["ok"] = result["ok"] and result["runtime_difference"] is None
                    result["pair"] = pair
                    rows.append(result)
                    print(
                        json.dumps(
                            {
                                "mode": mode,
                                "workload": workload,
                                "pair": pair,
                                "variant": variant,
                                "ok": result["ok"],
                                "value": result.get(metric),
                            }
                        ),
                        flush=True,
                    )
                    if not result["ok"]:
                        report["ok"] = False
                        break
                report[mode][workload] = {"samples": rows}
                if all(row["ok"] for row in rows) and len(rows) == count * 2:
                    report[mode][workload]["comparison"] = summarize_pairs(rows, metric)
                write_json(output / "summary.json", report)
                if not report["ok"]:
                    break
            if not report["ok"]:
                break
    _, final_reference_hashes = reference_manifest(reference, document)
    report["reference_unchanged"] = final_reference_hashes == reference_hashes
    report["sources_unchanged"] = {
        name: source_identity(Path(config[name]))["tree_sha256"] == identity["tree_sha256"]
        for name, identity in identities.items()
    }
    report["runner_unchanged"] = digest(Path(__file__).read_bytes()) == report["runner_sha256"]
    report["ok"] = (
        report["ok"]
        and report["reference_unchanged"]
        and report["runner_unchanged"]
        and all(report["sources_unchanged"].values())
    )
    write_json(output / "summary.json", report)
    write_json(output / "comparison.json", {key: report[key] for key in ("ok", "comparison_kind", "timing", "memory")})
    return 0 if report["ok"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    """提供公开成对入口及内部工作进程入口，并将比较失败转换为非零状态。"""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("reference", "before-source", "after-source", "docvortex-source", "python", "output", "document"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--rss-runs", type=int, default=3)
    parser.add_argument("--require-native-after", action="store_true")
    parser.add_argument("--worker", choices=("verify", "timing", "memory"), help=argparse.SUPPRESS)
    parser.add_argument("--config", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--variant", choices=("before", "after"), help=argparse.SUPPRESS)
    parser.add_argument("--workload", choices=("doc", "metafiles", "all"), help=argparse.SUPPRESS)
    parser.add_argument("--task-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        if args.config is None or args.task_output is None or args.variant is None or args.workload is None:
            parser.error("worker requires config, task-output, variant and workload")
        try:
            result = worker(read_json(args.config), args.variant, args.worker, args.workload, args.task_output)
        except Exception as exc:
            result = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
        write_json(args.task_output / "result.json", result)
        return 0 if result["ok"] else 1
    try:
        return orchestrate(args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"benchmark_pair: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
