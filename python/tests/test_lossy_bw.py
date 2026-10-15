"""Golden-model checks for the bior4.4 lossy path."""

from __future__ import annotations

import numpy as np
import pytest

from ecg_ref import config, lossy_bw, packer, pipeline_bw
from tests.conftest import requires_data

pywt = pytest.importorskip("pywt")


def _smooth(n: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    x = 200 * np.sin(2 * np.pi * t / 40.0) + 40 * np.sin(2 * np.pi * t / 11.0)
    x += rng.normal(0, 2.0, size=n)
    return np.rint(x).astype(np.int64)


def test_linear_dwt_roundtrip():
    x = _smooth(128).astype(np.float64)
    bands = lossy_bw.dwt_forward_lin(x, 4)
    rec = lossy_bw.dwt_inverse_lin(bands, 4)
    assert rec.shape == x.shape
    assert np.max(np.abs(rec - x)) < 1e-8


def test_inclusion_count_equals_n():
    mismatches = []
    for n in (2, 3, 16, 17, 32, 33, 64, 73, 100, 127, 128, 255, 256, 512, 1089):
        for m in (32, 64):
            l = lossy_bw.l_seg_of(n)
            n_eq = lossy_bw.inclusion_equation_count(n, l, m)
            if n_eq != n:
                mismatches.append((n, l, m, n_eq))
    assert mismatches == [], f"n_eq != N for {mismatches[:8]}"


def test_entropy_roundtrip_random_coeffs():
    rng = np.random.default_rng(1)
    for n in (16, 32, 64, 200, 512):
        levels = lossy_bw.l_seg_of(n)
        lengths = lossy_bw.band_lengths_method_a(n, levels)
        coded = {}
        for name in lossy_bw.band_order(levels):
            ln = lengths.get(name, 0)
            vals = rng.integers(-20, 21, size=ln).tolist()
            # sprinkle zeros
            for i in range(0, ln, 3):
                vals[i] = 0
            coded[name] = vals
        rec = lossy_bw.entropy_roundtrip(coded, levels)
        for name in coded:
            assert rec[name] == coded[name], name


def test_ca_dpcm_rice_every_sample_details_rle():
    """cA: DPCM then Rice each sample. Details: zero-run RLE + Rice."""
    ca = [3, 3, 3, 4, 4]
    w = packer.BitWriter()
    lossy_bw.encode_subband(w, ca, "cA5")
    ntok_ca = lossy_bw.rice_get(packer.BitReader(w.to_words()), lossy_bw.BW_RICE_K_NTOK)
    assert ntok_ca == len(ca)
    rec = lossy_bw.decode_subband(packer.BitReader(w.to_words()), len(ca), "cA5")
    assert rec == ca

    detail = [0, 0, 0, 5, 0, 0]
    wd = packer.BitWriter()
    lossy_bw.encode_subband(wd, detail, "cD2")
    ntok_d = lossy_bw.rice_get(packer.BitReader(wd.to_words()), lossy_bw.BW_RICE_K_NTOK)
    assert ntok_d == 1
    rec_d = lossy_bw.decode_subband(
        packer.BitReader(wd.to_words()), len(detail), "cD2")
    assert rec_d == detail


def test_default_encode_is_method_a():
    qp = lossy_bw.make_quant_params(4.0, 1.4)
    x = _smooth(80)
    dummy = lossy_bw.EdgeSpec("b", np.arange(40, dtype=np.int64))
    bw = lossy_bw.encode_segment(
        x, dummy, dummy, qp["t_q6"], qp["inv_d_q16"], qp["delta_real"])
    assert bw.method == "a"
    assert bw.margin == 0


def test_packet_skip_by_word_count():
    qp = lossy_bw.make_quant_params(8.0, 1.4)
    x = _smooth(80)
    left = lossy_bw.EdgeSpec("a", np.zeros(0, dtype=np.int64))
    right = left
    payload = lossy_bw.encode_segment(
        x, left, right, qp["t_q6"], qp["inv_d_q16"], qp["delta_real"],
        method="a")
    w = packer.BitWriter()
    lossy_bw.write_nonqrs_bw(w, payload)
    words = w.to_words()
    r = packer.BitReader(words)
    tag = r.get(1)
    length = r.get(config.LEN_FIELD_BITS)
    wc = r.get(config.SYMCOUNT_FIELD_BITS)
    r.align32()
    skipped = [r.get(32) for _ in range(wc)]
    assert tag == config.TAG_NONQRS
    assert length == 80
    assert len(skipped) == wc
    assert r.remaining() == 0


def test_method_a_reconstructs_smooth_signal():
    qp = lossy_bw.make_quant_params(4.0, 1.4)
    x = _smooth(256)
    left = lossy_bw.EdgeSpec("a", np.zeros(0, dtype=np.int64))
    bw = lossy_bw.encode_segment(
        x, left, left, qp["t_q6"], qp["inv_d_q16"], qp["delta_real"], method="a")
    rec = lossy_bw.reconstruct_segment(bw, left, left, qp["delta_real"])
    err = np.max(np.abs(rec - x.astype(np.float64)))
    # coarse T=4 on a 200-adu sinusoid should stay well below the amplitude
    assert err < 80.0
    assert bw.coded


def test_pywt_interior_correlation():
    """Interior coeffs track pywt bior4.4 periodization (scale ~1, shift 0)."""
    n = 1024
    x = _smooth(n, seed=3).astype(np.float64)
    levels = 4
    ours = lossy_bw.dwt_forward_lin(x, levels)
    py = pywt.wavedec(x, "bior4.4", level=levels, mode="periodization")
    for j, name in enumerate([f"cA{levels}", "cD4", "cD3", "cD2", "cD1"]):
        a = np.asarray(ours[name], dtype=np.float64)[16:-16]
        b = np.asarray(py[j], dtype=np.float64)[16:16 + len(a)]
        corr = float(np.corrcoef(a, b)[0, 1])
        scale = float(np.dot(a, b) / np.dot(b, b))
        assert abs(corr) > 0.999, f"{name} corr={corr}"
        assert abs(scale - 1.0) < 0.02, f"{name} scale={scale}"


def test_pywt_style_energy_ratio_near_one():
    """Near-orthonormal scaling: coefficient energy / signal energy ~ 1 on a
    periodic-friendly length (JPEG2000-style would be ~0.5)."""
    rng = np.random.default_rng(0)
    x = rng.normal(size=1024)
    bands = lossy_bw.dwt_forward_lin(x, 5)
    e_x = float(np.sum(x * x))
    e_c = sum(float(np.sum(np.square(v))) for v in bands.values())
    ratio = e_c / e_x
    assert 0.85 < ratio < 1.15, f"energy ratio {ratio}"


def test_method_b_low_t_reconstructs():
    n, m = 64, 32
    rng = np.random.default_rng(0)
    left = rng.integers(-40, 40, size=73).astype(np.int64)
    right = rng.integers(-40, 40, size=73).astype(np.int64)
    t = np.arange(n)
    seg = np.rint(80 * np.sin(2 * np.pi * t / 20.0)).astype(np.int64)
    qp = lossy_bw.make_quant_params(1.0, 1.2)
    L = lossy_bw.EdgeSpec("b", left)
    R = lossy_bw.EdgeSpec("b", right)
    bw = lossy_bw.encode_segment(
        seg, L, R, qp["t_q6"], qp["inv_d_q16"], qp["delta_real"],
        margin=m, method="b")
    rec = lossy_bw.reconstruct_segment(bw, L, R, qp["delta_real"])
    rmse = float(np.sqrt(np.mean((rec - seg) ** 2)))
    assert bw.method == "b"
    assert rmse < 15.0, f"rmse={rmse}"
    # n_eq == N
    assert lossy_bw.inclusion_equation_count(n, bw.levels, m) == n


def test_pywt_bior22_is_legall53_structure():
    """Brief's integer 5/3 matches pywt bior2.2 interior correlation (VERIFY)."""
    n = 256
    x = _smooth(n, seed=4).astype(np.float64)
    ours = lossy_bw.dwt_forward_lin(x, 3, wavelet="53")
    py = pywt.wavedec(x, "bior2.2", level=3, mode="periodization")
    a = np.asarray(ours["cA3"], dtype=np.float64)[8:-8]
    b = np.asarray(py[0], dtype=np.float64)[8:8 + len(a)]
    corr = float(np.corrcoef(a, b)[0, 1])
    assert abs(corr) > 0.9, f"bior2.2 cA corr={corr}"


def test_legall53_integer_roundtrip():
    x = [sample if abs(sample) < 20000 else 0
         for sample in _smooth(64).tolist()]
    x_q = [lossy_bw.sample_to_q6(v) for v in x]
    bands = lossy_bw.dwt_forward_int(x_q, 3, wavelet="53")
    low = bands["cA3"]
    for j in range(3, 0, -1):
        low = lossy_bw._inv_level_53_int(low, bands[f"cD{j}"])
    assert low[:len(x_q)] == x_q


def test_quant_params_are_representable():
    qp = lossy_bw.make_quant_params(5.25, 1.4)
    assert qp["t_q6"] == int(round(5.25 * 64))
    assert qp["inv_d_q16"] > 0
    # round-trip a coefficient just above T
    t = qp["t_q6"]
    c = t + (1 << config.BW_FRAC_DATA)
    q = lossy_bw.dzq_q(c, t, qp["inv_d_q16"])
    assert q != 0
    assert lossy_bw.dzq_q(t - 1, t, qp["inv_d_q16"]) == 0


@requires_data
def test_record_100_entropy_and_qrs_zero():
    encoded = pipeline_bw.encode_record_bw("100", 4.0, 1.4)
    rec = pipeline_bw.decode_bitstream_bw(encoded.payload, encoded)
    rec_i = np.rint(rec).astype(np.int64)
    assert encoded.qrs_mask.sum() > 0
    assert np.array_equal(rec_i[encoded.qrs_mask], encoded.samples[encoded.qrs_mask])
    for bw in encoded.bw_payloads:
        assert lossy_bw.entropy_roundtrip(bw.coded, bw.levels) == {
            k: v for k, v in bw.coded.items()
        }
    assert len(encoded.bw_payloads) == 369
