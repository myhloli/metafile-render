# Metafile Render 真实 DOC 性能优化计划

日期：2026-10-25。状态：已恢复并完成首轮本机验收；完整 DOC 与 39 图配对耗时分别降低 21.2% 和 23.7%。实现、逐批裁决和发行物依据见[验收报告](optimization-report-2026-10-25.md)。远端跨平台 CI 尚未执行。

以 DocVortex 的 `demo/ms_office_docs/docx_01.doc` 为主要性能语料，先减少重复矩阵计算和栅格图层处理，再根据剩余热点决定 Rust 批量内核的范围。每批独立比较性能、输出和内存，并保留可回退的实现边界。

## 基线与目标

### 固定输入与运行身份

| 项目 | 基线 |
| --- | --- |
| metafile-render | `7d12210d8840d94ecaf2d0ea416f0ecb87e47989`，0.3.1 |
| DocVortex | `1e7bc45aa7feb50bf305be46dbefe718f91bbfee`，0.6.1 |
| 原始 DOC | `demo/ms_office_docs/docx_01.doc`，1,863,680 字节 |
| DOC SHA256 | `6aae60b01f0e020c59d2299f217dbd5904df08182a31384db40953937f322fd1` |
| 环境 | Apple M4，macOS arm64，Python 3.14.4，Pillow 12.3.0，pyclipper 1.4.0 |
| 实际调用 | 39 次、39 份不同 EMF；`output_format="svg"`、`dpi=200`、`backend="auto"` |
| 图片尺寸 | 捕获的 39 次 `size_hint` 均为 `None`，由 EMF 头部与 200 DPI 确定；重放不注入固定尺寸 |
| 绘制内容 | 946 条路径命令，72,020 个路径点，20,235 个三次曲线段；全部 `ROP2=13` |
| 当前结果 | 3 个 DOC section、50 个素材；39 图均无 partial、无 PNG 重试 |

公式外观在这组输入中由路径轮廓组成，没有文字绘制命令，也没有位图绘制命令。SVG 请求始终走 replay，因此本基线不测量 Windows 原生 GDI。

初始证据目录：

`/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-doc-baseline/`

- [summary.json](/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-doc-baseline/summary.json)：环境、工作量、汇总和已知现象。
- [capture.json](/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-doc-baseline/capture.json)：原始输入指纹、实际参数、调用顺序及返回诊断。
- [document_timing.json](/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-doc-baseline/document_timing.json)：完整 DOC 无插桩计时。
- [document_attribution.json](/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-doc-baseline/document_attribution.json)：同一次 DOC 调用内的渲染占比。
- [metafile_timing.json](/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-doc-baseline/metafile_timing.json)：39 图重放、阶段时间和指令统计。
- `inputs/`、`rendered/`、`fallback_png/`、`document/`：EMF、SVG、PNG、ModelJSON、MiddleJSON 和素材。

这些绝对路径指向本地证据，不是运行项目所需的依赖。B0 将提供可参数化的重放入口；原始语料和大产物继续单独保存。

### 已测数据与解释

| 指标 | 实测值 | 测量口径 |
| --- | ---: | --- |
| 完整 DOC 首次解析 | 1,123 ms | 新进程完成 import 后的首次调用；不等同于冷磁盘缓存 |
| 完整 DOC 预热后解析 | 973 ms | 5 次中位数，范围 949–1,001 ms |
| 39 图独立重放累计 | 837 ms | 5 轮中位数，输入预加载；逐图计时后才校验输出 |
| DOC 内 metafile 占比 | 88.3% | 同一次插桩调用中测得；对应 DOC 约 1,020 ms、metafile 约 900 ms |
| EMF 准备与预扫描 | 23.6 ms | 39 图阶段累计中位数 |
| EMF 解析回放 | 266.2 ms | 同上 |
| SVG 内嵌 PNG 生成 | 469.4 ms | 包含栅格回放与 PNG 编码 |
| 栅格回放 | 418.8 ms | 包含路径绘制、裁剪、合成 |
| 路径绘制 | 349.9 ms | 包含路径变换、展平、mask 与图层操作 |
| 贝塞尔细分 | 28.9 ms | 已包含于上述栅格阶段 |
| 单次 DOC 进程 RSS 峰值 | 120.4 MiB | 独立新进程；在散列、导出和序列化前读取 |

