"""验证 WMF/EMF 解析、渲染与资源边界。"""

import base64
import struct
from io import BytesIO
from pathlib import Path

import pptx
import pyclipper
import pytest
from _metafile_test_utils import (
    basic_wmf,
    build_emf,
    build_placeable_wmf,
    emf_angle_arc,
    emf_arc_to,
    emf_begin_path,
    emf_close_figure,
    emf_create_brush,
    emf_create_pen,
    emf_end_path,
    emf_font,
    emf_intersect_clip_rect,
    emf_line_to,
    emf_move_to,
    emf_polybezier,
    emf_polyline_to,
    emf_record,
    emf_rectangle,
    emf_restoredc,
    emf_savedc,
    emf_select_object,
    emf_set_miter_limit,
    emf_set_text_align,
    emf_set_world_transform,
    emf_stretch_dib,
    emf_stroke_and_fill_path,
    emf_stroke_path,
    emf_text,
    emfplus_comment,
    wmf_move_to,
    wmf_record,
    wmf_rectangle,
    wmf_set_map_mode,
    wmf_set_text_align,
    wmf_textout,
)
from PIL import Image, ImageFont

import metafile_render.backends.bitmap as backends_bitmap
import metafile_render.backends.composite as backends_composite
import metafile_render.backends.paths as backends_paths
import metafile_render.backends.raster as backends_raster
import metafile_render.backends.svg as backends_svg
import metafile_render.backends.text as backends_text
from metafile_render import (
    MetafileError,
    MetafileMalformedError,
    MetafileResourceLimitError,
    MetafileUnsupportedError,
    limits,
    render_metafile,
)
from metafile_render import parser as metafile_parser
from metafile_render.commands import DibPayload, DrawImageCommand, DrawPathCommand, DrawTextCommand
from metafile_render.gdi import playback as gdi_playback
from metafile_render.geometry import FlattenBudget, PathBuilder, flatten_path, path_bounds
from metafile_render.primitives import ClipOperation, GraphicsPath, Matrix, Pen, Rect


def _open_result(payload: bytes) -> Image.Image:
    """解码渲染结果并返回脱离 BytesIO 生命周期的 RGBA 图片。"""
    with Image.open(BytesIO(payload)) as image:
        image.load()
        return image.convert("RGBA")


def _basic_emf_records() -> list[bytes]:
    """返回覆盖画笔、画刷、文字和 DIB 的基础 EMF records。"""
    return [
        emf_create_pen(1, 0x00FF0000, width=2),
        emf_create_brush(2, 0x0000FF00),
        emf_select_object(1),
        emf_select_object(2),
        emf_rectangle(5, 5, 95, 95),
        emf_stretch_dib(),
        emf_font(3),
        emf_select_object(3),
        emf_text("EMF", 20, 50, dx=18),
    ]


def _compound_square_path(*, inner_reversed: bool, include_island: bool = False) -> GraphicsPath:
    """构造用于验证 winding 与 even-odd 的嵌套方形复合路径。"""
    builder = PathBuilder()
    for point in ((10.0, 10.0), (90.0, 10.0), (90.0, 90.0), (10.0, 90.0)):
        if builder.current is None:
            builder.move_to(point)
        else:
            builder.line_to(point)
    builder.close()
    inner = (
        ((25.0, 25.0), (25.0, 75.0), (75.0, 75.0), (75.0, 25.0))
        if inner_reversed
        else ((25.0, 25.0), (75.0, 25.0), (75.0, 75.0), (25.0, 75.0))
    )
    builder.move_to(inner[0])
    for point in inner[1:]:
        builder.line_to(point)
    builder.close()
    if include_island:
        builder.move_to((40.0, 40.0))
        builder.line_to((60.0, 40.0))
        builder.line_to((60.0, 60.0))
        builder.line_to((40.0, 60.0))
        builder.close()
    return builder.build()


