"""Lossless QRS path: 2nd-order predictor + adaptive Golomb-Rice coder (spec 4.2).

Reconstruction is exact, which is what guarantees PRD = 0% across QRS regions. The
encoder emits an abstract list of (value, bit_count) pairs so it can be unit tested
without a bitstream, and the decoder consumes any object exposing `get(nbits)`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import config
from .fixedpoint import floor_log2, zigzag_map, zigzag_unmap


@dataclass
class QrsPayload:
    length: int
    anchor0: int
    anchor1: int
    k: int
    residual_sum: int
    escapes: int = 0
    bits: list[tuple[int, int]] = field(default_factory=list)

    @property
    def bit_count(self) -> int:
        return sum(nbits for _, nbits in self.bits)


def predict(prev1: int, prev2: int) -> int:
    """2nd-order linear predictor: y_hat(n) = 2*y(n-1) - y(n-2)."""
    return 2 * prev1 - prev2


def mapped_residuals(samples: np.ndarray) -> list[int]:
    """Zigzag-mapped prediction residuals for samples 2..W-1."""
    values = [int(v) for v in samples]
    out: list[int] = []
    for n in range(config.ANCHOR_COUNT, len(values)):
        error = values[n] - predict(values[n - 1], values[n - 2])
        out.append(zigzag_map(error))
    return out


def select_k(residual_sum: int, count: int) -> int:
    """k = max(0, floor(log2(S / WS))) over the mapped residual sum.

    `count` is the actual number of coded residuals, which is the window length minus
    the two anchors. Windows can be shorter than 73 samples when consecutive beats
    force a truncation, so the divisor is the real count rather than the nominal 73.
    An all-zero window yields mean 0, where floor_log2 returns 0 instead of diverging.
    """
    if count <= 0:
        return 0
    mean = residual_sum // count
    if mean <= 0:
        return 0
    return min(floor_log2(mean), config.GR_K_MAX)


def encode_qrs(samples: np.ndarray) -> QrsPayload:
    """Encode one QRS window losslessly."""
    values = [int(v) for v in samples]
    length = len(values)
    if length == 0:
        raise ValueError("cannot encode an empty QRS window")

    anchor0 = values[0]
    anchor1 = values[1] if length > 1 else 0
    residuals = mapped_residuals(samples)
    residual_sum = sum(residuals)
    k = select_k(residual_sum, len(residuals))

    bits: list[tuple[int, int]] = [
        (anchor0 & 0xFFFF, 16),
        (anchor1 & 0xFFFF, 16),
        (k, config.GR_K_BITS),
    ]

    escapes = 0
    mask = (1 << k) - 1
    for mapped in residuals:
        quotient = mapped >> k
        if quotient >= config.GR_Q_ESCAPE:
            # Escape: a fixed run of ones with no terminator, then the raw value.
            # This bounds any symbol to 63 bits instead of thousands.
            escapes += 1
            bits.append(((1 << config.GR_Q_ESCAPE) - 1, config.GR_Q_ESCAPE))
            bits.append((mapped & 0xFFFFFFFF, config.GR_ESCAPE_BITS))
        else:
            # `quotient` ones followed by a terminating zero, then k remainder bits.
            bits.append((((1 << quotient) - 1) << 1, quotient + 1))
            if k:
                bits.append((mapped & mask, k))

    return QrsPayload(length=length, anchor0=anchor0, anchor1=anchor1, k=k,
                      residual_sum=residual_sum, escapes=escapes, bits=bits)


def decode_qrs(reader, length: int) -> np.ndarray:
    """Decode one QRS window from a bit reader. Inverse of :func:`encode_qrs`."""
    from .fixedpoint import sign_extend

    if length == 0:
        return np.zeros(0, dtype=np.int64)

    anchor0 = sign_extend(reader.get(16), 16)
    anchor1 = sign_extend(reader.get(16), 16)
    k = reader.get(config.GR_K_BITS)

    out = [anchor0]
    if length > 1:
        out.append(anchor1)

    for _ in range(config.ANCHOR_COUNT, length):
        quotient = 0
        escaped = False
        while True:
            if quotient == config.GR_Q_ESCAPE:
                escaped = True
                break
            if reader.get(1) == 0:
                break
            quotient += 1

        if escaped:
            mapped = reader.get(config.GR_ESCAPE_BITS)
        else:
            remainder = reader.get(k) if k else 0
            mapped = (quotient << k) | remainder

        error = zigzag_unmap(mapped)
        out.append(predict(out[-1], out[-2]) + error)

    return np.asarray(out, dtype=np.int64)
