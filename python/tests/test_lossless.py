"""Lossless QRS path: exactness plus the Golomb-Rice edge cases."""

import numpy as np
import pytest

from ecg_ref import config, lossless, packer
from ecg_ref.fixedpoint import zigzag_map, zigzag_unmap


def roundtrip(samples):
    payload = lossless.encode_qrs(np.asarray(samples, dtype=np.int64))
    writer = packer.BitWriter()
    for value, nbits in payload.bits:
        writer.put(value, nbits)
    reader = packer.BitReader(writer.to_words())
    return lossless.decode_qrs(reader, len(samples)), payload


@pytest.mark.parametrize("value", [0, 1, -1, 7, -7, 32767, -32768, 12345, -12345])
def test_zigzag_is_bijective(value):
    assert zigzag_unmap(zigzag_map(value)) == value
    assert zigzag_map(value) >= 0


def test_zigzag_matches_spec_formula():
    for error in range(-50, 51):
        expected = 2 * error if error >= 0 else -2 * error - 1
        assert zigzag_map(error) == expected


def test_exact_reconstruction_on_synthetic_qrs():
    n = config.WS
    t = np.arange(n)
    samples = (300 * np.exp(-((t - 29) ** 2) / 18.0)).astype(np.int64) - 20
    decoded, _ = roundtrip(samples)
    assert np.array_equal(decoded, samples)


def test_all_zero_window_selects_k_zero():
    """S = 0 must not reach log2(0); k falls back to 0."""
    samples = np.zeros(config.WS, dtype=np.int64)
    decoded, payload = roundtrip(samples)
    assert payload.residual_sum == 0
    assert payload.k == 0
    assert np.array_equal(decoded, samples)


def test_constant_window_is_perfectly_predicted():
    """A constant run has zero residual under a 2nd-order predictor."""
    samples = np.full(config.WS, -1234, dtype=np.int64)
    decoded, payload = roundtrip(samples)
    assert payload.residual_sum == 0
    assert np.array_equal(decoded, samples)


def test_linear_ramp_is_perfectly_predicted():
    """y_hat = 2y(n-1) - y(n-2) is exact for any linear ramp."""
    samples = (np.arange(config.WS, dtype=np.int64) * 7) - 100
    decoded, payload = roundtrip(samples)
    assert payload.residual_sum == 0
    assert payload.k == 0
    assert np.array_equal(decoded, samples)


def test_extreme_alternating_window_is_still_exact():
    """Full-scale alternation maximises the residual magnitude.

    It does not trigger the escape: every residual is equally large, so the adaptive k
    saturates at 15 and the quotient stays small. That is the coder behaving correctly.
    """
    samples = np.array([32767 if i % 2 else -32768 for i in range(config.WS)],
                       dtype=np.int64)
    decoded, payload = roundtrip(samples)
    assert np.array_equal(decoded, samples)
    assert payload.k == config.GR_K_MAX


def test_outlier_window_triggers_escape():
    """The escape fires when one residual dwarfs the window mean that chose k."""
    samples = np.zeros(config.WS, dtype=np.int64)
    samples[40] = 32767
    decoded, payload = roundtrip(samples)
    assert np.array_equal(decoded, samples)
    assert payload.escapes > 0, "expected the escape path to engage"


def test_escape_bounds_symbol_length():
    """No symbol may exceed the escape prefix plus the raw field."""
    samples = np.zeros(config.WS, dtype=np.int64)
    samples[40] = 32767
    payload = lossless.encode_qrs(samples)
    worst = config.GR_Q_ESCAPE + config.GR_ESCAPE_BITS
    residual_bits = payload.bit_count - (32 + config.GR_K_BITS)
    assert residual_bits <= worst * (config.WS - config.ANCHOR_COUNT)


def test_escape_free_path_never_exceeds_escape_prefix():
    """Without the escape a quotient could run to thousands of bits; assert the cap."""
    rng = np.random.default_rng(2)
    for _ in range(50):
        samples = rng.integers(-32768, 32767, size=config.WS).astype(np.int64)
        payload = lossless.encode_qrs(samples)
        for value, nbits in payload.bits:
            assert nbits <= config.GR_ESCAPE_BITS


@pytest.mark.parametrize("length", [1, 2, 3, 4, 16, 72, 73])
def test_short_and_truncated_windows(length):
    """Truncated windows occur when consecutive beats collide."""
    rng = np.random.default_rng(length)
    samples = rng.integers(-2000, 2000, size=length).astype(np.int64)
    decoded, _ = roundtrip(samples)
    assert np.array_equal(decoded, samples)


def test_random_windows_are_always_exact():
    rng = np.random.default_rng(7)
    for _ in range(200):
        samples = rng.integers(-3000, 3000, size=config.WS).astype(np.int64)
        decoded, _ = roundtrip(samples)
        assert np.array_equal(decoded, samples)


def test_k_never_exceeds_field_width():
    rng = np.random.default_rng(11)
    for _ in range(100):
        samples = rng.integers(-32768, 32767, size=config.WS).astype(np.int64)
        payload = lossless.encode_qrs(samples)
        assert 0 <= payload.k <= config.GR_K_MAX
        assert payload.k < (1 << config.GR_K_BITS)


def test_select_k_matches_locked_formula():
    """k = max(0, floor(log2(S / count))), on the mapped-residual sum."""
    assert lossless.select_k(0, 71) == 0
    assert lossless.select_k(71, 71) == 0        # mean 1 -> log2 = 0
    assert lossless.select_k(2 * 71, 71) == 1    # mean 2
    assert lossless.select_k(255 * 71, 71) == 7  # mean 255 -> floor(log2)=7
    assert lossless.select_k(256 * 71, 71) == 8
    assert lossless.select_k(10 ** 9, 71) == config.GR_K_MAX
    assert lossless.select_k(100, 0) == 0        # empty window
