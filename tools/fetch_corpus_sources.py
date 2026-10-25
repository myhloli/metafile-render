"""下载固定提交中的公开回归图元，核验 Git blob 和 SHA256 后生成收集器输入。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen


def fetch_sources(catalog: Path, output: Path, *, max_bytes: int = 2 * 1024 * 1024) -> list[dict]:
    """逐文件下载到新目录，拒绝超限或来源内容变化，不覆盖已有语料。"""
    specification = json.loads(catalog.read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=False)
    sources = []
    for entry in specification["entries"]:
        if entry["bytes"] > max_bytes:
            raise ValueError(f"upstream input exceeds download limit: {entry['path']}")
        request = Request(entry["url"], headers={"User-Agent": "metafile-render-corpus/1"})
        with urlopen(request, timeout=30) as response:
            data = response.read(max_bytes + 1)
        if len(data) != entry["bytes"] or len(data) > max_bytes:
            raise ValueError(f"upstream input length changed: {entry['path']}")
        blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        sha = hashlib.sha256(data).hexdigest()
        if blob != entry["git_blob"] or sha != entry["sha256"]:
            raise ValueError(f"upstream input hash changed: {entry['path']}")
        path = output / (sha + PurePosixPath(entry["path"]).suffix.lower())
        path.write_bytes(data)
        sources.append(
            {
                "path": str(path.resolve()),
                "sha256": sha,
                "group": entry["group"],
                "origin": {key: entry[key] for key in ("repository", "commit", "path", "git_blob", "url")},
            }
        )
    (output / "sources.json").write_text(json.dumps(sources, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return sources


def main() -> None:
    """提供独立于 GitHub 连接器的固定语料重建入口。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    sources = fetch_sources(args.catalog, args.output)
    print(json.dumps({"downloaded": len(sources), "sources": str(args.output / "sources.json")}))


if __name__ == "__main__":
    main()
