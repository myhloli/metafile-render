"""验证稀疏合成缩小范围时的 alpha、隐藏 RGB 和原始错误。"""

from PIL import Image
import pytest

from metafile_render.backends import composite


@pytest.mark.parametrize("position", [(0, 0), (7, 0), (0, 6), (7, 6), (3, 2), None, "full"])
@pytest.mark.parametrize("destination_alpha", [0, 1, 127, 255])
def test_region_matches_pillow_for_all_source_alphas_and_repeated_composition(
    position: object, destination_alpha: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """四边、全空和满框均对照原 Pillow；连续合成覆盖所有源 alpha 值。"""
    monkeypatch.setattr(composite, "_COMPOSITE_REGION_MIN_PIXELS", 0)
    source = Image.new("RGBA", (32, 32), (19, 43, 71, 0))
    if position == "full":
        source.paste((13, 47, 97, 129), (0, 0, 32, 32))
    elif position is not None:
        x, y = position
        source.paste(
            Image.frombytes(
                "RGBA", (16, 16), bytes(c for alpha in range(256) for c in (alpha, alpha * 3 % 256, alpha * 7 % 256, alpha))
            ),
            (x, y),
        )
    expected = Image.new("RGBA", source.size, (97, 53, 29, destination_alpha))
    actual = expected.copy()
    before = source.tobytes()
    for _ in range(3):
        expected.alpha_composite(source)
        composite._alpha_composite_rgba(actual, source)
        assert actual.tobytes() == expected.tobytes()
    assert source.tobytes() == before


def test_large_sparse_path_exercises_default_threshold() -> None:
    """真实大画布路径无需测试开关即走局部合成，并保留画布外透明 RGB。"""
    source = Image.new("RGBA", (1025, 1025), (17, 59, 127, 0))
    source.paste((31, 83, 191, 97), (250, 245, 257, 252))
    actual = Image.new("RGBA", source.size, (19, 73, 157, 0))
    expected = actual.copy()
    expected.alpha_composite(source)
    composite._alpha_composite_rgba(actual, source)
    assert actual.tobytes() == expected.tobytes()


@pytest.mark.parametrize("mode", ["RGB", "L"])
def test_non_rgba_keeps_pillow_error(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """误传非 RGBA 模式仍交由 Pillow 检查，不能因空覆盖而掩盖错误。"""
    monkeypatch.setattr(composite, "_COMPOSITE_REGION_MIN_PIXELS", 0)
    source = Image.new(mode, (10, 10))
    with pytest.raises(ValueError) as reference:
        Image.new("RGBA", source.size).alpha_composite(source)
    with pytest.raises(type(reference.value), match=str(reference.value)):
        composite._alpha_composite_rgba(Image.new("RGBA", source.size), source)


def test_small_canvas_does_not_scan_bbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """原小画布热路径保持直接合成，避免为稀疏检测增加整图扫描。"""

    def forbidden(*args: object, **kwargs: object) -> None:
        """小画布若提取 bbox，说明阈值保护已失效。"""
        raise AssertionError("small canvas scanned bbox")

    monkeypatch.setattr(Image.Image, "getbbox", forbidden)
    canvas = Image.new("RGBA", (31, 29), (19, 73, 157, 0))
    composite._alpha_composite_rgba(canvas, Image.new("RGBA", canvas.size, (31, 83, 191, 97)))
    assert canvas.getpixel((10, 10)) == (31, 83, 191, 97)


def test_near_full_bbox_avoids_large_roi_copies(monkeypatch: pytest.MonkeyPatch) -> None:
    """接近整幅的覆盖框应直接合成，不能再创建两幅接近整图的 crop。"""
    source = Image.new("RGBA", (1025, 1025), (17, 59, 127, 0))
    source.paste((31, 83, 191, 97), (1, 1, 1024, 1024))
    actual = Image.new("RGBA", source.size, (19, 73, 157, 0))
    expected = actual.copy()
    expected.alpha_composite(source)

    def forbidden(*args: object, **kwargs: object) -> None:
        """整图合成不需要 crop，任何局部副本都会使测试失败。"""
        raise AssertionError("dense source cropped")

    monkeypatch.setattr(Image.Image, "crop", forbidden)
    composite._alpha_composite_rgba(actual, source)
    assert actual.tobytes() == expected.tobytes()


def test_sparse_roi_bounds_three_temporary_images(monkeypatch: pytest.MonkeyPatch) -> None:
    """稀疏覆盖时，三份局部像素容量不超过原整幅合成结果。"""
    monkeypatch.setattr(composite, "_COMPOSITE_REGION_MIN_PIXELS", 0)
    source = Image.new("RGBA", (32, 32), (17, 59, 127, 0))
    source.paste((31, 83, 191, 97), (10, 10, 18, 18))
    original = Image.Image.crop
    areas = []

    def counted(image: Image.Image, box: tuple[int, int, int, int] | None = None) -> Image.Image:
        """记录 Pillow 实际创建的两份 crop，而不改写像素处理。"""
        result = original(image, box)
        areas.append(result.width * result.height)
        return result

    monkeypatch.setattr(Image.Image, "crop", counted)
    composite._alpha_composite_rgba(Image.new("RGBA", source.size), source)
    assert len(areas) == 2 and all(area * 3 < 32 * 32 for area in areas)
