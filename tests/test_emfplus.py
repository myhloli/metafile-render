"""验证 EMF+ Only 的实际回放、近似行为和安全边界。"""

from __future__ import annotations

import struct
from io import BytesIO
from pathlib import Path

import pytest
from _emfplus_test_utils import (
    VERSION,
    fill_rect,
    font_object,
    plus_comment,
    plus_file,
    plus_header,
    plus_object,
    plus_record,
    solid,
    string_record,
)
from _metafile_test_utils import build_emf, emf_create_brush, emf_rectangle, emf_select_object
from PIL import Image

from metafile_render import (
    MetafileError,
    MetafileMalformedError,
    MetafileResourceLimitError,
    MetafileUnsupportedError,
    limits,
    render_metafile,
)
from metafile_render.binary import BoundedReader
from metafile_render.commands import DrawTextCommand
from metafile_render.emfplus import playback
from metafile_render.emfplus.binary import Cursor
from metafile_render.parser import parse_metafile


def rgba(payload: bytes) -> Image.Image:
    """解码输出并脱离临时字节流生命周期。"""
    with Image.open(BytesIO(payload)) as image:
        return image.convert("RGBA")


@pytest.mark.parametrize("output_format", ["png", "jpeg", "webp", "svg"])
def test_only_renders_all_formats(output_format: str) -> None:
    """Only 内容真正进入四种后端，基本图形无需标记部分输出。"""
    result = render_metafile(plus_file([fill_rect()]), output_format=output_format, dpi=100, backend="replay")
    assert result.source_format == "emf" and result.emfplus_mode == "only"
    assert (result.width, result.height) == (100, 100)
    assert not result.partial and not result.diagnostics
    if output_format == "svg":
        assert b"<path " in result.data and b'data-metafile-render="wmf-emf"' in result.data
    else:
        image = rgba(result.data)
        assert image.getpixel((20, 20))[0] > 240
        assert image.getpixel((20, 20))[1] < 15


def test_clear_replaces_existing_pixels_and_alpha() -> None:
    """透明清屏必须清除之前的图形，不能只叠加透明矩形。"""
    data = plus_file([fill_rect(), plus_record(0x4009, struct.pack("<I", 0)), fill_rect(0xFF0000FF, (60, 60, 20, 20))])
    image = rgba(render_metafile(data, dpi=100, backend="replay").data)
    assert image.getpixel((20, 20))[3] == 0
    assert image.getpixel((70, 70)) == (0, 0, 255, 255)
    assert b"<image " in render_metafile(data, output_format="svg", dpi=144, backend="replay").data


def test_object_fragments_reassemble_before_use() -> None:
    """跨注释对象只有在完整重组后才可使用。"""
    body = solid(0xFF00FF00)
    first = plus_record(0x4008, struct.pack("<I", len(body)) + body[:8], 0x8100)
    last = plus_record(0x4008, body[8:], 0x0100)
    data = build_emf(
        [plus_comment([plus_header(), first]), plus_comment([last, fill_rect(0, inline=False), plus_record(0x4002)])]
    )
    assert rgba(render_metafile(data, dpi=100, backend="replay").data).getpixel((20, 20)) == (0, 255, 0, 255)


def test_unsupported_object_replaces_old_slot() -> None:
    """不支持的新对象覆盖同一槽位后，不得继续使用旧画刷。"""
    data = plus_file(
        [
            plus_object(0, 1, solid(0xFFFF0000)),
            fill_rect(0, inline=False),
            plus_object(0, 9, struct.pack("<I", VERSION)),
            fill_rect(0, (60, 60, 20, 20), inline=False),
        ]
    )
    result = render_metafile(data, dpi=100, backend="replay")
    assert result.partial
    image = rgba(result.data)
    assert image.getpixel((20, 20))[0] == 255
    assert image.getpixel((70, 70))[3] == 0


def test_unsupported_state_stops_drawing_but_checks_record_boundaries() -> None:
    """未知状态后的绘制被停止，后面的非法长度仍需报错。"""
    prefix = [fill_rect(), plus_record(0x4039), fill_rect(0xFF0000FF, (60, 60, 20, 20))]
    result = render_metafile(plus_file(prefix), dpi=100, backend="replay")
    assert result.partial and result.diagnostics[0].code == "emfplus_state_unsupported"
    assert rgba(result.data).getpixel((70, 70))[3] == 0
    with pytest.raises(MetafileMalformedError):
        render_metafile(plus_file([*prefix, struct.pack("<HHII", 0x400A, 0, 1000, 988)]), dpi=144, backend="replay")


