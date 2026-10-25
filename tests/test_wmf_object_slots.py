"""验证首空槽索引的公开句柄语义、删除边界与资源上限。"""

from dataclasses import replace
import random
import struct

import pytest
from _metafile_test_utils import build_placeable_wmf, wmf_record, wmf_rectangle

from metafile_render.binary import BoundedReader
from metafile_render.gdi import playback as playback_module
from metafile_render.gdi.playback import _Playback
from metafile_render.gdi.record_types import WmfRecord
from metafile_render.gdi.wmf import _handle_wmf_objects_record
from metafile_render.models import MetafileMalformedError, MetafileResourceLimitError
from metafile_render.parser import parse_metafile, prepare_metafile
from metafile_render.primitives import Brush, Color


@pytest.mark.parametrize("declared", [0, 1, 4, 16, 32])
def test_wmf_slots_match_first_free_oracle(declared: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """与线性首空槽参考逐步比较，覆盖预留、扩表、删除、重复删除和满表。"""
    monkeypatch.setattr(playback_module, "MAX_OBJECTS", 16)
    header = prepare_metafile(build_placeable_wmf([]), dpi=144, size_hint=None)
    playback = _Playback(replace(header, wmf_object_count=declared))
    expected: list[Brush | None] = [None] * min(declared, 16)
    rng = random.Random(1947 + declared)
    for step in range(600):
        if step < 20 or rng.random() < 0.6:
            value = Brush(color=Color(step % 256, step // 256, 1))
            free = next((index for index, item in enumerate(expected) if item is None), len(expected))
            if free == len(expected) == 16:
                with pytest.raises(MetafileResourceLimitError, match="^WMF object table exceeds max_objects=16$"):
                    playback.create_wmf_object(value)
            else:
                if free == len(expected):
                    expected.append(value)
                else:
                    expected[free] = value
                playback.create_wmf_object(value)
        else:
            handle = rng.randrange(24)
            if handle < len(expected):
                expected[handle] = None
            playback.delete_wmf_object(handle)
            playback.delete_wmf_object(handle)
        assert playback.wmf_objects == expected


def test_wmf_deleted_handles_reuse_in_order_and_keep_selected_objects() -> None:
    """真实记录按最小槽复用，删除不改变已选对象及 SaveDC 保存的对象引用。"""
    records = [
        wmf_record(WmfRecord.CREATEBRUSHINDIRECT, struct.pack("<HIH", 0, color, 0)) for color in (0x0000FF, 0x00FF00, 0xFF0000)
    ]
    records.extend([wmf_record(WmfRecord.SELECTOBJECT, struct.pack("<H", 2)), wmf_record(WmfRecord.SAVEDC)])
    records.extend(wmf_record(WmfRecord.DELETEOBJECT, struct.pack("<H", handle)) for handle in (2, 0, 0, 15, 65535))
    records.extend(
        wmf_record(WmfRecord.CREATEBRUSHINDIRECT, struct.pack("<HIH", 0, color, 0)) for color in (0x00FFFF, 0xFFFF00)
    )
    records.extend(
        [
            wmf_rectangle(10, 10, 50, 50),
            wmf_record(WmfRecord.SELECTOBJECT, struct.pack("<H", 0)),
            wmf_rectangle(20, 20, 60, 60),
            wmf_record(WmfRecord.RESTOREDC, struct.pack("<h", -1)),
            wmf_rectangle(30, 30, 70, 70),
            wmf_record(WmfRecord.SELECTOBJECT, struct.pack("<H", 2)),
            wmf_rectangle(40, 40, 80, 80),
        ]
    )
    document = parse_metafile(build_placeable_wmf(records))
    assert not document.diagnostics
    assert [command.brush.color for command in document.commands] == [
        Color(0, 0, 255),
        Color(255, 255, 0),
        Color(0, 0, 255),
        Color(0, 255, 255),
    ]


def test_wmf_full_table_reuses_deleted_slot_before_limit_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """满表删除后仍可复用，只有确实需要新槽时才报告原资源错误。"""
    monkeypatch.setattr(playback_module, "MAX_OBJECTS", 2)
    playback = _Playback(prepare_metafile(build_placeable_wmf([]), dpi=144, size_hint=None))
    first, second, replacement = Brush(), Brush(kind="null"), Brush(color=Color(1, 2, 3))
    playback.create_wmf_object(first)
    playback.create_wmf_object(second)
    with pytest.raises(MetafileResourceLimitError, match="max_objects=2"):
        playback.create_wmf_object(replacement)
    playback.delete_wmf_object(0)
    playback.create_wmf_object(replacement)
    assert playback.wmf_objects == [replacement, second]


def test_wmf_payload_errors_precede_slot_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """满表时仍先检查记录载荷，截断的创建或删除不能改动空槽索引。"""
    monkeypatch.setattr(playback_module, "MAX_OBJECTS", 1)
    playback = _Playback(prepare_metafile(build_placeable_wmf([]), dpi=144, size_hint=None))
    value = Brush()
    playback.create_wmf_object(value)
    for kind in (WmfRecord.CREATEBRUSHINDIRECT, WmfRecord.DELETEOBJECT):
        with pytest.raises(MetafileMalformedError):
            _handle_wmf_objects_record(kind, BoundedReader(b"\x00"), playback)
    assert playback.wmf_objects == [value]
    playback.delete_wmf_object(0)
    playback.create_wmf_object(value)
    assert playback.wmf_objects == [value]
