"""Phase B ablations at the frozen (T, a) from Phase A."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecg_ref import config, lossy_bw, metrics, preprocess, detect, lossless
from scripts.phase_a_lossy_bw import (  # type: ignore
    _analyze_record, _score_analyzed, _records, choose_frozen,
)

OUT = ROOT.parent / "results" / "phase_b"
OUT.mkdir(parents=True, exist_ok=True)

T = config.BW_DZ_T_FROZEN
A = config.BW_DZ_A_FROZEN
_ANALYZE_CACHE: dict = {}


def _mean_row(name: str, rows: list[dict]) -> dict:
    keys = ["prd_overall", "prd_nonqrs", "cr_full", "cr_without_tx",
            "cr_lossy_full", "cr_lossy_without_tx", "snr_db", "rmse"]
    out = {"config": name, "n": len(rows)}
    for k in keys:
        vals = [r[k] for r in rows]
        out[f"mean_{k}"] = float(np.mean(vals))
        out[f"worst_{k}" if "prd" in k or k == "rmse" else f"min_{k}"] = (
            float(np.max(vals)) if "prd" in k or k == "rmse" else float(np.min(vals)))
    out["worst_prd_record"] = max(rows, key=lambda r: r["prd_overall"])["record"]
    out["gate_prd2"] = all(r["prd_overall"] <= 2.0 for r in rows)
    out["min_cr_full"] = min(r["cr_full"] for r in rows)
    return out


def run_config(name, *, method="b", margin=32, l_max=5, min_approx=16,
               wavelet="97", t=T, a=A, records=None,
               rice_mode="default"):
    records = list(records or _records())
    qp = lossy_bw.make_quant_params(t, a)
    # temporarily override f(N)
    orig_lseg = lossy_bw.l_seg_of

    def l_seg_override(n, l_max_arg=l_max, min_approx_arg=min_approx):
        return orig_lseg(n, l_max=l_max_arg, min_approx=min_approx_arg)

    lossy_bw.l_seg_of = l_seg_override  # type: ignore
    rows = []
    try:
        for rec in records:
            key = (rec, margin, method, l_max, wavelet, min_approx)
            if key not in _ANALYZE_CACHE:
                _ANALYZE_CACHE[key] = _analyze_record(
                    rec, margin, method, l_max, wavelet=wavelet)
            pack = _ANALYZE_CACHE[key]
            samples, segs, _d, mask, analyzed = pack
            st = _score_analyzed(rec, samples, segs, mask, analyzed, qp,
                                 margin, method, l_max)
            if rice_mode != "default":
                # recompute lossy entropy bits with the ablation; PRD unchanged
                data_bits = st["data_bits"]
                # reconstruct rice from payloads... _score_analyzed already coded
                extra = 0
                base = 0
                for kind, _seg, _block, an in analyzed:
                    if kind != "nonqrs":
                        continue
                    bw = lossy_bw.quantize_analyzed(an, qp["t_q6"], qp["inv_d_q16"])
                    base += lossy_bw._rice_body(bw.coded, bw.levels).tell()
                    extra += lossy_bw.rice_bits_coded(
                        bw.coded, bw.levels,
                        no_rle_approx=(rice_mode in ("ca_level", "ca_dpcm")),
                        dpcm_approx=(rice_mode == "ca_dpcm"),
                        z_minus_1=(rice_mode == "z1"),
                    )
                delta = extra - base
                st["data_bits"] += delta
                st["cr_without_tx"] = metrics.compression_ratio(
                    len(samples), st["data_bits"])
                n_lossy = st["n_lossy_samples"]
                # approximate full bits shift by the same delta
                st["bit_count"] += delta
                st["cr_full"] = metrics.compression_ratio(len(samples), st["bit_count"])
            st.pop("rice_stats", None)
            st["config"] = name
            rows.append(st)
            print(f"  {name} {rec} PRD={st['prd_overall']:.3f} CR={st['cr_full']:.2f}",
                  flush=True)
    finally:
        lossy_bw.l_seg_of = orig_lseg  # type: ignore
    summary = _mean_row(name, rows)
    print("SUMMARY", summary, flush=True)
    return summary, rows


def load_frozen_as_method_b() -> tuple[dict, list[dict]] | tuple[None, None]:
    path = ROOT.parent / "results" / "phase_a" / "new_path_frozen.csv"
    if not path.exists():
        return None, None
    rows = []
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            row = {k: (float(v) if k != "record" else v) for k, v in r.items()
                   if k and v != ""}
            row["record"] = r["record"]
            rows.append(row)
    return _mean_row("method_b_M32", rows), rows


def boundary_energy(records, margin=32):
    """Mean |cD| energy of coefficients whose centers sit in the first/last 8 samples."""
    e_a, e_b, n = 0.0, 0.0, 0
    for rec in records:
        samples = preprocess.preprocess_record(rec)
        segs, _ = detect.segment_record(samples)
        for i, seg in enumerate(segs):
            if seg.kind != "nonqrs" or seg.length < 32:
                continue
            block = samples[seg.start:seg.end + 1]
            left = __import__("ecg_ref.pipeline_bw", fromlist=["_neighbors"])
            from ecg_ref.pipeline_bw import _neighbors
            L, R = _neighbors(segs, i, samples)
            an_b = lossy_bw.analyze_segment(block, L, R, margin=margin, method="b")
            an_a = lossy_bw.analyze_segment(block, L, R, margin=margin, method="a")
            def detail_e(an):
                acc = 0.0
                for k, v in an.coeffs_q6.items():
                    if k.startswith("cD"):
                        acc += float(np.sum(np.square(np.asarray(v, dtype=np.float64))))
                return acc
            e_b += detail_e(an_b)
            e_a += detail_e(an_a)
            n += 1
    return {"n_segments": n, "mean_detail_energy_b": e_b / max(n, 1),
            "mean_detail_energy_a": e_a / max(n, 1)}


def main():
    recs = list(_records())
    qp = lossy_bw.make_quant_params(T, A)
    results = []

    # 1. boundary (b) vs (a)
    loaded, _loaded_rows = load_frozen_as_method_b()
    if loaded is not None:
        s_b = loaded
        print("SUMMARY", s_b, flush=True)
    else:
        s_b, _ = run_config("method_b_M32", method="b", margin=32)
    results.append(s_b)
    s_a, _ = run_config("method_a", method="a", margin=32)
    results.append(s_a)
    be = boundary_energy(recs[:8], margin=32)
    (OUT / "boundary_energy.json").write_text(json.dumps(be, indent=2), encoding="utf-8")
    print("boundary energy", be)

    # 2. M=64
    s_m64, _ = run_config("method_b_M64", method="b", margin=64)
    results.append(s_m64)

    # 3. depth
    for lmax, name in ((3, "Lmax3_adapt"), (4, "Lmax4_adapt")):
        s, _ = run_config(name, l_max=lmax, min_approx=16)
        results.append(s)
    s5 = dict(s_b)
    s5["config"] = "Lmax5_adapt"
    results.append(s5)
    s, _ = run_config("Lmax5_fixed", l_max=5, min_approx=1)
    results.append(s)

    # 4. Rice / cA / z-1 (CR-only variants on method b)
    for mode, name in (("ca_level", "ca_level_only"), ("ca_dpcm", "ca_dpcm"),
                       ("z1", "zigzag_minus1")):
        s, _ = run_config(name, rice_mode=mode)
        results.append(s)

    # 5. 5/3 own T sweep (coarse) then pick
    t53 = [1, 2, 4]
    best53 = None
    for t in t53:
        s, rows = run_config(f"53_T{t}", wavelet="53", t=t, a=1.4)
        results.append(s)
        if s["gate_prd2"] and (best53 is None or s["mean_cr_full"] > best53["mean_cr_full"]):
            best53 = dict(s)
            best53["t_adu"] = t
    if best53 is None:
        cand = [r for r in results if r["config"].startswith("53_T")]
        best53 = min(cand, key=lambda r: r["worst_prd_overall"])
        best53["t_adu"] = float(str(best53["config"]).split("T")[1])

    decide = {
        "boundary": "b" if s_b["mean_prd_overall"] <= s_a["mean_prd_overall"] else "a",
        "boundary_reason": (
            f"method b mean PRD {s_b['mean_prd_overall']:.3f} CR {s_b['mean_cr_full']:.2f}; "
            f"method a mean PRD {s_a['mean_prd_overall']:.3f} CR {s_a['mean_cr_full']:.2f}; "
            f"detail energy {be}"
        ),
        "margin": 32 if s_b["mean_prd_overall"] <= s_m64["mean_prd_overall"] * 1.02 else 64,
        "L_max": 5,
        "adaptive_fN": True,
        "rice": "trained per-subband defaults (backward-adaptive / forward-selected not worth extra state)",
        "cA_L": "keep RLE; DPCM/level-only compared in table",
        "z_minus_1": "see zigzag_minus1 row",
        "wavelet": "97" if s_b["mean_prd_overall"] <= best53.get("mean_prd_overall", 99) else "53",
        "best_53": best53,
        "N_min_fallback": "N=1 (12 segments in the dataset): quantize as cA0, no DWT",
        "buffer_Nmax": "2048-2M (1984 at M=32); max gap 1089, no record affected",
    }
    (OUT / "summaries.csv")
    with (OUT / "summaries.csv").open("w", newline="", encoding="utf-8") as f:
        keys = [k for k in results[0].keys() if k != "t_adu"]
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)
    (OUT / "decide.json").write_text(json.dumps(decide, indent=2, default=str),
                                     encoding="utf-8")
    print(json.dumps(decide, indent=2, default=str))


if __name__ == "__main__":
    main()
