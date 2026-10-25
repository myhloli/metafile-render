"""保护多来源成对验收的计数、原生执行和比例口径。"""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
import benchmark_corpus_pair as pair


def test_paired_ratio_uses_each_pair_instead_of_ratio_of_medians() -> None:
    """不稳定绝对时钟下仍以每对比例统计，不拼接独立中位数。"""
    result = pair.paired_statistics([1, 2, 10], [1.5, 1, 8])
    assert result["paired_ratio_median"] == 0.8
    assert result["reduction_percent"] == pytest.approx(20)
    with pytest.raises(ValueError):
        pair.paired_statistics([0], [1])
    with pytest.raises(ValueError):
        pair.paired_statistics([1], [])


def test_origin_aliases_do_not_duplicate_primary_totals() -> None:
    """Office 内嵌与实际调用别名只占一个主组，控制样本不混入主语料。"""
    row = {
        "origins": [{"group": "office_embedded"}, {"group": "office_runtime"}],
        "format": "png",
        "verification": {"statistics": {"categories": ["text", "wmf"]}},
    }
    groups = pair.case_groups(row)
    assert "primary" in groups and "group:office_runtime" in groups and "group:office_embedded" not in groups
    row["origins"] = [{"group": "upstream_control"}]
    assert "primary" not in pair.case_groups(row)


def test_incomplete_or_changed_timing_workload_cannot_pass() -> None:
    """少跑一个案例或输出漂移必须失败，即使工作进程自行宣称成功。"""
    identity = {"compute_backend": {"backend": "rust", "build_profile": "release", "native_calls": 1}}
    expected = {"one": {"sha256": "old"}, "two": {"sha256": "same"}}
    result = {"identity": identity, "cases": {"one": {"signature": {"sha256": "old"}}}}
    assert pair.validate_measurement(result, expected)
    result["cases"]["two"] = {"signature": {"sha256": "same"}}
    assert not pair.validate_measurement(result, expected)
    result["cases"]["one"]["signature"]["sha256"] = "new"
    assert pair.validate_measurement(result, expected)


@pytest.mark.parametrize(
    "backend",
    [
        {},
        {"backend": "python"},
        {"backend": "rust", "build_profile": "debug"},
        {"backend": "rust", "build_profile": "release", "native_calls": 0},
    ],
)
def test_missing_or_unexecuted_native_kernel_fails(backend: dict) -> None:
    """完整工作量必须实际命中 release 内核，不能只凭扩展存在推断执行。"""
    assert pair.validate_measurement({"identity": {"compute_backend": backend}, "cases": {}}, {})


def test_no_point_record_selection_can_validate_without_inventing_native_calls() -> None:
    """纯 WMF 子集仍要求 release 扩展，但不得捏造 EMF 点列内核调用。"""
    result = {"identity": {"compute_backend": {"backend": "rust", "build_profile": "release", "native_calls": 0}}, "cases": {}}
    assert not pair.validate_measurement(result, {}, require_calls=False)


def _reference_matrix() -> tuple[dict, list[dict]]:
    """提供两输入、两格式的完整参考，防止少测一项仍声称覆盖全部语料。"""
    cases = [{"id": digit * 64, "asset": digit * 64, "kwargs": {"output_format": "svg", "dpi": 200}} for digit in ("0", "1")]
    rows = [
        {
            "case": case["id"],
            "asset": case["asset"],
            "variant": case["id"][:20] + "-" + fmt,
            "format": fmt,
            "kwargs": dict(case["kwargs"], output_format=fmt),
        }
        for case in cases
        for fmt in ("svg", "png")
    ]
    return {"cases": cases}, rows


def test_complete_reference_matrix_is_valid() -> None:
    """格式矩阵完整且参数来自原清单时可以开始测量。"""
    manifest, rows = _reference_matrix()
    pair.validate_reference_matrix(manifest, rows)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "path", "case", "asset", "kwargs"])
def test_invalid_reference_matrix_cannot_shrink_or_redirect_workload(mutation: str) -> None:
    """拒绝漏项、重复、路径逃逸、输入替换与 DPI 漂移，避免错误性能分母。"""
    manifest, rows = _reference_matrix()
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(rows[0].copy())
    elif mutation == "path":
        rows[0]["variant"] = "../escape"
    elif mutation == "case":
        rows[0]["case"] = "2" * 64
    elif mutation == "asset":
        rows[0]["asset"] = "2" * 64
    else:
        rows[0]["kwargs"]["dpi"] = 72
    with pytest.raises(ValueError):
        pair.validate_reference_matrix(manifest, rows)