def test_emf_renders_png_jpeg_and_safe_svg() -> None:
    """验证同一 EMF 可输出三种格式且 SVG 不含活动或外部内容。"""
    data = build_emf(_basic_emf_records())

    png = render_metafile(data, output_format="png", dpi=144, backend="replay")
    jpeg = render_metafile(data, output_format="jpeg", dpi=144, backend="replay")
    svg = render_metafile(data, output_format="svg", dpi=144, backend="replay")

    assert png.media_type == "image/png"
    assert png.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert jpeg.media_type == "image/jpeg"
    assert jpeg.data.startswith(b"\xff\xd8\xff")
    assert svg.media_type == "image/svg+xml"
    assert svg.data.startswith(b'<svg xmlns="http://www.w3.org/2000/svg"')
    assert b"<script" not in svg.data
    assert b"http://" not in svg.data.replace(b"http://www.w3.org/2000/svg", b"")
    assert b"https://" not in svg.data
    assert (png.width, png.height) == (144, 144)
    assert not png.partial

    image = _open_result(png.data)
    assert image.getbbox() is not None
    assert image.getpixel((30, 30))[:3] == (255, 0, 0)
    assert image.getpixel((100, 30))[:3] == (0, 255, 0)
    assert image.getpixel((30, 110))[:3] == (0, 0, 255)
    assert image.getpixel((100, 110))[:3] == (255, 255, 255)
    # 字体会改变文字边缘像素；此处检查文字进入 SVG，定位和裁剪由专门测试覆盖。
    assert b"<text " in svg.data


def test_alpha_dib_unpremultiplies_channels_before_compositing() -> None:
    """验证 AC_SRC_ALPHA 的 premultiplied BGRA 在 Pillow 合成前恢复为 straight RGBA。"""
    header = bytearray(40)
    struct.pack_into("<IiiHHIIiiII", header, 0, 40, 2, -1, 1, 32, 0, 8, 0, 0, 0, 0)
    command = DrawImageCommand(
        image=DibPayload(bytes(header), bytes((0, 0, 128, 128, 0, 64, 0, 64)), use_source_alpha=True),
        destination=((0.0, 0.0), (2.0, 0.0), (2.0, 1.0), (0.0, 1.0)),
        source=None,
        rop=0,
    )

    decoded = backends_bitmap._decode_raw_alpha_dib(command)

    assert decoded is not None
    assert decoded.getpixel((0, 0)) == (255, 0, 0, 128)
    assert decoded.getpixel((1, 0)) == (0, 255, 0, 64)
    background = Image.new("RGBA", decoded.size, "white")
    background.alpha_composite(decoded)
    assert background.getpixel((0, 0)) == (255, 127, 127, 255)
    assert background.getpixel((1, 0)) == (191, 255, 191, 255)


def test_emf_save_restore_preserves_selected_objects_and_transform() -> None:
    """验证 SaveDC/RestoreDC 同时恢复 transform 与选中画刷。"""
    records = [
        emf_create_brush(1, 0x000000FF),
        emf_create_brush(2, 0x00FF0000),
        emf_select_object(1),
        emf_rectangle(0, 0, 20, 20),
        emf_savedc(),
        emf_set_world_transform((1, 0, 0, 1, 50, 0)),
        emf_select_object(2),
        emf_rectangle(0, 0, 20, 20),
        emf_restoredc(),
        emf_rectangle(25, 0, 45, 20),
    ]
    image = _open_result(render_metafile(build_emf(records), dpi=144, backend="replay").data)

    assert image.getpixel((14, 14))[0] > 200
    assert image.getpixel((50, 14))[0] > 200
    assert image.getpixel((86, 14))[2] > 200


def test_emf_to_records_append_to_one_path_figure_and_update_current_position() -> None:
    """验证 Path bracket 内 To records 连续追加且 CloseFigure 后新建 figure。"""
    data = build_emf(
        [
            emf_begin_path(),
            emf_move_to(10, 50),
            emf_polyline_to([(20, 50), (30, 50)]),
            emf_polybezier([(40, 50), (50, 50), (60, 50)], to=True),
            emf_close_figure(),
            emf_line_to(70, 70),
            emf_end_path(),
            emf_stroke_path(),
            emf_line_to(90, 90),
        ]
    )

    document = metafile_parser.parse_metafile(data, dpi=144)
    commands = [command for command in document.commands if isinstance(command, DrawPathCommand)]

    assert [segment.verb for segment in commands[0].path.segments] == ["M", "L", "L", "C", "Z", "M", "L"]
    assert commands[0].path.segments[5].points == ((60.0, 50.0),)
    assert commands[1].path.segments[0].points == ((70.0, 70.0),)


def test_emf_non_to_polybezier_does_not_change_gdi_current_position() -> None:
    """验证 PolyBezier 不像 PolyBezierTo 那样更新 DC current position。"""
    data = build_emf(
        [
            emf_move_to(5, 5),
            emf_begin_path(),
            emf_polybezier([(20, 20), (30, 0), (50, 0), (60, 20)], to=False),
            emf_end_path(),
            emf_stroke_path(),
            emf_line_to(10, 5),
        ]
    )

    document = metafile_parser.parse_metafile(data, dpi=144)
    commands = [command for command in document.commands if isinstance(command, DrawPathCommand)]

    assert commands[1].path.segments[0].points == ((5.0, 5.0),)


