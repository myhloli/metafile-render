# Metafile Render 第二轮优化验收报告

第二轮本机验收完成，保留两项改动：全覆盖裁剪的恒等 alpha 处理省略，以及单次 SVG 输出内有界的数字格式化复用。Rust 内核、公开参数、渲染尺寸和输出策略保持原样。

## 本轮结果

同机、独立新进程预热后测量，5 对 AB/BA 计时、3 对独立 RSS；数字缓存另作 9 对复测。耗时变化采用配对比值中位数。初始参考输出继续使用相同的 DOC 和 39 个实际 EMF，200 DPI、SVG、原始 `size_hint=None`。

| 对比 | 指标 | Before 中位数 | After 中位数 | 配对耗时降低 |
| --- | --- | ---: | ---: | ---: |
| 第二轮相对第一轮 | 完整 DOC | 823.2 ms | 705.4 ms | **14.64%** |
| 第二轮相对第一轮 | 39 图 | 663.1 ms | 555.8 ms | **16.11%** |
| 最终相对初始 `7d12210` | 完整 DOC | 970.7 ms | 659.0 ms | **32.11%** |
| 最终相对初始 `7d12210` | 39 图 | 854.0 ms | 544.2 ms | **37.03%** |

各行来自独立成对运行，不能混用绝对时间，也不能把批次百分比相加。两轮本轮累计比较均通过完整 DOC 语义、素材、调用清单、SVG、PNG 字节和 RGBA、尺寸、partial、完整 diagnostics 的严格核对；39 图完整 IR 也与第一轮一致。

本轮相对第一轮的 RSS 配对中位值：DOC 增加约 **0.08%**，39 图增加约 **1.51%**，未超过原计划 5% 的退化门槛。

## C1 全覆盖裁剪

重新归因发现裁剪应用约占第一轮 39 图重放的 15%。实际库存统计为 946 次裁剪应用，其中 943 次的最终 mask 全为 255，涉及约 1.76 亿像素。

实现仅在所有裁剪几何和预算计费完成后，对生成的 mask 分类。分类结果随内部图片副本缓存；全白 mask 对 RGBA alpha 是恒等乘法，因此省去 alpha 提取、乘法和写回。混合 mask、其他图层模式继续走原实现。

保留了原有缓存副本和 LRU 语义，没有借出可变缓存图像，也没有省略裁剪栈验证、几何生成或预算收费。测试覆盖隐藏 RGB、全部 alpha 值、combine modes、缓存副本污染、预算失败和错误模式。

独立配对增量：DOC 降低 **10.75%**，39 图降低 **12.43%**。

## C2 单次 SVG 数字复用

真实语料约 144,888 次数字格式化调用中，单次 SVG 的 4096 项缓存可复用约 106,796 次，约占 74%；实测单个 SVG 最大占用 3166 项。

仅对路径和 clipPath 的数字格式化复用原 `_svg_number()`，保留原变换、四位舍入、负零处理、字符串分隔、路径顺序和逐段预算检查。缓存不跨 SVG 输出共享，且严格有界。

初次增量为 DOC 约 2.97%、39 图约 4.79%。由于幅度较小，追加 9 对复测：全部配对同向改善，中位增量为 DOC **3.26%**、39 图 **4.33%**，因此纳入。本轮没有重新采用上一轮收益不足的路径变换/序列化融合方案。

测试覆盖精确格式、非有限数异常、命中缓存后的预算失败、独立文档生命周期和高唯一坐标的容量边界。

## 回归与发行物

- Rust / Python 配置分别 **384 passed、4 skipped**；跳过项仍为本机无法运行的 Windows 原生测试。
- mypy 55 个源文件、Ruff check/format、actionlint 通过。
- 3 对控制基准的 after/before 中位比值：重复位图 1.001、重复裁剪 1.000、填充描边 1.003，均处于噪声范围；67 组控制/fixture 输出指纹一致。
- 更新后的原生 wheel 在独立 Python 3.10.20 和 3.14.4 环境通过四种 API/CLI 输出和 39 图严格回放。
- 更新后的纯 Python wheel 和原生源码安装也通过相同验证；56 个 Python/typing 包文件与当前工作区一致。
- 本轮不修改 Rust 源码、ABI、加载协议或构建模式。远端跨平台 CI 仍待实际运行；尚未提交、推送或发布。

## 证据

根目录：`/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-optimization-round2/`。

- `sources/round1/`：本轮开始时的只读源码快照。
- `profile-round1/`、`clip-inventory.json`、`number-inventory.json`：重新归因及工作量统计。
- `results/aa/`：本轮 A/A 噪声校准；DOC 中位差异约 0.03%、39 图约 0.22%，原始数据保留异常样本。
- `results/opaque-clip/`、`results/svg-number-cache/`、`results/svg-number-cache-confirm/`：独立增量及复测。
- [相对第一轮累计报告](/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-optimization-round2/results/final-vs-round1/summary.json)
- [相对初始版本累计报告](/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-optimization-round2/results/final-vs-initial/summary.json)
- `ir-final.json`、`controls-final/summary.json`：IR 与控制语料。
- `install-native-310/`、`install-native-314/`、`install-pure-sdist-314/`：安装环境与验证报告。
- `release-artifacts/`、`final-summary.json`：本机构建产物及汇总。
