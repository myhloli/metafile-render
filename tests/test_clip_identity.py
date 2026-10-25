"""验证全覆盖裁剪省略 alpha 处理时的像素、缓存和预算语义。"""

import pytest
from PIL import Image, ImageChops

from metafile_render.backends import paths
from metafile_render.backends.session import RenderSession
from metafile_render.geometry import FlattenBudget, rectangle_path
from metafile_render.models import MetafileResourceLimitError
from metafile_render.primitives import ClipOperation, ClipStack, Matrix, Rect


def _rgba_layer() -> Image.Image:
    """构造含零 alpha 隐藏颜色及全部 alpha 值的确定性图层。"""
    raw = bytes(component for i in range(32 * 32) for component in (i % 251, i * 3 % 253, i * 7 % 255, i % 256))
    return Image.frombytes("RGBA", (32, 32), raw)


def _full_clip() -> ClipStack:
    """用越过四边的矩形产生全白 mask，避免边缘取整歧义。"""
    return (ClipOperation(rectangle_path(Rect(-2, -2, 34, 34)), "copy"),)


def test_full_coverage_keeps_all_rgba_and_avoids_alpha_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """全覆盖裁剪不得改变隐藏 RGB，也不应再提取整个 alpha 通道。"""
    layer = _rgba_layer()
    expected = layer.tobytes()
    original = Image.Image.getchannel

    def guarded(image: Image.Image, channel: int | str) -> Image.Image:
        """只拦截目标图层的 alpha 提取，其他 Pillow 操作照常执行。"""
        if image is layer:
            raise AssertionError("identity clip extracted alpha")
        return original(image, channel)

    monkeypatch.setattr(Image.Image, "getchannel", guarded)
    budget = FlattenBudget()
    paths._apply_clip(layer, _full_clip(), Matrix(), budget)
    assert layer.tobytes() == expected
    assert budget.used > 0


@pytest.mark.parametrize("mode", ["copy", "and", "or", "xor", "diff"])
def test_combine_modes_keep_exact_alpha_multiplication(mode: str) -> None:
    """各 combine mode 的最终 mask 仍决定像素，不能只凭第一条矩形跳过。"""
    clip = (
        *_full_clip(),
        ClipOperation(rectangle_path(Rect(8, 8, 23, 23)), mode),
    )
    expected = _rgba_layer()
    mask = paths._clip_mask(clip, Matrix(), expected.size, FlattenBudget())
    assert mask is not None
    expected.putalpha(ImageChops.multiply(expected.getchannel("A"), mask))
    actual = _rgba_layer()
    paths._apply_clip(actual, clip, Matrix(), FlattenBudget())
    assert actual.tobytes() == expected.tobytes()


def test_cached_identity_mask_is_copied_and_metadata_survives() -> None:
    """命中缓存仍返回独立图片，修改调用方副本不能污染后续身份判断。"""
    with RenderSession() as session:
        first = paths._clip_mask(_full_clip(), Matrix(), (32, 32), session.flatten, session)
        assert first is not None
        charged = session.flatten.used
        first.putpixel((10, 10), 0)
        actual = _rgba_layer()
        expected = actual.tobytes()
        paths._apply_clip(actual, _full_clip(), Matrix(), session.flatten, session)
        assert actual.tobytes() == expected
        assert session.flatten.used == charged
        assert session.cache.hits == 1


@pytest.mark.parametrize("cache_limit", [0, 1024 * 1024])
def test_identity_clip_keeps_flatten_budget_failure(cache_limit: int) -> None:
    """即使最终 mask 全白，首次生成几何仍必须在相同预算处失败。"""
    measured = FlattenBudget()
    paths._clip_mask(_full_clip(), Matrix(), (32, 32), measured)
    with RenderSession(cache_limit=cache_limit) as session:
        session.flatten = FlattenBudget(limit=measured.used - 1)
        with pytest.raises(MetafileResourceLimitError):
            paths._apply_clip(_rgba_layer(), _full_clip(), Matrix(), session.flatten, session)


def test_non_rgba_keeps_original_missing_alpha_error() -> None:
    """内部误传无 alpha 模式时仍保留原来的 Pillow 错误。"""
    with pytest.raises(ValueError):
        paths._apply_clip(Image.new("RGB", (32, 32)), _full_clip(), Matrix(), FlattenBudget())
