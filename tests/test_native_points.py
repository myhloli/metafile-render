"""验证 Rust 点列解码、数值结果及与原 Python 语义的衔接。"""

import math
import struct

import pytest
from _metafile_test_utils import build_emf
from test_mapping_batches import _poly_record

from metafile_render import _native_points as backend, limits
from metafile_render.binary import BoundedReader
from metafile_render.gdi import shapes
from metafile_render.gdi.emf import _handle_emf_poly
from metafile_render.gdi.playback import _Playback
from metafile_render.gdi.record_types import EmfRecord
from metafile_render.models import MetafileResourceLimitError
from metafile_render.parser import prepare_metafile
from metafile_render.primitives import Matrix

from metafile_render import _metafile_points as native


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("matrix", [Matrix(), Matrix(a=-1.25, b=0.125, c=0.5, d=2.0, e=3.25, f=-7.0)])
def test_native_points_match_python_double_operations(compact: bool, matrix: Matrix) -> None:
    """符号边界和非整数矩阵保持 Python double 的运算顺序与返回终点。"""
    bound = 32767 if compact else 2147483647
    points = [(-bound - 1, bound), (0, 0), (1, -1), (1023, 4096)]
    layout = "<2h" if compact else "<2i"
    payload = b"prefix" + b"".join(struct.pack(layout, *point) for point in points)
    result, last = native.decode_transform_points(
        payload, 6, len(points), compact, (matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f)
    )
    assert result == [matrix.transform_point(point) for point in points]
    assert last == points[-1]


def test_empty_native_batch_does_not_validate_unused_matrix() -> None:
    """零点批次不会提前检查本来没有使用的矩阵。"""
    assert native.decode_transform_points(b"", 0, 0, True, (math.nan, 0, 0, 1, 0, 0)) == ([], None)


@pytest.mark.parametrize("offset,count,compact", [(1, 1, True), (0, 2, True), (0, 1, False), (0, 1000001, True)])
def test_native_rejects_bad_ranges_before_allocation(offset: int, count: int, compact: bool) -> None:
    """越界长度或点数上限必须在生成大点列前拒绝。"""
    with pytest.raises(ValueError):
        native.decode_transform_points(b"\0" * 4, offset, count, compact, (1, 0, 0, 1, 0, 0))


@pytest.mark.parametrize("matrix", [(math.inf, 0, 0, 1, 0, 0), (math.nan, 0, 0, 1, 0, 0), (1e12, 0, 0, 1, 0, 0)])
def test_native_rejects_nonfinite_or_excessive_coordinates(matrix: tuple[float, ...]) -> None:
    """矩阵或结果超限时交回 Python 调用层重演公开错误。"""
    with pytest.raises(ValueError):
        native.decode_transform_points(struct.pack("<hh", 2, 0), 0, 1, True, matrix)


def test_native_build_has_protocol_and_profile() -> None:
    """原生发行物必须携带可核验的接口和构建模式。"""
    assert native.PROTOCOL_VERSION == 2
    assert native.BUILD_PROFILE in {"debug", "release"}


def test_input_budget_is_checked_before_native_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """累计点数超限必须先失败，不能先分配原生点数组。"""
    playback = _Playback(prepare_metafile(build_emf([]), dpi=200, size_hint=None))
    record = BoundedReader(_poly_record(EmfRecord.POLYLINETO16, [(1, 2), (3, 4)]))

    def forbidden() -> None:
        """若执行到加载器，说明原生路径抢在预算之前运行。"""
        raise AssertionError("native entered before budget")

    monkeypatch.setattr(limits, "MAX_TOTAL_POINTS", 1)
    monkeypatch.setattr(shapes, "get_native", forbidden)
    with pytest.raises(MetafileResourceLimitError, match="max_total_points=1"):
        _handle_emf_poly(EmfRecord.POLYLINETO16, record, playback)
    assert playback.context.budget.points == 2


def test_sliced_reader_has_no_owner_offset_assumption(monkeypatch: pytest.MonkeyPatch) -> None:
    """偏移不指向原 bytes 起点的只读视图仍生成相同命令并只计费一次。"""
    header = prepare_metafile(build_emf([]), dpi=200, size_hint=None)
    record_bytes = _poly_record(EmfRecord.POLYLINETO16, [(10, 20), (30, 40)])
    reference, actual = _Playback(header), _Playback(header)
    with monkeypatch.context() as local:
        # 只在测试内建立原状态机对照，发行物不提供 Python 后端开关。
        local.setattr("metafile_render.gdi.emf._read_native_to_points", lambda *args, **kwargs: None)
        _handle_emf_poly(EmfRecord.POLYLINETO16, BoundedReader(record_bytes), reference)
    calls = backend.backend_info()["native_calls"]
    _handle_emf_poly(EmfRecord.POLYLINETO16, BoundedReader(memoryview(b"prefix" + record_bytes)[6:]), actual)
    assert actual.commands == reference.commands
    assert actual.context.budget.points == reference.context.budget.points == 2
    assert actual.state.current_position == (30.0, 40.0)
    assert backend.backend_info()["native_calls"] == calls + 1
    backend.get_native.cache_clear()


def test_failed_native_validation_replays_python_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """目标点越界后回到 Python 重演，保留错误类型且不重复输入点计费。"""
    backend.get_native.cache_clear()
    playback = _Playback(prepare_metafile(build_emf([]), dpi=200, size_hint=None))
    playback.state.world_transform = Matrix(a=1e10)
    calls = backend.backend_info()["native_calls"]
    record = BoundedReader(_poly_record(EmfRecord.POLYLINETO16, [(0, 0), (101, 0)]))
    with pytest.raises(MetafileResourceLimitError, match="coordinate is non-finite or unreasonably large"):
        _handle_emf_poly(EmfRecord.POLYLINETO16, record, playback)
    assert playback.context.budget.points == 2
    assert not playback.commands
    assert backend.backend_info()["native_calls"] == calls + 1
    backend.get_native.cache_clear()
