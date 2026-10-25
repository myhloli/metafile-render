"""构建必需的 CPython 3.11 ABI3 原生扩展。"""

import platform
import sys
import sysconfig

from setuptools import setup
from setuptools_rust import Binding, RustExtension

supported = (
    platform.python_implementation() == "CPython"
    and not sysconfig.get_config_var("Py_GIL_DISABLED")
    and (3, 11) <= sys.version_info[:2] <= (3, 14)
)
if not supported:
    raise RuntimeError("Native wheels require GIL-enabled CPython 3.11-3.14")

setup(
    rust_extensions=[
        RustExtension(
            "metafile_render._metafile_points",
            path="rust/Cargo.toml",
            binding=Binding.PyO3,
            py_limited_api=True,
            debug=False,
            optional=False,
            cargo_manifest_args=["--locked"],
        )
    ],
    options={"bdist_wheel": {"py_limited_api": "cp311"}},
    zip_safe=False,
)