@pytest.mark.parametrize(
    "data",
    [
        build_emf(
            [
                emf_set_text_align(1),
                emf_move_to(10, 50),
                emf_text("first", 900, 900, dx=None),
                emf_text("second", 800, 800, dx=None),
            ]
        ),
        build_placeable_wmf(
            [
                wmf_set_text_align(1),
                wmf_move_to(10, 50),
                wmf_textout("first", 900, 900),
                wmf_textout("second", 800, 800),
            ]
        ),
    ],
    ids=["emf", "wmf"],
)
def test_updatecp_text_without_explicit_spacing_advances_current_position(data: bytes) -> None:
    """验证 EMF/WMF 无显式 spacing 的 TA_UPDATECP 文本不会相互覆盖。"""
    commands = [
        command for command in metafile_parser.parse_metafile(data, dpi=144).commands if isinstance(command, DrawTextCommand)
    ]

    assert len(commands) == 2
    assert commands[0].origin == (10.0, 50.0)
    assert commands[1].origin[0] > commands[0].origin[0]
    assert commands[1].origin[1] == pytest.approx(commands[0].origin[1])
    assert not commands[0].positions
    assert not commands[1].positions


@pytest.mark.parametrize(("text_align", "expected_x"), [(6, (40.0, 50.0)), (2, (30.0, 40.0))])
def test_explicit_spacing_applies_center_or_right_alignment_once(
    text_align: int,
    expected_x: tuple[float, float],
) -> None:
    """验证显式 DX 字符先按整串 alignment 平移，再从左侧 cell origin 绘制。"""
    document = metafile_parser.parse_metafile(
        build_emf([emf_set_text_align(text_align), emf_text("AB", 50, 50, dx=10)]), dpi=144
    )
    command = next(command for command in document.commands if isinstance(command, DrawTextCommand))
    positions, positioned_run = backends_text._aligned_text_positions(command, Matrix(), list(command.positions))
    svg = backends_svg._svg_text_elements(command, Matrix())

    assert command.advance_end == (70.0, 50.0)
    assert positioned_run
    assert tuple(position[0] for position in positions) == expected_x
    assert all(position[1] == 50.0 for position in positions)
    assert 'text-anchor="start"' in svg


def test_rotated_text_preserves_left_and_right_anchor_without_clipping() -> None:
    """验证旋转文字围绕 GDI reference 定位，左右 anchor 均保留完整字形。"""
    font = ImageFont.load_default(size=20)
    left = Image.new("RGBA", (180, 140), (0, 0, 0, 0))
    right = Image.new("RGBA", (180, 140), (0, 0, 0, 0))

    for layer, anchor in ((left, "la"), (right, "ra")):
        backends_text._draw_rotated_text(
            layer,
            (90.0, 70.0),
            "Anchor",
            font=font,
            fill=(0, 0, 0, 255),
            anchor=anchor,
            rotation=35.0,
            underline=True,
            strikeout=True,
        )

    left_pixels = sum(value > 0 for value in left.getchannel("A").getdata())
    right_pixels = sum(value > 0 for value in right.getchannel("A").getdata())

    assert left.getbbox() is not None and right.getbbox() is not None
    assert right_pixels == pytest.approx(left_pixels, rel=0.05)
    assert left.getbbox()[2] > 90
    assert right.getbbox()[0] < 90


@pytest.mark.parametrize(("sweep_angle", "expected_end_y"), [(90.0, -10.0), (-90.0, 10.0)])
def test_emf_anglearc_uses_sweep_direction_and_updates_current_position(
    sweep_angle: float,
    expected_end_y: float,
) -> None:
    """验证 AngleArc 绘制起始连线、使用正确 sweep 方向并更新 current position。"""
    data = build_emf(
        [emf_angle_arc(0, 0, 10, 0.0, sweep_angle), emf_line_to(20, 20)],
        bounds=(-20, -20, 30, 30),
    )
    commands = [
        command for command in metafile_parser.parse_metafile(data, dpi=144).commands if isinstance(command, DrawPathCommand)
    ]

    assert len(commands[0].path.segments) == 14
    assert commands[0].path.segments[0].points == ((0.0, 0.0),)
    assert commands[0].path.segments[1].points == ((10.0, 0.0),)
    assert commands[0].path.segments[-1].points[0] == pytest.approx((0.0, expected_end_y))
    assert commands[1].path.segments[0].points[0] == pytest.approx((0.0, expected_end_y))


