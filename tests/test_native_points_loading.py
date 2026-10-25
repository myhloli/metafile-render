"""验证必需原生扩展的缺失、损坏、协议及公开入口失败边界。"""

from collections.abc import Iterator
from types import ModuleType

import pytest
from _metafile_test_utils import build_emf, emf_rectangle

from metafile_render import _native_points as backend, render_metafile


@pytest.fixture(autouse=True)
def clear_loader() -> Iterator[None]:
    """逐例清空惰性加载缓存，使安装故障独立可测。"""
    backend.get_native.cache_clear()
    yield
    backend.get_native.cache_clear()


@pytest.mark.parametrize("kind", ["missing", "dependency", "binary", "protocol", "previous", "oserror"])
def test_native_installation_fault_is_fatal(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """扩展不存在或不可用时明确失败，不降级成其他运行版本。"""

    def broken(name: str) -> ModuleType:
        """模拟不同安装故障，保留异常链供用户定位原因。"""
        if kind == "missing":
            raise ModuleNotFoundError("missing", name=name)
        if kind == "dependency":
            raise ModuleNotFoundError("dependency missing", name="native_dependency")
        if kind == "binary":
            raise ImportError("invalid binary")
        if kind == "oserror":
            raise OSError("cannot load shared library")
        module = ModuleType(name)
        module.PROTOCOL_VERSION = 2 if kind == "previous" else 1
        return module

    monkeypatch.setattr(backend, "import_module", broken)
    with pytest.raises(RuntimeError, match="compatible Rust native extension") as error:
        backend.get_native()
    assert error.value.__cause__ is not None


@pytest.mark.parametrize("output_format", ["png", "jpeg", "webp", "svg"])
@pytest.mark.parametrize("drawing_backend", ["auto", "replay"])
def test_every_valid_render_requires_extension(
    output_format: str, drawing_backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绘制后端与输出格式不能绕过必需扩展的安装检查。"""

    def missing(name: str) -> ModuleType:
        """模拟在有效输入之前尚未安装原生扩展。"""
        raise ModuleNotFoundError("missing", name=name)

    monkeypatch.setattr(backend, "import_module", missing)
    with pytest.raises(RuntimeError, match="compatible Rust native extension"):
        render_metafile(build_emf([emf_rectangle(1, 1, 20, 20)]), output_format=output_format, backend=drawing_backend)


@pytest.mark.parametrize("value", ["python", "auto", "rust", "invalid"])
def test_retired_environment_variables_do_not_select_a_backend(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """删除旧开关的运行分支，遗留环境值不能关闭必需内核。"""
    monkeypatch.setenv("METAFILE_RENDER_COMPUTE_BACKEND", value)
    monkeypatch.setenv("METAFILE_RENDER_BUILD_MODE", value)

    def available(name: str) -> ModuleType:
        """返回当前协议的扩展，以核验旧环境变量已不再参与决策。"""
        module = ModuleType(name)
        module.PROTOCOL_VERSION = 3
        module.BUILD_PROFILE = "release"
        return module

    monkeypatch.setattr(backend, "import_module", available)
    assert backend.backend_info()["backend"] == "rust"
    assert backend.get_native().PROTOCOL_VERSION == 3
