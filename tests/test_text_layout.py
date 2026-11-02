"""回归控制字符、显式字距与 RAQM 记录级故障隔离。"""

from __future__ import annotations

import base64
import struct
from dataclasses import replace
from io import BytesIO
from typing import Any, NoReturn
from xml.etree import ElementTree as ET

import pytest
from PIL import Image, ImageDraw, ImageFont, features

from _emfplus_test_utils import font_object, plus_file, plus_record, string_record
from _metafile_test_utils import (
    build_emf,
    build_placeable_wmf,
    emf_font,
    emf_move_to,
    emf_rectangle,
    emf_select_object,
    emf_set_text_align,
    emf_text,
    wmf_move_to,
    wmf_record,
    wmf_rectangle,
    wmf_set_text_align,
    wmf_textout,
)
from metafile_render import MetafileResourceLimitError, render_metafile
from metafile_render.backends import svg as svg_backend
from metafile_render.commands import DrawTextCommand
from metafile_render.context import ReplayContext
from metafile_render.fonts.resolve import resolve_font
from metafile_render.models import MetafileOutputFormat
from metafile_render.parser import parse_metafile
from metafile_render.primitives import Point
from metafile_render.render import encode_document

_NS = "{http://www.w3.org/2000/svg}"
_SEPARATORS = "\n\r\x1c\x1d\x1e\x85\u2029"


def _scene(text: str, source: str) -> bytes:
    """在待测文字前后放置图形和正常文字，验证局部降级不影响相邻记录。"""
    if source == "wmf":
        return build_placeable_wmf(
            [wmf_rectangle(1, 1, 8, 8), wmf_textout(text, 10, 30), wmf_textout("OK", 10, 70)],
            bbox=(0, 0, 100, 100),
        )
    return build_emf([emf_rectangle(1, 1, 8, 8), emf_text(text, 10, 30, dx=None), emf_text("OK", 10, 70, dx=None)])


def _pixels(data: bytes) -> bytes:
    """完整解码 PNG，比较实际 RGBA 像素而非编码器元数据。"""
    with Image.open(BytesIO(data)) as image:
        return image.convert("RGBA").tobytes()


def _svg(data: bytes) -> tuple[ET.Element, bytes]:
    """验证 SVG 是合法 XML 且内嵌 PNG 完整可解码。"""
    root = ET.fromstring(data)
    metadata = root.find(f"{_NS}metadata")
    assert metadata is not None and metadata.text
    png = base64.b64decode(metadata.text, validate=True)
    _pixels(png)
    return root, png


def _ext_text(
    text: str,
    *,
    source: str = "emf",
    advances: list[Point] | None = None,
    options: int = 0,
) -> bytes:
    """构造带可选二维字距、显式背景和裁剪的单条 GDI 文字记录。"""
    if source == "wmf":
        raw = text.encode("cp1252")
        payload = struct.pack("<hhHH", 40, 50, len(raw), options)
        if options & 6:
            payload += struct.pack("<4h", 10, 10, 90, 60)
        payload += raw + b"\0" * (len(raw) % 2)
        if advances is not None:
            payload += struct.pack(f"<{len(advances)}h", *(int(point[0]) for point in advances))
        return wmf_record(0x0A32, payload)
    record = bytearray(emf_text(text, 50, 40, dx=None))
    struct.pack_into("<I", record, 52, options)
    struct.pack_into("<4i", record, 56, 10, 10, 90, 60)
    if advances is not None:
        struct.pack_into("<I", record, 72, len(record))
        values = [int(value) for point in advances for value in (point if options & 0x2000 else point[:1])]
        record.extend(struct.pack(f"<{len(values)}i", *values))
        struct.pack_into("<I", record, 4, len(record))
    return bytes(record)


def _driver_string(text: str, *, realized: bool) -> bytes:
    """构造逐字显式位置或由字体计算推进距离的 EMF+ Unicode 记录。"""
    positions = [(10.0, 70.0)] if realized else [(10.0 + 20 * index, 70.0) for index in range(len(text))]
    payload = struct.pack("<IIII", 0xFF000000, 5 if realized else 1, 0, len(text)) + text.encode("utf-16le")
    payload += struct.pack(f"<{len(positions) * 2}f", *(value for point in positions for value in point))
    return plus_record(0x4036, payload, 0x8000)


