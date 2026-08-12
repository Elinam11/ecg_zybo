"""Integer helpers whose semantics match the HLS/C++ implementation exactly.

Every arithmetic operation in the FPGA data path is expressed through these helpers so
the reference model and the synthesised hardware cannot disagree about rounding,
truncation, or sign handling. The C++ mirror lives in hls/src/ecg_fixedpoint.h.
"""

from __future__ import annotations


def asr(value: int, shift: int) -> int:
    """Arithmetic shift right: floor(value / 2**shift), including for negatives.

    Python's >> already floors, which is what an HLS arithmetic shift on a signed
    ap_int does. C++ >> on a signed integer is an arithmetic shift too (guaranteed
    since C++20, universal in practice before that).
    """
    return value >> shift


def floor_div(numerator: int, denominator: int) -> int:
    """floor(numerator / denominator) for a positive denominator.

    C++ integer division truncates toward zero, so the C++ mirror needs an explicit
    correction for negative numerators; this is the reference behaviour it matches.
    """
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    return numerator // denominator


def clamp(value: int, low: int, high: int) -> int:
    return low if value < low else (high if value > high else value)


def zigzag_map(error: int) -> int:
    """Map a signed prediction residual onto the non-negative integers (spec 4.2.2)."""
    return 2 * error if error >= 0 else -2 * error - 1


def zigzag_unmap(mapped: int) -> int:
    """Inverse of :func:`zigzag_map`."""
    if mapped & 1:
        return -((mapped + 1) // 2)
    return mapped // 2


def floor_log2(value: int) -> int:
    """floor(log2(value)) for value >= 1, else 0.

    Implemented as a bit-length so it is exact and matches the HLS leading-zero
    count, avoiding any floating-point log call in the data path.
    """
    if value <= 0:
        return 0
    return value.bit_length() - 1


def to_int16(value: int) -> int:
    """Reinterpret the low 16 bits of `value` as a signed 16-bit integer."""
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def to_uint32(value: int) -> int:
    return value & 0xFFFFFFFF


def sign_extend(value: int, bits: int) -> int:
    """Interpret the low `bits` of `value` as a signed two's-complement integer."""
    mask = (1 << bits) - 1
    value &= mask
    sign_bit = 1 << (bits - 1)
    return value - (1 << bits) if value & sign_bit else value
