"""Detection chain, window framing, and the consecutive-beat truncation case."""

import numpy as np
import pytest

from conftest import requires_data

from ecg_ref import config, detect, preprocess


def synthetic_ecg(beats: int, period: int, amplitude: int = 300) -> np.ndarray:
    """A crude but adequate impulse train for framing tests."""
    n = beats * period + config.THRESH_WARMUP + 2 * period
    x = np.zeros(n, dtype=np.int64)
    for b in range(beats):
        centre = config.THRESH_WARMUP + period + b * period
        for offset in range(-4, 5):
            x[centre + offset] = int(amplitude * np.exp(-(offset ** 2) / 3.0))
    return x.astype(np.int16)


# ---------------------------------------------------------------------------
# Filter / energy stages
# ---------------------------------------------------------------------------

def test_iir_bandpass_rejects_dc():
    """A constant input must decay to zero through the high-pass branch."""
    x = np.full(4000, 1000, dtype=np.int16)
    out = detect.iir_bandpass(x)
    assert abs(int(out[-1])) <= 2


def test_iir_bandpass_is_deterministic():
    rng = np.random.default_rng(0)
    x = rng.integers(-1000, 1000, size=2000).astype(np.int16)
    assert np.array_equal(detect.iir_bandpass(x), detect.iir_bandpass(x))


def test_tkeo_edges_are_zero():
    bp = np.arange(10, dtype=np.int64)
    psi = detect.tkeo(bp)
    assert psi[0] == 0 and psi[-1] == 0


def test_tkeo_matches_formula():
    bp = np.array([1, 2, 3, 4, 5], dtype=np.int64)
    psi = detect.tkeo(bp)
    for n in range(1, len(bp) - 1):
        assert psi[n] == bp[n] ** 2 - bp[n - 1] * bp[n + 1]


def test_moving_average_uses_zero_history_and_floors():
    psi = np.ones(40, dtype=np.int64) * config.MA_LEN
    smooth = detect.moving_average(psi)
    assert smooth[config.MA_LEN - 1] == config.MA_LEN
    assert smooth[0] == config.MA_LEN // config.MA_LEN * 1  # partial window, floored


def test_moving_average_length_matches_input():
    psi = np.arange(100, dtype=np.int64)
    assert len(detect.moving_average(psi)) == 100


# ---------------------------------------------------------------------------
# Window framing
# ---------------------------------------------------------------------------

def test_window_has_nominal_length_when_isolated():
    windows = detect.frame_windows(np.array([1000]), 5000)
    assert windows == [(1000 - config.QRS_PRE, 1000 + config.QRS_POST)]
    assert windows[0][1] - windows[0][0] + 1 == config.WS == 73


def test_windows_never_overlap_at_minimum_beat_spacing():
    """Refractory 72 < window 73, so adjacent windows would collide by one sample."""
    peaks = np.array([1000, 1000 + config.REFRACTORY,
                      1000 + 2 * config.REFRACTORY], dtype=np.int64)
    windows = detect.frame_windows(peaks, 10000)
    assert len(windows) == 3
    for (s0, e0), (s1, _) in zip(windows, windows[1:]):
        assert s1 > e0, "windows must not overlap"
    # The collision is resolved by truncating the later window, not the earlier one.
    assert windows[0][1] - windows[0][0] + 1 == config.WS
    assert windows[1][1] - windows[1][0] + 1 < config.WS


def test_window_clamped_at_record_boundaries():
    windows = detect.frame_windows(np.array([5, 4990]), 5000)
    assert windows[0][0] == 0
    assert windows[-1][1] == 4999


def test_segments_tile_the_record_exactly():
    peaks = np.array([500, 900, 1400], dtype=np.int64)
    n = 3000
    segments = detect.build_segments(detect.frame_windows(peaks, n), n)
    assert segments[0].start == 0
    assert segments[-1].end == n - 1
    for prev, nxt in zip(segments, segments[1:]):
        assert nxt.start == prev.end + 1, "no gaps or overlaps between segments"
    assert sum(s.length for s in segments) == n


def test_segments_alternate_kinds():
    peaks = np.array([500, 900], dtype=np.int64)
    segments = detect.build_segments(detect.frame_windows(peaks, 2000), 2000)
    kinds = [s.kind for s in segments]
    for prev, nxt in zip(kinds, kinds[1:]):
        assert prev != nxt


# ---------------------------------------------------------------------------
# End-to-end detection
# ---------------------------------------------------------------------------

def test_detects_synthetic_beats():
    period = 300
    beats = 20
    x = synthetic_ecg(beats, period)
    result = detect.detect(x)
    assert len(result.r_peaks) >= beats - 2


def test_refractory_suppresses_double_detections():
    result = detect.detect(synthetic_ecg(20, 300))
    gaps = np.diff(result.triggers)
    assert np.all(gaps > config.REFRACTORY), "refractory period violated"


def test_no_triggers_during_warmup():
    result = detect.detect(synthetic_ecg(20, 300))
    if len(result.triggers):
        assert result.triggers.min() >= config.THRESH_WARMUP


def test_peak_search_lands_on_the_apex():
    x = synthetic_ecg(6, 400, amplitude=500)
    result = detect.detect(x)
    for r in result.r_peaks:
        window = x[max(0, r - 6):r + 7]
        assert abs(int(x[r])) == int(np.abs(window).max())


def test_flat_signal_produces_no_detections():
    x = np.zeros(5000, dtype=np.int16)
    assert len(detect.detect(x).r_peaks) == 0


@requires_data
def test_detection_accuracy_on_record_100():
    """Module 1 verification: R-peaks must agree with the .atr annotations."""
    x = preprocess.preprocess_record("100")
    result = detect.detect(x)
    score = detect.score_detection(result.r_peaks, preprocess.read_annotations("100"))
    assert score.sensitivity > 95.0, f"sensitivity {score.sensitivity:.2f}%"
    assert score.ppv > 95.0, f"ppv {score.ppv:.2f}%"


@requires_data
def test_record_100_segments_cover_everything():
    x = preprocess.preprocess_record("100")
    segments, _ = detect.segment_record(x)
    assert sum(s.length for s in segments) == len(x)
    for s in segments:
        assert s.length > 0
        if s.kind == "qrs":
            assert s.length <= config.WS