嵌套阶段不能相加。无插桩计时、阶段插桩和 cProfile 的结果分别保存，不能混算收益；cProfile 中约 10.25 万次 `logical_matrix()` 调用用于定位重复计算，不将其 profiler 耗时当作真实速度基线。

首轮建议目标为：完整 DOC 和 39 图重放耗时均降低 **20%**，对应本机初始基线约 **778 ms** 和 **670 ms**。这是工程目标，不是已确认收益，也不是每一批必须达到的门槛。正式判定使用 B0 的同机成对比值；不能直接拿未来一次运行与上述绝对时间比较。

如果仅 Python 优化已经满足目标，且 Rust 增量低于噪声或维护成本不合理，可在已验收的批次停止。每批结果同时相对前一批和最初基线报告，Rust 收益只能归属于 Rust 接入前后的增量。

## 不变契约

支持范围例外：用户后续已明确选择参考 DocVortex，改为 CPython 3.11–3.14、cp311-abi3、五平台必需原生 wheel，取消纯 Python 兼容发行物。此新指示覆盖下列原方案中的 Python 3.10 与回退策略，见[原生发行策略](native-only-policy-2026-10-25.md)。

1. 保持 `render_metafile()` 的公共参数、返回模型和 `backend="auto"/"replay"` 路由语义。Rust 计算实现的选择不复用这个绘制后端参数。
2. 保持真实输入的调用顺序、次数、DPI、尺寸、输出格式、PNG fallback 和超采样策略。降低分辨率、移除 fallback 或跳过绘图不计为优化。
3. 首轮采用同平台、同依赖版本的严格输出相等：SVG/PNG 字节、PNG RGBA 像素、尺寸、格式、partial、完整 diagnostics 及异常行为。透明像素的 RGB 也属于像素结果。
4. 保持资源上限、计费次数、输入校验、记录位置及异常类型。减少实际分配不能顺便放宽名义画布或工作量预算。
5. 保留 Pillow 与现有 pyclipper；第一轮不更换字体引擎、布尔几何算法或描边扩展算法。
6. 保持 Python 3.10–3.14 及当前 Linux、macOS、Windows 支持范围；不借本轮优化提高 Python 下限。
7. 新增函数和方法均写中文注释。保留 DocVortex 工作区已有的无关改动；Python 开发环境可使用 `/Users/myhloli/projects/20240809magic_pdf/Magic-PDF/.venv4`。

当前 022 号公式左右端存在贴边/截切疑点，032、035 号编号也靠近右边缘。这些是已记录的基线现象，性能批次保持原样；若要修复，另立正确性变更与参考输出，避免混淆收益和渲染变化。

## 批次与依赖

| 批次 | 范围 | 依赖与状态 | 验收重点 |
| --- | --- | --- | --- |
| B0 | 冻结参考实现，建立成对计时与严格比较工具 | 已验收；A/A、失败负例、身份审计通过 | 可复现、不可覆盖、比较失败返回非零 |
| B1A | 同一记录内复用矩阵与矩阵校验 | 已验收并保留在工作区 | 正常路径收益，异常诊断不变 |
| B1B | POLYLINETO 重复映射与无用 bounds 扫描 | polyline 增量落在噪声内，已回退；bounds 仅隔离语义验证 | 每项独立差分与增量计时 |
| B2A | 减少全画布图层的冗余处理次数 | alpha 简化已验收；直接 paste 方案因回退淘汰 | 保持坐标、图层顺序和精确像素 |
| B2B | 有限范围内的局部 layer/mask | 最终合成裁框增量落在噪声内，已回退；未迁移几何/栅格原点 | ROI 边缘、裁剪、透明与预算 |
| B3A | Rust 扩展原型、绑定与发行方案 | 生产构建及原生/纯 wheel、源码回退本机验收完成 | 原生身份可核验，Python 支持范围不收窄 |
| B3B | Rust 批量点列解码、校验和变换 | 正式调用接入并验收；安全点数组复制计入 A/B | 相对优化后 Python 的真实增量 |
| B4 | SVG 批量序列化或剩余几何热点 | SVG 融合候选已测，增量未超过噪声，未纳入 | 每次只迁一个核，不放宽输出相等 |
| B5 | 累计回放、独立安装和跨平台验收 | 原生/纯 wheel、sdist 本机验收通过；3.10/3.14 已实测，远端 CI 待运行 | 源码、构建物、安装物、真实调用分别验证 |

