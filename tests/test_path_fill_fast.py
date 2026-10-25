"""验证不透明实心填充快路的像素、裁剪及预算边界。"""

from dataclasses import replace
from typing import Literal

import pytest
from PIL import Image

from metafile_render.backends import paths
from metafile_render.backends.composite import _composite_path
from metafile_render.backends.session import RenderSession
from metafile_render.commands import DrawPathCommand
from metafile_render.geometry import FlattenBudget, PathBuilder, rectangle_path
from metafile_render.models import MetafileResourceLimitError
from metafile_render.primitives import Brush, ClipOperation, ClipStack, Color, GraphicsPath, Matrix, Pen, Rect

_SIZE = (32, 32)
_COLOR = Color(17, 93, 201)
_MATRIX = Matrix(a=1.125, b=0.125, c=-0.125, d=0.875, e=-0.375, f=0.625)


def _test_path(kind: str) -> GraphicsPath:
    """构造包含孔洞、负小数坐标、曲线或空几何的测试路径。"""
    builder = PathBuilder()
    if kind == "compound":
        builder.extend(rectangle_path(Rect(-2.25, 2.75, 29.75, 28.25)))
        builder.extend(rectangle_path(Rect(7.25, 9.75, 19.75, 21.25)))
    elif kind == "cubic":
        builder.move_to((-2.25, 3.75))
        builder.cubic_to((7.5, -5.25), (24.75, 25.5), (32.25, 4.5))
        builder.line_to((14.25, 29.75))
        builder.close()
    return builder.build()


def _test_clip(mode: Literal["none", "copy", "and", "or", "xor", "diff"]) -> ClipStack:
    """以两个相交矩形覆盖各类裁剪运算及无裁剪分支。"""
    if mode == "none":
        return ()
    return (
        ClipOperation(rectangle_path(Rect(-1, 3, 17, 29)), "copy"),
        ClipOperation(rectangle_path(Rect(9, -2, 30, 18)), mode),
    )


def _fill_command(
    path: GraphicsPath,
    *,
    brush: Brush | None = None,
    clip: ClipStack = (),
    stroke: bool = False,
    fill_rule: Literal["evenodd", "nonzero"] = "evenodd",
) -> DrawPathCommand:
    """创建实心填充命令，按需叠加半透明虚线描边和裁剪。"""
    return DrawPathCommand(
        path,
        Pen(color=Color(201, 93, 17, 128), width=2, cap="square", dashes=(3, 2)),
        brush or Brush(color=_COLOR),
        stroke,
        True,
        fill_rule,
        clip,
        13,
    )


@pytest.mark.parametrize("kind", ["compound", "cubic", "empty"])
@pytest.mark.parametrize("fill_rule", ["evenodd", "nonzero"])
def test_fill_masks_remain_binary(kind: str, fill_rule: Literal["evenodd", "nonzero"]) -> None:
    """保护超采样空间填充 mask 的二值覆盖语义。"""
    mask = paths._path_mask(_test_path(kind), _MATRIX, _SIZE, fill_rule, FlattenBudget())
    assert set(mask.tobytes()) <= {0, 255}


@pytest.mark.parametrize("kind", ["compound", "cubic", "empty"])
@pytest.mark.parametrize("fill_rule", ["evenodd", "nonzero"])
@pytest.mark.parametrize("clip_mode", ["none", "copy", "and", "or", "xor", "diff"])
@pytest.mark.parametrize("stroke", [False, True])
def test_opaque_fill_matches_legacy_layer(
    kind: str,
    fill_rule: Literal["evenodd", "nonzero"],
    clip_mode: Literal["none", "copy", "and", "or", "xor", "diff"],
    stroke: bool,
) -> None:
    """在执行 ROP 合成之前对照保留的旧图层路径，核对全部 RGBA 字节和预算。"""
    command = _fill_command(_test_path(kind), fill_rule=fill_rule, clip=_test_clip(clip_mode), stroke=stroke)
    actual_budget, legacy_budget = FlattenBudget(), FlattenBudget()
    actual = paths._render_path_command(command, _MATRIX, _SIZE, 1, actual_budget)
    # ROP 在图层生成后才应用；非默认 ROP 的旧图层实现可作为不复制算法的像素参考。
    legacy = paths._render_path_command(replace(command, rop2=7), _MATRIX, _SIZE, 1, legacy_budget)
    assert actual.tobytes() == legacy.tobytes()
    assert actual_budget.used == legacy_budget.used