def test_emf_arcto_connects_current_position_to_projected_arc_endpoints() -> None:
    """验证 ArcTo 连接 current 与椭圆投影起点，并将 current 更新到投影终点。"""
    data = build_emf(
        [
            emf_move_to(10, 10),
            emf_arc_to((0, 0, 100, 100), (150, 50), (50, -100)),
            emf_line_to(80, 80),
        ]
    )
    commands = [
        command for command in metafile_parser.parse_metafile(data, dpi=144).commands if isinstance(command, DrawPathCommand)
    ]

    assert commands[0].path.segments[0].points == ((10.0, 10.0),)
    assert commands[0].path.segments[1].points[0] == pytest.approx((100.0, 50.0))
    assert commands[0].path.segments[-1].points[0] == pytest.approx((50.0, 0.0))
    assert commands[1].path.segments[0].points[0] == pytest.approx((50.0, 0.0))


def test_emf_stroke_and_fill_closes_every_open_figure() -> None:
    """验证 StrokeAndFillPath 在描边前显式闭合全部开放 figure。"""
    data = build_emf(
        [
            emf_begin_path(),
            emf_move_to(10, 10),
            emf_polyline_to([(40, 10), (40, 40)]),
            emf_move_to(60, 60),
            emf_polyline_to([(90, 60), (90, 90)]),
            emf_end_path(),
            emf_stroke_and_fill_path(),
        ]
    )

    command = next(
        command for command in metafile_parser.parse_metafile(data, dpi=144).commands if isinstance(command, DrawPathCommand)
    )

    assert [segment.verb for segment in command.path.segments] == ["M", "L", "L", "Z", "M", "L", "L", "Z"]


def test_compound_path_masks_preserve_nonzero_and_evenodd_topology() -> None:
    """验证 winding、alternate、孔洞和孔洞内岛屿的 mask 拓扑。"""
    opposite = _compound_square_path(inner_reversed=True)
    same = _compound_square_path(inner_reversed=False)
    island = _compound_square_path(inner_reversed=True, include_island=True)
    self_intersecting_builder = PathBuilder()
    self_intersecting_builder.move_to((10.0, 10.0))
    self_intersecting_builder.line_to((90.0, 90.0))
    self_intersecting_builder.line_to((10.0, 90.0))
    self_intersecting_builder.line_to((90.0, 10.0))
    self_intersecting_builder.close()

    winding_hole = backends_paths._path_mask(opposite, Matrix(), (100, 100), "nonzero")
    winding_filled = backends_paths._path_mask(same, Matrix(), (100, 100), "nonzero")
    evenodd_hole = backends_paths._path_mask(same, Matrix(), (100, 100), "evenodd")
    nested_island = backends_paths._path_mask(island, Matrix(), (100, 100), "nonzero")
    self_intersecting = backends_paths._path_mask(self_intersecting_builder.build(), Matrix(), (100, 100), "evenodd")

    assert winding_hole.getpixel((20, 20)) == 255
    assert winding_hole.getpixel((30, 30)) == 0
    assert winding_filled.getpixel((50, 50)) == 255
    assert evenodd_hole.getpixel((50, 50)) == 0
    assert nested_island.getpixel((30, 30)) == 0
    assert nested_island.getpixel((50, 50)) == 255
    assert self_intersecting.getbbox() is not None
    assert self_intersecting.getpixel((50, 25)) == 255


def test_polytree_paint_handles_deep_nesting_without_python_recursion() -> None:
    """验证恶意深层 PolyTree 使用显式栈绘制且继续计入 flatten 预算。"""
    root = pyclipper.PyPolyNode()
    parent = root
    for index in range(1100):
        child = pyclipper.PyPolyNode()
        child.Contour = [
            (index, index),
            (2201 - index, index),
            (2201 - index, 2201 - index),
            (index, 2201 - index),
        ]
        child.IsHole = bool(index & 1)
        parent.Childs.append(child)
        parent = child
    budget = FlattenBudget(limit=5000)

    backends_paths._paint_polytree(Image.new("L", (10, 10)), root, budget)

    assert budget.used == 4400


