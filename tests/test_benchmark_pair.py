"""验证成对基准的参考保护、实际候选产物、测量边界与失败状态。"""

from __future__ import annotations

import argparse
import base64
import importlib.util
from io import BytesIO
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
import xml.etree.ElementTree as ET

from PIL import Image
import pytest

_SPEC = importlib.util.spec_from_file_location("metafile_benchmark_pair", Path(__file__).parents[1] / "tools/benchmark_pair.py")
assert _SPEC is not None and _SPEC.loader is not None
benchmark = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(benchmark)


def svg_payload(color: str) -> tuple[bytes, bytes]:
    """构造仅包含固定像素 fallback 的小型 SVG，避免运行真实 DOC。"""
    stream = BytesIO()
    Image.new("RGBA", (2, 2), color).save(stream, format="PNG")
    png = stream.getvalue()
    encoded = base64.b64encode(png).decode("ascii")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2" viewBox="0 0 2 2" '
        'data-metafile-render="wmf-emf"><metadata id="metafile-render-raster-fallback" '
        f'data-mime="image/png">{encoded}</metadata></svg>'
    ).encode()
    return svg, png


def rendered_result(payload: bytes) -> SimpleNamespace:
    """提供与公开渲染结果字段一致的可控测试结果。"""
    return SimpleNamespace(
        data=payload,
        width=2,
        height=2,
        output_format="svg",
        media_type="image/svg+xml",
        source_format="emf",
        emfplus_mode="none",
        partial=False,
        diagnostics=[],
    )


class MinimalModel:
    """提供不会触发真实解析的最小 ModelJSON 接口。"""

    pages: list[list[dict[str, Any]]] = [[]]

    def to_json(self, *, skip_defaults: bool = False) -> str:
        """固定语义字节，使调用清单变化可以独立于文档内容验证。"""
        return "{}"


class MinimalDocument:
    """模拟单页、无素材文档的公开结果。"""

    def __init__(self) -> None:
        """为每个结果分配独立语义与素材容器。"""
        self.model_json = MinimalModel()
        self.middle_json = SimpleNamespace(pages=[[]])
        self.assets: dict[str, bytes] = {}
        self.diagnostics: list[Any] = []

    def to_dict(self) -> dict[str, Any]:
        """返回测试使用的固定中间语义对象。"""
        return {}


@pytest.fixture
def captured_case(tmp_path: Path) -> dict[str, Any]:
    """建立独立参考目录和源码布局，覆盖工具契约而不依赖 DocVortex 安装。"""
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "inputs").mkdir()
    (reference / "rendered").mkdir()
    (reference / "document").mkdir()
    document = tmp_path / "sample.doc"
    document.write_bytes(b"DOC fixture")
    input_data = b"EMF fixture"
    (reference / "inputs/one.emf").write_bytes(input_data)
    svg, png = svg_payload("red")
    (reference / "rendered/001.svg").write_bytes(svg)
    entry = {
        "index": 1,
        "input_sha256": benchmark.digest(input_data),
        "input_bytes": len(input_data),
        "input_file": "inputs/one.emf",
        "output_file": "rendered/001.svg",
        "kwargs": {"output_format": "svg", "dpi": 200, "size_hint": None, "backend": "auto"},
        "result": benchmark.result_signature(rendered_result(svg)),
    }
    capture = {
        "environment": {"source": str(document), "source_sha256": benchmark.digest(document.read_bytes())},
        "calls": [entry],
    }
    benchmark.write_json(reference / "capture.json", capture)
    benchmark.write_json(reference / "document/signature.json", benchmark.document_signature(MinimalDocument()))
    benchmark.write_json(reference / "document/middle.json", {})
    (reference / "document/model.json").write_text("{}", encoding="utf-8")
    sources = {}
    for variant, package in (("before", "metafile_render"), ("after", "metafile_render"), ("docvortex", "docvortex")):
        root = tmp_path / variant
        package_root = root / "src" / package
        package_root.mkdir(parents=True)
        (package_root / "__init__.py").write_text('"""测试源码归档。"""\n', encoding="utf-8")
        sources[variant + "_source"] = str(root)
    return {
        "reference": str(reference),
        "document": str(document),
        "calls": [entry],
        "python": sys.executable,
        "output": str(tmp_path / "results"),
        "samples": 5,
        "rss_runs": 3,
        **sources,
        "expected_svg": svg,
        "expected_png": png,
    }


