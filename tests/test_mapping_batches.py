"""验证点列矩阵复用不改变诊断、异常顺序、输入预算或记录间状态。"""

from __future__ import annotations

import struct

import pytest
from _metafile_test_utils import (
    build_emf,
    emf_begin_path,
    emf_end_path,
    emf_line_to,
    emf_move_to,
    emf_record,
    emf_restoredc,
    emf_savedc,
    emf_set_world_transform,
    emf_stroke_path,
)

from metafile_render.commands import DrawPathCommand
from metafile_render.context import ReplayContext
from metafile_render.gdi.record_types import EmfRecord
from metafile_render.models import MetafileResourceLimitError
from metafile_render.parser import parse_metafile


def _poly_record(record_type: int, points: list[tuple[int, int]]) -> bytes:
    """构造 16/32 位 EMF 点列记录，供折线和贝塞尔边界用例复用。"""
    bounds = (
        min((point[0] for point in points), default=0),
        min((point[1] for point in points), default=0),
        max((point[0] for point in points), default=0),
        max((point[1] for point in points), default=0),
    )
    layout = "<2h" if record_type >= 85 else "<2i"
    return emf_record(
        record_type,
        struct.pack("<4iI", *bounds, len(points)) + b"".join(struct.pack(layout, *point) for point in points),
    )


def _extreme_transform_records() -> list[bytes]:
    """用两个单独合法的矩阵生成只能在实际绘制时拒绝的累计变换。"""
    matrix = (1e10, 0.0, 0.0, 1e10, 0.0, 0.0)
    return [emf_set_world_transform(matrix), emf_record(36, struct.pack("<6fI", *matrix, 2))]


@pytest.mark.parametrize("record_type", [EmfRecord.POLYLINETO, EmfRecord.POLYLINETO16])
@pytest.mark.parametrize("active_path", [False, True])
def test_zero_extent_keeps_every_diagnostic_and_truncation(record_type: int, active_path: bool) -> None:
    """退化 POLYLINETO 保留两轮点映射及 current position 的全部诊断。"""
    context = ReplayContext()
    records = [emf_record(17, struct.pack("<i", 8)), emf_record(9, struct.pack("<2i", 0, 100))]
    if active_path:
        records.append(emf_begin_path())
    record_index = len(records) + 2
    record_offset = 88 + sum(len(record) for record in records)
    records.append(_poly_record(record_type, [(index, index % 10) for index in range(130)]))
    if active_path:
        records.extend([emf_end_path(), emf_stroke_path()])
    document = parse_metafile(build_emf(records), context=context)

    assert document.partial
    assert len(document.diagnostics) == 257
    assert all(item.code == "zero_window_extent" for item in document.diagnostics[:-1])
    assert all(
        (item.record_type, item.record_index, item.offset) == (record_type, record_index, record_offset)
        for item in document.diagnostics[:-1]
    )
    assert document.diagnostics[-1].code == "diagnostics_truncated"
    assert document.diagnostics[-1].message == "omitted 5 additional metafile diagnostics"
    assert context.budget.points == 130
    assert context.budget.commands == 1


@pytest.mark.parametrize("record_type", [2, 3, 4, 5, 6, 85, 86, 87, 88, 89])
def test_empty_point_records_do_not_validate_unused_transform(record_type: int) -> None:
    """没有实际点被消费时，不提前检查累计变换或生成新诊断。"""
    context = ReplayContext()
    document = parse_metafile(build_emf([*_extreme_transform_records(), _poly_record(record_type, [])]), context=context)

    assert not document.commands
    assert not document.diagnostics
    assert not document.partial
    assert context.budget.points == 0
    assert context.budget.commands == 0


@pytest.mark.parametrize("record_type", [2, 5, 85, 88])
def test_bezier_warning_keeps_order_before_transform_failure(record_type: int) -> None:
    """BezierTo 先报告点数问题，而普通 Bezier 先映射起点并可能直接失败。"""
    context = ReplayContext()
    to = record_type in {EmfRecord.POLYBEZIERTO, EmfRecord.POLYBEZIERTO16}
    points = [(index, index) for index in range(4 if to else 5)]

    with pytest.raises(MetafileResourceLimitError, match="^metafile cumulative transform exceeds coordinate budget$"):
        parse_metafile(build_emf([*_extreme_transform_records(), _poly_record(record_type, points)]), context=context)

    diagnostics = context.diagnostics.freeze()
    assert [item.code for item in diagnostics] == (["invalid_bezier_points"] if to else [])
    if to:
        assert diagnostics[0].record_type == record_type
        assert diagnostics[0].record_index == 4
        assert diagnostics[0].message == "PolyBezierTo point count is not a multiple of three"
    assert context.partial is to
    assert context.budget.points == len(points)
    assert context.budget.commands == 0


@pytest.mark.parametrize("record_type", [EmfRecord.POLYBEZIERTO, EmfRecord.POLYBEZIERTO16])
def test_truncated_bezier_to_never_maps_discarded_points(record_type: int) -> None:
    """不足一组三点的 BezierTo 仅报告点数问题，不触碰被截去点的变换。"""
    context = ReplayContext()
    document = parse_metafile(
        build_emf([*_extreme_transform_records(), _poly_record(record_type, [(1, 1), (2, 2)])]), context=context
    )

    assert not document.commands
    assert [item.code for item in document.diagnostics] == ["invalid_bezier_points"]
    assert context.budget.points == 2
    assert context.budget.commands == 0


