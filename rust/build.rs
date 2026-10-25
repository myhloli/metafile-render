/// 为直接使用 cargo 构建的 macOS 扩展补充 Python 动态符号查找参数。
fn main() {
    pyo3_build_config::add_extension_module_link_args();
}
