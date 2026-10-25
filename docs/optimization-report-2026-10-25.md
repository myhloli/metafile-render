# Metafile Render 真实 DOC 优化验收报告

本轮本机验收完成。保留矩阵复用、不透明填充 alpha 简化和 Rust 批量点列内核；公开渲染参数不变。相对 `7d12210`，真实 DOC 的配对耗时降低 **21.2%**，39 个实际 EMF 的重放耗时降低 **23.7%**，达到计划的首轮 20% 目标。

代码尚未提交、推送或发布。Linux/Windows 原生 wheel 的远端 CI 已配置，尚未实际运行。

## 输入和测量口径

- 原始输入：DocVortex `demo/ms_office_docs/docx_01.doc`，SHA256 `6aae60b01f0e020c59d2299f217dbd5904df08182a31384db40953937f322fd1`。
- 固定 DocVortex `1e7bc45a`，对比 metafile-render `7d12210` 与当前工作区。
- 39 个不同 EMF、946 条路径命令，200 DPI、SVG、实际 `size_hint=None`，按 EMF 头部确定尺寸。
- Apple M4 / macOS arm64，Python 3.14.4，Pillow 12.3.0，pyclipper 1.4.0。
- 每份样本均为独立新进程，导入后先预热，再执行无插桩测量；5 对 AB/BA 交替计时，3 对独立进程 RSS。正确性、IR 和安装验证分别执行。
- 耗时降低取每对 `after / before` 比值的中位数。下表绝对耗时为各变体的中位数，不能将各批增量相加。

## 最终累计结果

| 指标 | 初始实现 | 最终实现 | 配对变化 |
| --- | ---: | ---: | ---: |
| 完整 DOC 解析 | 958.0 ms | 755.6 ms | 耗时降低 21.15% |
| 39 个 EMF 重放 | 845.7 ms | 644.0 ms | 耗时降低 23.66% |
| DOC 进程 RSS 高水位 | 124.5 MiB | 122.6 MiB | 中位配对值降低 1.59% |
| 39 图进程 RSS 高水位 | 81.6 MiB | 83.5 MiB | 中位配对值增加 2.28% |

39 图的 RSS 增长未超过计划门槛。原始报告保留每个样本，包含单次较高峰值；这些高水位值不代表累计分配量。

严格比较通过：39 图完整 IR、SVG 字节、内嵌 PNG 字节及 RGBA、尺寸、格式、partial、完整 diagnostics，以及 DOC 的 ModelJSON、MiddleJSON、素材集合与字节。DOC 仍为 3 个 section、50 个素材，实际调用清单、DPI、次数和尺寸策略保持一致。既有公式边缘贴边现象仍保留为基线行为。

证据根目录：

`/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-optimization/`

- [最终配对原始报告](/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-optimization/results/final-release-cumulative/summary.json)
- [39 图 IR 比较](/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-optimization/ir-final.json)
- [汇总清单](/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-optimization/final-summary.json)

## 保留的实现

### B0 基线工具

`tools/benchmark_pair.py` 保护冻结参考和独立候选目录，记录解释器、源码、资源及实际原生模块身份，比较失败返回非零。`--require-native-after` 要求候选实际执行 release 内核，防止把未命中或回退误记为 Rust 成绩。计时、正确性和 RSS 分离，原始参考与 runner 均有冻结副本。

### B1A 矩阵复用

`point_mapper()` 仅在当前点批次中惰性复用组合矩阵，继续逐点检查坐标。空点列不提前触发错误，零 window extent 仍走原路径，保留逐次诊断和截断摘要。没有引入跨记录的可变矩阵缓存。

独立增量：DOC 降低 10.16%，39 图降低 12.07%。

### B2A alpha 简化

普通 ROP2=13、不透明实心填充时，直接把填充 mask 作为 alpha，省去恒定 255 通道的提取与乘法。保留完整图层、原有 alpha_composite、裁剪及预算。