def test_getdc_window_and_dual_do_not_double_draw() -> None:
    """Only 仅在 GetDC 区间绘制 EMF；Dual 只采用普通 EMF 分支。"""
    gdi = [emf_create_brush(1, 0x0000FF00), emf_select_object(1), emf_rectangle(60, 60, 80, 80)]
    data = build_emf(
        [
            plus_comment([plus_header(), fill_rect(), plus_record(0x4004)]),
            *gdi,
            plus_comment([plus_record(0x4003)]),
            emf_rectangle(10, 60, 30, 80),
            plus_comment([plus_record(0x4002)]),
        ]
    )
    image = rgba(render_metafile(data, dpi=100, backend="replay").data)
    assert image.getpixel((70, 70))[:3] == (0, 255, 0)
    assert image.getpixel((20, 70))[3] == 0
    dual = build_emf([plus_comment([plus_header(dual=True), fill_rect(), plus_record(0x4002)]), *gdi])
    image = rgba(render_metafile(dual, dpi=100, backend="replay").data)
    assert image.getpixel((20, 20))[3] == 0
    assert image.getpixel((70, 70))[:3] == (0, 255, 0)


def test_transform_order_and_state_restore() -> None:
    """验证 prepend/append 矩阵顺序及 Save/Restore 不泄漏变换。"""
    records = [
        plus_record(0x4025, struct.pack("<I", 7)),
        plus_record(0x402D, struct.pack("<2f", 10, 0)),
        plus_record(0x402E, struct.pack("<2f", 2, 2)),
        fill_rect(rect=(0, 0, 10, 10)),
        plus_record(0x4026, struct.pack("<I", 7)),
        fill_rect(0xFF0000FF, (60, 60, 10, 10)),
    ]
    image = rgba(render_metafile(plus_file(records), dpi=100, backend="replay").data)
    assert image.getpixel((15, 10))[:3] == (255, 0, 0)
    assert image.getpixel((65, 65))[:3] == (0, 0, 255)
    records[2] = plus_record(0x402E, struct.pack("<2f", 2, 2), 0x2000)
    image = rgba(render_metafile(plus_file(records), dpi=100, backend="replay").data)
    assert image.getpixel((15, 10))[3] == 0
    assert image.getpixel((25, 10))[:3] == (255, 0, 0)


@pytest.mark.parametrize("unit,extent", [(2, 100), (3, 72), (4, 1), (5, 300), (6, 25.4)])
def test_page_units_preserve_physical_extent(unit: int, extent: float) -> None:
    """各物理单位的一英寸在 100 DPI 下均映射为一百像素。"""
    doc = parse_metafile(
        plus_file([plus_record(0x4030, struct.pack("<f", 1), unit), fill_rect(rect=(0, 0, extent, extent))]), dpi=144
    )
    points = [p for segment in doc.commands[0].path.segments for p in segment.points]
    assert max(p[0] for p in points) == pytest.approx(100)


def test_clip_and_offset_preserve_local_region() -> None:
    """裁剪偏移后只显示指定区域，重置后恢复完整画布。"""
    records = [
        plus_record(0x4032, struct.pack("<4f", 10, 10, 20, 20)),
        plus_record(0x4035, struct.pack("<2f", 30, 0)),
        fill_rect(rect=(0, 0, 100, 100)),
        plus_record(0x4031),
        fill_rect(0xFF0000FF, (70, 70, 10, 10)),
    ]
    image = rgba(render_metafile(plus_file(records), dpi=100, backend="replay").data)
    assert image.getpixel((20, 20))[3] == 0
    assert image.getpixel((50, 20))[:3] == (255, 0, 0)
    assert image.getpixel((75, 75))[:3] == (0, 0, 255)


