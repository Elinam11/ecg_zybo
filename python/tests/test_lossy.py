"""Lossy non-QRS path: transform reversibility, thresholding, and RLE overflow."""

import numpy as np
import pytest

from ecg_ref import config, lossy


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("length", [16, 32, 64, 128, 160, 1024])
def test_haar_is_exactly_reversible(length):
    """The reversible lifting Haar must contribute zero distortion."""
    rng = np.random.default_rng(length)
    x = rng.integers(config.SAMPLE_MIN, config.SAMPLE_MAX,
                     size=length).astype(np.int64)
    recovered = lossy.haar_inverse(lossy.haar_forward(x))
    assert np.array_equal(recovered, x)


def test_haar_reversible_on_extremes():
    for x in (np.full(64, config.SAMPLE_MIN, dtype=np.int64),
              np.full(64, config.SAMPLE_MAX, dtype=np.int64),
              np.zeros(64, dtype=np.int64),
              np.array([config.SAMPLE_MAX if i % 2 else config.SAMPLE_MIN
                        for i in range(64)], dtype=np.int64)):
        assert np.array_equal(lossy.haar_inverse(lossy.haar_forward(x)), x)


def test_band_shapes_match_config():
    x = np.arange(160, dtype=np.int64)
    bands = lossy.haar_forward(x)
    expected = config.band_lengths(160)
    for band in config.BAND_ORDER:
        assert len(bands[band]) == expected[band]


def test_constant_signal_has_zero_detail():
    x = np.full(64, 500, dtype=np.int64)
    bands = lossy.haar_forward(x)
    for band in ("D1", "D2", "D3", "D4"):
        assert np.all(bands[band] == 0)


# ---------------------------------------------------------------------------
# Padding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("length", [1, 5, 15, 16, 17, 31, 33, 117, 913])
def test_padding_reaches_block_multiple(length):
    x = np.arange(length, dtype=np.int64)
    padded = lossy.pad_segment(x)
    assert len(padded) % config.DWT_BLOCK == 0
    assert len(padded) >= length
    assert len(padded) - length < config.DWT_BLOCK
    assert np.array_equal(padded[:length], x)
    if len(padded) > length:
        assert np.all(padded[length:] == x[-1]), "padding repeats the last sample"


