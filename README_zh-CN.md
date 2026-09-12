<div align="center">

<img src="https://raw.githubusercontent.com/myhloli/metafile-render/main/assets/metafile-render-overview.jpg" alt="Metafile Render — 将 WMF、EMF 转换为 SVG、PNG、JPEG 和 WebP" width="100%">

# Metafile Render

**让 Windows 图元文件轻松走进现代 Web。**

[![PyPI](https://img.shields.io/pypi/v/metafile-render)](https://pypi.org/project/metafile-render/)
[![Python](https://img.shields.io/pypi/pyversions/metafile-render)](https://pypi.org/project/metafile-render/)
[![CI](https://github.com/myhloli/metafile-render/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/myhloli/metafile-render/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/myhloli/metafile-render/blob/main/LICENSE)

[English](https://github.com/myhloli/metafile-render/blob/main/README.md) · **简体中文**

[快速开始](#快速开始) · [使用方法](#使用方法) · [参考文档](https://github.com/myhloli/metafile-render/blob/main/docs/reference.md)

</div>

## 项目简介

Metafile Render 将 Windows 图元文件（**WMF**）和增强型图元文件（**EMF**）
转换为 **SVG、PNG、JPEG 和 WebP**，适合在浏览器中展示旧版矢量图，
或处理从 Office 文档中提取的图元文件。

- **跨平台运行** — 支持 Linux、macOS 和 Windows。
- **安装简单** — 仅依赖 Pillow 和 pyclipper，无需 Office 或外部转换程序。
- **Python 与命令行** — 通过一个函数集成，也可直接在终端转换文件。
- **诊断清晰** — 可查看部分渲染、近似处理和跳过内容的详细信息。

## 快速开始

需要 **Python 3.10–3.14**。

```bash
pip install metafile-render
```

将已有的 EMF 文件转换为 SVG：

```bash
metafile-render input.emf -o output.svg
```

WMF 文件使用相同的命令，输出格式由文件扩展名决定。

## 使用方法

### Python

```python
from pathlib import Path
from metafile_render import render_metafile

result = render_metafile(
    Path("input.emf").read_bytes(),
    output_format="svg",
)
Path("output.svg").write_bytes(result.data)

print(result.width, result.height, result.media_type)
if result.partial:
    print("部分内容未完整渲染。")
for item in result.diagnostics:
    print(item.code, item.message)
```

`render_metafile()` 接收文件字节，返回图像字节、尺寸、格式信息和诊断结果。
默认输出为 **200 DPI** 的 PNG。

| Python 参数 | 默认值 | 用途 |
| :--- | :--- | :--- |
| `output_format` | `"png"` | 可选 `svg`、`png`、`jpeg` 或 `webp` |
| `dpi` | `None` → `200` | 所有输出（含 SVG）的分辨率，取值为 1–1200 的整数 |
| `size_hint` | `None` | 像素尺寸 `(width, height)`，适用于缺少物理尺寸的 WMF 等场景 |
| `backend` | `"auto"` | 自动选择后端；`"replay"` 强制使用跨平台回放引擎 |

画布限制可能使实际尺寸小于请求尺寸。完整返回字段和异常处理方式见
[API 参考](https://github.com/myhloli/metafile-render/blob/main/docs/reference.md#python-api)。

### 命令行

```bash
metafile-render input.emf -o output.png --dpi 300
metafile-render input.emf -o output.webp
metafile-render input.emf -o transparent.png --backend replay
metafile-render input.wmf -o sized.png --size 800 600
python -m metafile_render input.emf -o output.jpg --force
```

支持的扩展名：`.svg`、`.png`、`.jpg`、`.jpeg` 和 `.webp`。
输出目录须已存在，使用 `--force` 可覆盖已有输出文件；输入和输出必须为不同文件。
诊断信息写入 stderr。

## 渲染说明

| 主题 | 行为说明 |
| :--- | :--- |
| 后端 | SVG 始终使用跨平台回放。`auto` 模式下，Windows 对符合条件的栅格输出优先使用 Pillow 原生 GDI，其他平台使用回放；原生后端不可用或加载失败时回退到回放。 |
| 透明 | 回放引擎保留 PNG 和 WebP 的透明度；原生 GDI 和 JPEG 使用白色背景。 |
| SVG | 输出自包含，图像直接嵌入文件；部分操作会以栅格图嵌入 SVG。 |
| EMF+ | 支持常见 EMF+ Only 内容；Dual 文件使用 EMF 回退流。不保证完整的 GDI+ 还原效果。 |
| 字体 | 使用系统字体，缺失时可能替换；安装源文档所用字体可改善文字还原效果。 |

内容被近似处理或跳过时，会通过 `partial=True` 和 `diagnostics` 报告。
仅字体替换或后端回退不会使结果标记为部分渲染。
结构损坏、不支持或超出资源预算的输入可能抛出 `MetafileError`；
命令行转换出错时返回非零退出码，已完成的部分渲染返回 `0`。

## 文档与支持

- [技术参考（英文）](https://github.com/myhloli/metafile-render/blob/main/docs/reference.md) — 完整 API、命令行、输出格式、字体和资源限制。
- [开发指南（英文）](https://github.com/myhloli/metafile-render/blob/main/docs/reference.md#development) — 本地检查、内部架构、基准测试和发布流程。
- [问题反馈](https://github.com/myhloli/metafile-render/issues) — 请尽可能附上样本文件、转换参数和诊断信息。

## 许可证

[MIT](https://github.com/myhloli/metafile-render/blob/main/LICENSE) · Copyright © 2026 Xiaomeng Zhao (myhloli).
