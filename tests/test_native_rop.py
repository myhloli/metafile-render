"""验证整块原生 ROP 的真值表、长度边界及 RGBA 合成语义。"""

from concurrent.futures import ThreadPoolExecutor

from PIL import Image
import pytest

from metafile_render import _metafile_points as native
from metafile_render import _native_points as backend
from metafile_render.backends import composite

_TRUTH_TABLES = {
    "xor": 0b0110,
    "and": 0b1000,
    "or": 0b1110,
    "not_or": 0b0001,
    "and_not_source": 0b0100,
    "not_source": 0b0101,
    "source_and_not_destination": 0b0010,
    "not_and": 0b0111,
    "not_xor": 0b1001,
    "destination_or_not_source": 0b1101,
    "source_or_not_destination": 0b1011,
    "invert": 0b0011,
    "black": 0b0000,
    "white": 0b1111,
    "unknown": 0b1111,
}


def _truth_bytes(destination: bytes, source: bytes, operation: str) -> bytes:
    """按独立的二输入真值表逐位计算参考值，延续原长度语义。"""
    truth = _TRUTH_TABLES[operation]
    if operation == "not_source":
        pairs = ((0, value) for value in source)
    elif operation in {"invert", "black", "white", "unknown"}:
        pairs = ((value, 0) for value in destination)
    else:
        pairs = zip(destination, source)
    return bytes(
        sum(((truth >> ((((left >> bit) & 1) << 1) | ((right >> bit) & 1))) & 1) << bit for bit in range(8))
        for left, right in pairs
    )


@pytest.mark.parametrize("operation", _TRUTH_TABLES)
def test_native_rop_exhaustive_byte_pairs(operation: str) -> None:
    """全部 65536 个字节对逐位验证，避免颜色近似和高位符号扩展。"""
    destination = bytes(left for left in range(256) for _ in range(256))
    source = bytes(range(256)) * 256
    result = native.bitwise_channel_bytes(destination, source, operation)
    assert type(result) is bytes
    assert result == _truth_bytes(destination, source, operation)


@pytest.mark.parametrize("operation", _TRUTH_TABLES)
@pytest.mark.parametrize("destination,source", [(b"", b""), (b"", b"\xff"), (b"\x00\xff\x00", b"\xff"), (b"\x00", b"\xff\x00")])
def test_native_rop_empty_and_unequal_lengths(operation: str, destination: bytes, source: bytes) -> None:
    """空输入和不等长输入保留 zip、一元反相与填充各自的输出长度。"""
    assert native.bitwise_channel_bytes(destination, source, operation) == _truth_bytes(destination, source, operation)


@pytest.mark.parametrize("operation", _TRUTH_TABLES)
def test_native_rop_keeps_mask_and_hidden_rgba(operation: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """半透明和全透明像素的全部 RGBA 通道都与逐位参考合成一致。"""
    pixels = bytes(range(48))
    destination = Image.frombytes("RGBA", (4, 3), pixels)
    layer = Image.frombytes("RGBA", (4, 3), bytes(reversed(pixels)))
    layer.putalpha(Image.frombytes("L", (4, 3), bytes([0, 1, 127, 128, 254, 255] * 2)))
    reference = destination.copy()
    composite._paste_bitwise(destination, layer, operation)
    with monkeypatch.context() as local:
        local.setattr(composite, "_bitwise_channel_bytes", _truth_bytes)
        composite._paste_bitwise(reference, layer, operation)
    assert destination.tobytes() == reference.tobytes()
    destination.close()
    reference.close()
    layer.close()


def test_native_rop_releases_only_private_output_to_parallel_calls() -> None:
    """并发调用各自拥有输出缓冲区，输入复用不产生交叉污染。"""
    destination, source = bytes(range(256)) * 1024, bytes(reversed(range(256))) * 1024
    expected = bytes([255]) * len(source)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: native.bitwise_channel_bytes(destination, source, "xor"), range(12)))
    assert all(result == expected for result in results)


def test_rendering_wrapper_records_real_rop_execution() -> None:
    """执行计数只累计真正完成的原生 ROP 输出，不冒充点列调用。"""
    before = backend.backend_info()
    assert composite._bitwise_channel_bytes(b"\xff\x00", b"\x01\x02", "xor") == b"\xfe\x02"
    after = backend.backend_info()
    assert after["rop_calls"] == before["rop_calls"] + 1
    assert after["rop_bytes"] == before["rop_bytes"] + 2
    assert after["native_calls"] == before["native_calls"]
