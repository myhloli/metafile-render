"""验证不透明描边的局部像素覆盖与原整图颜色层逐字节等价。"""

from dataclasses import replace
from typing import Literal

from PIL import Image
import pytest

from metafile_render.backends import paths
from metafile_render.commands import DrawPathCommand
from metafile_render.geometry import FlattenBudget, PathBuilder, rectangle_path
from metafile_render.models import MetafileResourceLimitError
from metafile_render.primitives import Brush, ClipOperation, Color, Matrix, Pen, Rect


def _command(
    kind: str, cap: Literal["round", "flat", "square"], join: Literal["round", "bevel", "miter"], dashed: bool, clipped: bool
) -> DrawPathCommand:
    """构造曲线、交叉闭合轮廓和空路径，并叠加半透明填充及差集裁剪。"""
    builder = PathBuilder()
    if kind == "compound":
        builder.extend(rectangle_path(Rect(-2.25, 2.75, 29.75, 28.25)))
        builder.extend(rectangle_path(Rect(7.25, 9.75, 19.75, 21.25)))
    elif kind == "cubic":
        builder.move_to((-2.25, 3.75))
        builder.cubic_to((7.5, -5.25), (24.75, 25.5), (32.25, 4.5))
        builder.line_to((14.25, 29.75))
        builder.line_to((3.5, 4.25))
    return DrawPathCommand(
        builder.build(),
        Pen(color=Color(201, 93, 17), width=2.5, cap=cap, join=join, dashes=(3.5, 2.25) if dashed else ()),
        Brush(color=Color(17, 93, 201, 128)),
        True,
        True,
        "evenodd",
        (ClipOperation(rectangle_path(Rect(9, -2, 30, 18)), "diff"),) if clipped else (),
        13,
    )


def _render(command: DrawPathCommand, size: tuple[int, int] = (32, 32), budget: FlattenBudget | None = None) -> Image.Image:
    """固定非整数矩阵以覆盖端点量化、曲线展平和裁剪后的隐藏颜色。"""
    return paths._render_path_command(
        command, Matrix(a=1.125, b=0.125, c=-0.125, d=0.875, e=-0.375, f=0.625), size, 1, budget or FlattenBudget()
    )


@pytest.mark.parametrize("kind", ["compound", "cubic", "empty"])
@pytest.mark.parametrize("cap", ["round", "flat", "square"])
@pytest.mark.parametrize("join", ["round", "bevel", "miter"])
@pytest.mark.parametrize("dashed", [False, True])
@pytest.mark.parametrize("clipped", [False, True])
def test_stroke_region_matches_prior_layer(
    kind: str,
    cap: Literal["round", "flat", "square"],
    join: Literal["round", "bevel", "miter"],
    dashed: bool,
    clipped: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不同 cap/join/虚线及裁剪组合均保留完整 RGBA 和生成点数计费。"""
    command = _command(kind, cap, join, dashed, clipped)
    before_budget, after_budget = FlattenBudget(), FlattenBudget()
    monkeypatch.setattr(paths, "_STROKE_REGION_MIN_PIXELS", 10**20)
    expected = _render(command, budget=before_budget)
    monkeypatch.setattr(paths, "_STROKE_REGION_MIN_PIXELS", 0)
    actual = _render(command, budget=after_budget)
    assert actual.tobytes() == expected.tobytes()
    assert after_budget.used == before_budget.used


@pytest.mark.parametrize("alpha", [0, 1, 127, 254])
def test_translucent_stroke_retains_original_source_over(alpha: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """半透明和全透明笔继续用原 source-over，不能按二值覆盖直接替换。"""
    command = _command("cubic", "round", "miter", True, True)
    command = replace(command, pen=replace(command.pen, color=Color(201, 93, 17, alpha)))
    monkeypatch.setattr(paths, "_STROKE_REGION_MIN_PIXELS", 10**20)
    expected = _render(command)
    monkeypatch.setattr(paths, "_STROKE_REGION_MIN_PIXELS", 0)
    assert _render(command).tobytes() == expected.tobytes()


@pytest.mark.parametrize("rop", [7, 11, 16])
def test_nondefault_rop_stroke_keeps_old_layer(rop: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """非默认 ROP 路径继续产生原始图层供后续逻辑合成。"""
    command = replace(_command("compound", "square", "bevel", True, True), rop2=rop)
    monkeypatch.setattr(paths, "_STROKE_REGION_MIN_PIXELS", 10**20)
    expected = _render(command)
    monkeypatch.setattr(paths, "_STROKE_REGION_MIN_PIXELS", 0)
    assert _render(command).tobytes() == expected.tobytes()


def test_actual_large_canvas_avoids_color_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认阈值下不再分配整幅描边颜色层，真实大画布字节仍完全一致。"""
    command = _command("compound", "round", "miter", True, True)
    size = (1025, 1025)
    monkeypatch.setattr(paths, "_STROKE_REGION_MIN_PIXELS", 10**20)
    expected = _render(command, size)
    monkeypatch.setattr(paths, "_STROKE_REGION_MIN_PIXELS", 1024 * 1024)
    original = Image.new

    def guarded(mode: str, dimensions: tuple[int, int], color: object = 0) -> Image.Image:
        """拦截旧描边颜色层，透明画布、填充及 L mask 照常分配。"""
        assert (mode, dimensions, color) != ("RGBA", size, command.pen.color.rgba())
        return original(mode, dimensions, color)

    monkeypatch.setattr(Image, "new", guarded)
    assert _render(command, size).tobytes() == expected.tobytes()


def test_stroke_region_preserves_budget_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """快路必须在原路径计费点失败，不能省略虚线、轮廓或裁剪点。"""
    command = _command("cubic", "round", "miter", True, True)
    measured = FlattenBudget()
    _render(command, budget=measured)
    for threshold in (0, 10**20):
        monkeypatch.setattr(paths, "_STROKE_REGION_MIN_PIXELS", threshold)
        _render(command, budget=FlattenBudget(limit=measured.used))
        with pytest.raises(MetafileResourceLimitError, match=f"max_flattened_points={measured.used - 1}"):
            _render(command, budget=FlattenBudget(limit=measured.used - 1))
