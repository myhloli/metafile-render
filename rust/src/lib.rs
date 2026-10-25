//! 有界点列解码与仿射变换内核；GDI 状态和诊断仍由 Python 管理。

use pyo3::exceptions::{PyMemoryError, PyValueError};
use pyo3::prelude::*;

type Point = (f64, f64);
type Matrix = (f64, f64, f64, f64, f64, f64);
type MappedPoints = (Vec<Point>, Option<Point>);

const MAX_POINTS: usize = 1_000_000;
const MAX_COORDINATE: f64 = 1e12;

enum PointError {
    Count,
    Boundary,
    Matrix,
    Coordinate,
    Allocation,
}

/// 校验完整输入范围后批量解码并变换，保留最后一个未变换的逻辑坐标。
fn decode_transform_impl(
    data: &[u8],
    offset: usize,
    count: usize,
    compact: bool,
    matrix: Matrix,
) -> Result<MappedPoints, PointError> {
    if count > MAX_POINTS {
        return Err(PointError::Count);
    }
    let stride = if compact { 4 } else { 8 };
    let size = count.checked_mul(stride).ok_or(PointError::Boundary)?;
    let end = offset.checked_add(size).ok_or(PointError::Boundary)?;
    let bytes = data.get(offset..end).ok_or(PointError::Boundary)?;
    if count == 0 {
        // 与空 Python 点列一致：未消费任何点时不提前校验矩阵。
        return Ok((Vec::new(), None));
    }
    let (a, b, c, d, e, f) = matrix;
    if [a, b, c, d, e, f]
        .iter()
        .any(|value| !value.is_finite() || value.abs() > MAX_COORDINATE)
    {
        return Err(PointError::Matrix);
    }
    let mut mapped = Vec::new();
    mapped
        .try_reserve_exact(count)
        .map_err(|_| PointError::Allocation)?;
    let mut last = None;
    for item in bytes.chunks_exact(stride) {
        let (x, y) = if compact {
            (
                i16::from_le_bytes([item[0], item[1]]) as f64,
                i16::from_le_bytes([item[2], item[3]]) as f64,
            )
        } else {
            (
                i32::from_le_bytes([item[0], item[1], item[2], item[3]]) as f64,
                i32::from_le_bytes([item[4], item[5], item[6], item[7]]) as f64,
            )
        };
        // 保持 Python 的乘法与加法顺序；不用 mul_add 或近似数学选项。
        let mapped_x = (a * x + c * y) + e;
        let mapped_y = (b * x + d * y) + f;
        if !mapped_x.is_finite()
            || !mapped_y.is_finite()
            || mapped_x.abs() > MAX_COORDINATE
            || mapped_y.abs() > MAX_COORDINATE
        {
            return Err(PointError::Coordinate);
        }
        mapped.push((mapped_x, mapped_y));
        last = Some((x, y));
    }
    Ok((mapped, last))
}

/// 将内部校验失败转换成内部接口异常，Python 调用层负责重演原异常语义。
fn point_error(error: PointError) -> PyErr {
    match error {
        PointError::Count => PyValueError::new_err("point count exceeds native limit"),
        PointError::Boundary => PyValueError::new_err("point array exceeds byte boundary"),
        PointError::Matrix => PyValueError::new_err("matrix exceeds coordinate budget"),
        PointError::Coordinate => PyValueError::new_err("mapped point exceeds coordinate budget"),
        PointError::Allocation => PyMemoryError::new_err("cannot allocate mapped points"),
    }
}

/// 一次越过绑定边界处理完整点列，返回已校验坐标与最后一个逻辑点。
#[pyfunction]
fn decode_transform_points(
    py: Python<'_>,
    data: &[u8],
    offset: usize,
    count: usize,
    compact: bool,
    matrix: Matrix,
) -> PyResult<MappedPoints> {
    // data 借用不可变 Python bytes，释放 GIL 期间调用栈仍持有其所有权。
    py.detach(|| decode_transform_impl(data, offset, count, compact, matrix))
        .map_err(point_error)
}

/// 注册点列内核与接口身份，确保加载器和发行物可核验。
#[pymodule]
fn _metafile_points(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("PROTOCOL_VERSION", 2)?;
    module.add(
        "BUILD_PROFILE",
        if cfg!(debug_assertions) {
            "debug"
        } else {
            "release"
        },
    )?;
    module.add_function(wrap_pyfunction!(decode_transform_points, module)?)?;
    Ok(())
}