def test_container_mapping_and_restore() -> None:
    """容器源到目标矩形映射后恢复上层坐标。"""
    records = [
        plus_record(0x4027, struct.pack("<8fI", 40, 40, 40, 40, 0, 0, 20, 20, 1), 2),
        fill_rect(rect=(0, 0, 10, 10)),
        plus_record(0x4029, struct.pack("<I", 1)),
        fill_rect(0xFF0000FF, (10, 10, 10, 10)),
    ]
    image = rgba(render_metafile(plus_file(records), dpi=100, backend="replay").data)
    assert image.getpixel((50, 50))[:3] == (255, 0, 0)
    assert image.getpixel((15, 15))[:3] == (0, 0, 255)


@pytest.mark.parametrize(
    "flags,points",
    [
        (0, struct.pack("<6f", 10, 10, 30, 10, 10, 30)),
        (0x4000, struct.pack("<6h", 10, 10, 30, 10, 10, 30)),
        (0x0800, bytes([10, 10, 20, 0, 108, 20])),
    ],
)
def test_coordinate_encodings(flags: int, points: bytes) -> None:
    """三种坐标编码应得到相同的多边形位置。"""
    record = plus_record(0x400C, struct.pack("<II", 0xFFFF0000, 3) + points, flags | 0x8000)
    image = rgba(render_metafile(plus_file([record]), dpi=100, backend="replay").data)
    assert image.getpixel((15, 15))[:3] == (255, 0, 0)
    assert image.getpixel((40, 40))[3] == 0


def test_relative_fifteen_bit_and_rle_path() -> None:
    """验证十五位正负相对坐标以及 RLE 路径类型。"""
    cursor = Cursor(BoundedReader(bytes([0x81, 0x2C, 0xFE, 0xD4])))
    assert cursor.relative() == 300 and cursor.relative() == -300
    data = struct.pack("<III", VERSION, 3, 0x0800) + bytes([10, 10, 20, 0, 108, 20, 0x41, 0, 0x41, 1, 0x41, 0x81])
    image = rgba(
        render_metafile(
            plus_file([plus_object(0, 3, data), plus_record(0x4014, struct.pack("<I", 0xFF00FF00), 0x8000)]),
            dpi=100,
            backend="replay",
        ).data
    )
    assert image.getpixel((15, 15))[:3] == (0, 255, 0)


def test_unicode_lines_and_driver_positions() -> None:
    """普通 Unicode 换行和逐字基线位置保留到图元，glyph 模式明确跳过。"""
    driver = struct.pack("<IIII", 0xFF000000, 1, 0, 2) + "AB".encode("utf-16le") + struct.pack("<4f", 10, 70, 30, 70)
    data = plus_file([font_object(), string_record("Hello\n世界"), plus_record(0x4036, driver, 0x8000)])
    commands = [c for c in parse_metafile(data, dpi=144).commands if isinstance(c, DrawTextCommand)]
    assert [c.text for c in commands] == ["Hello", "世界", "AB"]
    assert commands[1].origin[1] > commands[0].origin[1]
    assert commands[2].positions == ((10, 70), (30, 70))
    glyph = bytearray(driver)
    struct.pack_into("<I", glyph, 4, 0)
    result = render_metafile(
        plus_file([fill_rect(), font_object(), plus_record(0x4036, bytes(glyph), 0x8000)]), dpi=144, backend="replay"
    )
    assert result.partial and any(d.code == "emfplus_text_skipped" for d in result.diagnostics)


@pytest.mark.parametrize(
    "pixel_format,raw",
    [
        (0x21808, bytes([0, 0, 255, 0])),
        (0x22009, bytes([0, 0, 255, 0])),
        (0x26200A, bytes([0, 0, 255, 128])),
        (0xE200B, bytes([0, 0, 128, 128])),
    ],
)
def test_raw_bitmaps_preserve_color_and_alpha(pixel_format: int, raw: bytes) -> None:
    """原始 RGB、ARGB 和预乘 PARGB 都规范化为正确的透明图片。"""
    obj = struct.pack("<IIiiiII", VERSION, 1, 1, 1, 4, pixel_format, 0) + raw
    draw = plus_record(0x401A, struct.pack("<II8f", 0xFFFFFFFF, 2, 0, 0, 1, 1, 10, 10, 40, 40), 0)
    result = render_metafile(plus_file([plus_object(0, 5, obj), draw]), dpi=100, backend="replay")
    pixel = rgba(result.data).getpixel((30, 30))
    assert pixel[0] >= 250 and pixel[1:3] == (0, 0)
    assert pixel[3] == (128 if pixel_format & 0x40000 else 255)