默认先完成 B0、B1A、B1B、B2A，再重测热点。B2B 与 B3 根据剩余时间占比、等价性风险和绑定成本决定先后；B2B 不作为 Rust 原型的强制前置条件。

### B0 冻结可比较的基线

现有脚本用于发现热点，仍有四个需要补齐的验收边界：源码路径硬编码，复制目录不能冻结旧实现；`capture` 可覆盖参考输出；PNG 指纹读取的是已保存的 capture 文件；比较失败目前只写布尔值，不使进程失败。

拟增加参数化的配对 runner 与 comparator，放入 `tools/`，具体文件名在实现时确定。

1. 将初始证据视为不可修改的 reference；使用独立的 reference、candidate source/interpreter 和唯一 output directory 参数。拒绝向 reference 或既有结果目录写入。
2. 冻结 before 源码或安装物，而不只是复制结果目录。保存两个项目的版本、源码差异指纹、脚本 SHA256、解释器及依赖身份。记录当前已加载的 DocVortex、Pillow、pyclipper 原生模块路径和二进制 SHA256，并保存可取得的接口协议与构建身份；新增 Rust 扩展时追加其实际 `.so/.pyd`、接口版本和 release 构建信息。
3. 开始前校验原始 DOC 和 39 份 EMF 的 SHA256、顺序、参数。完整 DOC 路径另核对实际调用清单，不能因漏调用、尺寸下降或 fallback 改变而得到虚假提速。
4. before/after 在不同进程运行，同机串行、交替并反序；每个变体至少 1 次预热、5 次正式样本。先进行相同实现 A/A，量化噪声，再按相同协议进行 A/B。全部原始样本、顺序和配对比值落盘。
5. 正式计时不插桩，也不在计时轮之间序列化大结果、散列或导出。正确性、阶段 trace、cProfile、内存分别运行；两变体保持相同 GC 策略。
6. 候选实际产生的 SVG、PNG 和文档素材保存到候选目录，再提取候选 PNG 像素并比较。不能把旧 capture 文件当作候选输出。
7. 比较 ModelJSON、MiddleJSON、素材路径集合与字节、每图结果字段及完整诊断；任何不一致或缺失返回非零并保存首个差异。SVG 矢量内容与 fallback 分别验证，fallback 相同不能代替 SVG 相同。
8. RSS 在独立新进程、相同阶段、散列及导出前采集，统一平台单位，报告绝对峰值；Python 分配峰值若需要则另跑，不用 tracemalloc 计时作为速度成绩。
9. 自动生成 summary/comparison，关联原始报告与运行身份。保留 022、032、035 的前后图和差异图，并复用已有 DOC 结构/素材导出回归。

退出条件：A/A 输出严格一致，故意更改一个输出能使 comparator 失败，参考目录不会被覆盖，计时噪声已记录，before 与 candidate 身份可区分。完成后保留初始采集值，同时将成对测量确认为正式验收基线。

### B1A 同一记录内复用矩阵

主要位置：[gdi/playback.py](../src/metafile_render/gdi/playback.py)、[gdi/shapes.py](../src/metafile_render/gdi/shapes.py)、[gdi/emf.py](../src/metafile_render/gdi/emf.py)。

- 在实际消费点列时惰性计算一次组合矩阵，供同一记录/批次的点使用；保留逐点有限数和坐标范围校验。
- 第一批不引入跨记录持久缓存，也不提前处理不绘图的状态记录。需要跨记录缓存时，另行覆盖 map mode、window/viewport、world transform、RestoreDC 和 EMF+ GetDC bridge 的失效边界。
- 零 window extent 等异常路径保留原有逐次诊断与截断摘要。诊断去重如有价值，应作为独立行为变更。
- 记录实际矩阵构造/组合次数和解析耗时；如果采用函数内缓存，不能仅用 `logical_matrix()` 调用次数判断是否成功。

验收：39 图和 DOC 输出一致；转换组合、保存/恢复状态、零 extent、非有限数、超大坐标、空点列回归通过；成对计时确认解析收益，完整 DOC 无稳定退化。回退范围限于批量映射入口及其调用点。

### B1B 消除解析冗余

分两项实施并分别记录增量：

