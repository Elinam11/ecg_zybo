"""Integration level: full encode -> bitstream -> decode, and the spec's targets."""

import numpy as np
import pytest

from conftest import requires_data

from ecg_ref import config, detect, lossy, metrics, pipeline


def test_synthetic_record_roundtrips():
    """A synthetic record exercises the whole pipeline without needing the dataset."""
    from test_detect import synthetic_ecg
    x = synthetic_ecg(30, 320)
    encoded = pipeline.encode_samples(x)
    decoded = pipeline.decode_bitstream(encoded.payload)

    assert decoded.checksum_ok
    assert decoded.header.magic == config.MAGIC_WORD
    assert decoded.header.sample_count == len(x)
    assert len(decoded.samples) == len(x)
    # Every QRS sample must be reproduced exactly.
    assert np.array_equal(decoded.samples[decoded.qrs_mask],
                          np.asarray(x, dtype=np.int64)[decoded.qrs_mask])


def test_qrs_regions_are_bit_exact_when_thresholds_are_zero():
    from test_detect import synthetic_ecg
    x = synthetic_ecg(20, 320)
    saved = dict(config.BAND_THRESHOLD)
    try:
        config.BAND_THRESHOLD.update({b: 0 for b in config.BAND_ORDER})
        encoded = pipeline.encode_samples(x)
        decoded = pipeline.decode_bitstream(encoded.payload)
        assert np.array_equal(decoded.samples, np.asarray(x, dtype=np.int64))
    finally:
        config.BAND_THRESHOLD.update(saved)


def test_header_reports_correct_qrs_count():
    from test_detect import synthetic_ecg
    x = synthetic_ecg(25, 320)
    encoded = pipeline.encode_samples(x)
    decoded = pipeline.decode_bitstream(encoded.payload)
    assert decoded.header.qrs_count == encoded.qrs_count


def test_bitstream_is_whole_words():
    from test_detect import synthetic_ecg
    encoded = pipeline.encode_samples(synthetic_ecg(10, 320))
    assert len(encoded.payload) % 4 == 0
    assert len(encoded.words) == len(encoded.payload) // 4


@requires_data
def test_record_100_meets_spec_targets():
    """Milestone M1: the gold-standard model must satisfy the spec's constraints."""
    evaluation, encoded, decoded = pipeline.evaluate_record("100")

    assert decoded.checksum_ok, "frame checksum mismatch"
    assert evaluation.prd_qrs == 0.0, (
        f"QRS regions must be lossless, got {evaluation.prd_qrs}%")
    assert evaluation.prd_gated < config.TARGET_PRD_OVERALL, (
        f"PRD {evaluation.prd_gated:.3f}% exceeds {config.TARGET_PRD_OVERALL}%")
    assert evaluation.compression_ratio > config.TARGET_CR, (
        f"CR {evaluation.compression_ratio:.2f} below {config.TARGET_CR}")


@requires_data
def test_record_100_qrs_samples_are_exactly_recovered():
    _, encoded, decoded = pipeline.evaluate_record("100")
    original = encoded.samples.astype(np.int64)
    assert np.array_equal(decoded.samples[decoded.qrs_mask],
                          original[decoded.qrs_mask])
    assert int(np.max(np.abs(decoded.samples[decoded.qrs_mask]
                            - original[decoded.qrs_mask]))) == 0


@requires_data
def test_record_100_segment_layout_matches_encoder():
    _, encoded, decoded = pipeline.evaluate_record("100")
    assert decoded.segment_count == len(encoded.segments)
    for expected, actual in zip(encoded.segments, decoded.segments):
        assert expected.kind == actual[0]
        assert expected.start == actual[1]
        assert expected.end == actual[2]


@requires_data
def test_no_coefficient_saturation_on_record_100():
    """The 10-bit value field must not clip with the frozen quantization shifts."""
    _, encoded, _ = pipeline.evaluate_record("100")
    assert encoded.saturated == 0


def test_prd_of_identical_signals_is_zero():
    x = np.array([1, -2, 3], dtype=np.int64)
    assert metrics.prd(x, x) == 0.0


def test_prd_handles_zero_energy_reference():
    z = np.zeros(4, dtype=np.int64)
    assert metrics.prd(z, z) == 0.0
    assert metrics.prd(z, np.ones(4, dtype=np.int64)) == float("inf")


def test_compression_ratio_formula():
    assert metrics.compression_ratio(1000, 16000) == pytest.approx(1.0)
    assert metrics.compression_ratio(1000, 3200) == pytest.approx(5.0)


def test_latency_conversion():
    assert metrics.latency_seconds(100_000_000) == pytest.approx(1.0)
