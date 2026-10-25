"""验证语料去重、固定来源校验及跨版本参考不能被候选覆盖。"""

import argparse
from io import BytesIO
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
import benchmark_corpus as runner
from collect_metafile_corpus import Corpus, vector_format
import fetch_corpus_sources as fetcher


def test_collection_deduplicates_bytes_but_preserves_parameters_and_origins(tmp_path: Path) -> None:
    """相同输入的不同尺寸/DPI 和多次出现不能被误算成多份唯一图片。"""
    corpus = Corpus(tmp_path / "corpus")
    data = b"\xd7\xcd\xc6\x9a" + b"payload"
    kwargs = {"output_format": "svg", "dpi": 200, "size_hint": None, "backend": "replay"}
    first = corpus.add(data, kwargs, {"group": "office_runtime", "source": "one.doc"})
    assert corpus.add(data, kwargs, {"group": "office_embedded", "source": "one.doc"}) == first
    assert corpus.add(data, dict(kwargs, dpi=144), {"group": "upstream_regression"}) != first
    corpus.save()
    manifest = json.loads((corpus.output / "manifest.json").read_text())
    assert len(manifest["assets"]) == 1 and len(manifest["cases"]) == 2
    assert len(manifest["cases"][0]["origins"]) == 2
    assert vector_format(data) == "wmf"
    with pytest.raises(FileExistsError):
        Corpus(corpus.output)


def test_declared_malformed_control_is_not_silently_dropped(tmp_path: Path) -> None:
    """损坏回归文件虽无合法签名，也可按明确来源保留为拒绝结果样本。"""
    corpus = Corpus(tmp_path / "corpus")
    assert corpus.add(b"broken", {}, {"group": "unknown"}) is None
    assert corpus.add(b"broken", {}, {"group": "robustness"}, declared="wmf") is not None
    assert len(corpus.assets) == 1


def _catalog(tmp_path: Path, data: bytes) -> Path:
    """构造固定哈希的下载清单，以独立测试内容漂移和流量上限。"""
    path = tmp_path / "catalog.json"
    entry = {
        "url": "https://example.invalid/input.wmf",
        "bytes": len(data),
        "git_blob": hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "path": "samples/input.wmf",
        "repository": "example/repo",
        "commit": "fixed",
        "group": "upstream_regression",
    }
    path.write_text(json.dumps({"entries": [entry]}))
    return path


def test_download_checks_both_hashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同长度替换也必须被内容哈希拒绝，不能只验证文件大小。"""
    catalog = _catalog(tmp_path, b"abcde")

    def changed(*args: object, **kwargs: object) -> BytesIO:
        """模拟上游返回了同尺寸但内容已变化的文件。"""
        return BytesIO(b"ABCDE")

    monkeypatch.setattr(fetcher, "urlopen", changed)
    with pytest.raises(ValueError, match="hash changed"):
        fetcher.fetch_sources(catalog, tmp_path / "download")


def test_download_read_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """响应体超出清单时只读取上限加一，避免无界收集。"""
    catalog = _catalog(tmp_path, b"abc")
    reads = []

    class Response(BytesIO):
        """记录下载器请求的读取长度。"""

        def read(self, size: int = -1) -> bytes:
            """禁止不带上限读取模拟的大响应。"""
            reads.append(size)
            return super().read(size)

    def oversized(*args: object, **kwargs: object) -> Response:
        """返回远大于清单的响应，检查请求长度而非内存大小。"""
        return Response(b"x" * 10000)

    monkeypatch.setattr(fetcher, "urlopen", oversized)
    with pytest.raises(ValueError, match="length changed"):
        fetcher.fetch_sources(catalog, tmp_path / "download", max_bytes=4)
    assert reads == [5]


def test_reference_comparison_ignores_identity_but_not_output() -> None:
    """解释器路径可不同，但输出字节变化仍属于契约差异。"""
    before = {"signature": {"sha256": "original"}, "identity": {"path": "before"}}
    after = {"signature": {"sha256": "original"}, "identity": {"path": "after"}}
    assert runner.verification_contract(before) == runner.verification_contract(after)
    after["signature"]["sha256"] = "changed"
    assert runner.verification_contract(before) != runner.verification_contract(after)


def test_candidate_self_consistency_cannot_replace_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """即使候选验证、计时和 RSS 都返回同样新字节，也必须对旧参考报错。"""
    corpus = Corpus(tmp_path / "corpus")
    corpus.add(b"\xd7\xcd\xc6\x9a" + b"data", {"dpi": 200}, {"group": "office_runtime"})
    corpus.save()
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    reference = tmp_path / "reference"
    reference.mkdir()
    variant = next(iter(corpus.cases))[:20] + "-svg"
    runner.write_json(
        reference / "summary.json",
        {
            "corpus_manifest_sha256": runner.digest((corpus.output / "manifest.json").read_bytes()),
            "cases": [{"variant": variant, "verification": {"signature": {"sha256": "old"}}}],
        },
    )

    def launch(*args: object, **kwargs: object) -> dict:
        """所有新结果自洽，但刻意与冻结参考不同。"""
        return {"identity": {}, "signature": {"sha256": "new"}}

    monkeypatch.setattr(runner, "launch", launch)
    args = argparse.Namespace(
        corpus=corpus.output,
        source=source,
        output=tmp_path / "results",
        reference=reference,
        repeats=1,
        timeout=1,
        formats="svg",
        python=sys.executable,
        verify_only=False,
    )
    assert runner.baseline(args) == 1
    result = json.loads((args.output / "summary.json").read_text())
    assert result["cases"][0]["stable_output"]
    assert result["cases"][0]["reference_difference"] is not None
