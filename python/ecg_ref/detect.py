"""Adaptive QRS detection and stream routing (spec 4.1).

Chain: first-order IIR bandpass -> TKEO -> moving-average smoothing -> adaptive
threshold -> refractory-gated trigger -> backwards peak search -> QRS window framing.

All arithmetic is integer and mirrors hls/src/detect.cpp exactly. Because the ECG
record lives in BRAM (random access, not a live stream), detection runs as one pass
that produces R-peak indices, and routing/compression runs as a second pass. This is
deterministic and avoids needing a lookahead buffer for the +43-sample window.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import config
from .fixedpoint import asr


@dataclass
class DetectResult:
    bandpass: np.ndarray    # int64, first-order IIR bandpass output
    tkeo: np.ndarray        # int64, Teager-Kaiser energy
    smooth: np.ndarray      # int64, moving-average smoothed energy
    threshold: np.ndarray   # int64, adaptive threshold trace
    triggers: np.ndarray    # int64, threshold-crossing sample indices
    r_peaks: np.ndarray     # int64, refined R apex indices


@dataclass(frozen=True)
class Segment:
    kind: str               # "qrs" or "nonqrs"
    start: int              # inclusive
    end: int                # inclusive

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def iir_bandpass(x: np.ndarray) -> np.ndarray:
    """First-order IIR bandpass built from two shift-based EMAs.

    The low-pass EMA keeps the QRS band; subtracting a slower EMA removes baseline
    and P/T wave energy. Both accumulators carry IIR_FRAC_BITS extra fractional bits
    so the arithmetic shift can never stall on a zero increment.
    """
    n_total = len(x)
    out = np.zeros(n_total, dtype=np.int64)
    lp_acc = 0
    hp_acc = 0
    frac = config.IIR_FRAC_BITS
    for n in range(n_total):
        lp_acc += asr((int(x[n]) << frac) - lp_acc, config.IIR_LP_SHIFT)
        lp_out = asr(lp_acc, frac)
        hp_acc += asr((lp_out << frac) - hp_acc, config.IIR_HP_SHIFT)
        out[n] = lp_out - asr(hp_acc, frac)
    return out


def tkeo(bp: np.ndarray) -> np.ndarray:
    """Teager-Kaiser energy: psi[n] = x[n]^2 - x[n-1]*x[n+1].

    The first and last samples have no neighbour pair and are defined as zero, which
    is what the HLS shift register produces from its zero-initialised taps.
    """
    n_total = len(bp)
    psi = np.zeros(n_total, dtype=np.int64)
    if n_total >= 3:
        core = bp[1:n_total - 1].astype(np.int64)
        prev = bp[0:n_total - 2].astype(np.int64)
        nxt = bp[2:n_total].astype(np.int64)
        psi[1:n_total - 1] = core * core - prev * nxt
    return psi


def moving_average(psi: np.ndarray) -> np.ndarray:
    """Rectangular moving average of length MA_LEN with floor division.

    Taps before the start of the record are zero, matching the HLS circular buffer.
    """
    n_total = len(psi)
    length = config.MA_LEN
    padded = np.concatenate([np.zeros(length - 1, dtype=np.int64),
                             psi.astype(np.int64)])
    cumulative = np.concatenate([np.zeros(1, dtype=np.int64), np.cumsum(padded)])
    window_sum = cumulative[length:length + n_total] - cumulative[0:n_total]
    return window_sum // length          # numpy // floors, matching Python and asr


def track_and_trigger(smooth: np.ndarray, samples: np.ndarray
                      ) -> tuple[np.ndarray, list[int], list[int]]:
    """Adaptive threshold tracking, refractory gating and backwards peak search.

    Returns the threshold trace, the trigger indices, and the refined R indices.
    """
    n_total = len(smooth)
    threshold = np.zeros(n_total, dtype=np.int64)
    triggers: list[int] = []
    r_peaks: list[int] = []

    thr_acc = config.THRESH_INIT
    frac = config.THRESH_FRAC_BITS
    refractory_left = 0

    for n in range(n_total):
        # Spec 4.1.4: the threshold absorbs the current sample before comparison.
        thr_acc += asr((int(smooth[n]) << frac) - thr_acc, config.THRESH_SHIFT)
        thr = asr(thr_acc, frac)
        threshold[n] = thr

        if refractory_left > 0:
            refractory_left -= 1
            continue
        if n < config.THRESH_WARMUP:
            continue
        if int(smooth[n]) > config.THRESH_FACTOR * thr:
            triggers.append(n)
            r_peaks.append(_search_peak(samples, n))
            refractory_left = config.REFRACTORY

    return threshold, triggers, r_peaks


def _search_peak(samples: np.ndarray, trigger: int, n_total: int | None = None) -> int:
    """Locate the R apex as the largest |sample| in a window around the trigger.

    The window is two-sided: the energy peak normally lags the apex, but on a quiet
    baseline the crossing can fire on the leading flank instead, so a backwards-only
    search would settle on the rising edge. Ties take the earliest index.
    """
    n_total = len(samples) if n_total is None else n_total
    lo = max(0, trigger - config.PEAK_SEARCH_BACK)
    hi = min(n_total - 1, trigger + config.PEAK_SEARCH_FWD)
    best_idx = lo
    best_mag = -1
    for m in range(lo, hi + 1):
        mag = abs(int(samples[m]))
        if mag > best_mag:
            best_mag = mag
            best_idx = m
    return best_idx


def detect(samples: np.ndarray) -> DetectResult:
    """Run the whole detection chain on a preprocessed int16 record."""
    bp = iir_bandpass(samples)
    psi = tkeo(bp)
    smooth = moving_average(psi)
    threshold, triggers, r_peaks = track_and_trigger(smooth, samples)
    return DetectResult(
        bandpass=bp,
        tkeo=psi,
        smooth=smooth,
        threshold=threshold,
        triggers=np.asarray(triggers, dtype=np.int64),
        r_peaks=np.asarray(r_peaks, dtype=np.int64),
    )


# ---------------------------------------------------------------------------
# Window framing and stream routing
# ---------------------------------------------------------------------------

def frame_windows(r_peaks: np.ndarray, n_total: int) -> list[tuple[int, int]]:
    """Turn R indices into non-overlapping [Q_start, S_end] windows.

    The refractory period guarantees consecutive R peaks are at least REFRACTORY
    samples apart, but a 73-sample window spanning R-29..R+43 can still collide with
    its predecessor by one sample at the minimum spacing. The start is therefore
    clamped past the previous window's end, which is exactly the truncation case the
    spec's Module 1 verification calls out. Windows are consequently allowed to be
    shorter than 73 samples, and the packet carries an explicit length.
    """
    windows: list[tuple[int, int]] = []
    prev_end = -1
    for r in (int(v) for v in r_peaks):
        start = max(r - config.QRS_PRE, prev_end + 1, 0)
        end = min(r + config.QRS_POST, n_total - 1)
        if end < start:
            continue
        windows.append((start, end))
        prev_end = end
    return windows


def _emit_nonqrs(segments: list[Segment], start: int, end: int) -> None:
    """Append a non-QRS run, splitting it to respect the hardware buffer bound.

    See config.MAX_LOSSY_SEGMENT: the PL cannot buffer an arbitrarily long run. Real
    records never reach the cap, so this is a safety bound rather than an active code
    path, but the reference model must apply it identically to stay bit-exact.
    """
    cursor = start
    while cursor <= end:
        stop = min(cursor + config.MAX_LOSSY_SEGMENT - 1, end)
        segments.append(Segment("nonqrs", cursor, stop))
        cursor = stop + 1


def build_segments(windows: list[tuple[int, int]], n_total: int) -> list[Segment]:
    """Tile the whole record with alternating non-QRS and QRS segments.

    Every sample belongs to exactly one segment, so the decoder can reconstruct the
    record by concatenation.
    """
    segments: list[Segment] = []
    cursor = 0
    for start, end in windows:
        if start > cursor:
            _emit_nonqrs(segments, cursor, start - 1)
        segments.append(Segment("qrs", start, end))
        cursor = end + 1
    if cursor <= n_total - 1:
        _emit_nonqrs(segments, cursor, n_total - 1)
    return segments


def segment_record(samples: np.ndarray) -> tuple[list[Segment], DetectResult]:
    result = detect(samples)
    windows = frame_windows(result.r_peaks, len(samples))
    return build_segments(windows, len(samples)), result


# ---------------------------------------------------------------------------
# Detection scoring against .atr ground truth
# ---------------------------------------------------------------------------

@dataclass
class DetectionScore:
    true_positive: int
    false_positive: int
    false_negative: int

    @property
    def sensitivity(self) -> float:
        denom = self.true_positive + self.false_negative
        return 100.0 * self.true_positive / denom if denom else 0.0

    @property
    def ppv(self) -> float:
        denom = self.true_positive + self.false_positive
        return 100.0 * self.true_positive / denom if denom else 0.0

    @property
    def f1(self) -> float:
        s, p = self.sensitivity, self.ppv
        return 2 * s * p / (s + p) if (s + p) else 0.0


def score_detection(r_peaks: np.ndarray, annotations: np.ndarray,
                    tolerance: int | None = None) -> DetectionScore:
    """Match detections to annotations within +/-tolerance samples (AAMI EC57 style).

    Each annotation may claim at most one detection and vice versa; matching walks
    both sorted lists once and takes the nearest admissible pair.
    """
    tolerance = config.DETECT_TOLERANCE if tolerance is None else tolerance
    detections = sorted(int(v) for v in r_peaks)
    truth = sorted(int(v) for v in annotations)

    i = j = 0
    matched = 0
    while i < len(detections) and j < len(truth):
        delta = detections[i] - truth[j]
        if abs(delta) <= tolerance:
            matched += 1
            i += 1
            j += 1
        elif delta < 0:
            i += 1
        else:
            j += 1
    return DetectionScore(true_positive=matched,
                          false_positive=len(detections) - matched,
                          false_negative=len(truth) - matched)
