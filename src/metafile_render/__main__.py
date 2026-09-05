# Copyright (c) 2026 Xiaomeng Zhao (myhloli)
# SPDX-License-Identifier: MIT
"""支持 python -m metafile_render。"""

from .cli import main

__all__: list[str] = []

if __name__ == "__main__":
    raise SystemExit(main())