def test_clip_mask_reuses_compound_path_topology_and_combine_modes() -> None:
    """验证裁剪 copy/and/or/xor/diff 与路径填充共享同一 winding 结果。"""
    outer = _compound_square_path(inner_reversed=False)
    inner_builder = PathBuilder()
    inner_builder.move_to((35.0, 35.0))
    inner_builder.line_to((65.0, 35.0))
    inner_builder.line_to((65.0, 65.0))
    inner_builder.line_to((35.0, 65.0))
    inner_builder.close()
    inner = inner_builder.build()
    copy_outer = ClipOperation(outer, "copy", "nonzero")
    copy_inner = ClipOperation(inner, "copy", "nonzero")

    intersection = backends_paths._clip_mask((copy_outer, ClipOperation(inner, "and", "nonzero")), Matrix(), (100, 100))
    union = backends_paths._clip_mask((copy_inner, ClipOperation(outer, "or", "nonzero")), Matrix(), (100, 100))
    xor = backends_paths._clip_mask((copy_inner, ClipOperation(outer, "xor", "nonzero")), Matrix(), (100, 100))
    difference = backends_paths._clip_mask((copy_outer, ClipOperation(inner, "diff", "nonzero")), Matrix(), (100, 100))

    assert intersection is not None and intersection.getpixel((50, 50)) == 255 and intersection.getpixel((20, 20)) == 0
    assert union is not None and union.getpixel((50, 50)) == 255 and union.getpixel((20, 20)) == 255
    assert xor is not None and xor.getpixel((50, 50)) == 0 and xor.getpixel((20, 20)) == 255
    assert difference is not None and difference.getpixel((50, 50)) == 0 and difference.getpixel((20, 20)) == 255


def test_adaptive_cubic_flattening_respects_error_and_point_budget() -> None:
    """验证更严格 flatness 产生更多点且离散点预算可以稳定终止。"""
    builder = PathBuilder()
    builder.move_to((0.0, 0.0))
    builder.cubic_to((0.0, 100.0), (100.0, 100.0), (100.0, 0.0))
    path = builder.build()

    coarse = flatten_path(path, flatness=10.0)[0][0]
    fine = flatten_path(path, flatness=0.1)[0][0]

    assert fine[-1] == coarse[-1] == (100.0, 0.0)
    assert len(fine) > len(coarse)
    with pytest.raises(MetafileResourceLimitError, match="max_flattened_points=2"):
        flatten_path(path, flatness=0.1, budget=FlattenBudget(limit=2))


def test_stroke_mask_honors_caps_and_svg_miter_limit() -> None:
    """验证 flat/square/round 端帽差异以及 SVG 保留 DC miter limit。"""
    subpaths = [([(20.0, 50.0), (80.0, 50.0)], False)]
    flat = backends_paths._stroke_mask(
        subpaths,
        size=(100, 100),
        width=10.0,
        pen=Pen(cap="flat"),
        dashes=(),
        miter_limit=10.0,
        budget=FlattenBudget(),
    )
    square = backends_paths._stroke_mask(
        subpaths,
        size=(100, 100),
        width=10.0,
        pen=Pen(cap="square"),
        dashes=(),
        miter_limit=10.0,
        budget=FlattenBudget(),
    )
    round_cap = backends_paths._stroke_mask(
        subpaths,
        size=(100, 100),
        width=10.0,
        pen=Pen(cap="round"),
        dashes=(),
        miter_limit=10.0,
        budget=FlattenBudget(),
    )
    dashed = backends_paths._stroke_mask(
        subpaths,
        size=(100, 100),
        width=6.0,
        pen=Pen(cap="flat"),
        dashes=(10.0, 10.0),
        miter_limit=10.0,
        budget=FlattenBudget(),
    )
    corner = [([(20.0, 80.0), (50.0, 20.0), (80.0, 80.0)], False)]
    miter = backends_paths._stroke_mask(
        corner,
        size=(100, 100),
        width=12.0,
        pen=Pen(cap="flat", join="miter"),
        dashes=(),
        miter_limit=10.0,
        budget=FlattenBudget(),
    )
    bevel = backends_paths._stroke_mask(
        corner,
        size=(100, 100),
        width=12.0,
        pen=Pen(cap="flat", join="bevel"),
        dashes=(),
        miter_limit=10.0,
        budget=FlattenBudget(),
    )
    svg = render_metafile(
        build_emf(
            [
                emf_set_miter_limit(3.5),
                emf_begin_path(),
                emf_move_to(10, 10),
                emf_line_to(90, 90),
                emf_end_path(),
                emf_stroke_path(),
            ]
        ),
        output_format="svg",
        dpi=144,
        backend="replay",
    ).data

    assert flat.getpixel((16, 50)) == 0
    assert square.getpixel((16, 50)) == 255
    assert round_cap.getpixel((16, 50)) == 255
    assert dashed.getpixel((25, 50)) == 255
    assert dashed.getpixel((35, 50)) == 0
    assert miter.getbbox() is not None and bevel.getbbox() is not None
    assert miter.getbbox()[1] < bevel.getbbox()[1]
    assert b'stroke-miterlimit="3.5"' in svg


