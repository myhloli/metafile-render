"""验证预算、缓存、字体选择和跨阶段诊断的行为边界。"""

from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from unittest.mock import Mock

import pytest
from _metafile_test_utils import build_emf, emf_font, emf_select_object, emf_text
from PIL import Image, ImageFont

from metafile_render import MetafileResourceLimitError, render_metafile
from metafile_render.backends.raster import render_pillow
from metafile_render.backends.session import ImageCache, RenderSession
from metafile_render.backends.svg import _svg_clip_definitions, _svg_fallback_scale
from metafile_render.commands import DrawImageCommand, DrawPathCommand, EncodedImage, MetafileDocument
from metafile_render.context import DiagnosticSink, ReplayContext
from metafile_render.fonts import resolve
from metafile_render.geometry import rectangle_path
from metafile_render.primitives import Brush, ClipOperation, Color, Pen, Rect
from metafile_render.sizing import check_render_size, render_work_units


def test_svg_fallback_scale_accounts_for_clips() -> None:
    """1× 可渲染的裁剪密集文档不能误选会超预算的 2× fallback。"""
    path = rectangle_path(Rect(0, 0, 100, 100))
    clip = (ClipOperation(path, "and"),) * 2
    command = DrawPathCommand(path, Pen(), Brush(), True, True, "evenodd", clip, 13)
    document = MetafileDocument("emf", "none", Rect(0, 0, 100, 100), 800, 800, (command,) * 100, (), False)
    check_render_size(800, 800, render_work_units(document))
    assert _svg_fallback_scale(document) == 1
    with pytest.raises(MetafileResourceLimitError):
        check_render_size(1600, 1600, render_work_units(document))


def test_cache_is_bounded_and_returns_independent_copies() -> None:
    """缓存按字节淘汰，调用者修改返回图片不会影响后续命中。"""
    cache = ImageCache(limit=32)
    red = Image.new("RGBA", (2, 2), "red")
    cache.put("red", red)
    copy = cache.get("red")
    assert copy is not None
    copy.putalpha(0)
    again = cache.get("red")
    assert again is not None and again.getpixel((0, 0)) == (255, 0, 0, 255)
    cache.put("blue", Image.new("RGBA", (2, 2), "blue"))
    cache.put("green", Image.new("RGBA", (2, 2), "green"))
    assert cache.used == 32 and cache.get("red") is None
    cache.put("oversized", Image.new("RGBA", (3, 3)))
    assert cache.used == 32 and cache.get("oversized") is None
    cache.clear()
    assert cache.used == 0 and not cache.entries


def test_cached_images_keep_per_command_alpha_and_clip() -> None:
    """复用同一图片时，各次 alpha 和裁剪仍独立，并与关闭缓存的输出一致。"""
    buffer = BytesIO()
    Image.new("RGBA", (2, 2), (255, 0, 0, 128)).save(buffer, format="PNG")
    payload = EncodedImage(buffer.getvalue())
    clip = (ClipOperation(rectangle_path(Rect(0, 0, 30, 20)), "and"),)
    first = DrawImageCommand(payload, ((0, 0), (10, 0), (10, 10), (0, 10)), None, 0, constant_alpha=128, clip=clip)
    second = replace(first, constant_alpha=255, destination=((20, 0), (30, 0), (30, 10), (20, 10)))
    document = MetafileDocument("emf", "only", Rect(0, 0, 40, 20), 40, 20, (first, second), (), False)
    with RenderSession() as session, RenderSession(cache_limit=0) as uncached:
        cached = render_pillow(document, session)
        plain = render_pillow(document, uncached)
        assert cached.tobytes() == plain.tobytes()
        assert cached.getpixel((5, 5))[3] == 64
        assert cached.getpixel((25, 5))[3] == 128
        assert session.cache.hits >= 2
    assert session.cache.used == 0


def test_diagnostics_freeze_is_idempotent() -> None:
    """解析和编码多次读取诊断快照不会重复追加省略摘要。"""
    sink = DiagnosticSink()
    for _ in range(300):
        sink.add("unsupported", "warning", "skipped")
    first = sink.freeze()
    assert first == sink.freeze()
    assert len(first) == 257 and first[-1].code == "diagnostics_truncated"


