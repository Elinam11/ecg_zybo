"""Offline preprocessing: MIT-BIH -> MLII -> Butterworth bandpass -> int16 -> .coe.

This is the only stage that uses floating point. Everything downstream (Python and
HLS alike) operates on the int16 array produced here, which is also the array loaded
into the FPGA's BRAM. PRD is therefore measured against this array, because the
hardware never observes anything else.
"""

from __future__ import annotations

import numpy as np
import wfdb
from scipy import signal

from . import config


def record_path(record: str) -> str:
    return str(config.MITDB_DIR / record)


def mlii_adc_gain(record: str) -> float:
    """The record's own MLII gain in ADC units per mV, taken from the .hea header."""
    header = wfdb.rdheader(record_path(record))
    channel = header.sig_name.index(config.TARGET_LEAD)
    gain = header.adc_gain[channel]
    return float(gain) if gain else config.QUANT_TARGET_ADU_PER_MV


def read_mlii(record: str, n_samples: int | None = None) -> np.ndarray:
    """Read the MLII lead of `record` in ADC units with the baseline removed.

    Staying in ADC units (rather than mV) keeps the signal integer-valued and makes
    the spec's band thresholds directly meaningful.
    """
    n_samples = config.N_SAMPLES if n_samples is None else n_samples
    start = config.RECORD_START
    rec = wfdb.rdrecord(record_path(record), physical=False,
                        sampfrom=start, sampto=start + n_samples)
    if config.TARGET_LEAD not in rec.sig_name:
        raise ValueError(f"record {record} has no {config.TARGET_LEAD} lead "
                         f"(leads present: {rec.sig_name})")
    if rec.fs != config.FS:
        raise ValueError(f"record {record} sampled at {rec.fs} Hz, expected {config.FS}")

    channel = rec.sig_name.index(config.TARGET_LEAD)
    raw = rec.d_signal[:, channel].astype(np.float64)
    baseline = rec.adc_zero[channel] if rec.adc_zero is not None else 0
    return raw - float(baseline)


def bandpass(x: np.ndarray) -> np.ndarray:
    """4th-order Butterworth bandpass, 0.5-40 Hz.

    Applied zero-phase (forward-backward) so QRS morphology is not shifted or skewed
    before quantization; this stage runs offline on the host, so non-causality is free.
    """
    sos = signal.butter(config.PRE_BP_ORDER,
                        [config.PRE_BP_LOW_HZ, config.PRE_BP_HIGH_HZ],
                        btype="bandpass", fs=config.FS, output="sos")
    return signal.sosfiltfilt(sos, x)


def normalization_gain(record: str) -> float:
    """Calibration-only rescaling factor for a record (1.0 for all of MIT-BIH).

    This is metadata-driven, not fitted: it maps whatever the record's converter used
    onto the reference 200 ADC units/mV so that the global band thresholds mean the
    same physical voltage everywhere.
    """
    return config.QUANT_TARGET_ADU_PER_MV / mlii_adc_gain(record)


def quantize(x: np.ndarray, gain: float = 1.0) -> np.ndarray:
    """Apply the calibration gain, round to nearest, and clip into int16."""
    scaled = np.rint(np.asarray(x, dtype=np.float64) * gain)
    clipped = np.clip(scaled, config.SAMPLE_MIN, config.SAMPLE_MAX)
    return clipped.astype(np.int16)


def clip_count(x: np.ndarray, gain: float) -> int:
    """How many samples saturate at the int16 rails for a given gain."""
    scaled = np.rint(np.asarray(x, dtype=np.float64) * gain)
    return int(np.count_nonzero((scaled < config.SAMPLE_MIN) |
                                (scaled > config.SAMPLE_MAX)))


def preprocess_record(record: str, n_samples: int | None = None) -> np.ndarray:
    """Full offline chain for one record -> int16 array ready for BRAM."""
    filtered = bandpass(read_mlii(record, n_samples))
    return quantize(filtered, normalization_gain(record))


