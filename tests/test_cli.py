"""验证安装后 CLI 的格式、诊断及文件写入边界。"""

import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Sequence

import pytest
from _metafile_test_utils import basic_wmf, build_emf, emf_record, emf_rectangle
from PIL import Image

from metafile_render import MetafileResourceLimitError, cli


def _run(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """通过安装后的模块入口执行真实子进程。"""
    return subprocess.run([sys.executable, "-m", "metafile_render", *arguments], capture_output=True, text=True, check=False)


@pytest.mark.parametrize("extension", ["svg", "png", "jpg", "jpeg", "webp", "WEBP"])
def test_cli_converts_all_extensions(tmp_path: Path, extension: str) -> None:
    """输出扩展名选择正确格式，尺寸提示由 API 生效。"""
    source = tmp_path / "input.wmf"
    source.write_bytes(basic_wmf())
    target = tmp_path / f"output.{extension}"
    result = _run([str(source), "-o", str(target), "--dpi", "144", "--size", "80", "60"])
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    if extension == "svg":
        assert b'data-metafile-render="wmf-emf"' in target.read_bytes()
    else:
        with Image.open(target) as image:
            assert image.size == (80, 60)
            assert image.format == ({"jpg": "JPEG", "jpeg": "JPEG"}.get(extension, extension.upper()))


def test_cli_preserves_output_unless_forced(tmp_path: Path) -> None:
    """默认保护已有文件，显式 force 才替换完整内容。"""
    source, target = tmp_path / "input.wmf", tmp_path / "output.png"
    source.write_bytes(basic_wmf())
    target.write_bytes(b"existing")
    result = _run([str(source), "-o", str(target)])
    assert result.returncode == 1
    assert target.read_bytes() == b"existing"
    result = _run([str(source), "-o", str(target), "--force"])
    assert result.returncode == 0, result.stderr
    assert target.read_bytes().startswith(b"\x89PNG")
    assert not list(tmp_path.glob(".*.tmp"))


def test_cli_rejects_same_file_even_with_force(tmp_path: Path) -> None:
    """输入输出同一文件时不得因 force 损坏源文件。"""
    source = tmp_path / "input.png"
    source.write_bytes(basic_wmf())
    result = _run([str(source), "-o", str(source), "--force"])
    assert result.returncode == 2
    assert source.read_bytes() == basic_wmf()


def test_cli_rejects_hardlink_to_input(tmp_path: Path) -> None:
    """不同路径指向同一 inode 时同样保护源文件。"""
    source, target = tmp_path / "input.wmf", tmp_path / "output.png"
    source.write_bytes(basic_wmf())
    target.hardlink_to(source)
    assert _run([str(source), "-o", str(target), "--force"]).returncode == 2
    assert source.read_bytes() == basic_wmf()


@pytest.mark.parametrize("arguments", [["--dpi", "0"], ["--dpi", "1201"], ["--size", "0", "30"]])
def test_cli_invalid_arguments_exit_two(tmp_path: Path, arguments: list[str]) -> None:
    """参数错误在访问源文件前以退出码二报告。"""
    result = _run([str(tmp_path / "missing.wmf"), "-o", str(tmp_path / "out.png"), *arguments])
    assert result.returncode == 2


def test_cli_rejects_unknown_extension(tmp_path: Path) -> None:
    """不支持的扩展名不得被默认当作 JPEG。"""
    assert _run([str(tmp_path / "input.wmf"), "-o", str(tmp_path / "out.pdf")]).returncode == 2


def test_cli_failed_conversion_leaves_existing_file_untouched(tmp_path: Path) -> None:
    """转换失败不会留下临时文件，也不会截断已有目标。"""
    source, target = tmp_path / "bad.emf", tmp_path / "out.png"
    source.write_bytes(b"bad")
    target.write_bytes(b"keep")
    result = _run([str(source), "-o", str(target), "--force"])
    assert result.returncode == 1
    assert target.read_bytes() == b"keep"
    assert not list(tmp_path.glob(".*.tmp"))
    missing = tmp_path / "new.png"
    assert _run([str(source), "-o", str(missing)]).returncode == 1
    assert not missing.exists()


def test_cli_reports_partial_results(tmp_path: Path) -> None:
    """部分渲染保留输出，并在 stderr 显示可定位诊断。"""
    source, target = tmp_path / "partial.emf", tmp_path / "out.png"
    source.write_bytes(build_emf([emf_record(118, b"\x00" * 16), emf_rectangle(5, 5, 95, 95)]))
    result = _run([str(source), "-o", str(target), "--backend", "replay"])
    assert result.returncode == 0
    assert "partial rendering" in result.stderr
    assert "unsupported_emf_record" in result.stderr
    assert target.exists()


def test_atomic_write_protects_racing_destination(tmp_path: Path) -> None:
    """目标在预检查之后出现时，原子发布仍不会覆盖它。"""
    target = tmp_path / "out.png"
    target.write_bytes(b"racing writer")
    with pytest.raises(FileExistsError):
        cli._write_output(target, b"new", overwrite=False)
    assert target.read_bytes() == b"racing writer"
    assert not list(tmp_path.glob(".*.tmp"))


def test_cli_bounds_input_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """文件超出输入预算时使用稳定资源异常停止读取。"""
    source = tmp_path / "large.wmf"
    source.write_bytes(b"123456")
    monkeypatch.setattr(cli, "MAX_METAFILE_BYTES", 4)
    with pytest.raises(MetafileResourceLimitError):
        cli._read_input(source)


def test_installed_console_script_and_module_version() -> None:
    """验证打包生成的 console script 与模块入口使用同一个版本。"""
    executable = shutil.which("metafile-render")
    assert executable is not None
    script = subprocess.run([executable, "--version"], capture_output=True, text=True, check=False)
    module = _run(["--version"])
    assert script.returncode == module.returncode == 0
    assert script.stdout == module.stdout == f"metafile-render {version('metafile-render')}\n"


def test_cli_emfplus_only_partial_and_error(tmp_path: Path) -> None:
    """安装入口可转换 Only 文件，并区分局部降级与无支持内容。"""
    from _emfplus_test_utils import fill_rect, plus_file, plus_record

    source, target = tmp_path / "only.emf", tmp_path / "only.webp"
    source.write_bytes(plus_file([plus_record(0x4023, flags=1), fill_rect()]))
    result = _run([str(source), "-o", str(target)])
    assert result.returncode == 0 and target.exists()
    assert "partial rendering" in result.stderr and "emfplus_rendering_approximation" in result.stderr
    source.write_bytes(plus_file([]))
    target.unlink()
    result = _run([str(source), "-o", str(target)])
    assert result.returncode == 1 and not target.exists()


def test_cli_default_resolution_and_replay_option(tmp_path: Path) -> None:
    """命令行省略 DPI 时使用 200，并允许显式选择解析回放。"""
    from io import BytesIO

    from PIL import Image

    source = tmp_path / "input.wmf"
    output = tmp_path / "output.png"
    source.write_bytes(basic_wmf())
    result = _run([str(source), "-o", str(output), "--backend", "replay"])
    assert result.returncode == 0
    with Image.open(BytesIO(output.read_bytes())) as image:
        assert image.size == (200, 200)
        assert image.info["dpi"] == pytest.approx((200, 200), abs=0.02)
    invalid = _run([str(source), "-o", str(tmp_path / "missing.png"), "--backend", "native"])
    assert invalid.returncode == 2 and not (tmp_path / "missing.png").exists()
