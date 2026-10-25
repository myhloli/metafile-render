# 第四轮：ROP、WMF 空槽与普通 EMF 点列

本轮完成三项生产实现及本机验收。基线为已发布 v0.4.0（`980876e`），候选为本轮结束时的冻结工作区。最终性能、安装和语料验收使用同一个标准 wheel 中的原生二进制。本报告记录本机验收结束时的证据；该批次随后纳入 0.4.1，提交、远端 CI 与发布状态见[执行状态](optimization-state-2026-10-25.md)。

## 实现

| 范围 | 实现 | 保留的边界 |
| --- | --- | --- |
| ROP | Rust 批量执行逐通道逻辑运算，直接写入最终 Python bytes，计算时释放 GIL | 原有裁剪、alpha、RGBA 合成顺序；全部操作真值表；空串、不等长输入及默认填充行为 |
| WMF 对象槽 | 首次未使用槽前沿与回收槽最小堆 | 最小空槽优先；重复、未使用及越界删除无操作；满表复用；已选对象引用和 SaveDC/RestoreDC 语义 |
| 普通 EMF 点列 | 16/32 位 Polyline、Polygon 复用现有解码与变换内核 | 不更新 current position；活动路径与闭合语义；先计费再分配；退化 extent、截断和坐标错误按既有状态机裁决 |

私有扩展协议升至 **3**，旧二进制明确拒绝并要求重建。`backend_info()` 新增 `rop_calls`、`rop_bytes`，点列计数继续独立保留。公开 API、绘制后端选择及设备/Python 支持策略不变。新增函数均有中文注释。

## 最终 wheel 配对结果

环境为 macOS arm64、CPython 3.14.4、Pillow 12.3.0、pyclipper 1.4.0。独立进程交替 AB/BA，5 对样本；热点样本预热后每次连续调用公开入口 5 次取平均。输入读取、指纹、诊断比较和身份记录在计时外。百分比取配对耗时比值中位数，不直接由表中两侧中位数相除；各批收益不能相加。

| 工作量 | v0.4.0 | 最终候选 | 配对耗时降低 |
| --- | ---: | ---: | ---: |
| 8 次 XOR 图形合成 | 184.09 ms | 20.34 ms | **88.96%** |
| 真实 generic-icon.emf | 3.474 ms | 0.568 ms | **83.78%** |
| 8000 个 WMF 对象 | 399.10 ms | 24.92 ms | **93.79%** |
| 2 万点普通 EMF 折线 | 31.24 ms | 20.70 ms | **33.87%** |

这组数字分别说明对应热点的收益，不能当作任意文件或生产流量的平均加速。

扩充语料按 5 对计时、3 对独立冷进程 RSS 测量：

| 工作量 | 之前总耗时中位数 | 之后总耗时中位数 | 配对变化 |
| --- | ---: | ---: | ---: |
| 全部可输出的 162 个格式组合，含控制组 | 11.118 s | 10.804 s | 耗时降低 **2.72%** |
| 主语料可输出的 126 个格式组合 | 7.110 s | 7.099 s | 耗时降低 0.08%，基本持平 |
| 全语料 ROP 分类交集，含控制组 | 1.635 s | 1.326 s | 耗时降低 **18.85%** |

整套语料进程 RSS 峰值中位数 570,114,048 → 565,657,600 bytes，配对降低约 0.74%，基本持平。该值不是单张图元内存占用。

原 `docx_01.doc` 配对为 645.86 → 647.55 ms，耗时约 +0.51%；39 图为 527.24 → 531.75 ms，约 +0.86%。该路径已使用 To 点列内核且不调用 ROP，本轮基本持平；文档结构、50 个素材及全部图元输出一致。

### 单例差异的处理

原 DOC 第 39 个图元 `b492901f25502e0dfd68-svg` 在完整语料顺序回放中为 6.374 → 8.480 ms，约 +2.11 ms。最终 wheel 的独立 9 对复测为 6.892 → 6.937 ms，配对约 +0.56%，未重现原来的增幅。直接 Cargo 构建阶段也记录过类似顺序差异；GC 插桩改变回放后的分配节奏，插桩中未重现同等增幅，因而没有确认差异的根因，也没有通过禁用 GC 改写生产行为。原始顺序结果、两次独立复测及诊断全部保留；本轮不声称每个案例都更快。

## 正确性、发行物与安装

- ROP 全部 65,536 个字节对按独立真值表校验；覆盖空串、不等长、默认操作、隐藏 RGB、全部关键 alpha 值和并发输出隔离。
- WMF 与首空槽线性参考逐步比较，覆盖预留、扩表、随机删除、重复删除、满表错误与复用；真实记录检查删除后已选对象及保存状态引用。
- 普通点列检查 16/32 位与活动路径、坐标变换、current position、输入预算、零 extent 诊断、截断与错误优先级。
- 最终实际二进制下 **740 passed、4 skipped**；4 项跳过需要 Windows 原生 GDI。Ruff check/format、mypy 55 个源码文件、Rust fmt、Release Clippy `-D warnings` 通过。
- ROP 批次、WMF 批次以及最终 wheel 的 180 个 SVG/PNG 组合均与冻结参考一致，包含完整 IR、输出字节、fallback PNG 字节及 RGBA、diagnostics、partial、拒绝类型与消息。9 个拒绝输入保留行为。
- 同一个 `cp311-abi3` wheel 在新的 CPython 3.11.15、3.14.4 环境分别通过 `pip check`、四种格式 API/CLI、39 图元及全部 180 组合回放。每个扩充回放实际执行 38,744 次 Release 点列调用、22 次 ROP 调用，原生校验失败为 0。
- wheel 的 57 个包文件与工作区一致。热点、DOC、扩充语料以及两个安装环境加载的原生二进制 SHA256 完全相同；早期直接 Cargo 构建的成绩保留为阶段记录，最终结论使用标准 wheel 重测结果。

wheel SHA256：`bb185285c79a18f20a37581d2f17bca375f98d1258057b1d2695864c607dd713`。

最终原生二进制 SHA256：`3f800074b77e14e2c4ff23fa24557854264e0372543b26ce962fe039b1750fcf`。

## 证据与状态

证据根目录：`/Users/myhloli/Documents/Codex/2026-10-25/metafile-hotspots-implementation/`。

- `sources/before/`、`sources/after-rop/`、`sources/after-wmf/`、`sources/final/`、`sources/final-package/`：分批及最终实际二进制快照。
- `results/rop/`、`results/wmf/`、`results/points/`：各批独立定向配对。
- `results/final-package/`、`results/doc-package/`、`results/corpus-package/`：最终 wheel 的正式配对与完整验证。
- `results/regression-isolated-package/`、`gc-before.json`、`gc-after.json`：单例独立复测与归因诊断。
- `dist/`、`artifact-audit-final.json`、`install-311/`、`install-314/`、`corpus-installed-311.json`、`corpus-installed-314.json`：发行物身份与安装回放。
- `final-summary.json`、`pytest-final.log`：汇总与最后一次完整测试。

本机验收结束时尚未修改版本号；报告中的 wheel 及指纹属于该冻结阶段。后续 0.4.1 的发行物、远端跨平台 CI、发布及实际安装分别验收，不用上述历史二进制指纹代替 0.4.1 发布产物。
