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


def compression_ratio(sample_count: int, compressed_bits: int) -> float:
    """CR = uncompressed bits / compressed bits, header included."""
    if compressed_bits <= 0:
        return float("inf")
    return (sample_count * config.SAMPLE_BITS) / compressed_bits


def latency_seconds(cycles: int, clock_hz: int = config.CLK_FREQ_HZ) -> float:
    return cycles / clock_hz


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