def namespace_for(case: dict[str, Any]) -> argparse.Namespace:
    """将测试布局转为公开命令参数。"""
    paths = {
        name: Path(case[name])
        for name in ("reference", "document", "before_source", "after_source", "docvortex_source", "python", "output")
    }
    return argparse.Namespace(**paths, samples=5, rss_runs=3)


def test_destination_rejects_existing_and_overlapping_paths(tmp_path: Path) -> None:
    """任何现有目录、源码子目录或祖先目录都不能成为输出目标。"""
    protected = tmp_path / "protected"
    protected.mkdir()
    for destination in (protected, protected / "new", tmp_path):
        with pytest.raises(ValueError):
            benchmark.validate_destination(destination, [protected])
    benchmark.validate_destination(tmp_path / "safe-sibling", [protected])


def test_destination_resolves_symlink_escape(tmp_path: Path) -> None:
    """输出路径经过符号链接仍不能写入参考目录。"""
    protected = tmp_path / "protected"
    protected.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(protected, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="overlaps"):
        benchmark.validate_destination(alias / "output", [protected])


def test_python_symlink_is_preserved(captured_case: dict[str, Any], tmp_path: Path) -> None:
    """虚拟环境 Python 的入口不得解析为基础解释器而丢失 site-packages。"""
    alias = tmp_path / "venv-python"
    try:
        alias.symlink_to(sys.executable)
    except OSError:
        pytest.skip("symlinks unavailable")
    args = namespace_for(captured_case)
    args.python = alias
    config = benchmark.configuration_paths(args)
    assert config["python"] == str(alias.absolute())
    assert Path(config["python"]) != alias.resolve()


@pytest.mark.parametrize(
    "relative",
    [
        "../escape",
        "/absolute",
        "inputs/../../escape",
        "C:/absolute",
        r"C:\absolute",
        r"\rooted",
        r"\\server\share\escape",
        "C:drive-relative",
        r"inputs\..\..\escape",
    ],
)
def test_manifest_paths_cannot_escape(tmp_path: Path, relative: str) -> None:
    """参考清单不能通过路径跳转读取或写入目录之外的文件。"""
    with pytest.raises(ValueError, match="unsafe"):
        benchmark.contained_path(tmp_path, relative)


def test_reference_hashes_detect_input_tampering(captured_case: dict[str, Any]) -> None:
    """输入字节损坏必须在任何渲染或输出创建之前失败。"""
    reference = Path(captured_case["reference"])
    _, fingerprints = benchmark.reference_manifest(reference, Path(captured_case["document"]))
    assert "rendered/001.svg" in fingerprints
    (reference / "inputs/one.emf").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="reference hash differs"):
        benchmark.reference_manifest(reference, Path(captured_case["document"]))


def test_source_identity_includes_runtime_resources(captured_case: dict[str, Any]) -> None:
    """字体及 XML 等非代码资源变化也必须改变源码身份，归档不要求有 Git。"""
    root = Path(captured_case["before_source"])
    resource = root / "src/metafile_render/runtime.xml"
    resource.write_text("one", encoding="utf-8")
    first = benchmark.source_identity(root)
    resource.write_text("two", encoding="utf-8")
    second = benchmark.source_identity(root)
    assert "git" not in first
    assert first["tree_sha256"] != second["tree_sha256"]
    assert "src/metafile_render/runtime.xml" in second["files"]


def test_pairs_reverse_order_and_compare_matching_samples() -> None:
    """配对按轮次匹配，BA 顺序不会导致反向或跨轮比较。"""
    assert benchmark.paired_order(3) == [(0, "before"), (0, "after"), (1, "after"), (1, "before"), (2, "before"), (2, "after")]
    rows = [
        {"pair": 0, "variant": "before", "seconds": 10.0},
        {"pair": 0, "variant": "after", "seconds": 8.0},
        {"pair": 1, "variant": "after", "seconds": 16.0},
        {"pair": 1, "variant": "before", "seconds": 20.0},
    ]
    assert benchmark.summarize_pairs(rows, "seconds")["paired_ratios"] == [0.8, 0.8]