def test_bitmap_source_crop_preserves_empty_and_partial_destination_mapping() -> None:
    """验证完全越界 source 不绘制，部分交集只映射到对应 destination 子区域。"""
    image = Image.new("RGBA", (2, 2), "red")
    destination = ((0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0))

    outside, outside_fraction = backends_bitmap._crop_source(image, Rect(10.0, 10.0, 12.0, 12.0))
    partial, partial_fraction = backends_bitmap._crop_source(image, Rect(-1.0, 0.0, 1.0, 2.0))
    mirrored, mirrored_fraction = backends_bitmap._crop_source(image, Rect(3.0, 0.0, -1.0, 2.0))

    assert outside is None
    assert outside_fraction == Rect(0.0, 0.0, 0.0, 0.0)
    assert partial is not None and partial.size == (1, 2)
    assert partial_fraction == Rect(0.5, 0.0, 1.0, 1.0)
    assert mirrored is not None and mirrored.size == (2, 2)
    assert mirrored_fraction == Rect(0.25, 0.0, 0.75, 1.0)
    assert backends_bitmap._crop_destination(destination, partial_fraction) == (
        (50.0, 0.0),
        (100.0, 0.0),
        (100.0, 100.0),
        (50.0, 100.0),
    )


def test_placeable_wmf_renders_vector_content() -> None:
    """验证 placeable WMF 的 checksum、对象表和逻辑尺寸进入 PNG。"""
    result = render_metafile(basic_wmf(), dpi=144, backend="replay")
    image = _open_result(result.data)

    assert result.source_format == "wmf"
    assert (result.width, result.height) == (144, 144)
    assert not result.partial
    assert image.getbbox() is not None
    assert image.getpixel((72, 72))[1] > 150


def test_wmf_roundrect_uses_record_parameter_order() -> None:
    """验证 META_ROUNDRECT 按 Height/Width/Bottom/Right/Top/Left 解码。"""
    payload = struct.pack("<hhhhhh", 100, 300, 900, 800, 200, 100)
    document = metafile_parser.parse_metafile(build_placeable_wmf([wmf_record(0x061C, payload)]), dpi=144)
    command = next(command for command in document.commands if isinstance(command, DrawPathCommand))

    assert path_bounds(command.path) == Rect(100.0, 200.0, 800.0, 900.0)
    assert command.path.segments[0].points == ((250.0, 200.0),)


@pytest.mark.parametrize("text_align", [2, 6], ids=["right", "center"])
def test_standard_wmf_fallback_bounds_include_aligned_text(text_align: int) -> None:
    """验证无 placeable header 时 CENTER/RIGHT 文本 bounds 不会从 origin 单向向右估算。"""
    standard_wmf = build_placeable_wmf([wmf_set_text_align(text_align), wmf_textout("AB", 100, 50)])[22:]

    document = metafile_parser.parse_metafile(standard_wmf, dpi=144)

    assert document.bounds.left < 100.0
    if text_align == 2:
        assert document.bounds.right == pytest.approx(100.0)
    else:
        assert (document.bounds.left + document.bounds.right) / 2.0 == pytest.approx(100.0)


@pytest.mark.parametrize(
    ("map_mode", "millimeters_per_unit"),
    [(2, 0.1), (3, 0.01), (4, 0.254), (5, 0.0254), (6, 25.4 / 1440.0)],
    ids=["lometric", "himetric", "loenglish", "hienglish", "twips"],
)
def test_fixed_wmf_map_modes_use_documented_physical_units(map_mode: int, millimeters_per_unit: float) -> None:
    """验证固定 map mode 已按 Windows 定义的毫米或英寸单位换算，防止误改 2.54 倍。"""
    data = build_placeable_wmf([wmf_set_map_mode(map_mode), wmf_rectangle(0, 0, 100, 100)])
    document = metafile_parser.parse_metafile(data, dpi=144)
    command = next(command for command in document.commands if isinstance(command, DrawPathCommand))
    bounds = path_bounds(command.path)

    assert bounds is not None
    expected = 100 * millimeters_per_unit * (144 / 25.4)
    assert abs(bounds.width) == pytest.approx(expected)
    assert abs(bounds.height) == pytest.approx(expected)


