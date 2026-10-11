"""PRDN1, per-cycle MAX, and PSim SDNN — formula-level tests (no MIT-BIH)."""

import numpy as np

from ecg_ref import metrics, pan_tompkins


def test_prdn1_hand_computed():
    x = np.array([1.0, 3.0, 5.0])
    xhat = np.array([1.0, 2.0, 6.0])
    # mean(x) = 3; SST = 4+0+4 = 8; SSE = 0+1+1 = 2
    expected = 100.0 * np.sqrt(2.0 / 8.0)
    assert abs(metrics.prd_n1(x, xhat) - expected) < 1e-12


def test_prdn1_is_not_standard_prd_when_mean_nonzero():
    x = np.array([10.0, 12.0, 11.0])
    xhat = np.array([10.0, 12.0, 10.0])
    assert metrics.prd_n1(x, xhat) != metrics.prd(x, xhat)


def test_prdn1_zero_when_identical():
    x = np.array([4.0, -1.0, 2.0])
    assert metrics.prd_n1(x, x) == 0.0


def test_max_nonqrs_ignores_qrs_error():
    x = np.array([0, 0, 10, 0, 0], dtype=np.float64)
    xhat = np.array([0, 3, 99, 1, 0], dtype=np.float64)
    qrs = np.array([False, False, True, False, False])
    assert metrics.max_error(x, xhat, ~qrs) == 3.0
    assert metrics.max_error(x, xhat, qrs) == 89.0  # |10 - 99|
    assert metrics.max_error(x, xhat) == 89.0


def test_cycle_bounds_midpoints():
    bounds = metrics.cycle_bounds(np.array([10, 30, 50]), 80)
    assert bounds == [(0, 20), (20, 40), (40, 80)]


def test_per_cycle_max_nonqrs_omits_empty_slice():
    x = np.zeros(20, dtype=np.float64)
    xhat = x.copy()
    xhat[5] = 7.0
    # One peak; the single cycle is the whole record. QRS covers sample 5.
    qrs = np.zeros(20, dtype=bool)
    qrs[5] = True
    vals = metrics.per_cycle_max(x, xhat, np.array([10]), sample_mask=~qrs)
    assert vals == [0.0]


def test_sdnn_and_psim_identical_peaks():
    peaks = np.array([0, 360, 720, 1080], dtype=np.int64)  # 1000 ms RR at 360 Hz
    s = metrics.sdnn_ms(peaks, fs=360.0)
    assert s == 0.0  # three equal intervals — wait, sample sd of [1000,1000,1000] is 0
    # sdnn_x == 0 → PSim is NaN by the stated rule
    assert np.isnan(metrics.psim_sdnn(s, s))


def test_psim_sdnn_formula():
    assert abs(metrics.psim_sdnn(100.0, 90.0) - 90.0) < 1e-12
    assert np.isnan(metrics.psim_sdnn(0.0, 1.0))
    assert np.isnan(metrics.psim_sdnn(float("nan"), 10.0))


def test_sdnn_needs_two_intervals():
    assert np.isnan(metrics.sdnn_ms(np.array([0, 360]), fs=360.0))
    peaks = np.array([0, 360, 800], dtype=np.int64)
    rr = np.array([360.0, 440.0]) * 1000.0 / 360.0
    expected = float(np.sqrt(np.sum((rr - rr.mean()) ** 2) / 1))
    assert abs(metrics.sdnn_ms(peaks, fs=360.0) - expected) < 1e-9


def test_pan_tompkins_finds_regular_beats():
    fs = 360.0
    period = 360  # 1 Hz
    n = period * 12
    t = np.arange(n) / fs
    # Narrow Gaussian QRS-like pulses on a quiet baseline.
    x = np.zeros(n)
    centres = [period * (k + 2) for k in range(8)]
    for c in centres:
        w = np.arange(n) - c
        x += 800.0 * np.exp(-(w ** 2) / 8.0)
    peaks = pan_tompkins.pan_tompkins(x, fs=fs)
    assert peaks.size >= 6
    # Detected peaks should sit near the synthetic centres (AAMI-style ±27 samples).
    matched = 0
    for c in centres:
        if np.any(np.abs(peaks.astype(np.int64) - c) <= 27):
            matched += 1
    assert matched >= 6


def test_psim_is_100_for_identical_nontrivial_sdnn():
    peaks = np.array([0, 300, 700, 1000, 1450], dtype=np.int64)
    s = metrics.sdnn_ms(peaks, fs=360.0)
    assert s > 0.0
    assert abs(metrics.psim_sdnn(s, s) - 100.0) < 1e-12
