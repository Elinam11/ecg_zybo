"""CR-only recount on method-(a) coeffs: cA RLE vs no-RLE, with/without DPCM.

PRD is unchanged (same quantized coefficients). Variants:
  A  default:        RLE+Rice on raw cA
  B  level-only:     Rice each cA sample (no RLE, no DPCM)
  C  DPCM+RLE:       first-differences, then RLE+Rice
  D  DPCM+level-only: first-differences, Rice each sample
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecg_ref import config, lossless, lossy_bw, metrics
from scripts.phase_a_lossy_bw import _analyze_record, _records

OUT = ROOT.parent / "results" / "phase_b"
OUT.mkdir(parents=True, exist_ok=True)

VARIANTS = (
    ("A_default_rle", dict(dpcm_approx=False, no_rle_approx=False)),
    ("B_level_only", dict(dpcm_approx=False, no_rle_approx=True)),
    ("C_dpcm_rle", dict(dpcm_approx=True, no_rle_approx=False)),
    ("D_dpcm_level", dict(dpcm_approx=True, no_rle_approx=True)),
)


def _packet_bits(rice_bits: int) -> tuple[int, int]:
    header = 1 + config.LEN_FIELD_BITS + config.SYMCOUNT_FIELD_BITS
    hdr_pad = (-header) % config.WORD_BITS
    rice_pad = (-rice_bits) % config.WORD_BITS
    data = header + rice_bits
    full = header + hdr_pad + rice_bits + rice_pad
    return data, full


def score_record(pack, qp) -> dict[str, dict]:
    samples, segs, _d, _mask, analyzed = pack
    n = len(samples)
    qrs_data = 0
    qrs_pad = 0
    n_lossy = 0
    acc = {name: {"data": 0, "full": 0, "lossy_data": 0, "lossy_full": 0}
           for name, _ in VARIANTS}
    for kind, _seg, block, an in analyzed:
        if kind == "qrs":
            payload = lossless.encode_qrs(block)
            raw = 1 + config.LEN_FIELD_BITS + payload.bit_count
            qrs_data += raw
            qrs_pad += (-raw) % config.WORD_BITS
            continue
        n_lossy += an.n
    for name, _ in VARIANTS:
        acc[name]["data"] = qrs_data
        acc[name]["full"] = qrs_data + qrs_pad
    for kind, _seg, block, an in analyzed:
        if kind != "nonqrs":
            continue
        bw = lossy_bw.quantize_analyzed(an, qp["t_q6"], qp["inv_d_q16"])
        for name, kwargs in VARIANTS:
            rb = lossy_bw.rice_bits_coded(bw.coded, bw.levels, **kwargs)
            data, full = _packet_bits(rb)
            acc[name]["data"] += data
            acc[name]["full"] += full
            acc[name]["lossy_data"] += data
            acc[name]["lossy_full"] += full
    rows = {}
    for name, _ in VARIANTS:
        a = acc[name]
        full_bits = config.HEADER_BITS + a["full"]
        rows[name] = {
            "data_bits": a["data"],
            "bit_count": full_bits,
            "cr_full": metrics.compression_ratio(n, full_bits),
            "cr_without_tx": metrics.compression_ratio(n, a["data"]),
            "cr_lossy_full": metrics.compression_ratio(n_lossy, a["lossy_full"]),
            "cr_lossy_without_tx": metrics.compression_ratio(
                n_lossy, a["lossy_data"]),
            "n_lossy_samples": n_lossy,
        }
    return rows


def main() -> None:
    qp = lossy_bw.make_quant_params(config.BW_DZ_T_FROZEN, config.BW_DZ_A_FROZEN)
    recs = list(_records())
    per: dict[str, list[dict]] = {name: [] for name, _ in VARIANTS}
    for rec in recs:
        pack = _analyze_record(rec, margin=0, method="a", l_max=config.BW_L_MAX)
        scored = score_record(pack, qp)
        for name, row in scored.items():
            row = dict(row)
            row["record"] = rec
            per[name].append(row)
            print(f"{name} {rec} CR_full={row['cr_full']:.3f} "
                  f"CR_ntx={row['cr_without_tx']:.3f}", flush=True)

    summaries = []
    for name, _ in VARIANTS:
        rows = per[name]
        summaries.append({
            "variant": name,
            "n": len(rows),
            "mean_cr_full": float(np.mean([r["cr_full"] for r in rows])),
            "min_cr_full": min(r["cr_full"] for r in rows),
            "mean_cr_without_tx": float(np.mean([r["cr_without_tx"] for r in rows])),
            "min_cr_without_tx": min(r["cr_without_tx"] for r in rows),
            "mean_cr_lossy_full": float(np.mean([r["cr_lossy_full"] for r in rows])),
            "min_cr_lossy_full": min(r["cr_lossy_full"] for r in rows),
            "worst_cr_record": min(rows, key=lambda r: r["cr_full"])["record"],
        })
    out_sum = OUT / "ca_coder_cr_method_a.csv"
    with out_sum.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(summaries[0].keys()))
        w.writeheader()
        w.writerows(summaries)
    out_per = OUT / "ca_coder_cr_method_a_per_record.csv"
    with out_per.open("w", newline="", encoding="utf-8") as f:
        fields = ["variant", "record", "cr_full", "cr_without_tx",
                  "cr_lossy_full", "cr_lossy_without_tx", "bit_count", "data_bits"]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for name, rows in per.items():
            for r in rows:
                w.writerow({"variant": name, **r})
    (OUT / "ca_coder_cr_method_a.json").write_text(
        json.dumps(summaries, indent=2), encoding="utf-8")
    print(json.dumps(summaries, indent=2), flush=True)


if __name__ == "__main__":
    main()