def test_vector_only_metafile_uses_4x_antialiasing_and_2x_svg_fallback() -> None:
    """验证矢量公式保持逻辑尺寸，同时使用 4× 栅格和 2× DOCX fallback。"""
    document = metafile_parser.parse_metafile(basic_wmf(), dpi=144)
    svg = render_metafile(basic_wmf(), output_format="svg", dpi=144, backend="replay").data
    fallback, logical_width, logical_height = _svg_fallback(svg)

    assert backends_raster._supersample_factor(document) == 4
    assert backends_svg._svg_fallback_scale(document) == 2
    assert (logical_width, logical_height) == (144, 144)
    with Image.open(BytesIO(fallback)) as image:
        assert image.size == (288, 288)
        assert image.info["dpi"][0] == pytest.approx(192, abs=1)


def test_emfplus_dual_uses_emf_fallback_and_empty_only_is_rejected() -> None:
    """验证 EMF+ Dual 播放 EMF fallback，Only 返回稳定不支持错误。"""
    records = [emfplus_comment(dual=True), emf_create_brush(1, 0x0000FF00), emf_select_object(1), emf_rectangle(5, 5, 95, 95)]
    dual = render_metafile(build_emf(records), dpi=144, backend="replay")

    assert dual.emfplus_mode == "dual"
    assert _open_result(dual.data).getbbox() is not None

    only_data = build_emf([emfplus_comment(dual=False), emf_rectangle(5, 5, 95, 95)])
    with pytest.raises(MetafileUnsupportedError, match=r"EMF\+ Only"):
        render_metafile(only_data, dpi=144, backend="replay")


def test_unknown_drawing_record_keeps_partial_result() -> None:
    """验证未知绘图 record 被跳过但其他内容继续输出。"""
    data = build_emf([emf_record(118, b"\x00" * 16), emf_rectangle(5, 5, 95, 95)])
    result = render_metafile(data, dpi=144, backend="replay")

    assert result.partial
    assert any(diagnostic.code == "unsupported_emf_record" for diagnostic in result.diagnostics)
    assert _open_result(result.data).getbbox() is not None


def test_malformed_record_and_emf_signature_fail_closed() -> None:
    """验证截断 record 与伪造签名均返回稳定 malformed 错误。"""
    valid = bytearray(build_emf([emf_rectangle(5, 5, 95, 95)]))
    valid[92:96] = (0x7FFFFFFC).to_bytes(4, "little")
    with pytest.raises(MetafileMalformedError):
        render_metafile(bytes(valid), dpi=144, backend="replay")
    with pytest.raises(MetafileMalformedError):
        render_metafile(b"not-a-metafile", dpi=144, backend="replay")


def test_canvas_is_downscaled_to_fixed_pixel_budget() -> None:
    """验证巨大物理 frame 只会触发等比缩放而不会分配无界画布。"""
    data = build_emf([emf_rectangle(0, 0, 100, 100)], frame=(0, 0, 254000, 254000))
    result = render_metafile(data, dpi=144, backend="replay")

    assert result.width * result.height <= 16_000_000
    assert max(result.width, result.height) <= 8192
    assert any(diagnostic.code == "canvas_downscaled" for diagnostic in result.diagnostics)


def test_fixed_record_point_and_state_limits_are_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证点数和 SaveDC 深度使用 metafile 专属固定安全预算。"""
    polygon_payload = (
        b"\x00" * 16
        + (3).to_bytes(4, "little")
        + b"".join(value.to_bytes(4, "little", signed=True) for value in (0, 0, 50, 0, 25, 50))
    )
    monkeypatch.setattr(limits, "MAX_POINTS_PER_RECORD", 2)
    with pytest.raises(MetafileResourceLimitError, match="max_points_per_record"):
        render_metafile(build_emf([emf_record(3, polygon_payload)]), dpi=144, backend="replay")

    monkeypatch.setattr(limits, "MAX_POINTS_PER_RECORD", 1_000_000)
    monkeypatch.setattr(gdi_playback, "MAX_STATE_DEPTH", 1)
    with pytest.raises(MetafileResourceLimitError, match="max_state_depth"):
        render_metafile(build_emf([emf_savedc(), emf_savedc(), emf_rectangle(1, 1, 10, 10)]), dpi=144, backend="replay")

    oversized_dib = bytearray(emf_stretch_dib())
    oversized_dib[84:88] = (100_000).to_bytes(4, "little", signed=True)
    oversized_dib[88:92] = (100_000).to_bytes(4, "little", signed=True)
    with pytest.raises(MetafileResourceLimitError, match="DIB dimensions"):
        render_metafile(build_emf([bytes(oversized_dib)]), dpi=144, backend="replay")

    monkeypatch.setattr(limits, "MAX_RENDER_WORK_PIXELS", 1)
    with pytest.raises(MetafileResourceLimitError, match="max_render_work_pixels"):
        render_metafile(build_emf([emf_rectangle(1, 1, 10, 10)]), dpi=144, backend="replay")


def test_clip_stack_and_render_work_use_fixed_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证累计 clip 操作受深度限制，渲染预算也计入逐项 mask 合成工作。"""
    clip = emf_intersect_clip_rect(0, 0, 100, 100)
    monkeypatch.setattr(gdi_playback, "MAX_CLIP_OPERATIONS", 2)
    with pytest.raises(MetafileResourceLimitError, match="max_clip_operations=2"):
        metafile_parser.parse_metafile(build_emf([clip, clip, clip, emf_rectangle(1, 1, 10, 10)]), dpi=144)

    monkeypatch.setattr(gdi_playback, "MAX_CLIP_OPERATIONS", 64)
    monkeypatch.setattr(limits, "MAX_TOTAL_CLIP_OPERATIONS", 1)
    with pytest.raises(MetafileResourceLimitError, match="max_total_clip_operations=1"):
        metafile_parser.parse_metafile(build_emf([clip, emf_rectangle(1, 1, 10, 10), emf_rectangle(20, 20, 30, 30)]), dpi=144)

    document = metafile_parser.parse_metafile(build_emf([clip, emf_rectangle(1, 1, 10, 10)]), dpi=144)
    assert backends_raster._render_work_units(document) == 2


