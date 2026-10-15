"""Integer golden model of the bior4.4 lossy path (brief v5, Phase A).

Hardware-accurate encoder: Q19.6 lifting, Q2.16 constants, 5.4 dead-zone
quantizer, method-(a) mirror boundaries, cA first-differences + per-sample
Rice, RLE + Rice on detail bands. Per-segment Rice state reset.
The host decoder may use floating point but the same lifting constants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np

from . import config, packer
from .fixedpoint import asr, clamp, zigzag_map, zigzag_unmap


# ---------------------------------------------------------------------------
# Q-format helpers
# ---------------------------------------------------------------------------

FRAC_DATA = config.BW_FRAC_DATA
FRAC_CONST = config.BW_FRAC_CONST
DATA_MIN = config.BW_DATA_MIN
DATA_MAX = config.BW_DATA_MAX


def to_q16(x: float) -> int:
    return int(round(x * (1 << FRAC_CONST)))


def from_q16(q: int) -> float:
    return q / float(1 << FRAC_CONST)


def sat25(v: int) -> int:
    return clamp(v, DATA_MIN, DATA_MAX)


def mul_q16(data_q6: int, const_q16: int) -> int:
    """data (Q19.6) * const (Q2.16) -> Q19.6, round-nearest, saturate 25-bit."""
    prod = data_q6 * const_q16
    half = 1 << (FRAC_CONST - 1)
    if prod >= 0:
        out = (prod + half) >> FRAC_CONST
    else:
        out = -(((-prod) + half) >> FRAC_CONST)
    return sat25(out)


LIFT_ALPHA_Q16 = to_q16(config.BW_LIFT_ALPHA)
LIFT_BETA_Q16 = to_q16(config.BW_LIFT_BETA)
LIFT_GAMMA_Q16 = to_q16(config.BW_LIFT_GAMMA)
LIFT_DELTA_Q16 = to_q16(config.BW_LIFT_DELTA)
SCALE_LOW_Q16 = to_q16(config.BW_SCALE_LOW)
SCALE_HIGH_Q16 = to_q16(config.BW_SCALE_HIGH)

# Real constants matching the quantized values the encoder actually uses.
LIFT_ALPHA = from_q16(LIFT_ALPHA_Q16)
LIFT_BETA = from_q16(LIFT_BETA_Q16)
LIFT_GAMMA = from_q16(LIFT_GAMMA_Q16)
LIFT_DELTA = from_q16(LIFT_DELTA_Q16)
SCALE_LOW = from_q16(SCALE_LOW_Q16)
SCALE_HIGH = from_q16(SCALE_HIGH_Q16)


def sample_to_q6(sample: int) -> int:
    return sat25(int(sample) << FRAC_DATA)


def q6_to_float(v: int) -> float:
    return v / float(1 << FRAC_DATA)


# ---------------------------------------------------------------------------
# Whole-sample symmetric reflection (edge not repeated)
# ---------------------------------------------------------------------------

def reflect_index(i: int, n: int) -> int:
    if n <= 0:
        raise ValueError("n must be positive")
    if n == 1:
        return 0
    while i < 0 or i > n - 1:
        if i < 0:
            i = -i
        else:
            i = 2 * (n - 1) - i
    return i


# ---------------------------------------------------------------------------
# Depth rule f(N)
# ---------------------------------------------------------------------------

def l_seg_of(n: int, l_max: int = config.BW_L_MAX,
             min_approx: int = config.BW_MIN_APPROX) -> int:
    """Largest L such that cA would still have >= min_approx samples, capped.

    N < 2 -> 0 (no transform). N such that N/2 < min_approx -> 0 (quantize
    samples directly as cA_0). Always stop before a band would be shorter
    than 2 samples.
    """
    if n < 2:
        return 0
    l = 0
    size = n
    while l < l_max and size >= 2:
        next_approx = (size + 1) // 2
        if next_approx < min_approx:
            break
        size = next_approx
        l += 1
        if size < 2:
            break
    return l


# ---------------------------------------------------------------------------
# Integer lifting (encoder)
# ---------------------------------------------------------------------------

def _reflect_pad(x: np.ndarray, extra: int = 1) -> np.ndarray:
    """Whole-sample symmetric pad: x[-1]=x[1], x[n]=x[n-2] (edge not repeated)."""
    n = len(x)
    if n == 0:
        return x
    if n == 1:
        return np.concatenate([np.repeat(x[0], extra), x, np.repeat(x[0], extra)])
    left = x[1:extra + 1][::-1]
    if len(left) < extra:
        left = np.concatenate([np.full(extra - len(left), x[reflect_index(-extra, n)]),
                               left])
    right = x[-2:-2 - extra:-1]
    if len(right) < extra:
        extra_r = [x[reflect_index(n + i, n)] for i in range(len(right), extra)]
        right = np.concatenate([right, np.asarray(extra_r, dtype=x.dtype)])
    return np.concatenate([left, x, right])


def _mul_q16_arr(data: np.ndarray, const_q16: int) -> np.ndarray:
    prod = data.astype(np.int64) * int(const_q16)
    half = 1 << (FRAC_CONST - 1)
    out = np.empty_like(prod)
    pos = prod >= 0
    out[pos] = (prod[pos] + half) >> FRAC_CONST
    out[~pos] = -(((-prod[~pos]) + half) >> FRAC_CONST)
    return np.clip(out, DATA_MIN, DATA_MAX)


def _lift_predict_update_int(y: np.ndarray, parity: int, const_q16: int) -> np.ndarray:
    n = len(y)
    padded = _reflect_pad(y, 1)
    idx = np.arange(parity, n, 2)
    neigh = padded[idx] + padded[idx + 2]
    out = y.copy()
    out[idx] = np.clip(y[idx] + _mul_q16_arr(neigh, const_q16), DATA_MIN, DATA_MAX)
    return out


def _lift_level_int(x: list[int] | np.ndarray) -> tuple[list[int], list[int]]:
    y = np.asarray(x, dtype=np.int64)
    n = len(y)
    if n < 2:
        return y.tolist(), []
    y = _lift_predict_update_int(y, 1, LIFT_ALPHA_Q16)
    y = _lift_predict_update_int(y, 0, LIFT_BETA_Q16)
    y = _lift_predict_update_int(y, 1, LIFT_GAMMA_Q16)
    y = _lift_predict_update_int(y, 0, LIFT_DELTA_Q16)
    low = _mul_q16_arr(y[0::2], SCALE_LOW_Q16)
    high = _mul_q16_arr(y[1::2], SCALE_HIGH_Q16)
    return low.tolist(), high.tolist()


def _lift_level_53_int(x: list[int] | np.ndarray) -> tuple[list[int], list[int]]:
    """Reversible LeGall 5/3 (JPEG2000 lossless / pywt bior2.2 structure)."""
    y = np.asarray(x, dtype=np.int64)
    n = len(y)
    if n < 2:
        return y.tolist(), []
    padded = _reflect_pad(y, 1)
    out = y.copy()
    odd = np.arange(1, n, 2)
    out[odd] = y[odd] - np.floor_divide(padded[odd] + padded[odd + 2], 2)
    padded = _reflect_pad(out, 1)
    even = np.arange(0, n, 2)
    out[even] = out[even] + np.floor_divide(padded[even] + padded[even + 2] + 2, 4)
    return out[0::2].tolist(), out[1::2].tolist()


def _lift_level_53_lin(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    y = np.asarray(x, dtype=np.float64)
    n = len(y)
    if n < 2:
        return y.copy(), np.zeros(0, dtype=np.float64)
    padded = _reflect_pad(y, 1)
    out = y.copy()
    odd = np.arange(1, n, 2)
    out[odd] = y[odd] - 0.5 * (padded[odd] + padded[odd + 2])
    padded = _reflect_pad(out, 1)
    even = np.arange(0, n, 2)
    out[even] = out[even] + 0.25 * (padded[even] + padded[even + 2])
    return out[0::2].copy(), out[1::2].copy()


def _inv_level_53_lin(low: np.ndarray, high: np.ndarray) -> np.ndarray:
    n_low = len(low)
    n_high = len(high)
    n = n_low + n_high
    y = np.zeros(n, dtype=np.float64)
    y[0::2] = np.asarray(low, dtype=np.float64)
    if n_high:
        y[1::2] = np.asarray(high, dtype=np.float64)
    padded = _reflect_pad(y, 1)
    even = np.arange(0, n, 2)
    y[even] = y[even] - 0.25 * (padded[even] + padded[even + 2])
    padded = _reflect_pad(y, 1)
    odd = np.arange(1, n, 2)
    y[odd] = y[odd] + 0.5 * (padded[odd] + padded[odd + 2])
    return y


def _inv_level_53_int(low: list[int], high: list[int]) -> list[int]:
    n_low = len(low)
    n_high = len(high)
    n = n_low + n_high
    y = np.zeros(n, dtype=np.int64)
    y[0::2] = np.asarray(low, dtype=np.int64)
    if n_high:
        y[1::2] = np.asarray(high, dtype=np.int64)
    padded = _reflect_pad(y, 1)
    even = np.arange(0, n, 2)
    y[even] = y[even] - np.floor_divide(padded[even] + padded[even + 2] + 2, 4)
    padded = _reflect_pad(y, 1)
    odd = np.arange(1, n, 2)
    y[odd] = y[odd] + np.floor_divide(padded[odd] + padded[odd + 2], 2)
    return y.tolist()


def dwt_forward_int(samples_q6: list[int], levels: int,
                    wavelet: str = "97") -> dict[str, list[int]]:
    """Full integer DWT. Bands named cA_L / cD_j."""
    if levels <= 0:
        return {"cA0": list(samples_q6)}
    bands: dict[str, list[int]] = {}
    low = list(samples_q6)
    step = _lift_level_53_int if wavelet == "53" else _lift_level_int
    for j in range(1, levels + 1):
        low, high = step(low)
        bands[f"cD{j}"] = high
    bands[f"cA{levels}"] = low
    return bands


# ---------------------------------------------------------------------------
# Linear (float) lifting — decoder inverse and A_u construction
# ---------------------------------------------------------------------------

def _lift_step_lin(y: np.ndarray, parity: int, const: float, sign: float = 1.0
                   ) -> np.ndarray:
    n = len(y)
    padded = _reflect_pad(y, 1)
    idx = np.arange(parity, n, 2)
    out = y.copy()
    out[idx] = y[idx] + sign * const * (padded[idx] + padded[idx + 2])
    return out


def _lift_level_lin(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    y = np.asarray(x, dtype=np.float64)
    n = len(y)
    if n < 2:
        return y.copy(), np.zeros(0, dtype=np.float64)
    y = _lift_step_lin(y, 1, LIFT_ALPHA)
    y = _lift_step_lin(y, 0, LIFT_BETA)
    y = _lift_step_lin(y, 1, LIFT_GAMMA)
    y = _lift_step_lin(y, 0, LIFT_DELTA)
    return SCALE_LOW * y[0::2], SCALE_HIGH * y[1::2]


def _inv_level_lin(low: np.ndarray, high: np.ndarray) -> np.ndarray:
    n_low = len(low)
    n_high = len(high)
    n = n_low + n_high
    y = np.zeros(n, dtype=np.float64)
    if SCALE_LOW != 0.0:
        y[0::2] = np.asarray(low, dtype=np.float64) / SCALE_LOW
    if SCALE_HIGH != 0.0 and n_high:
        y[1::2] = np.asarray(high, dtype=np.float64) / SCALE_HIGH
    y = _lift_step_lin(y, 0, LIFT_DELTA, sign=-1.0)
    y = _lift_step_lin(y, 1, LIFT_GAMMA, sign=-1.0)
    y = _lift_step_lin(y, 0, LIFT_BETA, sign=-1.0)
    y = _lift_step_lin(y, 1, LIFT_ALPHA, sign=-1.0)
    return y


def dwt_forward_lin(samples: np.ndarray, levels: int,
                    wavelet: str = "97") -> dict[str, np.ndarray]:
    if levels <= 0:
        return {"cA0": np.asarray(samples, dtype=np.float64)}
    bands: dict[str, np.ndarray] = {}
    low = np.asarray(samples, dtype=np.float64)
    step = _lift_level_53_lin if wavelet == "53" else _lift_level_lin
    for j in range(1, levels + 1):
        low, high = step(low)
        bands[f"cD{j}"] = high
    bands[f"cA{levels}"] = low
    return bands


def dwt_inverse_lin(bands: dict[str, np.ndarray], levels: int,
                    wavelet: str = "97") -> np.ndarray:
    if levels <= 0:
        return np.asarray(bands.get("cA0", np.zeros(0)), dtype=np.float64)
    low = np.asarray(bands[f"cA{levels}"], dtype=np.float64)
    inv = _inv_level_53_lin if wavelet == "53" else _inv_level_lin
    for j in range(levels, 0, -1):
        high = np.asarray(bands.get(f"cD{j}", np.zeros(max(len(low) - 1, 0))),
                          dtype=np.float64)
        low = inv(low, high)
    return low


# ---------------------------------------------------------------------------
# Band geometry
# ---------------------------------------------------------------------------

def band_order(levels: int) -> list[str]:
    """Coding order: coarsest approx first, then details coarse to fine."""
    if levels <= 0:
        return ["cA0"]
    return [f"cA{levels}"] + [f"cD{j}" for j in range(levels, 1, -1)]


def band_lengths_method_a(n: int, levels: int) -> dict[str, int]:
    if levels <= 0:
        return {"cA0": n}
    out: dict[str, int] = {}
    size = n
    for j in range(1, levels + 1):
        n_low = (size + 1) // 2
        n_high = size // 2
        out[f"cD{j}"] = n_high
        size = n_low
    out[f"cA{levels}"] = size
    return out


def _centers_in_segment(n_ext: int, n_seg: int, margin: int, step: int,
                        odd: bool) -> list[int]:
    """Indices i of the current band whose original-domain center is inside
    the segment [margin, margin+n_seg-1] of an extended signal of length n_ext.
    """
    lo, hi = margin, margin + n_seg - 1
    out: list[int] = []
    i = 0
    while True:
        center = step * (2 * i + 1) if odd else step * i
        if center > n_ext - 1 and center > hi:
            break
        if lo <= center <= hi:
            out.append(i)
        i += 1
        if i > n_ext + 2:
            break
    return out


def band_indices_method_b(n_seg: int, levels: int, margin: int
                          ) -> dict[str, list[int]]:
    n_ext = n_seg + 2 * margin
    if levels <= 0:
        return {"cA0": list(range(n_seg))}
    idx: dict[str, list[int]] = {}
    for j in range(1, levels + 1):
        step = 1 << (j - 1)
        idx[f"cD{j}"] = _centers_in_segment(n_ext, n_seg, margin, step, True)
    idx[f"cA{levels}"] = _centers_in_segment(
        n_ext, n_seg, margin, 1 << levels, False)
    return idx


def band_lengths_method_b(n_seg: int, levels: int, margin: int) -> dict[str, int]:
    return {k: len(v) for k, v in band_indices_method_b(n_seg, levels, margin).items()}


# ---------------------------------------------------------------------------
# Dead-zone quantizer (paper Eq. 3-4)
# ---------------------------------------------------------------------------

def dzq_q(c_q6: int, t_q6: int, inv_d_q16: int) -> int:
    mag = abs(c_q6)
    if mag < t_q6:
        return 0
    # |c|/Delta = |c_q6| * invD / 2^22 ; floor(x+0.5)
    prod = mag * inv_d_q16
    q = (prod + (1 << 21)) >> 22
    if q < 1:
        q = 1
    return q if c_q6 >= 0 else -q


def dzq_dequant(q: int, delta_real: float) -> float:
    return q * delta_real


def make_quant_params(t_adu: float, a: float) -> dict:
    """Representable T, Delta (Q19.6) and 1/Delta (Q0.16)."""
    t_q6 = max(1, int(round(t_adu * (1 << FRAC_DATA))))
    delta_q6 = max(1, int(round(a * t_q6)))
    t_real = t_q6 / float(1 << FRAC_DATA)
    delta_real = delta_q6 / float(1 << FRAC_DATA)
    inv_d_q16 = int(round((1 << FRAC_CONST) / delta_real)) if delta_real else 0
    return {
        "t_q6": t_q6, "delta_q6": delta_q6,
        "t_real": t_real, "delta_real": delta_real,
        "inv_d_q16": inv_d_q16, "a": a,
    }


# ---------------------------------------------------------------------------
# RLE + zigzag + Golomb-Rice
# ---------------------------------------------------------------------------

def rle_tokens(values: list[int]) -> list[tuple[int, int]]:
    tokens: list[tuple[int, int]] = []
    run = 0
    for v in values:
        if v == 0:
            run += 1
        else:
            tokens.append((run, v))
            run = 0
    return tokens


def rle_expand(tokens: list[tuple[int, int]], total: int) -> list[int]:
    out: list[int] = []
    for run, value in tokens:
        out.extend([0] * run)
        out.append(value)
    if len(out) < total:
        out.extend([0] * (total - len(out)))
    return out[:total]


def rice_put(writer: packer.BitWriter, value: int, k: int) -> None:
    if value < 0:
        raise ValueError("Rice codes non-negative values")
    k = max(0, min(k, config.BW_RICE_K_MAX))
    q = value >> k if k else value
    r = value & ((1 << k) - 1) if k else 0
    if q >= config.BW_RICE_Q_ESC:
        writer.put((1 << config.BW_RICE_Q_ESC) - 1, config.BW_RICE_Q_ESC)
        writer.put(value & 0xFFFFFFFF, config.BW_RICE_ESC_BITS)
    else:
        writer.put(((1 << q) - 1) << 1, q + 1)
        if k:
            writer.put(r, k)


def rice_get(reader: packer.BitReader, k: int) -> int:
    k = max(0, min(k, config.BW_RICE_K_MAX))
    q = 0
    escaped = False
    while True:
        if q == config.BW_RICE_Q_ESC:
            escaped = True
            break
        if reader.get(1) == 0:
            break
        q += 1
    if escaped:
        return reader.get(config.BW_RICE_ESC_BITS)
    r = reader.get(k) if k else 0
    return (q << k) | r


def k_run_default(band: str) -> int:
    table = getattr(config, "BW_RICE_K_TABLE", {})
    if band in table:
        return table[band][0]
    return config.BW_RICE_K_RUN


def k_lvl_default(band: str) -> int:
    table = getattr(config, "BW_RICE_K_TABLE", {})
    if band in table:
        return table[band][1]
    if band.startswith("cA"):
        return config.BW_RICE_K_LVL_APPROX
    return config.BW_RICE_K_LVL_DETAIL


# Rice k for the per-subband token-count prefix (lets an all-zero band emit
# n_tokens=0 and nothing else; decoder knows the subband length).
BW_RICE_K_NTOK = 4


def _dpcm(values: list[int]) -> list[int]:
    out = []
    prev = 0
    for v in values:
        out.append(v - prev)
        prev = v
    return out


def _idpcm(diffs: list[int]) -> list[int]:
    out = []
    prev = 0
    for d in diffs:
        v = d + prev
        out.append(v)
        prev = v
    return out


def encode_subband(writer: packer.BitWriter, values: list[int], band: str,
                   k_run: int | None = None, k_lvl: int | None = None) -> None:
    k_run = k_run_default(band) if k_run is None else k_run
    k_lvl = k_lvl_default(band) if k_lvl is None else k_lvl
    if band.startswith("cA") and not config.BW_CA_RLE:
        vals = _dpcm(values) if config.BW_CA_DPCM else list(values)
        rice_put(writer, len(vals), BW_RICE_K_NTOK)
        for v in vals:
            rice_put(writer, zigzag_map(v), k_lvl)
        return
    src = _dpcm(values) if (band.startswith("cA") and config.BW_CA_DPCM) else values
    tokens = rle_tokens(src)
    rice_put(writer, len(tokens), BW_RICE_K_NTOK)
    for run, level in tokens:
        rice_put(writer, run, k_run)
        rice_put(writer, zigzag_map(level), k_lvl)


def decode_subband(reader: packer.BitReader, length: int, band: str,
                   k_run: int | None = None, k_lvl: int | None = None
                   ) -> list[int]:
    k_run = k_run_default(band) if k_run is None else k_run
    k_lvl = k_lvl_default(band) if k_lvl is None else k_lvl
    if band.startswith("cA") and not config.BW_CA_RLE:
        n_tokens = rice_get(reader, BW_RICE_K_NTOK)
        diffs = [zigzag_unmap(rice_get(reader, k_lvl)) for _ in range(n_tokens)]
        levels = _idpcm(diffs) if config.BW_CA_DPCM else diffs
        if len(levels) < length:
            levels.extend([0] * (length - len(levels)))
        return levels[:length]
    n_tokens = rice_get(reader, BW_RICE_K_NTOK)
    tokens: list[tuple[int, int]] = []
    for _ in range(n_tokens):
        run = rice_get(reader, k_run)
        z = rice_get(reader, k_lvl)
        tokens.append((run, zigzag_unmap(z)))
    raw = rle_expand(tokens, length)
    if band.startswith("cA") and config.BW_CA_DPCM:
        return _idpcm(raw)
    return raw


# ---------------------------------------------------------------------------
# Neighbor / extension (method a / b)
# ---------------------------------------------------------------------------

@dataclass
class EdgeSpec:
    kind: str               # "a" mirror or "b" real neighbor
    samples: np.ndarray     # neighbor samples (QRS region), possibly empty


def _mirror_prefix(x: np.ndarray, m: int) -> np.ndarray:
    """m samples of whole-sample symmetric extension to the left of x."""
    n = len(x)
    out = np.zeros(m, dtype=np.int64)
    for i in range(m):
        out[m - 1 - i] = x[reflect_index(-1 - i, n)]
    return out


def _mirror_suffix(x: np.ndarray, m: int) -> np.ndarray:
    n = len(x)
    out = np.zeros(m, dtype=np.int64)
    for i in range(m):
        out[i] = x[reflect_index(n + i, n)]
    return out


def build_extended(segment: np.ndarray, left: EdgeSpec, right: EdgeSpec,
                   margin: int) -> np.ndarray:
    """Extended signal of length N+2M (int16 domain)."""
    seg = np.asarray(segment, dtype=np.int64)
    n = len(seg)

    def side(edge: EdgeSpec, which: str) -> np.ndarray:
        if margin <= 0:
            return np.zeros(0, dtype=np.int64)
        if edge.kind == "b" and len(edge.samples) > 0:
            neigh = np.asarray(edge.samples, dtype=np.int64)
            take = min(margin, len(neigh))
            if which == "left":
                real = neigh[-take:]
                if take < margin:
                    extra = _mirror_prefix(real, margin - take)
                    return np.concatenate([extra, real])
                return real
            real = neigh[:take]
            if take < margin:
                extra = _mirror_suffix(real, margin - take)
                return np.concatenate([real, extra])
            return real
        if which == "left":
            return _mirror_prefix(seg, margin)
        return _mirror_suffix(seg, margin)

    return np.concatenate([side(left, "left"), seg, side(right, "right")])


# ---------------------------------------------------------------------------
# Quantize / collect coded coefficients
# ---------------------------------------------------------------------------

def quantize_bands(bands_q6: dict[str, list[int]], levels: int,
                   t_q6: int, inv_d_q16: int,
                   keep_idx: dict[str, list[int]] | None = None
                   ) -> dict[str, list[int]]:
    coded: dict[str, list[int]] = {}
    for name in band_order(levels):
        src = bands_q6.get(name, [])
        if keep_idx is not None and name in keep_idx:
            src = [src[i] for i in keep_idx[name] if i < len(src)]
        coded[name] = [dzq_q(v, t_q6, inv_d_q16) for v in src]
    # cD1 is discarded: never coded even if present
    coded.pop("cD1", None)
    return coded


# ---------------------------------------------------------------------------
# Packet I/O
# ---------------------------------------------------------------------------

@dataclass
class BwPayload:
    length: int
    levels: int
    margin: int
    method: str                 # "a" or "b" (both edges recorded separately)
    left_edge: str
    right_edge: str
    coded: dict[str, list[int]] = field(default_factory=dict)
    word_count: int = 0
    rice_bits: int = 0          # entropy bits before word padding
    header_bits: int = 1 + 16 + 16
    pad_bits: int = 0
    rice_k: dict[str, tuple[int, int]] = field(default_factory=dict)
    wavelet: str = "97"


def rice_bits_coded(coded: dict[str, list[int]], levels: int, *,
                    no_rle_approx: bool = False, dpcm_approx: bool = False,
                    z_minus_1: bool = False) -> int:
    """Entropy-bit count for coder ablations (does not change reconstruction)."""
    w = packer.BitWriter()
    for name in band_order(levels):
        if name == "cD1":
            continue
        vals = list(coded.get(name, []))
        if name.startswith("cA") and dpcm_approx:
            vals = _dpcm(vals)
        k_run = k_run_default(name)
        k_lvl = k_lvl_default(name)
        if name.startswith("cA") and no_rle_approx:
            rice_put(w, len(vals), BW_RICE_K_NTOK)
            for v in vals:
                z = zigzag_map(v)
                rice_put(w, z, k_lvl)
            continue
        tokens = rle_tokens(vals)
        rice_put(w, len(tokens), BW_RICE_K_NTOK)
        for run, level in tokens:
            rice_put(w, run, k_run)
            z = zigzag_map(level)
            if z_minus_1:
                z = max(z - 1, 0)
            rice_put(w, z, k_lvl)
    return w.tell()


def _rice_body(coded: dict[str, list[int]], levels: int,
               rice_k: dict[str, tuple[int, int]] | None = None) -> packer.BitWriter:
    body = packer.BitWriter()
    for name in band_order(levels):
        if name == "cD1":
            continue
        kr, kl = (None, None) if rice_k is None else rice_k.get(name, (None, None))
        encode_subband(body, coded.get(name, []), name, k_run=kr, k_lvl=kl)
    return body


def write_nonqrs_bw(writer: packer.BitWriter, payload: BwPayload) -> None:
    """tag | length | payload_word_count | rice bits | align32.

    After the 33-bit header the stream is aligned so the rice payload is an
    integer number of 32-bit words (lets a decoder skip without Rice-decoding).
    """
    body = _rice_body(payload.coded, payload.levels, rice_k=payload.rice_k or None)
    rice_bits = body.tell()
    rice_pad = (-rice_bits) % config.WORD_BITS
    body.align32()
    words = body.to_words()
    payload.rice_bits = rice_bits
    payload.word_count = len(words)
    payload.pad_bits = rice_pad

    writer.put(config.TAG_NONQRS, 1)
    writer.put(payload.length, config.LEN_FIELD_BITS)
    writer.put(payload.word_count, config.SYMCOUNT_FIELD_BITS)
    writer.align32()
    for w in words:
        writer.put(w, config.WORD_BITS)


def read_nonqrs_bw(reader: packer.BitReader, levels: int,
                   lengths: dict[str, int]) -> dict[str, list[int]]:
    """Called after tag and length have been consumed? Full packet from tag.

    This helper expects to be positioned at the tag bit.
    """
    tag = reader.get(1)
    if tag != config.TAG_NONQRS:
        raise ValueError(f"expected TAG_NONQRS, got {tag}")
    length = reader.get(config.LEN_FIELD_BITS)
    word_count = reader.get(config.SYMCOUNT_FIELD_BITS)
    reader.align32()
    words = [reader.get(config.WORD_BITS) for _ in range(word_count)]
    inner = packer.BitReader(words)
    coded: dict[str, list[int]] = {}
    for name in band_order(levels):
        if name == "cD1":
            continue
        n = lengths.get(name, 0)
        coded[name] = decode_subband(inner, n, name)
    coded["_length"] = [length]  # stash
    return coded


# ---------------------------------------------------------------------------
# Method-(b) linear solve (host)
# ---------------------------------------------------------------------------

def _inside_coeff_vector_lin(bands: dict[str, np.ndarray], levels: int,
                             n_seg: int, margin: int,
                             include_cd1: bool) -> np.ndarray:
    idx = clip_keep_idx(bands, band_indices_method_b(n_seg, levels, margin))
    ordered: list[str] = []
    if include_cd1 and levels >= 1:
        ordered.append("cD1")
    for name in band_order(levels):
        if name not in ordered:
            ordered.append(name)
    parts: list[np.ndarray] = []
    for name in ordered:
        src = np.asarray(bands.get(name, np.zeros(0)), dtype=np.float64)
        take = idx.get(name, [])
        parts.append(np.array([float(src[i]) if i < len(src) else 0.0 for i in take],
                              dtype=np.float64))
    if not parts:
        return np.zeros(0, dtype=np.float64)
    return np.concatenate(parts)


@lru_cache(maxsize=None)
def _a_u_pinv(n_seg: int, levels: int, margin: int,
              left_kind: str, right_kind: str,
              wavelet: str = "97") -> tuple[np.ndarray, np.ndarray]:
    """Return (pinv(A_u), cond_estimate). Columns = unknown segment samples."""
    n_ext = n_seg + 2 * margin
    n_eq = n_seg
    a_u = np.zeros((n_eq, n_seg), dtype=np.float64)
    for col in range(n_seg):
        x = np.zeros(n_ext, dtype=np.float64)
        x[margin + col] = 1.0
        bands = dwt_forward_lin(x, levels, wavelet=wavelet)
        a_u[:, col] = _inside_coeff_vector_lin(bands, levels, n_seg, margin, True)
    cond = float(np.linalg.cond(a_u)) if n_seg else float("inf")
    pinv = np.linalg.pinv(a_u) if n_seg else a_u.T
    return pinv, cond


def solve_method_b(coded: dict[str, list[int]], n_seg: int, levels: int,
                   margin: int, left: EdgeSpec, right: EdgeSpec,
                   delta_real: float, wavelet: str = "97"
                   ) -> tuple[np.ndarray, float]:
    """Least-squares solve for the unknown segment samples. Returns (x_u, cond)."""
    pinv, cond = _a_u_pinv(n_seg, levels, margin, left.kind, right.kind, wavelet)
    # c_q: dequantized inside rows, cD1 = 0
    idx = band_indices_method_b(n_seg, levels, margin)
    cq_parts: list[np.ndarray] = []
    if levels >= 1:
        cq_parts.append(np.zeros(len(idx.get("cD1", [])), dtype=np.float64))
    for name in band_order(levels):
        qs = coded.get(name, [])
        cq_parts.append(np.array([dzq_dequant(q, delta_real) for q in qs],
                                 dtype=np.float64))
    c_q = np.concatenate(cq_parts) if cq_parts else np.zeros(0)

    # A_k x_k : transform of extended signal with segment zeros, margins filled
    zeros = np.zeros(n_seg, dtype=np.int64)
    ext_k = build_extended(zeros, left, right, margin).astype(np.float64)
    bands_k = dwt_forward_lin(ext_k, levels, wavelet=wavelet)
    a_k_xk = _inside_coeff_vector_lin(bands_k, levels, n_seg, margin, True)

    rhs = c_q - a_k_xk
    if pinv.shape[1] != rhs.shape[0]:
        # length mismatch: fall back to lstsq on the fly
        n_ext = n_seg + 2 * margin
        a_u = np.zeros((len(rhs), n_seg), dtype=np.float64)
        for col in range(n_seg):
            x = np.zeros(n_ext, dtype=np.float64)
            x[margin + col] = 1.0
            bands = dwt_forward_lin(x, levels, wavelet=wavelet)
            vec = _inside_coeff_vector_lin(bands, levels, n_seg, margin, True)
            a_u[:len(vec), col] = vec
        x_u, *_ = np.linalg.lstsq(a_u, rhs, rcond=None)
        return x_u, float(np.linalg.cond(a_u)) if n_seg else float("inf")
    x_u = pinv @ rhs
    return x_u, cond


# ---------------------------------------------------------------------------
# Segment encode / reconstruct
# ---------------------------------------------------------------------------

def clip_keep_idx(bands: dict, keep_idx: dict[str, list[int]]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for name, ids in keep_idx.items():
        n_band = len(bands.get(name, []))
        out[name] = [i for i in ids if 0 <= i < n_band]
    return out


def inclusion_equation_count(n_seg: int, levels: int, margin: int) -> int:
    """Number of solver rows (inside cD1 + coded bands). Must equal N."""
    dummy = [0] * (n_seg + 2 * margin)
    bands = dwt_forward_int(dummy, levels)
    idx = clip_keep_idx(bands, band_indices_method_b(n_seg, levels, margin))
    n_eq = len(idx.get("cD1", []))
    for name in band_order(levels):
        n_eq += len(idx.get(name, []))
    return n_eq


@dataclass
class AnalyzedSegment:
    samples: np.ndarray
    left: EdgeSpec
    right: EdgeSpec
    n: int
    levels: int
    margin: int
    method: str
    coeffs_q6: dict[str, list[int]]   # already restricted to coded (and cD1) rows
    wavelet: str = "97"


def analyze_segment(samples: np.ndarray, left: EdgeSpec, right: EdgeSpec,
                    margin: int = config.BW_BND_MARGIN,
                    l_max: int = config.BW_L_MAX,
                    method: str = config.BW_DEFAULT_METHOD,
                    wavelet: str = "97") -> AnalyzedSegment:
    seg = np.asarray(samples, dtype=np.int64)
    n = len(seg)
    levels = l_seg_of(n, l_max)
    if method != "b" or levels == 0 or n == 0:
        x_int = [sample_to_q6(int(v)) for v in seg]
        bands = dwt_forward_int(x_int, levels, wavelet=wavelet)
        coeffs = {k: list(v) for k, v in bands.items()}
        return AnalyzedSegment(seg, EdgeSpec("a", np.zeros(0, dtype=np.int64)),
                               EdgeSpec("a", np.zeros(0, dtype=np.int64)),
                               n, levels, 0, "a", coeffs, wavelet)
    ext = build_extended(seg, left, right, margin)
    x_int = [sample_to_q6(int(v)) for v in ext]
    bands = dwt_forward_int(x_int, levels, wavelet=wavelet)
    idx = clip_keep_idx(bands, band_indices_method_b(n, levels, margin))
    coeffs: dict[str, list[int]] = {}
    for name, ids in idx.items():
        src = bands.get(name, [])
        coeffs[name] = [src[i] for i in ids]
    return AnalyzedSegment(seg, left, right, n, levels, margin, "b", coeffs,
                           wavelet)


def quantize_analyzed(an: AnalyzedSegment, t_q6: int, inv_d_q16: int) -> BwPayload:
    coded = {name: [dzq_q(v, t_q6, inv_d_q16) for v in vals]
             for name, vals in an.coeffs_q6.items() if name != "cD1"}
    return BwPayload(length=an.n, levels=an.levels, margin=an.margin,
                     method=an.method, left_edge=an.left.kind,
                     right_edge=an.right.kind, coded=coded,
                     wavelet=an.wavelet)


def encode_segment(samples: np.ndarray, left: EdgeSpec, right: EdgeSpec,
                   t_q6: int, inv_d_q16: int, delta_real: float,
                   margin: int = config.BW_BND_MARGIN,
                   l_max: int = config.BW_L_MAX,
                   method: str = config.BW_DEFAULT_METHOD,
                   wavelet: str = "97") -> BwPayload:
    del delta_real  # used at decode time; kept in the signature for call-site symmetry
    seg = np.asarray(samples, dtype=np.int64)
    n = len(seg)
    levels = l_seg_of(n, l_max)
    if method != "b" or levels == 0 or n == 0:
        x_int = [sample_to_q6(int(v)) for v in seg]
        bands = dwt_forward_int(x_int, levels, wavelet=wavelet)
        coded = quantize_bands(bands, levels, t_q6, inv_d_q16, keep_idx=None)
        return BwPayload(length=n, levels=levels, margin=0, method="a",
                         left_edge="a", right_edge="a", coded=coded,
                         wavelet=wavelet)

    left_use = left
    right_use = right
    ext = build_extended(seg, left_use, right_use, margin)
    x_int = [sample_to_q6(int(v)) for v in ext]
    bands = dwt_forward_int(x_int, levels, wavelet=wavelet)
    idx = clip_keep_idx(bands, band_indices_method_b(n, levels, margin))
    coded = quantize_bands(bands, levels, t_q6, inv_d_q16, keep_idx=idx)
    return BwPayload(length=n, levels=levels, margin=margin, method="b",
                     left_edge=left_use.kind, right_edge=right_use.kind,
                     coded=coded, wavelet=wavelet)


def reconstruct_segment(payload: BwPayload, left: EdgeSpec, right: EdgeSpec,
                        delta_real: float) -> np.ndarray:
    n = payload.length
    levels = payload.levels
    if payload.method == "a" or levels == 0:
        bands_hat: dict[str, np.ndarray] = {}
        lengths = band_lengths_method_a(n, levels)
        for name, ln in lengths.items():
            if name == "cD1":
                bands_hat[name] = np.zeros(ln, dtype=np.float64)
            else:
                qs = payload.coded.get(name, [0] * ln)
                bands_hat[name] = np.array(
                    [dzq_dequant(q, delta_real) for q in qs], dtype=np.float64)
        rec = dwt_inverse_lin(bands_hat, levels, wavelet=payload.wavelet)
        return rec[:n]

    x_u, _cond = solve_method_b(
        payload.coded, n, levels, payload.margin, left, right, delta_real,
        wavelet=payload.wavelet)
    return x_u


def entropy_roundtrip(coded: dict[str, list[int]], levels: int,
                      rice_k: dict[str, tuple[int, int]] | None = None
                      ) -> dict[str, list[int]]:
    """decode(encode(q)) for the quantized subbands (bit-exact check)."""
    body = _rice_body(coded, levels, rice_k=rice_k)
    body.align32()
    reader = packer.BitReader(body.to_words())
    lengths = {name: len(vals) for name, vals in coded.items()}
    out: dict[str, list[int]] = {}
    for name in band_order(levels):
        if name == "cD1":
            continue
        kr, kl = (None, None) if rice_k is None else rice_k.get(name, (None, None))
        out[name] = decode_subband(reader, lengths.get(name, 0), name,
                                   k_run=kr, k_lvl=kl)
    return out