1. 去除 `_handle_emf_poly()` 中 POLYLINETO 生成后未使用的预映射列表。保持逻辑 current position、活动 PathBuilder、点预算及非法输入的检查顺序；无法保持异常诊断语义的情况保留原路径。
2. `finalize()` 仅在头部 bounds 缺失或退化时调用 `_commands_bounds()`。保持现有 `None`/接近零尺寸判断、normalized 和空命令行为。

验收：命令内容、source location、诊断及公开输出相同；每项差分可单独回退。完成后更新配对结果和残余 profile，后续 Rust 不再沿用原始 266 ms 作为可优化空间。

### B2A 减少图层处理次数

主要位置：[backends/paths.py](../src/metafile_render/backends/paths.py)、[backends/raster.py](../src/metafile_render/backends/raster.py)、[backends/composite.py](../src/metafile_render/backends/composite.py)。

保持完整画布、全局坐标、现有 clip 和合成顺序，逐项检查普通 `ROP2=13` 路径中的冗余建层、通道提取/乘法及透明层合成。优先限定在能证明等价的单一实心填充分支，复杂 brush、半透明、多重覆盖和其他 ROP 继续走原实现。

不能直接把“透明层合成”替换为图层引用赋值：透明像素的 RGB、后续合成和编码字节仍可能不同。每个快路径都需要适用条件和精确对照，避免一次重写整个 renderer。

验收：真实样本和覆盖透明重叠、fill/stroke、clip 的定向用例逐字节/逐像素一致；报告原生图像操作次数、按尺寸和模式估算的缓冲区处理量、栅格耗时与独立 RSS。缓冲区处理量是估计指标，不等同于实际内存分配或 RSS。收益来自减少像素处理，不能归因为 Rust。

### B2B 局部图层与 mask

只有 B2A 后全画布操作仍占主要耗时，才进入独立 ROI 原型。首批限定为有可靠保守包围盒的普通填充路径；描边、hatch、复杂 clip、ROP 等未覆盖分支保留旧实现。

- 优先保留全局 transform、flatten 和 quantize，再进行整数栅格原点平移；避免提前改变浮点计算顺序、曲线误差判据和负坐标舍入。
- 包围盒必须包含曲线极值的保守范围；扩展描边支持时计入 pen 宽度、cosmetic 语义、cap、join、miter。不可只取端点范围。
- clip 顺序和 copy/and/or/xor/diff 语义不变。最初可复用全局 clip mask 后裁切；若缓存局部 mask，key 必须包含原点和尺寸。
- 保留全局超采样及最终整图 Lanczos/UnsharpMask，不能逐 ROI 缩放或锐化。hatch 扩展需保留全局相位。
- 保留名义画布和工作量预算、生成点计费，以及现有 64 MiB 会话缓存的所有权边界。

验收增加：负坐标、越界图形、半透明覆盖、空/退化 ROI、裁剪边缘、开放/闭合路径。任何边缘像素差异先缩小快路径或回退，不自动放宽容差。ROI 方案没有稳定收益时可跳过，保留 B2A。

### B3A Rust 扩展与绑定原型

进入条件：B1/B2 后的独立阶段归因仍显示点列解码、校验、变换占 39 图重放的显著比例，建议以同一归因运行内约 10% 的占比作为原型筛选线；同时有证据说明它不是大部分耗在 Python 返回对象构造或其他原生库中。该比例只用于筛选，不与另一轮无插桩总时间混算；原型收益最终由无插桩成对总耗时确认。

采用私有计算扩展与 PyO3 绑定，Python 继续负责公共 API、状态机、记录路由和诊断。原型期保留优化后的 Python 实现作为强制对照；基准明确选择 Python/native，要求实际 native 导入成功，禁止静默回退后仍标记为 Rust 成绩。

在这一批确定并记录构建后端、PyO3/Rust 版本、是否采用 `abi3-py310`、架构矩阵及安装回退策略。优先评估 maturin，但不能直接套用其他项目的 Python 3.11 ABI3 配置。运行时回退与没有 wheel 时的 sdist 安装是不同问题，必须分别验证并说明。

先完成一个窄接口的构建、导入和同结果 smoke，确认 release 构建、wheel 内容和 Python 3.10/3.14 加载，再进入计算核。构建产物与配对输出放到空间充足的位置，避免 Cargo target 挤占当前工作卷。

