# 多来源图元性能语料与基线

已将原单份 DOC 的 39 个 EMF 扩充为 **90 份去重图元：75 EMF、15 WMF**。SVG/PNG 两种输出共 180 个组合，162 个组合可渲染并取得稳定的 5 次预热后计时及独立进程 RSS；18 个组合被当前解析器稳定拒绝。跨进程重复检查与冻结参考重放均通过，没有未捕获异常或超时。

本次只增加语料、工具和基线，没有修改生产解析或渲染算法。已有两轮优化和必需原生策略保持原样。

## 来源与分组

| 主组 | 唯一图元 | 完整渲染 | 部分渲染 | 当前解析器拒绝 |
| --- | ---: | ---: | ---: | ---: |
| Office 实际渲染调用 | 39 | 39 | 0 | 0 |
| Office 额外内嵌图元 | 3 | 3 | 0 | 0 |
| Office 模板图标 | 4 | 4 | 0 | 0 |
| 上游回归样本 | 22 | 13 | 4 | 5 |
| 上游生成控制样本 | 6 | 1 | 5 | 0 |
| 项目 GDI+ 生成控制样本 | 12 | 4 | 8 | 0 |
| 上游 fuzz/crash 鲁棒性样本 | 4 | 0 | 0 | 4 |
| 总计 | **90** | **64** | **17** | **9** |

各行按主组独占计数，同一文件的多个来源别名不重复增加唯一图元数量。主语料共 68 份，生成控制样本和鲁棒性样本不充当真实业务覆盖。上游回归来源也不意味着每个文件都来自生产文档。

已实际解析 DocVortex demo 的 DOC、DOCX、PPT、PPTX、XLS、XLSX、RTF、ODT、ODP、ODS 共 10 份文档，均成功。当前只有原 DOC 实际发出 39 次图元渲染请求，其余文档没有额外请求；额外 3 份图元来自 OfficeArt 内嵌资源，不能计成真实渲染调用。容器扫描按内容签名检查，而非只看扩展名。

公开来源固定于以下提交，下载同时校验原始 Git blob、大小和 SHA256：

- [LibreOffice/core](https://github.com/LibreOffice/core/tree/0ea8f79d15a62dfb5a0325640ed5771fc8b75ef2/emfio/qa/cppunit)：16 份，其中 10 份回归样本、6 份生成控制样本。
- [Apache POI](https://github.com/apache/poi/tree/4f3c25563919e7efa4a75feccf6cef1ac3d05a04/test-data/slideshow)：16 份，其中 12 份回归样本、4 份 fuzz/crash 样本。
- 当前安装的 `pypptx-with-oxml` Office 模板图标：4 份，记录包版本及来源。

原始二进制和生成输出保存在内置盘证据目录，仓库仅保存来源目录、哈希、工具和报告。上游许可证/NOTICE 链接随 `tests/corpus/upstream-metafiles.json` 记录。

## 实际指令覆盖

只统计成功输出的 SVG 对应唯一输入；类别可重叠。依据为解析得到的 IR，不根据文件名推断。

| 类别 | 主语料中可渲染样本 | 含控制组的全部可渲染样本 |
| --- | ---: | ---: |
| WMF | 10 | 10 |
| 文字 | 16 | 21 |
| 位图 | 6 | 10 |
| 非默认 ROP | 1 | 5 |
| 三次曲线 | 45 | 50 |
| 至少 1000 个路径点 | 27 | 28 |
| 裁剪 | 50 | 56 |
| EMF+ Dual | 3 | 14 |
| EMF+ Only | 0 | 6 |

原 39 图全部为普通 ROP2=13 路径，未覆盖文字和位图命令。扩充后 WMF、文字、位图和混合操作已有真实图元支持；真实 ROP 和 EMF+ Only 业务样本仍较少，不能用生成控制组替代这个缺口。

## 性能口径与观察

输入保留原始字节、实际 DPI 和尺寸策略；独立图元使用公开入口默认 200 DPI，不注入固定小画布。PNG 是额外基准格式，不必然是文档调用者原来的请求。

每个输入/格式在独立进程中验证输出，计时进程预热后运行 5 次，RSS 另用新进程读取。散列、IR 统计与文件输出放在计时区间之外。所有结果均为当前版本基线，不是跨版本成对加速成绩；后续优化仍须做 AB/BA 配对。

本机较慢的新场景包括：

| 来源 | 格式 | 输出尺寸 | 当前中位耗时约 |
| --- | --- | --- | ---: |
| LibreOffice `tdf88163-wrong-font-size.wmf` | PNG | 2133×1560 | 822 ms |
| POI `file-45.wmf` | SVG | 1357×1300 | 810 ms |
| LibreOffice `TestEmfPlusDrawLineWithDash.emf`（控制组） | PNG | 1919×2058 | 801 ms |
| LibreOffice `fdo79679-2.emf` | SVG | 1182×887 | 700 ms |

这些场景与原 DOC 的公式轮廓不同，可用于后续定位 WMF 文字、虚线和较大画布的成本。

## 拒绝与部分渲染

9 个输入被稳定拒绝，其中 4 个属于显式 fuzz/crash 组。其余为 POI `61338.wmf`、`VHZ2NYFUYUUJNGLABL26ORTQZA76FJEW.emf`、`bug69924.wmf`、`empty-polygon-close.wmf`，以及 LibreOffice `tdf93750.emf`。

当前错误涉及签名、记录/声明长度、复合路径或 EMF+ Header。这里只记录当前解析器的行为，不据此把所有文件判定为损坏，也没有在本任务中修复算法。17 个可输出的输入仍有 partial 诊断。输出稳定性不等于已经证明原文视觉忠实度。

## 复测入口

先构建当前必需 Rust 扩展，使用全新目录保存每次收集和测量：

```bash
python tools/fetch_corpus_sources.py tests/corpus/upstream-metafiles.json /path/to/new/upstream
python tools/collect_metafile_corpus.py --upstream /path/to/new/upstream/sources.json --office-root /path/to/docvortex/demo --docvortex-source /path/to/docvortex --template-icons --controls tests/fixtures/gdiplus --output /path/to/new/corpus
python tools/benchmark_corpus.py --corpus /path/to/new/corpus --output /path/to/new/baseline
python tools/benchmark_corpus.py --corpus /path/to/new/corpus --reference /path/to/new/baseline --verify-only --output /path/to/new/replay
```

`--reference` 比较当前实际输出、IR、指令统计和错误类型/信息，候选不能重新覆盖参考；差异返回非零。`--verify-only` 用于不运行性能采样的正确性回放。完整基线模式也可带 `--reference`，但单次历史时间差不会自动标记为加速结果。

本次证据根目录：`/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-real-corpus/`。

- `corpus/manifest.json`、`corpus/inputs/`：去重输入及来源/调用参数。
- `baseline/summary.json`：180 个组合的输出、计时、RSS、指令与拒绝结果。
- `baseline-runner.py`：首次基线使用的工具副本。
- `replay-current/summary.json`：独立冻结参考回放。
- `coverage.json`：分类汇总；`provenance.json`：来源身份。
- `sources/reference/`：与测量源代码树哈希一致的只读运行源码快照。

不提交、推送或发布本次修改；远端跨平台 CI 仍待执行。
