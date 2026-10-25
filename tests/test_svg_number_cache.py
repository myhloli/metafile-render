"""验证 SVG 数字复用的精确格式、异常、预算和单次文档生命周期。"""

from collections.abc import Callable
import gc
import weakref

import pytest
from _metafile_test_utils import build_emf, emf_rectangle

from metafile_render import render_metafile
from metafile_render.backends import svg
from metafile_render.models import MetafileResourceLimitError
from metafile_render.primitives import GraphicsPath, Matrix, PathSegment


@pytest.mark.parametrize("value", [0.0, -0.0, 0.03125, 0.09375, -0.03125, 1.23456, 1e12 + 0.5])
def test_cached_coordinates_keep_exact_rounding(value: float) -> None:
    """复用原格式化函数，负零、半值和大坐标不能因缓存改变字符串。"""
    path = GraphicsPath((PathSegment("M", ((value, value),)),))
    formatter = svg._svg_number_formatter()
    expected = svg._svg_path_data(path, Matrix())
    assert svg._svg_path_data(path, Matrix(), formatter) == expected
    assert svg._svg_path_data(path, Matrix(), formatter) == expected


@pytest.mark.parametrize("value,error", [(float("nan"), ValueError), (float("inf"), OverflowError)])
def test_nonfinite_errors_remain_visible(value: float, error: type[Exception]) -> None:
    """失败结果不进入缓存，多次遇到非有限数仍抛出原异常。"""
    formatter = svg._svg_number_formatter()
    for _ in range(2):
        with pytest.raises(error):
            formatter(value)


def test_cached_numbers_do_not_bypass_path_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """坐标命中缓存也必须继续累计每个输出片段的字节预算。"""
    path = GraphicsPath((PathSegment("M", ((0.0, 0.0),)), PathSegment("L", ((0.0, 0.0),))))
    formatter = svg._svg_number_formatter()
    svg._svg_path_data(path, Matrix(), formatter)
    monkeypatch.setattr(svg, "MAX_GENERATED_SVG_BYTES", 11)
    with pytest.raises(MetafileResourceLimitError):
        svg._svg_path_data(path, Matrix(), formatter)


def test_svg_formatter_is_not_retained_between_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    """实际 SVG 输出结束后释放坐标缓存，后续文档创建独立缓存。"""
    original = svg._svg_number_formatter
    created: list[weakref.ReferenceType] = []

    def tracked() -> Callable[[float], str]:
        """只保留弱引用，以观察渲染结束后的对象生命周期。"""
        formatter = original()
        created.append(weakref.ref(formatter))
        return formatter

    monkeypatch.setattr(svg, "_svg_number_formatter", tracked)
    data = build_emf([emf_rectangle(1, 2, 40, 50)] * 3)
    first = render_metafile(data, output_format="svg", dpi=144)
    second = render_metafile(data, output_format="svg", dpi=144)
    gc.collect()
    assert first.data == second.data
    assert len(created) == 2
    assert all(reference() is None for reference in created)

    def uncached() -> Callable[[float], str]:
        """提供原未缓存函数，独立确认真实 SVG 字节没有变化。"""
        return svg._svg_number

    monkeypatch.setattr(svg, "_svg_number_formatter", uncached)
    assert render_metafile(data, output_format="svg", dpi=144).data == first.data


def test_coordinate_cache_remains_bounded() -> None:
    """高唯一坐标输入仍只保留限定条目，而不是按整份文档无限增长。"""
    formatter = svg._svg_number_formatter()
    for i in range(10000):
        formatter(i / 3.0)
    assert formatter.cache_info().currsize <= 4096