def test_font_style_precedes_regular_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """同时存在常规和粗斜体字体时，必须实际选择粗斜体文件。"""
    resolve.resolve_font.cache_clear()
    styled = Mock(spec=ImageFont.FreeTypeFont)
    styled.getname.return_value = ("Arial", "Bold Italic")

    def load(candidate: str, size: int) -> ImageFont.FreeTypeFont:
        """模拟安装的两个字体文件，以检查最终选择而非候选列表实现。"""
        if candidate == "bold-italic.ttf":
            return styled
        if candidate == "regular.ttf":
            raise AssertionError("regular font must not win")
        raise OSError("not installed")

    monkeypatch.setattr(resolve, "_font_file_index", lambda: {"arial": "regular.ttf", "arialbolditalic": "bold-italic.ttf"})
    monkeypatch.setattr(resolve.ImageFont, "truetype", load)
    resolved = resolve.resolve_font("Arial", 17, 700, True, 1)
    assert resolved.candidate == "bold-italic.ttf" and resolved.reason is None
    resolve.resolve_font.cache_clear()


def test_renderer_font_diagnostic_keeps_source_location() -> None:
    """后端阶段的字体替代信息保留原始文字记录位置，不误标 partial。"""
    data = build_emf([emf_font(1, "MissingFont-030-Unique"), emf_select_object(1), emf_text("AB", 20, 50)])
    result = render_metafile(data, backend="replay")
    assert not result.partial
    diagnostics = [d for d in result.diagnostics if d.code == "font_substituted"]
    assert len(diagnostics) == 1 and diagnostics[0].level == "info"
    assert diagnostics[0].record_type == 84 and diagnostics[0].offset is not None


def test_svg_definitions_are_in_size_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """裁剪定义单独超过预算时即可拒绝，不等待生成所有绘图元素。"""
    from metafile_render.backends import svg
    from metafile_render.primitives import Matrix

    path = rectangle_path(Rect(0, 0, 100, 100))
    command = DrawPathCommand(
        path, Pen(), Brush(color=Color(0, 0, 0)), False, True, "evenodd", (ClipOperation(path, "and"),), 13
    )
    document = MetafileDocument("emf", "none", Rect(0, 0, 100, 100), 100, 100, (command,), (), False)
    monkeypatch.setattr(svg, "MAX_GENERATED_SVG_BYTES", 10)
    with pytest.raises(MetafileResourceLimitError):
        _svg_clip_definitions(document, Matrix())


def test_context_input_budget_is_independent_of_render_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """重复图元仍按输入复杂度计费，不能因后端能够缓存而绕过限制。"""
    from metafile_render import limits

    path = rectangle_path(Rect(0, 0, 10, 10))
    command = DrawPathCommand(path, Pen(), Brush(), False, True, "evenodd", (), 13)
    context = ReplayContext()
    monkeypatch.setattr(limits, "MAX_COMMANDS", 1)
    context.append_command(command)
    with pytest.raises(MetafileResourceLimitError):
        context.append_command(command)


def test_composed_transform_budget_precedes_native(monkeypatch: pytest.MonkeyPatch) -> None:
    """连续合法矩阵组合成极端坐标时，两种后端均在绘制前拒绝。"""
    import struct

    from _metafile_test_utils import emf_record, emf_rectangle, emf_set_world_transform

    from metafile_render.backends import windows

    matrix = (1e10, 0.0, 0.0, 1e10, 0.0, 0.0)
    data = build_emf(
        [emf_set_world_transform(matrix), emf_record(36, struct.pack("<6fI", *matrix, 2)), emf_rectangle(0, 0, 10, 10)]
    )
    native = Mock(side_effect=AssertionError("must check composed coordinates first"))
    monkeypatch.setattr(windows, "is_windows", lambda: True)
    monkeypatch.setattr(windows, "render_native", native)
    for backend in ("auto", "replay"):
        with pytest.raises(MetafileResourceLimitError, match="transform"):
            render_metafile(data, backend=backend)
    native.assert_not_called()


def test_rotated_text_patch_has_pixel_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """局部文字旋转画布也必须在分配前检查固定像素预算。"""
    from metafile_render import limits
    from metafile_render.backends.text import _draw_rotated_text

    monkeypatch.setattr(limits, "MAX_CANVAS_PIXELS", 1)
    with pytest.raises(MetafileResourceLimitError):
        _draw_rotated_text(
            Image.new("RGBA", (20, 20)),
            (10, 10),
            "bounded text",
            font=ImageFont.load_default(size=20),
            fill=(0, 0, 0, 255),
            anchor="ls",
            rotation=45,
            underline=False,
            strikeout=False,
        )