@pytest.mark.parametrize("record_type", [2, 3, 4, 5, 6, 85, 86, 87, 88, 89])
def test_every_point_keeps_coordinate_budget(record_type: int) -> None:
    """前几个点合法也不能跳过后续坐标检查，失败仍按原始点数计费。"""
    context = ReplayContext()
    if record_type in {2, 85}:
        points = [(0, 0), (99, 0), (100, 0), (101, 0)]
    elif record_type in {EmfRecord.POLYBEZIERTO, EmfRecord.POLYBEZIERTO16}:
        points = [(99, 0), (100, 0), (101, 0)]
    else:
        points = [(0, 0), (100, 0), (101, 0)]
    records = [emf_set_world_transform((1e10, 0.0, 0.0, 1.0, 0.0, 0.0)), _poly_record(record_type, points)]

    with pytest.raises(MetafileResourceLimitError, match="^metafile coordinate is non-finite or unreasonably large$"):
        parse_metafile(build_emf(records), context=context)

    assert not context.diagnostics.freeze()
    assert context.budget.points == len(points)
    assert context.budget.commands == 0


@pytest.mark.parametrize("active_path", [False, True])
def test_mapping_batches_do_not_leak_across_state_records(active_path: bool) -> None:
    """世界与窗口变换、RestoreDC 以及 To 的逻辑终点分别影响后续记录。"""
    records = [emf_move_to(1, 1)]
    if active_path:
        records.append(emf_begin_path())
    records.extend(
        [
            _poly_record(88, [(2, 1), (3, 1), (4, 1)]),
            emf_savedc(),
            emf_set_world_transform((2.0, 0.0, 0.0, 3.0, 10.0, 20.0)),
            emf_record(17, struct.pack("<i", 8)),
            emf_record(9, struct.pack("<2i", 10, 10)),
            emf_record(11, struct.pack("<2i", 20, 30)),
            emf_record(EmfRecord.SETWINDOWORGEX, struct.pack("<2i", 2, 3)),
            emf_record(EmfRecord.SETVIEWPORTORGEX, struct.pack("<2i", 5, 7)),
            _poly_record(89, [(5, 1), (6, 1)]),
            emf_restoredc(),
            _poly_record(88, [(5, 1), (6, 1), (7, 1)]),
            emf_line_to(8, 1),
        ]
    )
    if active_path:
        records.extend([emf_end_path(), emf_stroke_path()])
    document = parse_metafile(build_emf(records))

    assert not document.diagnostics
    assert not document.partial
    paths = []
    for command in document.commands:
        assert isinstance(command, DrawPathCommand)
        paths.append(command.path)
    segments = [(segment.verb, segment.points) for path in paths for segment in path.segments]
    expected = [
        ("M", ((1.0, 1.0),)),
        ("C", ((2.0, 1.0), (3.0, 1.0), (4.0, 1.0))),
        ("M", ((37.0, 67.0),)),
        ("L", ((41.0, 67.0),)),
        ("L", ((45.0, 67.0),)),
        ("M", ((4.0, 1.0),)),
        ("C", ((5.0, 1.0), (6.0, 1.0), (7.0, 1.0))),
    ]
    if not active_path:
        expected.append(("M", ((7.0, 1.0),)))
    expected.append(("L", ((8.0, 1.0),)))
    assert segments == expected


@pytest.mark.parametrize("record_type", [7, 8, 90, 91])
def test_multi_polygon_batches_keep_subpath_boundaries(record_type: int) -> None:
    """同条记录中复用矩阵仍保留空多边形、各子路径起点和闭合语义。"""
    points = [(1, 2), (3, 4), (5, 6), (7, 8), (9, 10)]
    layout = "<2h" if record_type >= 85 else "<2i"
    payload = struct.pack("<4iII3I", 1, 2, 9, 10, 3, len(points), 2, 0, 3)
    payload += b"".join(struct.pack(layout, *point) for point in points)
    context = ReplayContext()
    document = parse_metafile(
        build_emf([emf_set_world_transform((2.0, 1.0, -1.0, 3.0, 5.0, 7.0)), emf_record(record_type, payload)]),
        context=context,
    )

    assert not document.diagnostics
    assert len(document.commands) == 1
    command = document.commands[0]
    assert isinstance(command, DrawPathCommand)
    close = record_type in {8, 91}
    expected = [("M", ((5.0, 14.0),)), ("L", ((7.0, 22.0),))]
    if close:
        expected.append(("Z", ()))
    expected.extend([("M", ((9.0, 30.0),)), ("L", ((11.0, 38.0),)), ("L", ((13.0, 46.0),))])
    if close:
        expected.append(("Z", ()))
    assert [(segment.verb, segment.points) for segment in command.path.segments] == expected
    assert command.fill is close
    assert context.budget.points == len(points)
    assert context.budget.commands == 1
