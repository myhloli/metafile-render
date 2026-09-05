"""验证统一 DPI、原生优先策略和 Windows 实际渲染。"""

from __future__ import annotations

import base64
import sys
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest
from _emfplus_test_utils import fill_rect, plus_file
from _metafile_test_utils import basic_wmf, build_emf, emf_record, emf_rectangle
from PIL import Image

from metafile_render import (
    MetafileMalformedError,
    MetafileOutputFormat,
    MetafileResourceLimitError,
    api,
    limits,
    render_metafile,
)
from metafile_render.backends import windows


@pytest.mark.parametrize("output_format", ["png", "jpeg", "webp", "svg"])
@pytest.mark.parametrize("dpi", [None, 96, 144, 200])
def test_resolution_and_metadata(output_format: MetafileOutputFormat, dpi: int | None) -> None:
    """同一英寸图像的四种输出都遵循 200 默认值和显式分辨率。"""
    result = render_metafile(basic_wmf(), output_format=output_format, dpi=dpi, backend="replay")
    expected = 200 if dpi is None else dpi
    assert (result.width, result.height) == (expected, expected)
    if output_format == "svg":
        root = ElementTree.fromstring(result.data)
        assert root.get("width") == str(expected)
        metadata = root.find("{http://www.w3.org/2000/svg}metadata")
        assert metadata is not None and metadata.text is not None
        with Image.open(BytesIO(base64.b64decode(metadata.text))) as fallback:
            assert fallback.size == (expected * 2, expected * 2)
            assert fallback.info["dpi"] == pytest.approx((192, 192), abs=0.02)
    else:
        with Image.open(BytesIO(result.data)) as image:
            assert image.size == (expected, expected)
            if output_format in {"png", "jpeg"}:
                assert image.info["dpi"] == pytest.approx((expected, expected), abs=0.02)


@pytest.mark.parametrize("output_format", ["png", "jpeg", "webp", "svg"])
def test_omitted_dpi_and_size_hint(output_format: MetafileOutputFormat) -> None:
    """省略 DPI 与 None 等价，像素尺寸提示仍优先于物理换算。"""
    payload = basic_wmf()
    default = render_metafile(payload, output_format=output_format, backend="replay")
    explicit = render_metafile(payload, output_format=output_format, dpi=None, backend="replay")
    assert default.data == explicit.data and default.width == default.height == 200
    hinted = render_metafile(payload, output_format=output_format, size_hint=(123, 77), backend="replay")
    assert (hinted.width, hinted.height) == (123, 77)


def test_native_success_avoids_full_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """合法但回放不支持的绘制记录也可以由原生后端完成。"""
    data = build_emf([emf_record(0xFFFE)])
    native = Mock(return_value=Image.new("RGB", (200, 200), "blue"))
    monkeypatch.setattr(windows, "is_windows", lambda: True)
    monkeypatch.setattr(windows, "render_native", native)
    monkeypatch.setattr(api, "parse_metafile", Mock(side_effect=AssertionError("must not build replay IR")))
    result = render_metafile(data)
    native.assert_called_once_with(data, (200, 200), 200)
    assert not result.partial and not result.diagnostics
    with Image.open(BytesIO(result.data)) as image:
        assert image.convert("RGBA").getpixel((50, 50)) == (0, 0, 255, 255)