@pytest.mark.parametrize(
    "records",
    [
        [],
        [plus_object(64, 1, solid(0xFFFF0000))],
        [fill_rect(5, inline=False)],
        [plus_record(0x4026, struct.pack("<I", 99))],
        [plus_record(0x402A, struct.pack("<6f", float("nan"), 0, 0, 1, 0, 0))],
        [plus_record(0x4008, struct.pack("<I", 40) + b"abcd", 0x8100)],
    ],
)
def test_invalid_or_empty_only_is_rejected(records: list[bytes]) -> None:
    """空内容、非法对象和不可恢复状态不得被当成成功图片。"""
    with pytest.raises((MetafileMalformedError, MetafileUnsupportedError)):
        render_metafile(plus_file(records), dpi=144, backend="replay")


def test_nested_record_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    """内部记录、状态和对象预算不能被单一外层 comment 绕过。"""
    monkeypatch.setattr(playback, "MAX_RECORDS", 2)
    with pytest.raises(MetafileResourceLimitError):
        render_metafile(plus_file([fill_rect()]), dpi=144, backend="replay")
    monkeypatch.setattr(playback, "MAX_RECORDS", 100)
    monkeypatch.setattr(playback, "MAX_STATE_DEPTH", 1)
    with pytest.raises(MetafileResourceLimitError):
        render_metafile(
            plus_file([plus_record(0x4025, struct.pack("<I", 0)), plus_record(0x4025, struct.pack("<I", 1))]),
            dpi=144,
            backend="replay",
        )
    monkeypatch.setattr(limits, "MAX_METAFILE_BYTES", 8)
    with pytest.raises(MetafileResourceLimitError):
        render_metafile(plus_file([plus_object(0, 1, solid(0xFF000000))]), dpi=144, backend="replay")


def test_truncated_only_never_leaks_decoder_exceptions() -> None:
    """逐个截断位置只抛稳定公开异常。"""
    data = plus_file([plus_object(0, 1, solid(0xFFFF0000)), fill_rect(0, inline=False)])
    for length in range(len(data)):
        with pytest.raises(MetafileError):
            render_metafile(data[:length], dpi=144, backend="replay")


@pytest.mark.parametrize("scene", ["geometry", "state", "images", "text", "fallback", "mixed"])
def test_native_gdiplus_only_fixtures(scene: str) -> None:
    """原生 GDI+ 样本必须产生实际内容，降级样本必须附带诊断。"""
    root = Path(__file__).parent / "fixtures/gdiplus"
    result = render_metafile((root / f"{scene}-only.emf").read_bytes(), dpi=96, backend="replay")
    image = rgba(result.data)
    assert result.emfplus_mode == "only"
    assert image.width >= 250 and image.height >= 250
    assert image.convert("RGB").getextrema() != ((255, 255), (255, 255), (255, 255))
    if scene == "fallback":
        assert result.partial
        assert {d.code for d in result.diagnostics} >= {"emfplus_brush_approximation", "emfplus_unsupported_brush"}
    elif scene != "text":
        assert not result.partial
    dual = render_metafile((root / f"{scene}-dual.emf").read_bytes(), dpi=96, backend="replay")
    assert dual.emfplus_mode == "dual"


def test_bad_plus_header_cannot_fall_back_to_plain_emf() -> None:
    """损坏的 Plus Header 不能绕过识别并被当成空白普通 EMF。"""
    malformed = plus_record(0x4001)
    data = build_emf([plus_comment([malformed, plus_record(0x4002)])])
    with pytest.raises(MetafileMalformedError):
        render_metafile(data, dpi=144, backend="replay")
    with pytest.raises(MetafileMalformedError):
        render_metafile(build_emf([plus_comment([fill_rect(), plus_record(0x4002)])]), dpi=144, backend="replay")


def test_missing_eof_and_duplicate_header_are_malformed() -> None:
    """Header/EOF 控制记录必须完整且唯一。"""
    for records in [
        [plus_header(), fill_rect()],
        [plus_header(), plus_header(), plus_record(0x4002)],
        [plus_header(), plus_record(0x4002), fill_rect()],
    ]:
        with pytest.raises(MetafileMalformedError):
            render_metafile(build_emf([plus_comment(records)]), dpi=144, backend="replay")


