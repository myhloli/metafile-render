"""验证公开输出格式、WebP 编码和 API 参数边界。"""

from io import BytesIO
from unittest.mock import Mock

import pytest
from _metafile_test_utils import basic_wmf, build_emf, emf_stretch_dib
from PIL import Image

from metafile_render import MetafileResourceLimitError, MetafileUnsupportedError, render_metafile
from metafile_render import render as renderer


def test_webp_preserves_alpha_and_uses_lossy_quality_90(monkeypatch: pytest.MonkeyPatch) -> None:
    """从实际渲染图验证 WebP 透明度、尺寸和编码参数，允许有损 RGB 差异。"""
    payload = basic_wmf()
    saved_options: dict[str, object] = {}
    original_save = Image.Image.save

    def save(image: Image.Image, fp: BytesIO, format: str | None = None, **params: object) -> None:
        """记录 WebP 参数并调用真正的 Pillow 编码器。"""
        if format == "WEBP":
            saved_options.update(params)
        original_save(image, fp, format=format, **params)

    monkeypatch.setattr(Image.Image, "save", save)
    result = render_metafile(payload, output_format="webp")
    assert result.output_format == "webp"
    assert result.media_type == "image/webp"
    assert result.data[:4] == b"RIFF" and result.data[8:12] == b"WEBP"
    assert saved_options == {"lossless": False, "quality": 90, "method": 4}
    with Image.open(BytesIO(result.data)) as webp, Image.open(BytesIO(render_metafile(payload).data)) as png:
        assert webp.size == png.size == (result.width, result.height)
        assert webp.convert("RGBA").getchannel("A").tobytes() == png.convert("RGBA").getchannel("A").tobytes()
        assert webp.convert("RGBA").getchannel("A").getextrema() == (0, 255)
        assert webp.convert("RGB").getpixel((72, 72))[1] > 150


def test_webp_missing_encoder_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    """编码器缺失时在栅格化之前抛出稳定的不支持错误。"""
    monkeypatch.setattr(renderer.features, "check", lambda _feature: False)
    rasterize = Mock(side_effect=AssertionError("must not rasterize"))
    monkeypatch.setattr(renderer, "render_pillow", rasterize)
    with pytest.raises(MetafileUnsupportedError, match="WebP encoder is unavailable"):
        render_metafile(basic_wmf(), output_format="webp")
    rasterize.assert_not_called()


def test_svg_budget_stops_before_expensive_image_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """超限 SVG 必须在继续编码昂贵位图节点前失败。"""
    image_element = Mock(side_effect=AssertionError("must not encode image element"))
    monkeypatch.setattr(renderer, "MAX_GENERATED_SVG_BYTES", 1)
    monkeypatch.setattr(renderer, "_svg_image_element", image_element)
    with pytest.raises(MetafileResourceLimitError, match="max_generated_svg_bytes"):
        render_metafile(build_emf([emf_stretch_dib()]), output_format="svg")
    image_element.assert_not_called()


@pytest.mark.parametrize("output_format", ["jpg", "pdf", "", "WEBP"])
def test_api_rejects_unknown_format(output_format: str) -> None:
    """公开 API 只接受规范输出类型，别名由 CLI 扩展名层处理。"""
    with pytest.raises(ValueError, match="unsupported metafile output format"):
        render_metafile(basic_wmf(), output_format=output_format)  # type: ignore[arg-type]


@pytest.mark.parametrize("dpi", [0, 1201, 1.5])
def test_api_rejects_invalid_dpi(dpi: int | float) -> None:
    """无效分辨率在进入回放前被拒绝。"""
    with pytest.raises(ValueError, match="dpi must"):
        render_metafile(basic_wmf(), dpi=dpi)  # type: ignore[arg-type]