参考：[PyO3 性能与解释器分离](https://pyo3.rs/v0.30.1/performance)、[PyO3 构建与分发](https://pyo3.rs/v0.30.1/building-and-distribution.html)、[maturin 构建指南](https://www.maturin.rs/)。纯 Rust 计算可在不访问 Python 对象时与解释器分离；并发和 wheel 安装收益仍需单独实测。

### B3B Rust 批量点列内核

建议边界为一条记录内的 `decode → validate → transform`：输入整段字节/只读 view、point 格式、offset/count/stride、已组合矩阵和必要预算；一次返回该批点和需要保留的逻辑终点。

- 保留 signed 16/32 位读取、读取边界和分配前的数量检查。错误结果回到 Python 边界时恢复既有异常类型、record index/offset 与信息。
- 保留输入点计费，不能因批量解码而重复计费或漏计；POLYLINETO/BezierTo 更新逻辑 current position，不能用设备坐标替代。
- 保持 double 运算顺序，不引入 fast-math、主动 FMA 或不同舍入规则。源 bytes/view 在 native 访问期间必须保持有效。
- 每条记录/数组跨绑定边界一次，避免逐点 FFI。分别测核内、绑定、Python 对象物化、解析和完整 DOC 耗时；如果大量 tuple 构造抵消收益，再在本批内评估紧凑结果表示。
- 不在同一批迁移完整 GDI/EMF+ 状态机、Clipper 或栅格引擎。

退出条件：相对 B3 前的优化后 Python 基线，39 图和完整 DOC 都有超过噪声的增量收益，输出及异常语义相同，RSS 无稳定退化。若只在核内变快而公开调用没有收益，停止扩大 Rust 范围。

### B4 按残余 profile 选择后续内核

每次只选一项，完成后再次测量；不预设全部执行。

| 候选 | 启动依据 | 兼容约束 |
| --- | --- | --- |
| SVG 路径批量序列化 | 路径变换、数字格式化仍占明显比例；初始 profile 有约 14.5 万次 `_svg_number` 调用 | 保持 `round(value, 4)`、负零、整数/小数格式、空格、路径顺序、输出字节预算和最终 SVG 字节 |
| transform/flatten/quantize 批量核 | B1–B3 后几何 Python 成本仍值得处理 | 保持 flatness 0.25、最大深度 12、细分顺序、Python ties-to-even、量化 scale 256、闭合点和预算计费 |
| record 扫描/validation-only | 其他真实文件中扫描或 Windows preflight 成为主要热点 | 保留 EMF+ Header/EOF、坏输入检查、来源位置与资源上限 |

初始 cubic 细分仅约 29 ms，不能据此承诺全流程大幅提速。SVG 序列化若改变字节，即使看起来相同，也先按兼容性差异处理；不能为了通过而修改 reference。

### B5 累计回归与分发验收

每个被接受的批次都重新运行 39 图、完整 DOC、仓库既有基准和相关回归；阶段收尾再运行完整测试、lint、format、mypy 与构建检查。针对不同路径的补充语料如下：

| 语料/入口 | 用途 |
| --- | --- |
| 完整 `docx_01.doc` | 主性能指标、真实调用清单、语义与素材完整性 |
| 39 个实际 EMF | 逐图输出、SVG/PNG 双重验证、慢图 022/032/035 审阅 |
| GDI+ Only/Dual fixtures、Office 图标 | EMF+、图片、文字、裁剪及其他 ROP 的既有覆盖 |
| `tools/benchmark_rendering.py --replay` | 重复位图、重复裁剪、填充描边的退化控制 |
| 密集点列、变换与畸形输入的定向回归 | 源代码语义和资源边界；必要时从现有测试构造器生成 |
| WMF 与 Windows native 测试 | 防止 EMF 主样本掩盖其他格式和后端退化 |

当前 CI 覆盖 Python 3.10–3.14 与 Linux/macOS/Windows。若引入 Rust，扩展 wheel 构建及匹配平台安装矩阵，分别验证：

1. 构建来源、release 配置、原生库与 Python 包的内容和版本一致。
2. 在仓库目录之外安装 wheel、导入真正的扩展并运行 API/CLI 和真实图像 smoke；独立验证 sdist 构建。
3. 更新 [check_distributions.py](../tools/check_distributions.py) 中“恰好一个 wheel 加一个 sdist”的假设：按平台选择可安装 wheel，不能将其他架构的 wheel 当作当前平台安装测试。
4. 保留最小依赖测试和 Windows 原生 GDI 测试；对同平台 before/after 作比较，不要求不同系统字体环境的结果相互相等。
5. 更新安装与开发文档。构建通过、独立安装、远端 CI、提交、推送和发布分别记录；发布不属于本计划创建或本地优化验收的完成条件。

## 每批通用验收与停止条件

- **正确性**：主语料及定向边界严格一致；比较失败必须阻止该批验收。若确有必要改变语义，单独列为正确性/兼容性变更并重新定义基线。
- **性能**：报告全部成对样本、配对耗时比、绝对节省时间和 A/A 噪声。收益必须超过噪声；建议新增 Rust 复杂度至少带来 39 图重放 5% 的稳定增量，且完整 DOC 同向改善。
- **退化**：完整 DOC 或重点控制语料若退化超过 5% 且超过 A/A 噪声，复测确认后回退或缩小范围。极短样本同时看绝对差值，避免只看百分比。
- **内存**：至少 3 次独立新进程配对 RSS，顺序交替。稳定增长超过 5% 时先定位，不能用某一次峰值或 tracemalloc 数值替代 RSS 结论。
- **可审阅**：每批保留独立 diff/回退点、输入和代码身份、comparison、timing、memory、测试日志及必要的视觉差异图。每个报告列出相对前一批的增量和相对最初基线的累计值。
- **停止**：目标已达到且剩余候选收益不足，或候选收益落在噪声内、绑定开销抵消、像素/诊断难以保持一致时，停止该方向，保留已验收批次。

## 本轮暂缓事项

- ROP 字节内核：本 DOC 调用为 0；仅在增加实际命中的业务语料后重新排优先级。
- 字体/字形缓存、EMF+ 图片编解码往返：当前 39 图没有对应绘制命令。
- WMF 对象空槽 O(N²)：问题已在合成输入验证，但本 DOC 只渲染 EMF；作为独立 WMF 批次处理。
- 替换 pyclipper、整套 renderer Rust 重写、命令乱序并行、跨文档大缓存：当前证据不足以承担这些范围的兼容性和维护成本。

首轮已完成本机累计验收，达到 20% 目标，已淘汰候选保持隔离。后续先运行已配置的跨平台 CI；提交、推送与发布分别处理。详见[任务状态](optimization-state-2026-10-25.md)与[验收报告](optimization-report-2026-10-25.md)。


## 第二轮执行记录

第二轮已在首轮冻结源码上完成，保留全覆盖裁剪省略恒等 alpha 运算，以及单次 SVG 输出内有界数字格式化复用。相对第一轮，DOC 和 39 图配对耗时分别再降低 14.64% 与 16.11%；相对初始版本的独立累计比较为 32.11% 与 37.03%。这些结果不由批次百分比相加得到。

完整范围、预算与缓存约束、独立复测和安装物依据见[第二轮验收报告](optimization-round2-report-2026-10-25.md)。其余范围与未执行的远端 CI 边界保持明确记录。


## 原生发行策略更新

已按后续用户指示移除纯 Python 兼容构建与后端选择，Python/ABI3 下限均为 3.11，支持平台与 DocVortex 当前五平台矩阵一致。只发布原生 wheel；本机 3.11/3.14 安装和真实图元验收通过，跨平台远端 CI 尚未执行。详见[策略与验收记录](native-only-policy-2026-10-25.md)。

## 第三轮执行记录

扩充为 90 份图元后，第三轮已完成本机验收。保留大画布的稀疏 source-over 合成、不透明实心填充与描边的局部像素写入，并限制局部合成的临时副本容量。几何、裁剪、超采样、预算和原生发行策略保持原样。

相对第三轮前的独立累计配对：主语料耗时降低 29.12%，全部可输出组合降低 24.78%，主语料 WMF 降低 41.62%，整个语料进程峰值 RSS 降低 19.37%。原 DOC/39 图元配对变化约 −0.89% / +0.74%，在噪声内基本持平。180 个格式组合的完整输出、IR、诊断及拒绝行为一致；独立 CPython 3.11/3.14 wheel 回放通过，612 项测试通过、4 个 Windows GDI 项跳过。

完整批次、冻结源码、A/A、反序配对和安装物依据见[第三轮计划](optimization-round3-plan-2026-10-25.md)及[验收报告](optimization-round3-report-2026-10-25.md)。原始批次增益不相加。后续 mask union 草案没有进入生产；远端 CI、提交、推送和发布仍未执行。
