"""验证大画布不透明实心局部填充的像素、分支和固定预算。"""

from dataclasses import replace
from typing import Literal

from PIL import Image
import pytest

from metafile_render.backends import paths
from metafile_render.commands import DrawPathCommand
from metafile_render.geometry import FlattenBudget, PathBuilder, rectangle_path
from metafile_render.models import MetafileResourceLimitError
from metafile_render.primitives import Brush, ClipOperation, Color, Matrix, Pen, Rect


def _command(kind: str, clip: str, stroke: bool, rule: Literal["evenodd", "nonzero"]) -> DrawPathCommand:
    """构造空、孔洞或带小数曲线的填充，按需叠加虚线和裁剪。"""
    builder = PathBuilder()
    if kind == "compound":
        builder.extend(rectangle_path(Rect(-2.25, 2.75, 29.75, 28.25)))
        builder.extend(rectangle_path(Rect(7.25, 9.75, 19.75, 21.25)))
    elif kind == "cubic":
        builder.move_to((-2.25, 3.75))
        builder.cubic_to((7.5, -5.25), (24.75, 25.5), (32.25, 4.5))
        builder.line_to((14.25, 29.75))
        builder.close()
    return DrawPathCommand(
        builder.build(),
        Pen(color=Color(201, 93, 17, 128), width=2, cap="square", dashes=(3, 2)),
        Brush(color=Color(17, 93, 201)),
        stroke,
        True,
        rule,
        () if clip == "none" else (ClipOperation(rectangle_path(Rect(9, -2, 30, 18)), clip),),
        13,
    )


def _render(command: DrawPathCommand, size: tuple[int, int] = (32, 32), budget: FlattenBudget | None = None) -> Image.Image:
    """使用同一非整数矩阵和完整画布渲染，不裁剪输入几何。"""
    return paths._render_path_command(
        command, Matrix(a=1.125, b=0.125, c=-0.125, d=0.875, e=-0.375, f=0.625), size, 1, budget or FlattenBudget()
    )


@pytest.mark.parametrize("kind", ["compound", "cubic", "empty"])
@pytest.mark.parametrize("clip", ["none", "copy", "diff"])
@pytest.mark.parametrize("stroke", [False, True])
@pytest.mark.parametrize("rule", ["evenodd", "nonzero"])
def test_region_fill_matches_prior_layer_and_budget(
    kind: str, clip: str, stroke: bool, rule: Literal["evenodd", "nonzero"], monkeypatch: pytest.MonkeyPatch
) -> None:
    """快路与原填充层分支逐字节对照，覆盖孔洞、隐藏 RGB、混合描边和空 mask。"""
    command = _command(kind, clip, stroke, rule)
    expected_budget, actual_budget = FlattenBudget(), FlattenBudget()
    monkeypatch.setattr(paths, "_SOLID_FILL_REGION_MIN_PIXELS", 10**20)
    expected = _render(command, budget=expected_budget)
    monkeypatch.setattr(paths, "_SOLID_FILL_REGION_MIN_PIXELS", 0)
    actual = _render(command, budget=actual_budget)
    assert actual.tobytes() == expected.tobytes()
    assert actual_budget.used == expected_budget.used


@pytest.mark.parametrize("change", ["transparent", "translucent", "hatch", "null", "rop"])
def test_ineligible_fill_retains_original_pixels(change: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """半透明、hatch、null 和非默认 ROP 不能被强制替换成不透明 paste。"""
    command = _command("compound", "diff", True, "evenodd")
    if change in {"transparent", "translucent"}:
        command = replace(command, brush=Brush(color=Color(17, 93, 201, 0 if change == "transparent" else 128)))
    elif change == "hatch":
        command = replace(command, brush=Brush(kind="hatch", color=Color(17, 93, 201), hatch=0))
    elif change == "null":
        command = replace(command, brush=Brush(kind="null"))
    else:
        command = replace(command, rop2=7)
    monkeypatch.setattr(paths, "_SOLID_FILL_REGION_MIN_PIXELS", 10**20)
    expected = _render(command)
    monkeypatch.setattr(paths, "_SOLID_FILL_REGION_MIN_PIXELS", 0)
    assert _render(command).tobytes() == expected.tobytes()


def test_large_fill_avoids_intermediate_color_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认阈值下大画布不再分配整幅颜色层，实际 RGBA 仍与原层一致。"""
    command = _command("compound", "copy", False, "evenodd")
    size = (1025, 1025)
    monkeypatch.setattr(paths, "_SOLID_FILL_REGION_MIN_PIXELS", 10**20)
    expected = _render(command, size)
    monkeypatch.setattr(paths, "_SOLID_FILL_REGION_MIN_PIXELS", 1024 * 1024)
    original = Image.new

    def guarded(mode: str, dimensions: tuple[int, int], color: object = 0) -> Image.Image:
        """拦截旧颜色层分配，透明画布和 L mask 仍按原路径创建。"""
        assert (mode, dimensions, color) != ("RGBA", size, command.brush.color.rgba())
        return original(mode, dimensions, color)

    monkeypatch.setattr(Image, "new", guarded)
    assert _render(command, size).tobytes() == expected.tobytes()


def test_region_fill_preserves_exact_budget_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """局部像素写入发生在完整几何计费之后，不能跳过孔洞或裁剪预算。"""
    command = _command("cubic", "diff", True, "evenodd")
    measured = FlattenBudget()
    _render(command, budget=measured)
    for threshold in (0, 10**20):
        monkeypatch.setattr(paths, "_SOLID_FILL_REGION_MIN_PIXELS", threshold)
        _render(command, budget=FlattenBudget(limit=measured.used))
        with pytest.raises(MetafileResourceLimitError, match=f"max_flattened_points={measured.used - 1}"):
            _render(command, budget=FlattenBudget(limit=measured.used - 1))
