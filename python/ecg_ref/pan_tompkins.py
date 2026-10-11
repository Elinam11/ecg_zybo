"""Offline Pan–Tompkins QRS detector for PSim SDNN scoring.

This is *not* part of the FPGA compressor. The compressor segments with the TKEO
chain in ``detect.py``. Pan–Tompkins is used only as an independent detector run
on the original and reconstructed waveforms so PSim SDNN does not share a peak
list with the encoder.

The 1985 paper designed integer filters for 200 Hz. Here the same analog
specification is realized at the native MIT-BIH rate (360 Hz):

* bandpass 5–15 Hz (2nd-order Butterworth, causal)
* five-point derivative (Pan–Tompkins coefficients, delay scaled to ``fs``)
* pointwise square
* moving-window integration of 150 ms
* dual adaptive thresholds, 200 ms refractory, 166% RR search-back

Citation: J. Pan and W. J. Tompkins, "A Real-Time QRS Detection Algorithm,"
IEEE Trans. Biomed. Eng., vol. BME-32, no. 3, pp. 230–236, Mar. 1985.
"""

from __future__ import annotations

import numpy as np
from scipy import signal

from . import config

# Analog band used by Pan and Tompkins to isolate the QRS (their integer LP+HP
# pair at 200 Hz is approximately this passband).
_PT_LOW_HZ = 5.0
_PT_HIGH_HZ = 15.0
_PT_INTEGRATOR_S = 0.150
_PT_REFRACTORY_S = 0.200
_PT_LEARN_S = 2.0
_PT_SEARCHBACK = 1.66
_PT_REFINE_S = 0.075  # ±75 ms local-max refine on the bandpass output


def pan_tompkins(x: np.ndarray, fs: float | None = None) -> np.ndarray:
    """Return R-peak sample indices for ``x`` sampled at ``fs`` (default MIT-BIH)."""
    fs = float(config.FS if fs is None else fs)
    x = np.asarray(x, dtype=np.float64)
    if x.size < int(fs):
        return np.zeros(0, dtype=np.int64)

    filtered = _bandpass(x, fs)
    integrated = _integrate(_square(_derivative(filtered, fs)), fs)
    candidates = _adaptive_peaks(integrated, fs)
    return _refine_peaks(filtered, candidates, fs)


def _bandpass(x: np.ndarray, fs: float) -> np.ndarray:
    sos = signal.butter(2, [_PT_LOW_HZ, _PT_HIGH_HZ], btype="bandpass",
                        fs=fs, output="sos")
    return signal.sosfilt(sos, x)


def _derivative(x: np.ndarray, fs: float) -> np.ndarray:
    """Five-point derivative from Pan–Tompkins, delay interpolated at ``fs``.

    At 200 Hz the kernel is ``(1/8)[-1, -2, 0, 2, 1]`` with taps at n±1, n±2.
    Those delays are 5 ms and 10 ms; at a different ``fs`` the same times are
    rounded to the nearest sample.
    """
    d1 = max(1, int(round(0.005 * fs)))
    d2 = max(d1 + 1, int(round(0.010 * fs)))
    y = np.zeros_like(x)
    n = np.arange(d2, len(x) - d2)
    y[n] = (1.0 / 8.0) * (-x[n - d2] - 2.0 * x[n - d1] + 2.0 * x[n + d1] + x[n + d2])
    return y


def _square(x: np.ndarray) -> np.ndarray:
    return x * x


def _integrate(x: np.ndarray, fs: float) -> np.ndarray:
    win = max(1, int(round(_PT_INTEGRATOR_S * fs)))
    kernel = np.ones(win, dtype=np.float64) / win
    return np.convolve(x, kernel, mode="same")


def _adaptive_peaks(integrated: np.ndarray, fs: float) -> np.ndarray:
    refractory = max(1, int(round(_PT_REFRACTORY_S * fs)))
    learn_n = min(len(integrated), max(1, int(round(_PT_LEARN_S * fs))))
    learn = integrated[:learn_n]

    peak_h = float(np.max(learn)) if learn.size else 0.0
    if peak_h <= 0.0:
        return np.zeros(0, dtype=np.int64)

    spki = 0.25 * peak_h
    npki = 0.5 * float(np.mean(learn))
    threshold = npki + 0.25 * (spki - npki)

    local_maxima = []
    for i in range(1, len(integrated) - 1):
        if integrated[i] >= integrated[i - 1] and integrated[i] > integrated[i + 1]:
            local_maxima.append(i)

    peaks: list[int] = []
    last = -refractory
    for i in local_maxima:
        amp = float(integrated[i])
        if amp > threshold and i - last >= refractory:
            peaks.append(i)
            last = i
            spki = 0.125 * amp + 0.875 * spki
        else:
            npki = 0.125 * amp + 0.875 * npki
        threshold = npki + 0.25 * (spki - npki)

    if len(peaks) >= 3:
        peaks = _searchback(integrated, peaks, fs, refractory)
    return np.asarray(peaks, dtype=np.int64)


def _searchback(integrated: np.ndarray, peaks: list[int], fs: float,
                refractory: int) -> list[int]:
    """Insert a missed peak if an RR gap exceeds 166% of the mean RR."""
    rr = np.diff(np.asarray(peaks, dtype=np.int64))
    if rr.size == 0:
        return peaks
    limit = int(round(_PT_SEARCHBACK * float(np.mean(rr))))
    extra: list[int] = []
    for left, right in zip(peaks, peaks[1:]):
        if right - left <= limit:
            continue
        lo = left + refractory
        hi = right - refractory
        if hi <= lo:
            continue
        j = lo + int(np.argmax(integrated[lo:hi]))
        # Second (lower) threshold: half of the first, as in the 1985 paper.
        thresh2 = 0.5 * float(integrated[left])
        if float(integrated[j]) > thresh2:
            extra.append(int(j))
    if not extra:
        return peaks
    merged = sorted(set(peaks) | set(extra))
    pruned: list[int] = []
    for p in merged:
        if not pruned or p - pruned[-1] >= refractory:
            pruned.append(p)
    return pruned


def _refine_peaks(filtered: np.ndarray, candidates: np.ndarray,
                  fs: float) -> np.ndarray:
    """Move each integrated-signal peak to the nearest |bandpass| apex."""
    if candidates.size == 0:
        return candidates
    radius = max(1, int(round(_PT_REFINE_S * fs)))
    refined: list[int] = []
    n = len(filtered)
    for c in (int(v) for v in candidates):
        lo = max(0, c - radius)
        hi = min(n, c + radius + 1)
        local = np.abs(filtered[lo:hi])
        refined.append(lo + int(np.argmax(local)))
    # Deduplicate after refine (two candidates can collapse onto one apex).
    unique = sorted(set(refined))
    return np.asarray(unique, dtype=np.int64)