def test_strict_comparison_does_not_conflate_missing_or_boolean_values() -> None:
    """字段缺失与 False/0 差异不能被宽松比较掩盖。"""
    assert benchmark.first_difference({"partial": False}, {"partial": 0})["path"] == "$.partial"
    assert benchmark.first_difference({"diagnostics": []}, {})["actual_keys"] == []
    assert benchmark.first_difference({"x": [1, 2]}, {"x": [1, 2]}) is None


@pytest.mark.parametrize(
    "backend", [None, {"backend": "python"}, {"backend": "rust", "build_profile": "release", "native_calls": 0}]
)
def test_native_requirement_rejects_unexecuted_kernel(backend: dict[str, Any] | None) -> None:
    """模块可用但没有实际调用也不能通过强制原生性能验收。"""
    result = {"ok": True, "identity": {"compute_backend": backend}}
    benchmark.require_native_execution(result)
    assert result["ok"] is False


def test_native_requirement_accepts_actual_release_execution() -> None:
    """只有记录到 release 内核调用的结果才可作为原生性能样本。"""
    result = {"ok": True, "identity": {"compute_backend": {"backend": "rust", "build_profile": "release", "native_calls": 1}}}
    benchmark.require_native_execution(result)
    assert result["ok"] is True


def test_runtime_identity_detects_native_binary_drift() -> None:
    """版本号相同但实际原生库字节变化时，仍然拒绝将两次测量配对。"""
    expected = {"loaded_native_modules": {"pyclipper._pyclipper": {"sha256": "before"}}}
    actual = {"loaded_native_modules": {"pyclipper._pyclipper": {"sha256": "after"}}}
    assert benchmark.runtime_difference(expected, actual)["path"] == "$.loaded_native_modules.pyclipper._pyclipper.sha256"
    assert benchmark.runtime_difference(expected, {"loaded_native_modules": {}}) is None


def test_same_workload_rejects_missing_native_module() -> None:
    """完整验证进程缺失共享原生模块必须失败，较小计时工作量仍可使用子集。"""
    before = {
        "packages": {"metafile_render": "/before/__init__.py", "PIL": "/pillow/__init__.py"},
        "loaded_native_modules": {"pyclipper._pyclipper": {"sha256": "fixed"}},
    }
    after = {
        "packages": {"metafile_render": "/after/__init__.py", "PIL": "/pillow/__init__.py"},
        "loaded_native_modules": {},
    }
    difference = benchmark.runtime_difference(before, after, ignore_metafile=True, allow_module_subset=False)
    assert difference["path"] == "$.loaded_native_modules"
    assert difference["expected_keys"] == ["pyclipper._pyclipper"]
    assert difference["actual_keys"] == []
    assert benchmark.runtime_difference(before, after, ignore_metafile=True) is None


