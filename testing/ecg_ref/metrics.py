"""Compression ratio, PRD and latency metrics (spec 7)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import config


def prd(original: np.ndarray, reconstructed: np.ndarray) -> float:
    """Percentage RMS difference between two equal-length signals.

    Returns 0.0 for an all-zero reference (an empty or flat region) rather than
    dividing by zero.
    """
    ref = np.asarray(original, dtype=np.float64)
    rec = np.asarray(reconstructed, dtype=np.float64)
    if ref.shape != rec.shape:
        raise ValueError(f"shape mismatch: {ref.shape} vs {rec.shape}")
    if ref.size == 0:
        return 0.0
    energy = float(np.sum(ref * ref))
    if energy == 0.0:
        return 0.0 if float(np.sum((ref - rec) ** 2)) == 0.0 else float("inf")
    error = float(np.sum((ref - rec) ** 2))
    return 100.0 * float(np.sqrt(error / energy))


def prd_baseline_inclusive(original: np.ndarray, reconstructed: np.ndarray,
                           baseline: float | None = None) -> float:
    """PRD measured against the signal with its DC baseline restored.

    Spec section 7 says the reconstruction is "evaluated against raw signals" without
    stating whether the reference retains the ADC baseline offset. The two readings
    differ by more than an order of magnitude, because MIT-BIH sits on a 1024-count
    offset whose energy dwarfs the zero-mean waveform energy:

    * strict (:func:`prd`) divides by the energy of the bandpassed, zero-mean signal
      that is actually presented to the compressor. This is the conservative reading
      and the one this project reports as its primary figure.
    * baseline-inclusive (this function) divides by the energy of the signal including
      the offset, which is the convention used by most of the ECG compression
      literature that the < 2% target is drawn from.

    Both are reported so the numbers can never be mistaken for each other.
    """
    ref = np.asarray(original, dtype=np.float64)
    rec = np.asarray(reconstructed, dtype=np.float64)
    if ref.size == 0:
        return 0.0
    offset = float(config.MITBIH_ADC_ZERO) if baseline is None else float(baseline)
    shifted = ref + offset
    energy = float(np.sum(shifted * shifted))
    if energy == 0.0:
        return 0.0
    error = float(np.sum((ref - rec) ** 2))
    return 100.0 * float(np.sqrt(error / energy))


def snr_db(original: np.ndarray, reconstructed: np.ndarray) -> float:
    """10 log10(sum(x^2) / sum((x-xhat)^2)). inf if the error is exactly zero."""
    ref = np.asarray(original, dtype=np.float64)
    rec = np.asarray(reconstructed, dtype=np.float64)
    if ref.size == 0:
        return float("inf")
    energy = float(np.sum(ref * ref))
    error = float(np.sum((ref - rec) ** 2))
    if error == 0.0:
        return float("inf")
    if energy == 0.0:
        return float("-inf")
    return 10.0 * float(np.log10(energy / error))


def rmse(original: np.ndarray, reconstructed: np.ndarray) -> float:
    ref = np.asarray(original, dtype=np.float64)
    rec = np.asarray(reconstructed, dtype=np.float64)
    if ref.size == 0:
        return 0.0
    return float(np.sqrt(np.mean((ref - rec) ** 2)))


def compression_ratio(sample_count: int, compressed_bits: int) -> float:
    """CR = uncompressed bits / compressed bits, header included."""
    if compressed_bits <= 0:
        return float("inf")
    return (sample_count * config.SAMPLE_BITS) / compressed_bits


def latency_seconds(cycles: int, clock_hz: int = config.CLK_FREQ_HZ) -> float:
    return cycles / clock_hz


def prd_n1(original: np.ndarray, reconstructed: np.ndarray) -> float:
    """PRDN1: PRD with the reference mean removed (Němcová / metrics note).

    Distinct from :func:`prd`, whose denominator is ``sum(x^2)`` and does not
    subtract ``mean(x)``.
    """
    ref = np.asarray(original, dtype=np.float64)
    rec = np.asarray(reconstructed, dtype=np.float64)
    if ref.shape != rec.shape:
        raise ValueError(f"shape mismatch: {ref.shape} vs {rec.shape}")
    if ref.size == 0:
        return 0.0
    centered = ref - float(np.mean(ref))
    energy = float(np.sum(centered * centered))
    error = float(np.sum((ref - rec) ** 2))
    if energy == 0.0:
        return 0.0 if error == 0.0 else float("inf")
    return 100.0 * float(np.sqrt(error / energy))


def max_error(original: np.ndarray, reconstructed: np.ndarray,
              mask: np.ndarray | None = None) -> float:
    """MAX = max |x - xhat| over the selected samples (ADU).

    ``mask`` True keeps a sample. An empty selection returns 0.0.
    """
    ref = np.asarray(original, dtype=np.float64)
    rec = np.asarray(reconstructed, dtype=np.float64)
    if ref.shape != rec.shape:
        raise ValueError(f"shape mismatch: {ref.shape} vs {rec.shape}")
    if mask is not None:
        keep = np.asarray(mask, dtype=bool)
        if keep.shape != ref.shape:
            raise ValueError("mask shape mismatch")
        ref, rec = ref[keep], rec[keep]
    if ref.size == 0:
        return 0.0
    return float(np.max(np.abs(ref - rec)))


def cycle_bounds(r_peaks: np.ndarray, n_total: int) -> list[tuple[int, int]]:
    """Half-open [start, end) cycles from midpoints between compressor R-peaks.

    Cycle i runs from mid(R[i-1], R[i]) to mid(R[i], R[i+1]); the first and last
    cycles are clamped to the record. This is our definition (the metrics note
    does not specify cycle edges).
    """
    peaks = sorted({int(v) for v in np.asarray(r_peaks, dtype=np.int64)
                    if 0 <= int(v) < n_total})
    if not peaks or n_total <= 0:
        return []
    if len(peaks) == 1:
        return [(0, n_total)]
    bounds: list[tuple[int, int]] = []
    for i, _r in enumerate(peaks):
        start = 0 if i == 0 else (peaks[i - 1] + peaks[i]) // 2
        end = n_total if i == len(peaks) - 1 else (peaks[i] + peaks[i + 1]) // 2
        if end > start:
            bounds.append((start, end))
    return bounds


def per_cycle_max(original: np.ndarray, reconstructed: np.ndarray,
                  r_peaks: np.ndarray,
                  sample_mask: np.ndarray | None = None) -> list[float]:
    """Per-cycle MAX. Cycles with no samples after ``sample_mask`` are omitted."""
    ref = np.asarray(original, dtype=np.float64)
    rec = np.asarray(reconstructed, dtype=np.float64)
    keep = None if sample_mask is None else np.asarray(sample_mask, dtype=bool)
    values: list[float] = []
    for start, end in cycle_bounds(r_peaks, len(ref)):
        sl = slice(start, end)
        if keep is None:
            mask = None
        else:
            local = keep[sl]
            if not np.any(local):
                continue
            mask = local
        values.append(max_error(ref[sl], rec[sl], mask))
    return values


def sdnn_ms(r_peaks: np.ndarray, fs: float | None = None) -> float:
    """Sample SDNN of RR intervals in milliseconds. NaN if fewer than two intervals."""
    fs = float(config.FS if fs is None else fs)
    peaks = np.asarray(sorted(int(v) for v in r_peaks), dtype=np.int64)
    if peaks.size < 2:
        return float("nan")
    rr_ms = np.diff(peaks).astype(np.float64) * 1000.0 / fs
    if rr_ms.size < 2:
        # One interval: sample SD is undefined (M-1 = 0).
        return float("nan")
    return float(np.sqrt(np.sum((rr_ms - rr_ms.mean()) ** 2) / (rr_ms.size - 1)))


def psim_sdnn(sdnn_x: float, sdnn_hat: float) -> float:
    """PSim SDNN = (1 - |SDNN_x - SDNN_hat| / SDNN_x) * 100. NaN if SDNN_x is 0."""
    if not np.isfinite(sdnn_x) or not np.isfinite(sdnn_hat) or sdnn_x == 0.0:
        return float("nan")
    return float((1.0 - abs(sdnn_x - sdnn_hat) / sdnn_x) * 100.0)


@dataclass
class Evaluation:
    record: str
    sample_count: int
    compressed_bits: int
    compression_ratio: float
    prd_overall: float
    prd_qrs: float
    prd_nonqrs: float
    prd_overall_baseline: float
    qrs_count: int
    qrs_sample_count: int
    segment_count: int
    escapes: int
    saturated: int
    checksum_ok: bool
    max_abs_error: int

    @property
    def cr_pass(self) -> bool:
        return self.compression_ratio > config.TARGET_CR

    @property
    def prd_gated(self) -> float:
        """The PRD figure the < 2% constraint is applied to."""
        return (self.prd_overall_baseline if config.PRD_GATE_METRIC == "baseline"
                else self.prd_overall)

    @property
    def prd_pass(self) -> bool:
        return self.prd_gated < config.TARGET_PRD_OVERALL

    @property
    def qrs_pass(self) -> bool:
        return self.prd_qrs == config.TARGET_PRD_QRS

    @property
    def passed(self) -> bool:
        return self.cr_pass and self.prd_pass and self.qrs_pass and self.checksum_ok

    def summary(self) -> str:
        flag = "PASS" if self.passed else "FAIL"
        return (f"{self.record}: CR={self.compression_ratio:.2f} "
                f"PRD={self.prd_overall:.3f}% "
                f"PRD_base={self.prd_overall_baseline:.3f}% "
                f"PRD_qrs={self.prd_qrs:.3f}% "
                f"beats={self.qrs_count} [{flag}]")


def evaluate(record: str, original: np.ndarray, reconstructed: np.ndarray,
             qrs_mask: np.ndarray, compressed_bits: int, *,
             qrs_count: int, segment_count: int, escapes: int = 0,
             saturated: int = 0, checksum_ok: bool = True) -> Evaluation:
    """Bundle every metric for one record."""
    ref = np.asarray(original, dtype=np.int64)
    rec = np.asarray(reconstructed, dtype=np.int64)
    mask = np.asarray(qrs_mask, dtype=bool)

    return Evaluation(
        record=record,
        sample_count=len(ref),
        compressed_bits=compressed_bits,
        compression_ratio=compression_ratio(len(ref), compressed_bits),
        prd_overall=prd(ref, rec),
        prd_qrs=prd(ref[mask], rec[mask]),
        prd_nonqrs=prd(ref[~mask], rec[~mask]),
        prd_overall_baseline=prd_baseline_inclusive(ref, rec),
        qrs_count=qrs_count,
        qrs_sample_count=int(np.count_nonzero(mask)),
        segment_count=segment_count,
        escapes=escapes,
        saturated=saturated,
        checksum_ok=checksum_ok,
        max_abs_error=int(np.max(np.abs(ref - rec))) if len(ref) else 0,
    )


@dataclass
class QualityReport:
    """PRDN1 / MAX / PSim SDNN for one record (paper evaluation extras)."""

    record: str
    prdn1: float
    max_global: float
    max_nonqrs: float
    max_qrs: float
    max_nonqrs_cycle_mean: float
    max_nonqrs_cycle_worst: float
    n_cycles_nonqrs: int
    sdnn_orig_ms: float
    sdnn_rec_ms: float
    psim_sdnn: float
    n_peaks_orig: int
    n_peaks_rec: int


def evaluate_quality(record: str, original: np.ndarray, reconstructed: np.ndarray,
                     qrs_mask: np.ndarray, r_peaks: np.ndarray,
                     pt_orig: np.ndarray, pt_rec: np.ndarray) -> QualityReport:
    """Score the three extra metrics. ``r_peaks`` are compressor (TKEO) peaks."""
    ref = np.asarray(original)
    rec = np.asarray(reconstructed)
    mask = np.asarray(qrs_mask, dtype=bool)
    nonqrs = ~mask
    cycle_vals = per_cycle_max(ref, rec, r_peaks, sample_mask=nonqrs)
    sdnn_x = sdnn_ms(pt_orig)
    sdnn_hat = sdnn_ms(pt_rec)
    return QualityReport(
        record=record,
        prdn1=prd_n1(ref, rec),
        max_global=max_error(ref, rec),
        max_nonqrs=max_error(ref, rec, nonqrs),
        max_qrs=max_error(ref, rec, mask),
        max_nonqrs_cycle_mean=(float(np.mean(cycle_vals)) if cycle_vals
                               else float("nan")),
        max_nonqrs_cycle_worst=(float(np.max(cycle_vals)) if cycle_vals
                                else float("nan")),
        n_cycles_nonqrs=len(cycle_vals),
        sdnn_orig_ms=sdnn_x,
        sdnn_rec_ms=sdnn_hat,
        psim_sdnn=psim_sdnn(sdnn_x, sdnn_hat),
        n_peaks_orig=int(np.asarray(pt_orig).size),
        n_peaks_rec=int(np.asarray(pt_rec).size),
    )