@pytest.mark.parametrize("failure", [OSError("unavailable"), ValueError("native rejected input")])
def test_native_failure_reports_info_and_replays(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    """原生失败仅产生信息诊断，成功回放不会被误标为部分输出。"""
    monkeypatch.setattr(windows, "is_windows", lambda: True)
    monkeypatch.setattr(windows, "render_native", Mock(side_effect=failure))
    data = basic_wmf()
    result = render_metafile(data)
    assert result.data == render_metafile(data, backend="replay").data
    assert not result.partial
    assert [(d.code, d.level) for d in result.diagnostics] == [("native_backend_fallback", "info")]


def test_missing_native_capability_uses_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """未包含原生模块的 Windows Pillow 构建仍可转换。"""
    monkeypatch.setattr(windows, "is_windows", lambda: True)
    monkeypatch.setattr(windows, "available", lambda: False)
    result = render_metafile(basic_wmf())
    assert not result.partial and result.diagnostics[0].code == "native_backend_fallback"


@pytest.mark.parametrize("scenario", ["svg", "replay", "only", "standard_wmf", "non_windows"])
def test_routes_that_never_call_native(monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    """矢量输出、显式回放及原生不支持的输入必须绕开原生加载器。"""
    native = Mock(side_effect=AssertionError("must not call native"))
    monkeypatch.setattr(windows, "render_native", native)
    monkeypatch.setattr(windows, "is_windows", lambda: scenario != "non_windows")
    data = plus_file([fill_rect()]) if scenario == "only" else basic_wmf()[22:] if scenario == "standard_wmf" else basic_wmf()
    result = render_metafile(
        data,
        output_format="svg" if scenario == "svg" else "png",
        backend="replay" if scenario == "replay" else "auto",
        size_hint=(100, 100),
    )
    assert result.width == result.height == 100
    native.assert_not_called()


def test_native_preflight_rejects_bad_known_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """原生系统不能绕过已知记录的字段边界校验。"""
    native = Mock(side_effect=AssertionError("must validate before native"))
    monkeypatch.setattr(windows, "is_windows", lambda: True)
    monkeypatch.setattr(windows, "render_native", native)
    with pytest.raises(MetafileMalformedError):
        render_metafile(build_emf([emf_record(43)]))
    native.assert_not_called()


def test_native_work_budget_is_not_a_fallback_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """已知超预算在原生分配前拒绝，不会被后端切换吞掉。"""
    native = Mock(side_effect=AssertionError("must validate work first"))
    monkeypatch.setattr(windows, "is_windows", lambda: True)
    monkeypatch.setattr(windows, "render_native", native)
    monkeypatch.setattr(limits, "MAX_RENDER_WORK_PIXELS", 1)
    with pytest.raises(MetafileResourceLimitError):
        render_metafile(basic_wmf())
    native.assert_not_called()


def test_native_load_receives_bounded_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """超大尺寸提示必须在原生像素分配前收缩。"""

    def render(data: bytes, size: tuple[int, int], dpi: int) -> Image.Image:
        """仅在确认目标已受限后模拟分配像素。"""
        assert size == (8192, 1) and dpi == 200
        return Image.new("RGB", size, "white")

    monkeypatch.setattr(windows, "is_windows", lambda: True)
    monkeypatch.setattr(windows, "render_native", render)
    result = render_metafile(basic_wmf(), size_hint=(20000, 1))
    assert (result.width, result.height) == (8192, 1)
    assert result.diagnostics[0].code == "canvas_downscaled"


def test_backend_validation() -> None:
    """公开接口仅接受显式声明的两种策略。"""
    with pytest.raises(ValueError, match="backend"):
        render_metafile(basic_wmf(), backend="native")  # type: ignore[arg-type]


@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows GDI rendering")
@pytest.mark.parametrize("source", ["wmf", "emf", "dual"])
def test_real_native_matches_direct_pillow(source: str) -> None:
    """三类输入在 Windows 上实际调用原生模块，并与直接 Pillow 加载比较。"""
    assert windows.available()
    if source == "wmf":
        data = basic_wmf()
    elif source == "emf":
        data = build_emf([emf_rectangle(20, 35, 80, 90)], bounds=(10, 20, 110, 120))
    else:
        data = (Path(__file__).parent / "fixtures/gdiplus/geometry-dual.emf").read_bytes()
    result = render_metafile(data, size_hint=(240, 180))
    assert not any(d.code == "native_backend_fallback" for d in result.diagnostics)
    with Image.open(BytesIO(data)) as reference:
        reference._size = (240, 180)
        reference.load()
        with Image.open(BytesIO(result.data)) as output:
            assert output.convert("RGB").tobytes() == reference.convert("RGB").tobytes()
            assert output.convert("RGBA").getchannel("A").getextrema() == (255, 255)
            assert output.convert("RGB").getextrema() != ((255, 255),) * 3


@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows GDI concurrency")
def test_real_native_concurrent_inputs_keep_independent_bounds() -> None:
    """并发转换的共享 handler 不得混用不同输入的边界。"""
    payloads = [
        build_emf([emf_rectangle(x + 5, y + 5, x + 70, y + 80)], bounds=(x, y, x + 100, y + 100))
        for x, y in [(0, 0), (10, 20), (-20, 5), (70, 30)]
    ]

    def convert(index: int) -> bytes:
        """使用不同画布反复转换，验证与串行结果一致。"""
        result = render_metafile(payloads[index % 4], size_hint=(100 + index % 4 * 10, 130))
        assert not any(d.code == "native_backend_fallback" for d in result.diagnostics)
        return result.data

    expected = [convert(index) for index in range(4)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        actual = list(pool.map(convert, range(32)))
    assert actual == expected * 8
