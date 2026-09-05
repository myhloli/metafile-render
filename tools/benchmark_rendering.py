"""用确定性输入记录转换耗时、Python 分配峰值与进程 RSS 峰值。"""

from __future__ import annotations

import argparse
import json
import statistics
import struct
import sys
import time
import tracemalloc
from importlib.metadata import version
from pathlib import Path


def make_cases() -> dict[str, bytes]:
    """复用源代码测试构造器生成重复位图、裁剪和贝塞尔图形场景。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from _metafile_test_utils import (
        build_emf,
        emf_create_brush,
        emf_create_pen,
        emf_intersect_clip_rect,
        emf_record,
        emf_rectangle,
        emf_select_object,
        emf_stretch_dib,
    )

    objects = [
        emf_create_brush(1, 0x00804020),
        emf_create_pen(2, 0x00204080, width=2),
        emf_select_object(1),
        emf_select_object(2),
    ]
    return {
        "repeated_bitmap": build_emf([emf_stretch_dib()] * 80),
        "repeated_clip": build_emf(
            [
                *objects,
                emf_intersect_clip_rect(0, 0, 95, 95),
                emf_intersect_clip_rect(5, 5, 100, 100),
                *[emf_rectangle(i % 30, i % 30, 80, 80) for i in range(80)],
            ]
        ),
        "fill_and_stroke": build_emf(
            [*objects, *[emf_record(42, struct.pack("<4i", i % 30, i % 30, 90, 90)) for i in range(60)]]
        ),
    }


def peak_rss_bytes() -> int | None:
    """在提供 resource 的平台读取进程 RSS 高水位，明确区别于 Python 堆。"""
    try:
        import resource
    except ImportError:
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def main() -> None:
    """计时与分配追踪分开运行，允许在旧版环境执行同一输入基准。"""
    from metafile_render import render_metafile

    parser = argparse.ArgumentParser()
    parser.add_argument("--replay", action="store_true", help="force replay; omit for pre-0.3 comparisons on non-Windows")
    parser.add_argument("--repeats", type=int, default=5)
    arguments = parser.parse_args()
    if arguments.repeats < 1:
        parser.error("repeats must be positive")
    results: dict[str, object] = {"version": version("metafile-render"), "platform": sys.platform}
    for name, data in make_cases().items():

        def convert() -> None:
            """两边使用相同 144 DPI 和目标尺寸，隔离默认值变化。"""
            if arguments.replay:
                render_metafile(data, dpi=144, size_hint=(160, 160), backend="replay")
            else:
                render_metafile(data, dpi=144, size_hint=(160, 160))

        convert()
        elapsed = []
        for _ in range(arguments.repeats):
            start = time.perf_counter()
            convert()
            elapsed.append(time.perf_counter() - start)
        tracemalloc.start()
        convert()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        results[name] = {
            "median_seconds": statistics.median(elapsed),
            "peak_python_bytes": peak,
            "process_peak_rss_bytes": peak_rss_bytes(),
        }
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()

__all__ = ["main"]