def test_verify_uses_actual_candidate_svg(
    captured_case: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """改变候选 fallback 必须产生失败和蓝色候选 PNG，不能复用红色参考 PNG。"""
    blue_svg, blue_png = svg_payload("blue")

    def extract_for_test(payload: bytes, output_format: str) -> tuple[dict[str, Any], bytes]:
        """独立解码小型测试 SVG，隔离工具测试对 DocVortex 的依赖。"""
        assert output_format == "svg"
        root = ET.fromstring(payload)
        encoded = root.find("{http://www.w3.org/2000/svg}metadata")
        assert encoded is not None and encoded.text is not None
        png = base64.b64decode(encoded.text)
        with Image.open(BytesIO(png)) as image:
            return {"rgba_sha256": benchmark.digest(image.convert("RGBA").tobytes())}, png

    def render(data: bytes, **kwargs: Any) -> SimpleNamespace:
        """返回故意改变像素的候选结果。"""
        return rendered_result(blue_svg)

    monkeypatch.setattr(benchmark, "image_signature", extract_for_test)
    destination = tmp_path / "candidate"
    rows = benchmark.verify_metafiles(captured_case, SimpleNamespace(render_metafile=render), destination)
    assert not rows[0]["ok"] and rows[0]["differences"]
    assert (destination / "rendered/001.svg").read_bytes() == blue_svg
    assert (destination / "fallback_png/001.png").read_bytes() == blue_png
    assert (Path(captured_case["reference"]) / "rendered/001.svg").read_bytes() == captured_case["expected_svg"]
    with Image.open(destination / "pixel_differences/001.png") as image:
        assert image.getextrema()[1] > 0


def test_document_call_changes_fail_even_with_identical_semantics(
    captured_case: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使文档语义未变，真实调用 DPI 改变仍必须使验证失败。"""

    def render(data: bytes, **kwargs: Any) -> SimpleNamespace:
        """返回与参考完全相同的图像以隔离调用参数验证。"""
        return rendered_result(captured_case["expected_svg"])

    office = SimpleNamespace(render_metafile=render)
    import_original = benchmark.importlib.import_module

    def import_module(name: str) -> Any:
        """仅替换 Office 图片入口，其他导入保持正常。"""
        return office if name == "docvortex.analyzers.native.office.image" else import_original(name)

    def parse(path: Path, *, keep_model_json: bool) -> MinimalDocument:
        """模拟同样输出却改变真实图片 DPI 的错误候选。"""
        kwargs = dict(captured_case["calls"][0]["kwargs"], dpi=100)
        office.render_metafile(b"EMF fixture", **kwargs)
        return MinimalDocument()

    monkeypatch.setattr(benchmark.importlib, "import_module", import_module)
    result = benchmark.verify_document(captured_case, SimpleNamespace(parse=parse), tmp_path / "candidate")
    assert not result["ok"]
    assert result["differences"][0]["path"] == "$[0].kwargs.dpi"
    assert office.render_metafile is render


def test_memory_records_peak_before_identity_hashing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """内存峰值采集必须先于身份散列，防止验证产物污染 RSS。"""
    events: list[str] = []

    def run() -> object:
        """标记一次真实工作量调用。"""
        events.append("run")
        return object()

    def rss() -> int:
        """标记高水位采样边界。"""
        events.append("rss")
        return 100

    def identity() -> dict[str, Any]:
        """标记可能分配内存的身份散列。"""
        events.append("identity")
        return {}

    monkeypatch.setattr(benchmark, "load_runtime", lambda *_: (None, None))
    monkeypatch.setattr(benchmark, "workload_runner", lambda *_: run)
    monkeypatch.setattr(benchmark, "peak_rss_bytes", rss)
    monkeypatch.setattr(benchmark, "process_identity", identity)
    result = benchmark.worker({}, "before", "memory", "doc", tmp_path)
    assert result["ok"] and events == ["rss", "run", "rss", "identity"]


def test_failed_comparison_returns_nonzero_without_timing(
    captured_case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选输出不一致必须返回非零，且不能继续跑性能后报告成功。"""
    modes: list[str] = []

    def launch(
        config_path: Path, config: dict[str, Any], variant: str, mode: str, workload: str, destination: Path
    ) -> dict[str, Any]:
        """模拟 after 比较失败，同时检查调度是否停止。"""
        modes.append(mode)
        return {"ok": variant == "before", "differences": ["changed output"] if variant == "after" else []}

    monkeypatch.setattr(benchmark, "launch", launch)
    assert benchmark.orchestrate(namespace_for(captured_case)) == 1
    summary = benchmark.read_json(Path(captured_case["output"]) / "summary.json")
    assert summary["ok"] is False and summary["reference_unchanged"] is True
    assert modes == ["verify", "verify"]


def test_successful_orchestration_keeps_all_pairs(
    captured_case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整调度为两个工作量各保留五对计时与三对独立 RSS 样本。"""

    def launch(
        config_path: Path, config: dict[str, Any], variant: str, mode: str, workload: str, destination: Path
    ) -> dict[str, Any]:
        """提供固定测量值，检查汇总与配对调度而不执行真实渲染。"""
        return {"ok": True, "variant": variant, "seconds": 1.0, "peak_rss_bytes": 100}

    monkeypatch.setattr(benchmark, "launch", launch)
    assert benchmark.orchestrate(namespace_for(captured_case)) == 0
    summary = benchmark.read_json(Path(captured_case["output"]) / "summary.json")
    assert summary["comparison_kind"] == "A/A"
    for workload in ("doc", "metafiles"):
        assert len(summary["timing"][workload]["samples"]) == 10
        assert len(summary["memory"][workload]["samples"]) == 6
        assert summary["timing"][workload]["comparison"]["paired_ratios"] == [1.0] * 5
    assert all(summary["sources_unchanged"].values())
