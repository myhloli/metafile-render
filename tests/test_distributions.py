"""验证五平台原生发行物门禁与本机安装选择。"""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from zipfile import ZipFile

from packaging.tags import sys_tags
import pytest

_TOOLS = Path(__file__).parents[1] / "tools"
sys.path.insert(0, str(_TOOLS))
_SPEC = importlib.util.spec_from_file_location("metafile_distribution_check", _TOOLS / "check_distributions.py")
assert _SPEC is not None and _SPEC.loader is not None
checker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(checker)
EXPECTED_PLATFORMS = sys.modules["verify_release_artifacts"].EXPECTED_PLATFORMS
verify_artifacts = sys.modules["verify_release_artifacts"].verify_artifacts
verify_wheel = sys.modules["verify_release_artifacts"].verify_wheel


def _wheel(directory: Path, platform: str, *, tag: str = "cp311-abi3", native: bool = True) -> Path:
    """构造包含真实结构元数据的门禁样本，不把伪二进制用于运行验证。"""
    path = directory / f"metafile_render-0.3.1-{tag}-{platform}.whl"
    info = "metafile_render-0.3.1.dist-info/"
    with ZipFile(path, "w") as package:
        package.writestr(info + "METADATA", "Name: metafile-render\nVersion: 0.3.1\nRequires-Python: >=3.11,<3.15\n")
        package.writestr(info + "WHEEL", f"Root-Is-Purelib: false\nTag: {tag}-{platform}\n")
        if native:
            suffix = "pyd" if platform.startswith("win_") else "abi3.so"
            package.writestr("metafile_render/_metafile_points." + suffix, b"native binary")
    return path


def test_complete_five_platform_set_is_required(tmp_path: Path) -> None:
    """发布集合必须覆盖所有五个平台，不能遗漏或重复。"""
    for platform in EXPECTED_PLATFORMS:
        _wheel(tmp_path, platform)
    assert len(verify_artifacts(tmp_path, "v0.3.1")) == 5
    next(tmp_path.iterdir()).unlink()
    with pytest.raises(AssertionError):
        verify_artifacts(tmp_path, "v0.3.1")


def test_install_selection_only_skips_supported_foreign_platforms(tmp_path: Path) -> None:
    """本机只安装兼容产物，但其他已支持平台的合法 wheel 仍需通过结构门禁。"""
    platforms = {tag.platform for tag in sys_tags()}
    local = next(p for p in EXPECTED_PLATFORMS if p in platforms)
    compatible = _wheel(tmp_path, local)
    foreign = _wheel(tmp_path, next(p for p in EXPECTED_PLATFORMS if p not in platforms))
    selected, skipped = checker.select_distributions(tmp_path)
    assert selected == [compatible]
    assert skipped == [foreign]


@pytest.mark.parametrize(
    "tag,platform,native",
    [
        ("py3-none", "any", False),
        ("cp310-abi3", "macosx_14_0_arm64", True),
        ("cp311-abi3", "macosx_14_0_arm64", False),
        ("cp311-abi3", "macosx_14_0_x86_64", True),
    ],
)
def test_rejects_compatibility_or_unsupported_wheels(tmp_path: Path, tag: str, platform: str, native: bool) -> None:
    """纯 wheel、旧 ABI、缺扩展及不支持的 Intel macOS 都不得进入发布或安装验收。"""
    with pytest.raises(AssertionError):
        verify_wheel(_wheel(tmp_path, platform, tag=tag, native=native))


def test_source_distribution_is_rejected(tmp_path: Path) -> None:
    """源码归档不能混入只发布原生 wheel 的目录。"""
    (tmp_path / "metafile_render-0.3.1.tar.gz").touch()
    with pytest.raises(AssertionError, match="Only native wheels"):
        checker.select_distributions(tmp_path)


def test_relative_install_output_uses_absolute_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """相对输出目录切换 cwd 后仍使用绝对解释器路径，避免重复拼接安装目录。"""
    import json

    monkeypatch.chdir(tmp_path)
    root = Path("validation")
    root.mkdir()
    distribution = tmp_path / "sample.whl"
    distribution.write_bytes(b"distribution")
    calls = []

    def create(builder: object, directory: Path) -> None:
        """隔离虚拟环境创建，只检查目标路径规范化，不在单测中安装依赖。"""
        assert directory.is_absolute()

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        """记录子进程路径并提供验证回执，所有进程均应使用同一绝对工作目录。"""
        assert Path(command[0]).is_absolute()
        assert kwargs["cwd"] == root.resolve()
        calls.append(command)
        if "--output" in command:
            output = Path(command[command.index("--output") + 1])
            assert output.is_absolute()
            output.mkdir()
            (output / "report.json").write_text(json.dumps({"backend": {"backend": "rust"}}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(checker.venv.EnvBuilder, "create", create)
    monkeypatch.setattr(checker.subprocess, "run", run)
    report = checker.check_distribution(distribution, root, None)
    assert len(calls) == 3
    assert report["distribution"] == distribution.name