独立增量：相对 B1A，DOC 再降低 6.06%，39 图再降低 6.30%。

### B3 Rust 点列内核

Rust 一次解码、校验和变换一条 To 记录的 PointS/PointL 点数组。Python 继续负责状态机、逻辑 current position、PathBuilder 和诊断。

- 原有输入点计费先执行，再进入原生分配。
- 仅接管完整正常点列；零 extent、空列和 Bezier 截断等情况保留原路径。
- 传递经边界验证的紧凑点数组 bytes，消除原型的 owner/base_offset 假设。
- 原生坐标校验失败后在同一 handler 的 Python 后半段重演，不重复计费，保留异常顺序和局部路径行为。
- 使用 PyO3 0.28.2、`abi3-py310`、release 构建，没有全局实验 hook。
- `auto` 仅在扩展确实缺失时回退；损坏或协议不匹配明确失败。内部 `python` / `rust` 选择与公开绘制后端独立。

相对优化后的 Python 实现，安全接入的独立增量为 DOC 降低 **6.08%**、39 图降低 **6.63%**。最终验证进程实际记录 17,722 次原生调用、151,644 个点、0 次原生校验回退；其中包含独立图元及完整 DOC 两轮正确性验证。

## 未纳入的候选

| 候选 | 实测或依据 | 处理 |
| --- | --- | --- |
| POLYLINETO 预映射复用 | DOC 约 1.84%、39 图约 1.50% 增量，处于 A/A 波动内 | 回退，保留隔离候选 |
| 直接 paste 填充 | DOC 慢 9.84%、39 图慢 14.66% | 淘汰 |
| 最终合成 bbox 裁框 | DOC 约 1.57%、39 图约 1.76% 增量，未超过噪声 | 回退 |
| SVG 变换与序列化融合 | 在 Rust 基础上约 0.75% / 1.14% 增量 | 不纳入 |
| 按需 finalize bounds | 语义验证通过，原始归因耗时很小，未认定整体收益 | 暂缓 |

ROP 内核、字体缓存、图片编解码和 WMF 对象槽算法没有进入本轮主要语料的适用热点，继续留在后续范围。

## 回归和发行物验收

- 完整测试：强制 Rust、Python 配置分别 **362 passed，4 skipped**；跳过项为本机无法运行的 Windows 原生测试。
- mypy：55 个源文件通过；Ruff check/format、Rust fmt、release Clippy `-D warnings`、actionlint、Twine 均通过。
- 三组控制基准的配对 after/before 比值分别约为 0.980（重复位图）、0.928（重复裁剪）、0.935（填充描边）；67 组控制及 fixture 的跨格式输出指纹一致。
- 同一原生 wheel 在独立 Python **3.10.20 / 3.14.4** 环境通过四种 API/CLI 输出及 39 图严格回放，实际导入来自安装环境并执行 release Rust 内核。
- 纯 Python wheel、强制原生的 sdist 安装、移除 Rust 编译器后的自动 sdist 回退均通过四种 API/CLI 输出及 39 图严格回放。
- 最终 wheel 的 56 个 Python/typing 文件与工作区一致；纯 wheel 无原生库；sdist 的 Rust 源码、锁文件、构建脚本与工作区一致。

发行策略保留 setuptools，使用可选 setuptools-rust 扩展。`native` 构建必须成功，`python` 构建生成纯 wheel，源码默认 `auto`。发布流程收集 Linux x86-64、macOS arm64、Windows x64 的原生 wheel，同时提供纯 wheel 覆盖其他环境；以上远端构建尚未运行。

相关证据：`controls-final/summary.json`、`installed-final-310/report.json`、`installed-final-314/report.json`、`install-pure-sdist-314/summary.json`、`install-sdist-no-rust/summary.json`、`artifact-audit-final.json`。最终产物位于 `release-artifacts/`，尚未上传。