def smooth_signal(length: int, seed: int, amplitude: int = 300) -> np.ndarray:
    """A band-limited signal, representative of a real bandpassed non-QRS segment.

    Successive differences stay small, which is the condition under which every
    coefficient fits the signed 10-bit value field.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(length)
    phase = rng.random() * 6.283
    wave = (amplitude * np.sin(2 * np.pi * t / 45.0 + phase)
            + 0.3 * amplitude * np.sin(2 * np.pi * t / 17.0))
    return np.rint(wave).astype(np.int64)


@pytest.mark.parametrize("length", [1, 5, 15, 16, 17, 31, 117, 913])
def test_unthresholded_segment_roundtrips_exactly(length):
    """With thresholds at zero and no saturation, the lossy path is lossless.

    This is what the reversible transform buys: all distortion comes from
    thresholding, nothing from the transform itself.
    """
    x = smooth_signal(length, seed=length)
    zero = {band: 0 for band in config.BAND_ORDER}
    payload = lossy.encode_nonqrs(x, zero)
    assert payload.saturated == 0, "10-bit field must not clip for smooth input"
    recon = lossy.decode_nonqrs(payload.tokens, length)
    assert len(recon) == length
    assert np.array_equal(recon, x)


def test_detail_band_saturates_on_uncorrelated_full_scale_input():
    """Documents the field limit: a lifting detail coefficient is a raw difference.

    Uncorrelated input makes |x0 - x1| exceed the signed 10-bit range, which the
    encoder must saturate and count rather than silently wrap.
    """
    rng = np.random.default_rng(0)
    x = rng.integers(-500, 500, size=64).astype(np.int64)
    zero = {band: 0 for band in config.BAND_ORDER}
    payload = lossy.encode_nonqrs(x, zero)
    assert payload.saturated > 0
    for _, value in payload.tokens:
        assert config.RLE_VAL_MIN <= value <= config.RLE_VAL_MAX


# ---------------------------------------------------------------------------
# Thresholding and quantization
# ---------------------------------------------------------------------------

def test_hard_threshold_zeroes_below_and_keeps_above():
    coeffs = np.array([-100, -33, -32, -31, 0, 31, 32, 33, 100], dtype=np.int64)
    quantized, saturated = lossy.quantize_band(coeffs, "D1", {"D1": 32})
    assert saturated == 0
    # |c| < 32 is discarded, |c| >= 32 survives (shift is 0, so value is unchanged)
    assert list(quantized) == [-100, -33, -32, 0, 0, 0, 32, 33, 100]


def test_quantized_values_fit_the_10_bit_field():
    coeffs = np.array([5000, -5000], dtype=np.int64)
    quantized, saturated = lossy.quantize_band(coeffs, "A4", {"A4": 0})
    assert saturated == 2
    assert quantized[0] == config.RLE_VAL_MAX
    assert quantized[1] == config.RLE_VAL_MIN


def test_token_pack_unpack_roundtrip():
    for run in (0, 1, 31, 62, config.RLE_RUN_MAX):
        for value in (0, 1, -1, 511, -512, 123, -123):
            word = lossy.pack_token(run, value)
            assert 0 <= word < (1 << config.RLE_TOKEN_BITS)
            assert lossy.unpack_token(word) == (run, value)


# ---------------------------------------------------------------------------
# RLE, including the >63 overflow case (spec 10.1 Module 3)
# ---------------------------------------------------------------------------

def test_short_run_encodes_in_one_token():
    values = np.array([0, 0, 0, 5], dtype=np.int64)
    assert lossy.rle_encode(values) == [(3, 5)]


def test_run_of_exactly_63_emits_continuation():
    values = np.array([0] * 63 + [7], dtype=np.int64)
    tokens = lossy.rle_encode(values)
    assert tokens == [(config.RLE_RUN_MAX, 0), (0, 7)]


@pytest.mark.parametrize("zeros", [64, 100, 126, 127, 200, 255, 256, 1000])
def test_long_zero_runs_emit_continuation_tuples(zeros):
    """Runs longer than 63 must chain (63, 0) markers without corruption."""
    values = np.array([0] * zeros + [-9], dtype=np.int64)
    tokens = lossy.rle_encode(values)
    assert all(0 <= run <= config.RLE_RUN_MAX for run, _ in tokens)
    expected_markers = zeros // config.RLE_RUN_MAX
    assert sum(1 for run, val in tokens if val == 0) == expected_markers
    assert np.array_equal(lossy.rle_decode(tokens, len(values)), values)


def test_all_zero_vector_emits_no_trailing_tokens():
    """Trailing zeros are implicit because the decoder knows the coefficient count."""
    values = np.zeros(40, dtype=np.int64)
    assert lossy.rle_encode(values) == []
    assert np.array_equal(lossy.rle_decode([], 40), values)


def test_rle_roundtrip_random():
    rng = np.random.default_rng(3)
    for _ in range(200):
        n = int(rng.integers(1, 400))
        values = rng.integers(-40, 40, size=n).astype(np.int64)
        values[rng.random(n) < 0.8] = 0          # mostly zeros, like real coefficients
        tokens = lossy.rle_encode(values)
        assert np.array_equal(lossy.rle_decode(tokens, n), values)


def test_continuation_marker_is_unambiguous():
    """A zero value field always means 'continue the run', never a real coefficient."""
    values = np.array([0] * 63 + [0] * 63 + [1], dtype=np.int64)
    tokens = lossy.rle_encode(values)
    assert tokens.count((config.RLE_RUN_MAX, 0)) == 2
    assert np.array_equal(lossy.rle_decode(tokens, len(values)), values)
