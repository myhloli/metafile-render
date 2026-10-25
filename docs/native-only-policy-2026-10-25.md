# Metafile Render 原生发行策略调整

已按用户要求参考 DocVortex 当前 `1e7bc45a` 的原生发行策略，替代前两轮使用的可选 Rust / 纯 Python 回退方案。Python SDK、状态机及尚未下沉的解析逻辑继续保留；不再提供可选择或自动降级的无 Rust 运行版本。

## 支持范围

- 启用 GIL 的 **CPython 3.11–3.14**，元数据为 `Requires-Python >=3.11,<3.15`。
- PyO3 `abi3-py311`，发行标签为 **`cp311-abi3`**。同一平台的一个 wheel 覆盖上述 Python 版本。
- 原生扩展为必需组件；缺失、损坏或协议不匹配时明确失败。扩展协议已提升至 2，防止误用旧的可选内核构建。
- 仅发布原生 wheel，不发布 `py3-none-any` 或 sdist。

| 平台 | 发布标签 |
| --- | --- |
| Linux x86-64，glibc 2.28+ | `manylinux_2_28_x86_64` |
| Linux aarch64，glibc 2.28+ | `manylinux_2_28_aarch64` |
| Windows x64 | `win_amd64` |
| Windows ARM64 | `win_arm64` |
| macOS 14+ arm64 | `macosx_14_0_arm64` |

Python 3.10、PyPy、free-threaded CPython、Intel macOS、musl 不属于该发布矩阵。该范围与 DocVortex 当前配置一致。

## 实现调整

1. 删除构建模式选择和可选扩展设置，`RustExtension(optional=False)`，编译失败即失败。
2. 删除运行时 Python/auto/rust 选择分支及缺扩展回退。旧构建和计算后端环境变量不再参与决策，也不增加专门的废弃值拒绝分支。
3. 有效输入的所有公开渲染格式和绘制后端均检查必需扩展；公共 `backend="auto"/"replay"` 的 GDI/replay 路由保持原义。
4. 保留异常记录的 Python 校验与诊断顺序重演。这属于数据错误处理，与无 Rust 兼容版本不同。
5. 原生 wheel 工作流扩为五平台，构建于最小 Python 3.11，安装端点为 3.11 和 3.14；发布门禁核对完整集合、版本、Python 范围、ABI3 标签及扩展载荷。
6. 本地 Linux 开发构建可接受原生 `linux_x86_64/linux_aarch64` 标签供安装验证；正式发布仍只接受上述 manylinux 标签。

Windows ARM64 的 `pyclipper` 当前没有兼容预编译 wheel，已用二进制依赖解析探测确认。依照 DocVortex 的现有处理，在该平台配置 MSVC 并验证常规依赖源码构建；本项目本身仍只发布必需原生 wheel。

## 本机验收

- 完整回归 **397 passed、4 skipped**；跳过项仍是 Windows 原生 GDI 测试。
- 新 wheel：`metafile_render-0.3.1-cp311-abi3-macosx_14_0_arm64.whl`。
- 同一 wheel 在独立 Python **3.11.15 和 3.14.4** 环境通过四种 API/CLI 输出、39 图 SVG/PNG 字节、RGBA、尺寸及完整结果字段严格比较。
- 无 Rust 编译器时构建返回失败，没有生成兼容 wheel；Python 3.10 对该 wheel 的安装解析被拒绝。
- 加载负例覆盖扩展缺失、依赖缺失、坏二进制、操作系统加载失败、旧协议，以及全部格式和 `auto/replay` 入口。
- 纯 wheel、旧 `cp310-abi3`、缺二进制、Intel macOS、源码归档和不完整五平台集合均被发布门禁拒绝。
- mypy 55 个源文件、Ruff、Rust fmt、actionlint 通过。

切换前后 5 对计时与 3 对 RSS 严格比较全部通过。DOC 配对耗时约增加 1.38%，39 图约降低 0.25%，处于已观察噪声范围；两项 RSS 均无稳定退化。本次是支持策略与发行方式调整，没有宣称新的算法加速。

本机只验证了 macOS arm64。其余四平台的构建、安装和 Windows ARM64 依赖编译仍需执行已配置的远端 CI。代码未提交、推送或发布，项目发布版本尚未调整。

## 证据

目录：`/Users/myhloli/Documents/Codex/2026-10-25/metafile-render-native-policy/`。

- `sources/optional-before/`：切换前的只读源码快照。
- `dist/`：本机 cp311-abi3 wheel。
- `install-311/summary.json`、`install-314/summary.json`：独立安装与真实图元回放。
- `paired-transition/summary.json`：切换前后的输出、性能和 RSS 对照。
- `pytest.log`、`no-rust-build.log`、`python310-rejected.log`：正向回归和负向边界。

前两轮验收报告中的纯 Python wheel、3.10 和 cp310-abi3 记录属于历史方案；当前发布政策以本文和 `docs/reference.md` 为准。