def test_opaque_fill_keeps_transparent_rgb_before_and_after_clip() -> None:
    """填充外保留透明黑，裁剪掉的已填充像素保留旧路径的隐藏 RGB。"""
    clip = (ClipOperation(rectangle_path(Rect(10, 0, 31, 31)), "and"),)
    command = _fill_command(rectangle_path(Rect(4, 4, 20, 20)), clip=clip)
    image = paths._render_path_command(command, Matrix(), _SIZE, 1, FlattenBudget())
    assert image.getpixel((2, 2)) == (0, 0, 0, 0)
    assert image.getpixel((12, 12)) == _COLOR.rgba()
    assert image.getpixel((6, 6)) == (17, 93, 201, 0)


@pytest.mark.parametrize("alpha", [0, 64, 128, 254])
def test_translucent_solid_fill_keeps_source_over_pixels(alpha: int) -> None:
    """半透明和全透明画刷继续保留原有 RGB、alpha 及连续覆盖语义。"""
    color = Color(17, 93, 201, alpha)
    command = _fill_command(rectangle_path(Rect(4, 4, 20, 20)), brush=Brush(color=color))
    layer = paths._render_path_command(command, Matrix(), _SIZE, 1, FlattenBudget())
    assert layer.getpixel((2, 2)) == (0, 0, 0, 0)
    assert layer.getpixel((12, 12)) == (color.rgba() if alpha else (0, 0, 0, 0))
    destination = Image.new("RGBA", _SIZE, (29, 57, 113, 96))
    expected = Image.new("RGBA", (1, 1), (29, 57, 113, 96))
    source = Image.new("RGBA", (1, 1), color.rgba())
    for _ in range(2):
        _composite_path(destination, layer, command.rop2)
        expected = Image.alpha_composite(expected, source)
    assert destination.getpixel((12, 12)) == expected.getpixel((0, 0))
    assert destination.getpixel((2, 2)) == (29, 57, 113, 96)


def test_hatch_fill_retains_pattern_and_transparent_gaps() -> None:
    """hatch 画刷继续绘制条纹，不能被实心颜色快路填满。"""
    command = _fill_command(rectangle_path(Rect(0, 0, 31, 31)), brush=Brush(kind="hatch", color=_COLOR, hatch=0))
    layer = paths._render_path_command(command, Matrix(), _SIZE, 1, FlattenBudget())
    assert layer.getpixel((12, 8)) == _COLOR.rgba()
    assert layer.getpixel((12, 9)) == (0, 0, 0, 0)


def test_nondefault_rop_keeps_exact_bitwise_composition() -> None:
    """非默认 ROP 保持原图层及逐通道 XOR 合成，透明区不覆盖目标。"""
    command = replace(_fill_command(rectangle_path(Rect(4, 4, 20, 20))), rop2=7)
    layer = paths._render_path_command(command, Matrix(), _SIZE, 1, FlattenBudget())
    canvas = Image.new("RGBA", _SIZE, (29, 57, 113, 255))
    _composite_path(canvas, layer, command.rop2)
    assert canvas.getpixel((12, 12)) == (29 ^ 17, 57 ^ 93, 113 ^ 201, 255)
    assert canvas.getpixel((2, 2)) == (29, 57, 113, 255)


def test_fill_budget_boundary_matches_legacy() -> None:
    """快路和旧路径在相同生成点数处成功或超限，不改变拒绝边界。"""
    command = _fill_command(_test_path("cubic"), clip=_test_clip("xor"), stroke=True)
    measured = FlattenBudget()
    paths._render_path_command(command, _MATRIX, _SIZE, 1, measured)
    for rop2 in (13, 7):
        exact = FlattenBudget(limit=measured.used)
        paths._render_path_command(replace(command, rop2=rop2), _MATRIX, _SIZE, 1, exact)
        assert exact.used == measured.used
        with pytest.raises(MetafileResourceLimitError, match=f"max_flattened_points={measured.used - 1}"):
            paths._render_path_command(replace(command, rop2=rop2), _MATRIX, _SIZE, 1, FlattenBudget(limit=measured.used - 1))


def test_repeated_clipped_fill_preserves_cache_and_pixels() -> None:
    """重复裁剪命中缓存后，快路仍与旧图层逐字节一致且点预算相同。"""
    command = _fill_command(_test_path("cubic"), clip=_test_clip("and"))
    with RenderSession() as actual_session, RenderSession() as legacy_session:
        for _ in range(2):
            actual = paths._render_path_command(command, _MATRIX, _SIZE, 1, actual_session.flatten, actual_session)
            legacy = paths._render_path_command(
                replace(command, rop2=7), _MATRIX, _SIZE, 1, legacy_session.flatten, legacy_session
            )
            assert actual.tobytes() == legacy.tobytes()
            assert actual_session.flatten.used == legacy_session.flatten.used
        assert actual_session.cache.hits == legacy_session.cache.hits == 1