def test_svg_text_escapes_markup_characters() -> None:
    """验证 EMF 文字无法把脚本或 XML 标签注入 SVG。"""
    data = build_emf([emf_font(1), emf_select_object(1), emf_text("<script>&", 5, 30)])
    svg = render_metafile(data, output_format="svg", dpi=144, backend="replay").data

    assert b"<script>" not in svg
    assert b"&lt;" in svg
    assert b"&gt;" in svg
    assert b"&amp;" in svg
    assert svg.index(b"<rect") < svg.index(b"<text")


def test_raster_operations_use_exact_bitwise_channels() -> None:
    """验证常见 ROP2/ROP3 不使用颜色明暗近似替代位运算。"""
    destination = bytes((0x0F, 0x55, 0xAA))
    source = bytes((0x33, 0x0F, 0xF0))

    assert backends_composite._bitwise_channel_bytes(destination, source, "xor") == bytes((0x3C, 0x5A, 0x5A))
    assert backends_composite._bitwise_channel_bytes(destination, source, "and") == bytes((0x03, 0x05, 0xA0))
    assert backends_composite._bitwise_channel_bytes(destination, source, "or") == bytes((0x3F, 0x5F, 0xFA))
    assert backends_composite._bitwise_channel_bytes(destination, source, "not_source") == bytes((0xCC, 0xF0, 0x0F))
    assert backends_composite._bitwise_channel_bytes(destination, source, "not_xor") == bytes((0xC3, 0xA5, 0xA5))


@pytest.mark.parametrize("data", [build_emf([emf_rectangle(5, 5, 95, 95)]), basic_wmf()])
def test_every_truncated_prefix_fails_without_unexpected_exception(data: bytes) -> None:
    """验证每个截断位置都只产生稳定格式错误，不泄漏 struct/Pillow 异常。"""
    for size in range(len(data)):
        with pytest.raises(MetafileError):
            render_metafile(data[:size], dpi=144, backend="replay")


@pytest.mark.parametrize("name", ["docx-icon.emf", "generic-icon.emf", "pptx-icon.emf", "xlsx-icon.emf"])
def test_real_python_pptx_emf_icons_render_without_partial_result(name: str) -> None:
    """验证依赖包内真实 Office EMF 图标的文字、alpha 和裁剪组合。"""
    template = Path(pptx.__file__).resolve().parent / "templates" / name
    result = render_metafile(template.read_bytes(), dpi=144, backend="replay")
    image = _open_result(result.data)

    assert not result.partial
    assert all(diagnostic.code == "font_substituted" and diagnostic.level == "info" for diagnostic in result.diagnostics)
    assert image.width >= 100
    assert image.height >= 90
    assert image.getbbox() is not None
    assert image.getchannel("A").getextrema()[1] == 255


def _svg_fallback(payload: bytes) -> tuple[bytes, int, int]:
    """读取测试生成 SVG 的固定 PNG 元数据并检查公共输出标记。"""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(payload)
    assert root.get("data-metafile-render") == "wmf-emf"
    metadata = root.find("{http://www.w3.org/2000/svg}metadata")
    assert metadata is not None
    assert metadata.get("id") == "metafile-render-raster-fallback"
    assert metadata.get("data-mime") == "image/png"
    return base64.b64decode(metadata.text or "", validate=True), int(root.get("width", "")), int(root.get("height", ""))
