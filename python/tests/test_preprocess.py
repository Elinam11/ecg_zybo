"""Preprocessing and BRAM initialisation file round-trips."""

import numpy as np
import pytest

from conftest import requires_data

from ecg_ref import config, preprocess


def test_write_read_coe_roundtrip(tmp_path):
    rng = np.random.default_rng(0)
    samples = rng.integers(config.SAMPLE_MIN, config.SAMPLE_MAX,
                           size=1000).astype(np.int16)
    path = tmp_path / "test.coe"
    preprocess.write_coe(samples, path)
    assert np.array_equal(preprocess.read_coe(path), samples)


def test_coe_handles_sign_extremes(tmp_path):
    samples = np.array([config.SAMPLE_MIN, -1, 0, 1, config.SAMPLE_MAX],
                       dtype=np.int16)
    path = tmp_path / "edge.coe"
    preprocess.write_coe(samples, path)
    assert np.array_equal(preprocess.read_coe(path), samples)


def test_coe_uses_twos_complement(tmp_path):
    path = tmp_path / "neg.coe"
    preprocess.write_coe(np.array([-1], dtype=np.int16), path)
    assert "FFFF" in path.read_text().upper()


def test_coe_binary_radix_roundtrip(tmp_path):
    samples = np.array([-2, 0, 7], dtype=np.int16)
    path = tmp_path / "bin.coe"
    preprocess.write_coe(samples, path, radix=2)
    assert np.array_equal(preprocess.read_coe(path), samples)


def test_coe_header_format(tmp_path):
    path = tmp_path / "fmt.coe"
    preprocess.write_coe(np.array([1, 2], dtype=np.int16), path)
    text = path.read_text()
    assert text.startswith("memory_initialization_radix=16;")
    assert "memory_initialization_vector=" in text
    assert text.rstrip().endswith(";")


def test_dat_roundtrip(tmp_path):
    samples = np.array([-32768, -1, 0, 12345, 32767], dtype=np.int16)
    path = tmp_path / "test.dat"
    preprocess.write_dat(samples, path)
    assert np.array_equal(preprocess.read_dat(path), samples)


def test_quantize_clips_into_int16():
    x = np.array([-1e9, -32769.0, -0.4, 0.5, 32768.0, 1e9])
    q = preprocess.quantize(x, 1.0)
    assert q.dtype == np.int16
    assert q.min() >= config.SAMPLE_MIN
    assert q.max() <= config.SAMPLE_MAX


def test_quantize_rounds_to_nearest():
    x = np.array([0.4, 0.6, -0.4, -0.6, 2.5])
    q = preprocess.quantize(x, 1.0)
    # numpy rint is round-half-to-even, which is what the reference model defines.
    assert list(q) == [0, 1, 0, -1, 2]


@requires_data
def test_record_100_shape_and_type():
    x = preprocess.preprocess_record("100")
    assert x.shape == (config.N_SAMPLES,)
    assert x.dtype == np.int16


@requires_data
def test_bandpass_removes_dc():
    x = preprocess.read_mlii("100")
    filtered = preprocess.bandpass(x)
    assert abs(float(np.mean(filtered))) < 1.0


@requires_data
def test_calibration_gain_is_unity_for_mitbih():
    """Every MIT-BIH record is 200 adu/mV, so no rescaling should occur."""
    assert preprocess.normalization_gain("100") == pytest.approx(1.0)


@requires_data
def test_full_record_coe_roundtrip(tmp_path):
    x = preprocess.preprocess_record("100")
    path = tmp_path / "100.coe"
    preprocess.write_coe(x, path)
    assert np.array_equal(preprocess.read_coe(path), x)


@requires_data
def test_annotations_are_within_range():
    ann = preprocess.read_annotations("100")
    assert len(ann) > 300
    assert ann.min() >= 0
    assert ann.max() < config.N_SAMPLES
