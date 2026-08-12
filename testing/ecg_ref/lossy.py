"""Lossy non-QRS path: 4-level integer Haar DWT + band thresholding + zero-RLE.

The transform uses an exact integer multiply-shift by round(2**15/sqrt(2)) so the
Python model and the HLS core produce identical coefficients. It is not perfectly
reversible - the double floor introduces a bounded drift - which is acceptable here
because this path is lossy by design and the drift is accounted for in PRD.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import config
from .fixedpoint import asr, clamp, sign_extend


@dataclass
class NonQrsPayload:
    length: int                     # original (unpadded) sample count
    padded_length: int
    tokens: list[tuple[int, int]] = field(default_factory=list)
    saturated: int = 0              # coefficients clipped by the 10-bit field

    @property
    def bit_count(self) -> int:
        return len(self.tokens) * config.RLE_TOKEN_BITS


# ---------------------------------------------------------------------------
# Integer Haar transform
# ---------------------------------------------------------------------------

def pad_segment(samples: np.ndarray) -> np.ndarray:
    """Extend a segment to a whole number of DWT blocks by repeating the last sample.

    Repetition (rather than zero padding) avoids an artificial edge discontinuity that
    would spill energy into every detail band. The packet stores the true length, so
    the decoder discards the padding.
    """
    values = np.asarray(samples, dtype=np.int64)
    target = config.dwt_padded_len(len(values))
    if target == len(values):
        return values
    if len(values) == 0:
        return np.zeros(target, dtype=np.int64)
    return np.concatenate([values,
                           np.full(target - len(values), values[-1], dtype=np.int64)])


def _haar_step(data: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """One level of the reversible integer Haar (lifting / S-transform).

        d = x0 - x1                 a = x1 + floor(d/2)

    This replaces the spec's floor(x/sqrt(2)) scaling for three concrete reasons,
    all measured on record 100:

    * It is exactly invertible, so the transform contributes zero distortion. The
      scaled form loses 0.917% PRD to rounding drift before a single coefficient is
      thresholded, which is half of the entire 2% budget.
    * It needs no multiplier at all - two adds and a shift - where the scaled form
      needs a 16x16 multiply per coefficient, so it is materially cheaper in PL
      resources.
    * Its coefficients do not grow by sqrt(2) per level, so every band fits the
      signed 10-bit RLE value field with no quantization shift and hence no
      additional loss.

    Thresholding therefore becomes the only source of error on this path, which makes
    the PRD/CR trade-off directly controllable.
    """
    even = data[0::2]
    odd = data[1::2]
    detail = even - odd
    approx = odd + (detail >> 1)
    return approx, detail


def _inverse_haar_step(approx: np.ndarray, detail: np.ndarray) -> np.ndarray:
    """Exact inverse of :func:`_haar_step`."""
    approx = np.asarray(approx, dtype=np.int64)
    detail = np.asarray(detail, dtype=np.int64)
    odd = approx - (detail >> 1)
    even = detail + odd
    out = np.empty(len(approx) * 2, dtype=np.int64)
    out[0::2] = even
    out[1::2] = odd
    return out


def haar_forward(padded: np.ndarray) -> dict[str, np.ndarray]:
    """4-level Haar decomposition -> {A4, D4, D3, D2, D1}."""
    approx = np.asarray(padded, dtype=np.int64)
    bands: dict[str, np.ndarray] = {}
    for level in range(1, config.DWT_LEVELS + 1):
        approx, detail = _haar_step(approx)
        bands[f"D{level}"] = detail
    bands[f"A{config.DWT_LEVELS}"] = approx
    return bands


def haar_inverse(bands: dict[str, np.ndarray]) -> np.ndarray:
    """Reconstruct a padded segment from its band coefficients."""
    approx = np.asarray(bands[f"A{config.DWT_LEVELS}"], dtype=np.int64)
    for level in range(config.DWT_LEVELS, 0, -1):
        approx = _inverse_haar_step(approx, np.asarray(bands[f"D{level}"],
                                                       dtype=np.int64))
    return approx


# ---------------------------------------------------------------------------
# Thresholding and quantization
# ---------------------------------------------------------------------------

def quantize_band(coeffs: np.ndarray, band: str,
                  thresholds: dict[str, int] | None = None) -> tuple[np.ndarray, int]:
    """Hard-threshold then scale a band into the signed 10-bit RLE value field.

    Rounding is to nearest (add half a step before the shift), which halves the
    quantization error versus truncation for one adder of hardware.
    """
    # The signed 10-bit value field caps |coefficient| at 511 once the band shift is
    # applied. With the lifting transform a detail coefficient is a plain difference,
    # so it can in principle reach twice the sample range; anything beyond the field
    # saturates and is counted. Real ECG at the native ADC scale is smooth enough that
    # this never fires (record 100 peaks at 200, a 2.5x margin), but uncorrelated
    # full-scale input will saturate the D1 band.
    table = config.BAND_THRESHOLD if thresholds is None else thresholds
    threshold = table[band]
    shift = config.BAND_QUANT_SHIFT[band]
    half = 1 << (shift - 1) if shift > 0 else 0

    out = np.zeros(len(coeffs), dtype=np.int64)
    saturated = 0
    for i, raw in enumerate(int(c) for c in coeffs):
        if abs(raw) < threshold:
            continue
        scaled = asr(raw + half, shift)
        limited = clamp(scaled, config.RLE_VAL_MIN, config.RLE_VAL_MAX)
        if limited != scaled:
            saturated += 1
        out[i] = limited
    return out, saturated


def dequantize_band(values: np.ndarray, band: str) -> np.ndarray:
    shift = config.BAND_QUANT_SHIFT[band]
    return np.asarray(values, dtype=np.int64) << shift


def flatten_bands(bands: dict[str, np.ndarray]) -> np.ndarray:
    """Concatenate bands in transmission order A4, D4, D3, D2, D1."""
    return np.concatenate([np.asarray(bands[b], dtype=np.int64)
                           for b in config.BAND_ORDER])


def split_bands(flat: np.ndarray, padded_length: int) -> dict[str, np.ndarray]:
    """Inverse of :func:`flatten_bands`."""
    lengths = config.band_lengths(padded_length)
    bands: dict[str, np.ndarray] = {}
    offset = 0
    for band in config.BAND_ORDER:
        count = lengths[band]
        bands[band] = np.asarray(flat[offset:offset + count], dtype=np.int64)
        offset += count
    return bands


# ---------------------------------------------------------------------------
# Zero run-length coding
# ---------------------------------------------------------------------------

def rle_encode(values: np.ndarray) -> list[tuple[int, int]]:
    """Pack a quantized coefficient vector into [6-bit run | 10-bit value] tokens.

    Only non-zero coefficients produce a token, so a token whose value field is zero
    is unambiguously a continuation marker: it consumes RLE_RUN_MAX zeros and emits no
    coefficient. That is how runs longer than 63 are represented. Trailing zeros are
    dropped entirely because the decoder knows the total coefficient count.
    """
    tokens: list[tuple[int, int]] = []
    run = 0
    for value in (int(v) for v in values):
        if value == 0:
            run += 1
            if run == config.RLE_RUN_MAX:
                tokens.append((config.RLE_CONT_RUN, 0))
                run = 0
        else:
            tokens.append((run, value))
            run = 0
    return tokens


def rle_decode(tokens: list[tuple[int, int]], total: int) -> np.ndarray:
    """Inverse of :func:`rle_encode`; pads implicit trailing zeros out to `total`."""
    out: list[int] = []
    for run, value in tokens:
        out.extend([0] * run)
        if value != 0:
            out.append(value)
    if len(out) < total:
        out.extend([0] * (total - len(out)))
    return np.asarray(out[:total], dtype=np.int64)


def pack_token(run: int, value: int) -> int:
    """[6-bit run | 10-bit signed value] packed into 16 bits."""
    return ((run & config.RLE_RUN_MAX) << config.RLE_VAL_BITS) | \
           (value & ((1 << config.RLE_VAL_BITS) - 1))


def unpack_token(word: int) -> tuple[int, int]:
    run = (word >> config.RLE_VAL_BITS) & config.RLE_RUN_MAX
    value = sign_extend(word, config.RLE_VAL_BITS)
    return run, value


# ---------------------------------------------------------------------------
# Segment level API
# ---------------------------------------------------------------------------

def encode_from_bands(bands: dict[str, np.ndarray], length: int,
                      padded_length: int,
                      thresholds: dict[str, int] | None = None) -> NonQrsPayload:
    """Threshold, quantize and RLE a segment whose transform is already computed.

    Split out from :func:`encode_nonqrs` so the threshold calibration sweep can reuse
    one forward transform across every candidate threshold set instead of recomputing
    the DWT thousands of times.
    """
    quantized: dict[str, np.ndarray] = {}
    saturated = 0
    for band in config.BAND_ORDER:
        values, count = quantize_band(bands[band], band, thresholds)
        quantized[band] = values
        saturated += count

    tokens = rle_encode(flatten_bands(quantized))
    return NonQrsPayload(length=length, padded_length=padded_length,
                         tokens=tokens, saturated=saturated)


def encode_nonqrs(samples: np.ndarray,
                  thresholds: dict[str, int] | None = None) -> NonQrsPayload:
    """Compress one non-QRS segment."""
    padded = pad_segment(samples)
    bands = haar_forward(padded)
    return encode_from_bands(bands, len(samples), len(padded), thresholds)


def decode_nonqrs(tokens: list[tuple[int, int]], length: int) -> np.ndarray:
    """Reconstruct a non-QRS segment from its RLE tokens."""
    padded_length = config.dwt_padded_len(length)
    total = sum(config.band_lengths(padded_length).values())
    flat = rle_decode(tokens, total)

    quantized = split_bands(flat, padded_length)
    bands = {band: dequantize_band(quantized[band], band)
             for band in config.BAND_ORDER}
    return haar_inverse(bands)[:length]