@pytest.mark.parametrize("source,separator", [("wmf", c) for c in _SEPARATORS[:5]] + [("emf", c) for c in _SEPARATORS])
@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("output_format", ["png", "svg"])
def test_control_text_keeps_visible_content(
    source: str, separator: str, mixed: bool, output_format: MetafileOutputFormat
) -> None:
    """纯控制字符和混合文字都只丢弃控制字符，前后图文及两种输出保持一致。"""
    text = f"A{separator}B" if mixed else separator
    result = render_metafile(_scene(text, source), output_format=output_format, size_hint=(100, 100), backend="replay")
    expected = render_metafile(
        _scene("AB" if mixed else "", source), output_format=output_format, size_hint=(100, 100), backend="replay"
    )
    assert result.partial
    warnings = [d for d in result.diagnostics if d.code == "text_control_filtered"]
    assert len(warnings) == 1 and warnings[0].level == "warning"
    assert warnings[0].record_type == (0x0521 if source == "wmf" else 84)
    assert warnings[0].record_index is not None and warnings[0].offset is not None
    if output_format == "svg":
        root, fallback = _svg(result.data)
        assert "".join(node.text or "" for node in root.iter(f"{_NS}text")) == ("ABOK" if mixed else "OK")
        assert _pixels(fallback) == _pixels(_svg(expected.data)[1])
    else:
        assert _pixels(result.data) == _pixels(expected.data)


@pytest.mark.parametrize("character", ["\0", "\x07", "\x0b", "\x0c", "\x1f", "\ufffe", "\uffff"])
def test_xml_invalid_characters_do_not_escape_into_svg(character: str) -> None:
    """未触发 RAQM 的 XML 非法字符也必须在 SVG 序列化前清理。"""
    result = render_metafile(_scene(f"A{character}B", "emf"), output_format="svg", backend="replay")
    root, _fallback = _svg(result.data)
    assert "".join(node.text or "" for node in root.iter(f"{_NS}text")) == "ABOK"
    assert result.partial


@pytest.mark.parametrize("source", ["wmf", "emf"])
@pytest.mark.parametrize("alignment", [2, 6])
def test_explicit_advances_keep_positions_alignment_and_current_point(source: str, alignment: int) -> None:
    """过滤中间字符后保留原始字距总和、二维位置及 CENTER/RIGHT 对齐。"""
    advances = [(10.0, 1.0), (20.0, 2.0), (30.0, 3.0)]
    options = 0x2000 if source == "emf" else 0
    if source == "emf":
        data = build_emf(
            [
                emf_move_to(50, 40),
                emf_set_text_align(alignment | 1),
                _ext_text("A\rB", advances=advances, options=options),
                emf_text("OK", 0, 0, dx=10),
            ]
        )
    else:
        data = build_placeable_wmf(
            [
                wmf_move_to(50, 40),
                wmf_set_text_align(alignment | 1),
                _ext_text("A\rB", source=source, advances=advances),
                wmf_textout("OK", 0, 0),
            ],
            bbox=(0, 0, 100, 100),
        )
    document = parse_metafile(data, dpi=100, size_hint=(100, 100))
    first, second = [c for c in document.commands if isinstance(c, DrawTextCommand)]
    assert first.text == "AB"
    assert first.positions == ((50, 40), (80, 43 if source == "emf" else 40))
    assert first.advance_end == second.origin == (110, 46 if source == "emf" else 40)
    svg, _mime = encode_document(document, "svg")
    root, _fallback = _svg(svg)
    nodes = list(root.iter(f"{_NS}text"))
    offset = 60 if alignment == 2 else 30
    assert [float(node.attrib["x"]) for node in nodes[:2]] == [50 - offset, 80 - offset]


