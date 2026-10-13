"""验证复杂图元在固定工作预算内自动缩小，且各后端行为一致。"""

from __future__ import annotations

import base64
from io import BytesIO
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest
from _metafile_test_utils import build_emf, emf_intersect_clip_rect, emf_record, emf_rectangle
from PIL import Image

from metafile_render import MetafileBackend, MetafileOutputFormat, MetafileResourceLimitError, limits, render_metafile
from metafile_render.backends import windows
from metafile_render.parser import parse_metafile
from metafile_render.sizing import _bounded_canvas_size, check_render_size, choose_raster_scale, render_work_units


@pytest.mark.parametrize(
    ("requested", "budget", "work", "expected"),
    [
        ((10, 10), 200, 2, (10, 10)),
        ((10, 10), 199, 2, (9, 9)),
        ((10000, 1), 32, 2, (16, 1)),
        ((1, 10000), 32, 2, (1, 16)),
        ((10, 10), 2, 2, (1, 1)),
        ((3809, 1939), 512_000_000, 125, (2836, 1443)),
    ],
)
def test_bounded_work_dimensions(
    monkeypatch: pytest.MonkeyPatch,
    requested: tuple[int, int],
    budget: int,
    work: int,
    expected: tuple[int, int],
) -> None:
    """覆盖预算等值、刚超限、极端宽高比及真实样本的尺寸计算。"""
    monkeypatch.setattr(limits, "MAX_RENDER_WORK_PIXELS", budget)
    width, height, downscaled = _bounded_canvas_size(requested, work_units=work)
    assert (width, height) == expected
    assert downscaled == (requested != expected)
    check_render_size(width, height, work)


def test_canvas_rounding_stays_within_pixel_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """画布四舍五入超过像素上限时，最终尺寸仍必须收回预算内。"""
    monkeypatch.setattr(limits, "MAX_CANVAS_PIXELS", 99)
    width, height, downscaled = _bounded_canvas_size((100, 100))
    assert downscaled and width * height <= 99
    check_render_size(width, height)


def test_dense_clips_are_included_in_public_sizing(monkeypatch: pytest.MonkeyPatch) -> None:
    """多层裁剪必须逐命令计费，防止尺寸计算漏算 mask 工作量。"""
    monkeypatch.setattr(limits, "MAX_RENDER_WORK_PIXELS", 660_000)
    data = build_emf(
        [emf_intersect_clip_rect(0, 0, 100, 100)] * 32 + [emf_rectangle(5, 5, 45, 45), emf_rectangle(55, 55, 95, 95)]
    )
    document = parse_metafile(data)
    assert render_work_units(document) == 66
    result = render_metafile(data, backend="replay")
    assert (result.width, result.height) == (100, 100)
    assert not result.partial
    with Image.open(BytesIO(result.data)) as image:
        assert image.getbbox() is not None


def _clipped_emf(*, raster_svg: bool = False) -> bytes:
    """构造两个带裁剪的图元，可选 ROP2 触发 SVG 整图栅格包装。"""
    records = [emf_intersect_clip_rect(0, 0, 100, 100)]
    if raster_svg:
        records.append(emf_record(20, (9).to_bytes(4, "little")))
    return build_emf([*records, emf_rectangle(5, 5, 45, 45), emf_rectangle(55, 55, 95, 95)])


@pytest.mark.parametrize("fmt", ["png", "jpeg", "webp", "svg"])
@pytest.mark.parametrize("raster_svg", [False, True])
def test_public_outputs_fit_work_budget(monkeypatch: pytest.MonkeyPatch, fmt: MetafileOutputFormat, raster_svg: bool) -> None:
    """四种输出及两种 SVG 路径均缩至安全尺寸，内嵌 PNG 与返回尺寸一致。"""
    monkeypatch.setattr(limits, "MAX_RENDER_WORK_PIXELS", 40_000)
    data = _clipped_emf(raster_svg=raster_svg)
    document = parse_metafile(data)
    assert render_work_units(document) == 4
    assert choose_raster_scale(document, maximum=4) == 1
    result = render_metafile(data, output_format=fmt, backend="replay")
    assert (result.width, result.height) == (100, 100)
    assert not result.partial
    warnings = [item for item in result.diagnostics if item.code == "canvas_downscaled"]
    assert len(warnings) == 1 and warnings[0].level == "warning"
    assert "200x200 to 100x100" in warnings[0].message
    assert "render-work" in warnings[0].message
    payload = result.data
    if fmt == "svg":
        root = ElementTree.fromstring(payload)
        assert (root.get("width"), root.get("height"), root.get("viewBox")) == ("100", "100", "0 0 100 100")
        metadata = root.find("{http://www.w3.org/2000/svg}metadata")
        assert metadata is not None and metadata.text
        payload = base64.b64decode(metadata.text)
        if raster_svg:
            element = root.find("{http://www.w3.org/2000/svg}image")
            assert element is not None and element.get("width") == "100" and element.get("height") == "100"
    with Image.open(BytesIO(payload)) as image:
        assert image.size == (100, 100)
        if fmt in {"png", "jpeg"}:
            assert image.info["dpi"] == pytest.approx((200, 200), abs=0.02)


@pytest.mark.parametrize("fallback", [False, True])
def test_native_and_replay_use_same_bounded_size(monkeypatch: pytest.MonkeyPatch, fallback: bool) -> None:
    """原生分配接收安全尺寸，失败回放也使用同样尺寸且只报告一次缩放。"""
    monkeypatch.setattr(limits, "MAX_RENDER_WORK_PIXELS", 40_000)
    monkeypatch.setattr(windows, "is_windows", lambda: True)
    native = Mock(side_effect=OSError("unavailable")) if fallback else Mock(return_value=Image.new("RGB", (100, 100)))
    monkeypatch.setattr(windows, "render_native", native)
    data = _clipped_emf()
    result = render_metafile(data)
    native.assert_called_once_with(data, (100, 100), 200)
    assert (result.width, result.height) == (100, 100)
    assert not result.partial
    assert sum(item.code == "canvas_downscaled" for item in result.diagnostics) == 1
    if fallback:
        assert result.data == render_metafile(data, backend="replay").data


@pytest.mark.parametrize("backend", ["auto", "replay"])
def test_impossible_minimum_is_rejected_before_allocation(monkeypatch: pytest.MonkeyPatch, backend: MetafileBackend) -> None:
    """连单像素也超工作预算时仍报错，不能调用原生分配或吞掉硬限制。"""
    monkeypatch.setattr(limits, "MAX_RENDER_WORK_PIXELS", 3)
    monkeypatch.setattr(windows, "is_windows", lambda: True)
    native = Mock(side_effect=AssertionError("must not allocate"))
    monkeypatch.setattr(windows, "render_native", native)
    with pytest.raises(MetafileResourceLimitError, match="max_render_work_pixels"):
        render_metafile(_clipped_emf(), backend=backend)
    native.assert_not_called()
