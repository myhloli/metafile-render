# Metafile Render 优化执行状态

状态：第四轮热点优化及本机验收已完成，当前发布目标为 **v0.4.1**。跨平台 CI 和发布状态分别见 [Actions](https://github.com/myhloli/metafile-render/actions/workflows/ci.yml)、[GitHub Release](https://github.com/myhloli/metafile-render/releases/tag/v0.4.1) 和 [PyPI](https://pypi.org/project/metafile-render/0.4.1/)。

第四轮已完成 ROP Rust 字节内核、WMF 首空槽索引及普通 EMF Polyline/Polygon 点列扩展，私有协议为 3；本机 740 项测试、Python 3.11/3.14 独立 wheel 安装与 180 组合回放通过。该批次纳入 0.4.1，提交、远端 CI 和发布结果分别验收。完整性能、单例差异和产物身份见[第四轮验收报告](optimization-round4-report-2026-10-25.md)。

当前支持策略：启用 GIL 的 CPython 3.11–3.14、cp311-abi3、DocVortex 同款五平台原生 wheel；纯 Python 兼容包、运行切换和 sdist 发布已移除。详见[原生发行策略](native-only-policy-2026-10-25.md)。历史验收记录中的 3.10、cp310 和纯包方案已被本次用户指示替代。

[第三轮验收报告](optimization-round3-report-2026-10-25.md) · [第三轮计划](optimization-round3-plan-2026-10-25.md) · [第二轮验收报告](optimization-round2-report-2026-10-25.md) · [首轮验收报告](optimization-report-2026-10-25.md) · [优化计划](optimization-plan-2026-10-25.md)

## 当前成果

- 真实语料已扩大为 90 个去重输入（75 EMF、15 WMF），180 个 SVG/PNG 组合的当前版本基线已完成。
- 其中主语料 68 个、生成控制 18 个、鲁棒性控制 4 个；81 个输入可渲染，9 个稳定拒绝，全部结果可独立重放。
- 新语料包含 WMF、文字、位图和 ROP；partial/拒绝范围在第三轮保持一致。详见[语料与基线报告](real-corpus-report-2026-10-25.md)。
- 第三轮保留大画布稀疏合成、不透明实心填充/描边的局部像素写入，以及临时副本容量保护。
- 相对第三轮前：主语料耗时降低 29.12%，WMF 降低 41.62%，文字降低 33.03%，位图降低 22.15%；类别只统计主语料交集。
- 全部可输出组合耗时降低 24.78%，整个语料进程峰值 RSS 降低 19.37%。原 DOC/39 图元配对耗时变化约 −0.89% / +0.74%，在噪声内基本持平。
- 最新完整测试：619 passed、4 Windows GDI 项跳过；独立 CPython 3.11/3.14 原生 wheel 均通过四格式 API/CLI、39 图元及扩充语料 180 组合回放。
- 第三轮证据位于 `/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-optimization-round3/`，已保留原始配对、最终发行物和独立第三轮补丁。

## 第二轮历史成果

- 第二轮新增全覆盖裁剪的恒等 alpha 处理省略，以及单次 SVG 内最多 4096 项的数字格式化缓存。
- 相对第一轮：DOC 823.2 → 705.4 ms，配对耗时降低 14.64%；39 图 663.1 → 555.8 ms，降低 16.11%。
- 相对最初版本的独立累计比较：DOC 降低 32.11%，39 图降低 37.03%；完整输出、诊断、素材与 IR 一致。
- 第二轮当时的完整测试：Rust / Python 配置分别 384 passed、4 skipped；当时的原生/纯 wheel、源码安装与 3.10/3.14 回放通过。该兼容发行方案已由后续必需原生策略替代。
- 第二轮证据位于 `/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-optimization-round2/`，第一轮证据保留。

## 首轮成果

- 已保留 B0 基准工具、B1A 矩阵复用、B2A 不透明 alpha 简化、B3 Rust 批量点列内核。
- 最终累计 A/B：DOC 958.0 → 755.6 ms，配对耗时降低 21.15%；39 图 845.7 → 644.0 ms，降低 23.66%。
- 39 图完整 IR、SVG、PNG 像素与字节、诊断，以及 DOC 语义和 50 个素材均严格一致。
- Rust / Python 配置各 362 passed、4 Windows 项跳过；mypy、Ruff、Rust fmt/Clippy、actionlint、Twine 通过。
- 原生 wheel 在 Python 3.10.20 和 3.14.4 实际安装、执行并通过 39 图回放；纯 wheel、原生源码安装与无 Rust 编译器的源码回退也通过。
- 首轮 B1B、整图直接 paste、无阈值 bbox 裁框和 SVG 融合候选未保留；第三轮单独验证的大画布局部方案见新报告。已有贴边现象未混入性能批次修复。

## 工作区与证据

优化前基点为 `7d12210d8840d94ecaf2d0ea416f0ecb87e47989`，完整实现纳入 v0.4.0 交付变更。新增原生 crate 位于 `rust/`，加载器为 `src/metafile_render/_native_points.py`。公共 `backend=auto/replay` 含义保持不变。

最新证据目录：`/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-optimization-round3/`，入口为 `final-summary.json`、`results/final-vs-before/`、`results/doc-control/` 和 `delivery-snapshot/round3.patch`。

首轮历史证据目录：`/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-optimization/`。

- `reference/` 与 `sources/before-7d12210/`：只读初始参考。
- `results/final-release-cumulative/summary.json`：最终原始配对结果。
- `final-summary.json`、`ir-final.json`、`controls-final/summary.json`：汇总、IR 和控制语料。
- `installed-final-310/`、`installed-final-314/`：最终原生 wheel 安装验收。
- `install-pure-sdist-314/`、`install-sdist-no-rust/`：纯 wheel 与源码回退验收。
- `release-artifacts/`、`artifact-audit-final.json`：最终本机构建产物及来源校验。
- `pause-20261025-1419/`：上次暂停的历史恢复快照，不能覆盖当前新增 Rust 和打包改动。

## 后续边界

Linux/Windows/macOS 原生 wheel 由 CI 构建并安装验证；本机 4 个 Windows 原生 GDI 测试跳过，执行结果以 Windows CI 为准。提交、推送、发布和线上安装须分别验收，实时结果见顶部链接。历史优化报告记录的是各轮结束时的状态。

继续工作时先核对当前 diff 和验收报告，不要重新套用历史暂停快照或已淘汰的候选。正式计时继续使用独立进程、反序配对和严格输出比较，强制原生测量使用 `--require-native-after`。

外置工作卷空间有限，构建、Cargo target 和测试缓存继续使用内置盘证据目录；Python 环境可沿用 `.venv4`，mypy 在证据目录 `qa-packages/`。所有新增函数与方法继续使用中文注释。
