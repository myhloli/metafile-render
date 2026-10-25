//! 连续字节 ROP 内核；输出直接写入 Python bytes，避免逐字节对象与中间副本。

use pyo3::prelude::*;
use pyo3::types::PyBytes;

#[derive(Clone, Copy)]
enum Operation {
    Xor,
    And,
    Or,
    NotOr,
    AndNotSource,
    NotSource,
    SourceAndNotDestination,
    NotAnd,
    NotXor,
    DestinationOrNotSource,
    SourceOrNotDestination,
    Invert,
    Black,
    White,
}

impl Operation {
    /// 保持 Python 私有接口对未知操作按白色填充的既有行为。
    fn parse(value: &str) -> Self {
        match value {
            "xor" => Self::Xor,
            "and" => Self::And,
            "or" => Self::Or,
            "not_or" => Self::NotOr,
            "and_not_source" => Self::AndNotSource,
            "not_source" => Self::NotSource,
            "source_and_not_destination" => Self::SourceAndNotDestination,
            "not_and" => Self::NotAnd,
            "not_xor" => Self::NotXor,
            "destination_or_not_source" => Self::DestinationOrNotSource,
            "source_or_not_destination" => Self::SourceOrNotDestination,
            "invert" => Self::Invert,
            "black" => Self::Black,
            _ => Self::White,
        }
    }

    /// 二元运算延续 zip 的最短长度，一元与填充分别使用原来的输入长度。
    fn output_size(self, destination: &[u8], source: &[u8]) -> usize {
        match self {
            Self::NotSource => source.len(),
            Self::Invert | Self::Black | Self::White => destination.len(),
            _ => destination.len().min(source.len()),
        }
    }
}

/// 在已分配且尚未暴露的输出区间执行精确 u8 逻辑运算。
fn fill_bytes(output: &mut [u8], destination: &[u8], source: &[u8], operation: Operation) {
    match operation {
        Operation::NotSource => {
            for (out, value) in output.iter_mut().zip(source) {
                *out = !value;
            }
        }
        Operation::Invert => {
            for (out, value) in output.iter_mut().zip(destination) {
                *out = !value;
            }
        }
        Operation::Black => output.fill(0),
        Operation::White => output.fill(255),
        _ => {
            for ((out, left), right) in output.iter_mut().zip(destination).zip(source) {
                *out = match operation {
                    Operation::Xor => left ^ right,
                    Operation::And => left & right,
                    Operation::Or => left | right,
                    Operation::NotOr => !(left | right),
                    Operation::AndNotSource => left & !right,
                    Operation::SourceAndNotDestination => right & !left,
                    Operation::NotAnd => !(left & right),
                    Operation::NotXor => !(left ^ right),
                    Operation::DestinationOrNotSource => left | !right,
                    Operation::SourceOrNotDestination => right | !left,
                    _ => unreachable!("unary and fill operations are handled above"),
                };
            }
        }
    }
}

/// 一次分配最终 bytes，释放 GIL 后只访问存活的不可变输入与独占输出切片。
#[pyfunction]
pub(crate) fn bitwise_channel_bytes<'py>(
    py: Python<'py>,
    destination: &[u8],
    source: &[u8],
    operation: &str,
) -> PyResult<Bound<'py, PyBytes>> {
    let operation = Operation::parse(operation);
    let size = operation.output_size(destination, source);
    PyBytes::new_with(py, size, |output| {
        // new_with 持有尚未返回的 bytes；此时没有其他 Python 引用可读写其缓冲区。
        py.detach(|| fill_bytes(output, destination, source, operation));
        Ok(())
    })
}