def test_continued_object_mismatch_and_oversize_fail_closed() -> None:
    """分段对象不可跨槽混接、提前结束或超出声明长度。"""
    start = plus_record(0x4008, struct.pack("<I", 12) + solid(0xFF000000)[:8], 0x8100)
    for end in [
        plus_record(0x4008, b"1234", 0x0101),
        plus_record(0x4008, b"12", 0x0100),
        plus_record(0x4008, b"12345678", 0x0100),
    ]:
        with pytest.raises(MetafileMalformedError):
            render_metafile(plus_file([start, end]), dpi=144, backend="replay")


def test_clip_path_combination_and_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """路径裁剪正确限制填充，累计操作仍受预算保护。"""
    path = struct.pack("<III8f", VERSION, 4, 0, 10, 10, 40, 10, 40, 40, 10, 40) + bytes([0, 1, 1, 129])
    records = [plus_object(0, 3, path), plus_record(0x4033, flags=0), fill_rect(rect=(0, 0, 100, 100))]
    image = rgba(render_metafile(plus_file(records), dpi=100, backend="replay").data)
    assert image.getpixel((20, 20))[:3] == (255, 0, 0)
    assert image.getpixel((60, 60))[3] == 0
    monkeypatch.setattr(playback, "MAX_CLIP_OPERATIONS", 1)
    with pytest.raises(MetafileResourceLimitError):
        render_metafile(plus_file([*records[:2], plus_record(0x4033, flags=0x100), fill_rect()]), dpi=144, backend="replay")


def test_quality_approximations_are_reported_with_offsets() -> None:
    """SourceCopy 等已知近似必须保留记录类型与文件偏移。"""
    result = render_metafile(
        plus_file([plus_record(0x4023, flags=1), plus_record(0x4021, flags=7), fill_rect()]), dpi=144, backend="replay"
    )
    assert result.partial
    assert {d.record_type for d in result.diagnostics} == {0x4023, 0x4021}
    assert all(d.offset is not None and d.offset > 88 and d.record_index is not None for d in result.diagnostics)


def test_negative_stride_and_invalid_bitmap_dimensions() -> None:
    """负 stride 的底部起始行可恢复，非法尺寸在解码前拒绝。"""
    from metafile_render.emfplus.objects import PlusImage, parse_object

    obj = struct.pack("<IIiiiII", VERSION, 1, 1, 2, -4, 0x26200A, 0) + bytes([255, 0, 0, 255, 0, 0, 255, 255])
    parsed = parse_object(5, BoundedReader(obj), lambda _code, _message: None)
    assert isinstance(parsed, PlusImage)
    image = rgba(parsed.png)
    assert image.getpixel((0, 0)) == (255, 0, 0, 255)
    assert image.getpixel((0, 1)) == (0, 0, 255, 255)
    bad = struct.pack("<IIiiiII", VERSION, 1, 100000, 100000, 400000, 0x26200A, 0)
    with pytest.raises(MetafileResourceLimitError):
        render_metafile(plus_file([plus_object(0, 5, bad), fill_rect()]), dpi=144, backend="replay")


def test_driver_string_extra_matrix_and_realized_advance() -> None:
    """逐字位置的额外矩阵与字体 advance 模式使用一致坐标。"""
    data = (
        struct.pack("<IIII", 0xFF000000, 5, 1, 2)
        + "AB".encode("utf-16le")
        + struct.pack("<2f", 10, 50)
        + struct.pack("<6f", 1, 0, 0, 1, 5, 0)
    )
    result = render_metafile(plus_file([font_object(), plus_record(0x4036, data, 0x8000)]), dpi=144, backend="replay")
    assert result.partial
    doc = parse_metafile(plus_file([font_object(), plus_record(0x4036, data, 0x8000)]), dpi=144)
    command = doc.commands[0]
    assert isinstance(command, DrawTextCommand)
    assert command.positions[0] == (15, 50)
    assert command.positions[1][0] > 15


def test_rendering_origin_consumes_both_coordinates() -> None:
    """非零首坐标不得通过短路漏读第二个坐标。"""
    result = render_metafile(plus_file([plus_record(0x401D, struct.pack("<ii", 5, 6)), fill_rect()]), dpi=144, backend="replay")
    assert result.partial