def read_annotations(record: str, n_samples: int | None = None) -> np.ndarray:
    """Beat annotation sample indices (.atr), restricted to the analysed window.

    Non-beat annotations (rhythm changes, noise markers, etc.) are filtered out so the
    result is a clean ground truth for R-peak scoring.
    """
    n_samples = config.N_SAMPLES if n_samples is None else n_samples
    start = config.RECORD_START
    ann = wfdb.rdann(record_path(record), "atr",
                     sampfrom=start, sampto=start + n_samples)
    beat_symbols = set("NLRBAaJSVrFejnE/fQ?")
    idx = [s - start for s, sym in zip(ann.sample, ann.symbol) if sym in beat_symbols]
    return np.asarray(idx, dtype=np.int64)


# ---------------------------------------------------------------------------
# BRAM initialisation files
# ---------------------------------------------------------------------------

def write_coe(samples: np.ndarray, path, radix: int = 16) -> None:
    """Write a Vivado .coe memory initialisation file (16-bit words).

    Values are emitted as two's-complement so the BRAM contents match the int16
    interpretation used by the HLS core.
    """
    if radix not in (2, 16):
        raise ValueError("radix must be 2 or 16")
    words = [int(v) & 0xFFFF for v in samples]
    lines = [f"memory_initialization_radix={radix};",
             "memory_initialization_vector="]
    if radix == 16:
        body = [f"{w:04X}" for w in words]
    else:
        body = [f"{w:016b}" for w in words]
    lines.append(",\n".join(body) + ";")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def read_coe(path) -> np.ndarray:
    """Read back a .coe file written by :func:`write_coe` (round-trip verification)."""
    text = path.read_text(encoding="ascii")
    radix = 16
    for line in text.splitlines():
        if line.startswith("memory_initialization_radix"):
            radix = int(line.split("=")[1].strip().rstrip(";"))
            break
    body = text.split("memory_initialization_vector=", 1)[1]
    tokens = [t.strip() for t in body.replace(";", "").split(",") if t.strip()]
    values = [int(t, radix) for t in tokens]
    return np.asarray([v - 0x10000 if v & 0x8000 else v for v in values], dtype=np.int16)


def write_coe32(samples: np.ndarray, path) -> None:
    """Write a 32-bit-wide .coe holding two samples per word.

    The sample store is a true dual-port block RAM with asymmetric widths: the core
    reads it 16 bits at a time, but the PS writes it through a 32-bit AXI BRAM
    controller, and Vivado initialises an asymmetric memory using the *port A* width.
    Sample 2i lands in the low half-word and sample 2i+1 in the high half, matching how
    the memory generator maps a narrow port onto a wide one.
    """
    values = [int(v) & 0xFFFF for v in samples]
    if len(values) % 2:
        values.append(0)
    words = [values[i] | (values[i + 1] << 16) for i in range(0, len(values), 2)]
    lines = ["memory_initialization_radix=16;",
             "memory_initialization_vector=",
             ",\n".join(f"{w:08X}" for w in words) + ";"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def read_coe32(path) -> np.ndarray:
    """Unpack a 32-bit .coe back into int16 samples (round-trip verification)."""
    text = path.read_text(encoding="ascii")
    body = text.split("memory_initialization_vector=", 1)[1]
    tokens = [t.strip() for t in body.replace(";", "").split(",") if t.strip()]
    out = []
    for token in tokens:
        word = int(token, 16)
        for half in (word & 0xFFFF, (word >> 16) & 0xFFFF):
            out.append(half - 0x10000 if half & 0x8000 else half)
    return np.asarray(out, dtype=np.int16)


def write_bin32(samples: np.ndarray, path) -> None:
    """Write the raw little-endian int16 image of the sample store.

    This is what XSCT writes straight into the BRAM over JTAG (`mwr -bin -file`), which
    is how a record is swapped without re-synthesising anything. Little-endian int16 is
    already the packed 32-bit layout the memory expects: sample 2i in the low half-word,
    sample 2i+1 in the high half.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(np.asarray(samples, dtype="<i2").tobytes())


def read_bin32(path) -> np.ndarray:
    return np.frombuffer(path.read_bytes(), dtype="<i2")


def write_dat(samples: np.ndarray, path) -> None:
    """Write a plain one-integer-per-line file, used by the C++ testbenches."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(str(int(v)) for v in samples) + "\n", encoding="ascii")


def read_dat(path) -> np.ndarray:
    return np.asarray([int(line) for line in path.read_text().split()], dtype=np.int64)
