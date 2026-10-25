"""发布前检查完整的五平台原生 wheel 集合，拒绝兼容包和源码发行物。"""

from __future__ import annotations

import argparse
from email.parser import BytesParser
from pathlib import Path
import zipfile


EXPECTED_PLATFORMS = frozenset(
    {
        "manylinux_2_28_x86_64",
        "manylinux_2_28_aarch64",
        "win_amd64",
        "win_arm64",
        "macosx_14_0_arm64",
    }
)


def verify_wheel(wheel: Path, expected_version: str | None = None, *, allow_local_linux: bool = False) -> str:
    """核对单个原生 wheel 的标签、元数据和扩展，返回已验证的平台。"""
    assert wheel.is_file() and wheel.suffix == ".whl", f"Only native wheels may be published: {wheel}"
    parts = wheel.name.removesuffix(".whl").split("-")
    assert len(parts) == 5, wheel.name
    name, version, interpreter, abi, platform = parts
    assert name == "metafile_render", wheel.name
    if expected_version is not None:
        assert version == expected_version, wheel.name
    assert (interpreter, abi) == ("cp311", "abi3"), wheel.name
    allowed = EXPECTED_PLATFORMS | ({"linux_x86_64", "linux_aarch64"} if allow_local_linux else set())
    assert platform in allowed, wheel.name
    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
        assert len(members) == len(set(members)), f"Duplicate wheel members: {wheel.name}"
        metadata_names = [name for name in members if name.endswith(".dist-info/METADATA")]
        wheel_names = [name for name in members if name.endswith(".dist-info/WHEEL")]
        expected_info = f"metafile_render-{version}.dist-info/"
        assert metadata_names == [expected_info + "METADATA"], wheel.name
        assert wheel_names == [expected_info + "WHEEL"], wheel.name
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
        wheel_metadata = BytesParser().parsebytes(archive.read(wheel_names[0]))
        assert metadata["Name"] == "metafile-render" and metadata["Version"] == version, wheel.name
        from packaging.specifiers import SpecifierSet

        assert SpecifierSet(metadata["Requires-Python"]) == SpecifierSet(">=3.11,<3.15"), wheel.name
        assert wheel_metadata["Root-Is-Purelib"] == "false", wheel.name
        assert wheel_metadata.get_all("Tag") == [f"cp311-abi3-{platform}"], wheel.name
        binaries = [
            name for name in members if name.startswith("metafile_render/_metafile_points.") and name.endswith((".so", ".pyd"))
        ]
        assert len(binaries) == 1 and archive.getinfo(binaries[0]).file_size > 0, wheel.name
        assert binaries[0].endswith(".pyd" if platform.startswith("win_") else ".so"), wheel.name
    return platform


def verify_artifacts(directory: Path, release_tag: str) -> list[Path]:
    """核对五平台完整集合，拒绝纯 wheel、源码包、重复平台或遗漏。"""
    artifacts = sorted(directory.iterdir())
    assert len(artifacts) == len(EXPECTED_PLATFORMS), artifacts
    platforms = [verify_wheel(wheel, release_tag.removeprefix("v")) for wheel in artifacts]
    assert len(set(platforms)) == len(platforms) and set(platforms) == EXPECTED_PLATFORMS, platforms
    return artifacts


def main() -> None:
    """读取待发布目录与标签，并打印已验证的具体 wheel 文件名。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("release_tag")
    arguments = parser.parse_args()
    for wheel in verify_artifacts(arguments.directory, arguments.release_tag):
        print(f"Verified release artifact: {wheel.name}")


if __name__ == "__main__":
    main()