@pytest.mark.parametrize("source", ["wmf", "emf"])
def test_control_only_text_retains_explicit_background_and_clip(source: str) -> None:
    """全部字符被过滤时仍填充 ETO_OPAQUE 背景并保留裁剪。"""
    record = _ext_text("\r", source=source, advances=[(10, 0)], options=6)
    data = build_emf([record]) if source == "emf" else build_placeable_wmf([record], bbox=(0, 0, 100, 100))
    document = parse_metafile(data, dpi=100, size_hint=(100, 100))
    command = document.commands[0]
    assert isinstance(command, DrawTextCommand) and not command.text and command.clip
    result = render_metafile(data, output_format="svg", size_hint=(100, 100), backend="replay")
    root, fallback = _svg(result.data)
    assert not list(root.iter(f"{_NS}text"))
    assert list(root.iter(f"{_NS}rect")) and list(root.iter(f"{_NS}g"))
    with Image.open(BytesIO(fallback)) as image:
        assert image.getpixel((image.width // 2, image.height // 3))[3] == 255
        assert image.getpixel((0, 0))[3] == 0


@pytest.mark.parametrize("source", ["wmf", "emf"])
def test_updatecp_layout_failure_keeps_previous_position(monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    """推进距离计算失败只跳过当前文字，并保留当前位置供后续记录使用。"""
    original = ImageFont.FreeTypeFont.getlength

    def fail_length(self: ImageFont.FreeTypeFont, text: str, *args: Any, **kwargs: Any) -> float:
        """模拟清理后仍发生的 RAQM 布局失败。"""
        if text == "FAIL":
            raise RuntimeError("raqm_layout() failed.")
        return original(self, text, *args, **kwargs)

    monkeypatch.setattr(ImageFont.FreeTypeFont, "getlength", fail_length)
    if source == "emf":
        data = build_emf(
            [
                emf_move_to(10, 30),
                emf_set_text_align(1),
                emf_text("FAIL", 0, 0, dx=None),
                emf_text("OK", 0, 0, dx=None),
                emf_text("NEXT", 0, 0, dx=None),
            ]
        )
    else:
        data = build_placeable_wmf(
            [
                wmf_move_to(10, 30),
                wmf_set_text_align(1),
                wmf_textout("FAIL", 0, 0),
                wmf_textout("OK", 0, 0),
                wmf_textout("NEXT", 0, 0),
            ]
        )
    document = parse_metafile(data, dpi=100)
    first, second = [c for c in document.commands if isinstance(c, DrawTextCommand)]
    assert first.text == "OK" and first.origin == (10, 30)
    assert second.origin[0] > first.origin[0]
    assert document.partial and len([d for d in document.diagnostics if d.code == "text_layout_failed"]) == 1
    result = render_metafile(data, backend="replay")
    assert result.partial


@pytest.mark.parametrize("phase", ["vector", "raster_bbox", "raster"])
@pytest.mark.parametrize("rotation", [0, 35])
@pytest.mark.parametrize("explicit_background", [False, True])
def test_layout_failure_is_atomic_and_shared_by_svg_and_png(
    monkeypatch: pytest.MonkeyPatch, phase: str, rotation: int, explicit_background: bool
) -> None:
    """矢量或栅格阶段失败均丢弃整条文字，保留显式背景和后续文字且诊断一次。"""
    context = ReplayContext()
    document = parse_metafile(
        build_emf(
            [_ext_text("AX", advances=[(10, 0)] * 2, options=6 if explicit_background else 0), emf_text("OK", 10, 80, dx=None)]
        ),
        dpi=100,
        context=context,
    )
    original_bad = document.commands[0]
    assert isinstance(original_bad, DrawTextCommand)
    bad = replace(original_bad, rotation=rotation, font=replace(original_bad.font, underline=True, strikeout=True))
    document = replace(document, commands=(bad, *document.commands[1:]))
    expected = replace(document, commands=(replace(bad, text="", positions=()), *document.commands[1:]))
    expected_svg, _mime = encode_document(expected, "svg")
    expected_png, _mime = encode_document(expected, "png")
    if phase == "vector":
        original_bounds = svg_backend._text_background_bounds

        def fail_bounds(command: DrawTextCommand, *args: Any, **kwargs: Any) -> tuple[float, float, float, float]:
            """只在 SVG 文字背景度量时失败，检查 PNG 是否共享跳过结果。"""
            if command.text == "AX":
                raise RuntimeError("raqm_layout() failed.")
            return original_bounds(command, *args, **kwargs)

        monkeypatch.setattr(svg_backend, "_text_background_bounds", fail_bounds)
    elif phase == "raster_bbox":
        original_bbox = ImageDraw.ImageDraw.textbbox

        def fail_bbox(self: ImageDraw.ImageDraw, xy: Point, text: str, *args: Any, **kwargs: Any) -> Any:
            """让真正的 Pillow 字形或背景度量失败，避免只测试绘制入口。"""
            if text == "X":
                raise RuntimeError("raqm_layout() failed.")
            return original_bbox(self, xy, text, *args, **kwargs)

        monkeypatch.setattr(ImageDraw.ImageDraw, "textbbox", fail_bbox)
    else:
        original_text = ImageDraw.ImageDraw.text

        def fail_text(self: ImageDraw.ImageDraw, xy: Point, text: str, *args: Any, **kwargs: Any) -> None:
            """先成功绘制 A，再让 X 失败以验证整条命令的原子性。"""
            if text == "X":
                raise RuntimeError("raqm_layout() failed.")
            original_text(self, xy, text, *args, **kwargs)

        monkeypatch.setattr(ImageDraw.ImageDraw, "text", fail_text)
        png, _mime = encode_document(document, "png", context=context)
        assert _pixels(png) == _pixels(expected_png)
    svg, _mime = encode_document(document, "svg", context=context)
    root, fallback = _svg(svg)
    assert "".join(node.text or "" for node in root.iter(f"{_NS}text")) == "OK"
    assert _pixels(fallback) == _pixels(_svg(expected_svg)[1])
    warnings = [d for d in context.diagnostics.freeze() if d.code == "text_layout_failed"]
    assert context.partial and len(warnings) == 1
    assert warnings[0].record_type == 84 and warnings[0].offset == 88


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("unrelated failure"),
        MemoryError("raqm_layout() failed."),
        MetafileResourceLimitError("raqm_layout() failed."),
    ],
)
@pytest.mark.parametrize("phase", ["measure", "draw"])
def test_unrelated_and_resource_errors_propagate(monkeypatch: pytest.MonkeyPatch, error: Exception, phase: str) -> None:
    """未知运行时错误、内存错误和资源预算错误不能被文字降级吞掉。"""

    def fail(*_args: Any, **_kwargs: Any) -> NoReturn:
        """向真实度量或绘制入口注入必须传播的异常。"""
        raise error

    if phase == "measure":
        monkeypatch.setattr(ImageFont.FreeTypeFont, "getlength", fail)
        data = build_emf([emf_set_text_align(1), emf_text("A", 10, 30, dx=None)])
    else:
        monkeypatch.setattr(ImageDraw.ImageDraw, "text", fail)
        data = _scene("A", "emf")
    with pytest.raises(type(error), match=str(error).replace("(", r"\(").replace(")", r"\)")):
        render_metafile(data, output_format="svg", backend="replay")


def test_emfplus_lines_and_driver_positions_keep_original_meaning() -> None:
    """DrawString 先分行，DriverString 清理字符时仍保留其余字符的原始位置。"""
    driver = struct.pack("<IIII", 0xFF000000, 1, 0, 3) + "A\rB".encode("utf-16le") + struct.pack("<6f", 10, 70, 20, 70, 40, 70)
    data = plus_file([font_object(), string_record("A\r\nB\rC\nD\x85E\u2029F"), plus_record(0x4036, driver, 0x8000)])
    document = parse_metafile(data, dpi=100)
    commands = [c for c in document.commands if isinstance(c, DrawTextCommand)]
    assert [c.text for c in commands] == ["A", "B", "C", "DEF", "AB"]
    assert commands[0].origin[1] < commands[1].origin[1] < commands[2].origin[1] < commands[3].origin[1]
    assert commands[-1].positions == ((10, 70), (40, 70))
    warnings = [d for d in document.diagnostics if d.code == "text_control_filtered"]
    assert len(warnings) == 2
    _svg(render_metafile(data, output_format="svg", backend="replay").data)


@pytest.mark.parametrize("source", ["wmf", "emf"])
def test_updatecp_cleaning_matches_visible_text_advance(source: str) -> None:
    """无显式字距时使用清理后的字符串更新当前位置，避免残留控制字符进入 getlength。"""
    origins = []
    for text in ("A\rB", "AB"):
        if source == "emf":
            data = build_emf([emf_set_text_align(1), emf_text(text, 0, 0, dx=None), emf_text("OK", 0, 0, dx=None)])
        else:
            data = build_placeable_wmf([wmf_set_text_align(1), wmf_textout(text, 0, 0), wmf_textout("OK", 0, 0)])
        document = parse_metafile(data, dpi=100)
        commands = [c for c in document.commands if isinstance(c, DrawTextCommand)]
        origins.append(commands[-1].origin)
    assert origins[0] == origins[1] and origins[0][0] > 0


@pytest.mark.parametrize("source", ["wmf", "emf"])
def test_control_only_explicit_advance_updates_current_point(source: str) -> None:
    """纯控制字符没有字形命令，但其显式字距仍推进后续记录的位置。"""
    record = _ext_text("\r", source=source, advances=[(30, 0)])
    if source == "emf":
        data = build_emf([emf_set_text_align(1), record, emf_text("OK", 0, 0, dx=None)])
    else:
        data = build_placeable_wmf([wmf_set_text_align(1), record, wmf_textout("OK", 0, 0)])
    commands = parse_metafile(data, dpi=100).commands
    assert len(commands) == 1 and isinstance(commands[0], DrawTextCommand)
    assert commands[0].origin == (30, 0)


def test_emfplus_realized_advance_cleans_before_measurement() -> None:
    """DriverString 的字体推进模式先清理字符，再计算其余文字位置。"""
    data = plus_file([font_object(), _driver_string("A\rB", realized=True)])
    expected = plus_file([font_object(), _driver_string("AB", realized=True)])
    actual_command = parse_metafile(data, dpi=100).commands[0]
    expected_command = parse_metafile(expected, dpi=100).commands[0]
    assert actual_command == expected_command
    _svg(render_metafile(data, output_format="svg", backend="replay").data)


@pytest.mark.parametrize("source", ["draw_string", "driver_string"])
def test_emfplus_measurement_failure_keeps_following_text(monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    """EMF+ 行宽或逐字推进失败不终止后续记录，同源多次失败只报告一次。"""
    original = ImageFont.FreeTypeFont.getlength

    def fail_length(self: ImageFont.FreeTypeFont, text: str, *args: Any, **kwargs: Any) -> float:
        """让指定测试字符的实际字体度量失败。"""
        if "X" in text:
            raise RuntimeError("raqm_layout() failed.")
        return original(self, text, *args, **kwargs)

    monkeypatch.setattr(ImageFont.FreeTypeFont, "getlength", fail_length)
    failed = string_record("X\nX") if source == "draw_string" else _driver_string("AX", realized=True)
    data = plus_file([font_object(), failed, string_record("OK")])
    document = parse_metafile(data, dpi=100)
    assert [c.text for c in document.commands if isinstance(c, DrawTextCommand)] == ["OK"]
    warnings = [d for d in document.diagnostics if d.code == "text_layout_failed"]
    assert len(warnings) == 1 and warnings[0].record_type == (0x401C if source == "draw_string" else 0x4036)
    assert warnings[0].offset is not None
    _svg(render_metafile(data, output_format="svg", backend="replay").data)


@pytest.mark.parametrize("source", ["wmf", "emf", "emfplus", "emfplus_driver"])
def test_filtered_characters_still_consume_input_budget(monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    """控制字符即使没有生成可见字形，也不能绕过原始输入点数预算。"""
    from metafile_render import limits

    monkeypatch.setattr(limits, "MAX_TOTAL_POINTS", 4)
    if source == "wmf":
        data = build_placeable_wmf([wmf_textout("\r" * 5, 0, 0)])
    elif source == "emf":
        data = build_emf([emf_text("\r" * 5, 0, 0, dx=None)])
    elif source == "emfplus":
        data = plus_file([font_object(), string_record("\r" * 5)])
    else:
        data = plus_file([font_object(), _driver_string("\r" * 5, realized=True)])
    with pytest.raises(MetafileResourceLimitError):
        render_metafile(data, backend="replay")


def test_normal_unicode_and_format_controls_are_preserved() -> None:
    """中英文、阿拉伯文、TAB、组合字符及双向/连字控制符不被广泛过滤。"""
    text = "中文 العربية e\u0301 \t\u200c\u200d\u200e\u200f\u2028\u202aAB\u202c"
    data = _scene(text, "emf")
    result = render_metafile(data, output_format="svg", backend="replay")
    root, _fallback = _svg(result.data)
    assert not result.partial
    assert next(root.iter(f"{_NS}text")).text == text


def test_ansi_ellipsis_is_not_mistaken_for_unicode_nel() -> None:
    """CP1252 的 0x85 是可见省略号，必须在解码后按 Unicode 判断控制字符。"""
    result = render_metafile(_scene("A…B", "wmf"), output_format="svg", backend="replay")
    root, _fallback = _svg(result.data)
    assert next(root.iter(f"{_NS}text")).text == "A…B" and not result.partial


def test_glyph_indices_are_not_filtered_as_unicode_controls() -> None:
    """glyph-index 数据先按既有策略转成占位字形，不能误当控制字符删除。"""
    data = build_emf([_ext_text("\r", options=0x10)])
    document = parse_metafile(data, dpi=100)
    assert isinstance(document.commands[0], DrawTextCommand) and document.commands[0].text == "□"
    assert not any(d.code == "text_control_filtered" for d in document.diagnostics)


def test_failure_does_not_skip_identical_text_in_another_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """两条文字相同但来源不同的命令只跳过实际失败的一条。"""
    original = svg_backend._text_background_bounds

    def fail_first(command: DrawTextCommand, *args: Any, **kwargs: Any) -> tuple[float, float, float, float]:
        """模拟只有第一条记录的背景度量失败。"""
        if command.origin == (10, 30):
            raise RuntimeError("raqm_layout() failed.")
        return original(command, *args, **kwargs)

    monkeypatch.setattr(svg_backend, "_text_background_bounds", fail_first)
    data = build_emf([emf_text("SAME", 10, 30, dx=None), emf_text("SAME", 10, 70, dx=None)])
    result = render_metafile(data, output_format="svg", size_hint=(100, 100), backend="replay")
    root, _fallback = _svg(result.data)
    nodes = list(root.iter(f"{_NS}text"))
    assert len(nodes) == 1 and nodes[0].text == "SAME" and nodes[0].attrib["y"] == "70"


@pytest.mark.skipif(not features.check("raqm"), reason="RAQM is unavailable outside the dedicated required CI job")
def test_real_raqm_cjk_control_text() -> None:
    """使用真实 RAQM 和 CJK 字体请求验证混合控制字符，专用 CI 强制检查 RAQM 可用。"""
    resolved = resolve_font("Noto Sans CJK SC", 12, 400, False, 134)
    if "Noto" not in resolved.family:
        pytest.skip("Noto CJK is unavailable outside the dedicated required CI job")
    assert resolved.font.layout_engine == ImageFont.Layout.RAQM
    font_record = bytearray(emf_font(1, "Noto Sans CJK SC"))
    font_record[35] = 134
    data = build_emf([bytes(font_record), emf_select_object(1), emf_text("中" + _SEPARATORS + "文", 10, 40, dx=None)])
    result = render_metafile(data, output_format="svg", backend="replay")
    root, fallback = _svg(result.data)
    assert next(root.iter(f"{_NS}text")).text == "中文" and result.partial
    pixels = _pixels(fallback)
    assert any(pixels[index] < 220 and pixels[index + 3] > 0 for index in range(0, len(pixels), 4))
