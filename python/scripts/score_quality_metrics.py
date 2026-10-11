"""Score PRDN1, per-cycle/non-QRS MAX, and PSim SDNN on every MLII record.

Runs the Python reference encode/decode (bit-identical to hardware frames) and
writes paper/quality_metrics.csv. Hardware recapture is unnecessary when
records_performance.csv already shows matches_golden=True.

Usage:
    python python/scripts/score_quality_metrics.py
    python python/scripts/score_quality_metrics.py 100 213
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np

from ecg_ref import config, metrics, pan_tompkins, pipeline

OUT_DEFAULT = config.REPO_ROOT / "paper" / "quality_metrics.csv"
FIELDS = (
    "record",
    "prdn1",
    "max_global",
    "max_nonqrs",
    "max_qrs",
    "max_nonqrs_cycle_mean",
    "max_nonqrs_cycle_worst",
    "n_cycles_nonqrs",
    "sdnn_orig_ms",
    "sdnn_rec_ms",
    "psim_sdnn",
    "n_peaks_orig",
    "n_peaks_rec",
)


def _fmt(value: float) -> str:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return ""
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def score_record(record: str) -> metrics.QualityReport:
    _evaluation, encoded, decoded = pipeline.evaluate_record(record)
    pt_orig = pan_tompkins.pan_tompkins(encoded.samples, fs=config.FS)
    pt_rec = pan_tompkins.pan_tompkins(decoded.samples, fs=config.FS)
    return metrics.evaluate_quality(
        record=record,
        original=encoded.samples,
        reconstructed=decoded.samples,
        qrs_mask=decoded.qrs_mask,
        r_peaks=encoded.detection.r_peaks,
        pt_orig=pt_orig,
        pt_rec=pt_rec,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", nargs="*", default=None)
    parser.add_argument("--out", type=Path, default=OUT_DEFAULT)
    args = parser.parse_args()

    records = list(args.records) if args.records else list(config.MLII_RECORDS)
    missing = [r for r in records if not (config.MITDB_DIR / f"{r}.hea").exists()]
    if missing:
        print("MIT-BIH files missing:", ", ".join(missing), file=sys.stderr)
        print("Run: python python/scripts/download_mitdb.py", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    rows: list[metrics.QualityReport] = []
    for rec in records:
        print(f"scoring {rec} ...", flush=True)
        rows.append(score_record(rec))

    with args.out.open("w", newline="", encoding="ascii") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for q in rows:
            writer.writerow({k: _fmt(getattr(q, k)) for k in FIELDS})

    prdn1 = [q.prdn1 for q in rows]
    print(f"\nwrote {args.out}")
    print(f"PRDN1  mean={np.mean(prdn1):.4f}  max={np.max(prdn1):.4f}")
    print(f"MAX_nonQRS  mean={np.mean([q.max_nonqrs for q in rows]):.2f}  "
          f"max={np.max([q.max_nonqrs for q in rows]):.2f}")
    psim = [q.psim_sdnn for q in rows if math.isfinite(q.psim_sdnn)]
    if psim:
        print(f"PSim SDNN  mean={np.mean(psim):.2f}  min={np.min(psim):.2f}  "
              f"n={len(psim)}/{len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
