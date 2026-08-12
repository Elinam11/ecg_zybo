"""Run the golden reference model over records and report CR / PRD / detection.

Also emits the golden vectors that the HLS testbenches compare against, so this script
is the gate between Phase 1 and Phase 2.

Usage:
    python python/scripts/run_reference.py 100 --golden
    python python/scripts/run_reference.py --all --report reference_matrix.md
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import _bootstrap  # noqa: F401
import numpy as np

from ecg_ref import config, detect, pipeline, preprocess


def analyse(record: str, emit_golden: bool, n_samples: int | None = None,
            golden_name: str | None = None) -> dict:
    evaluation, encoded, decoded = pipeline.evaluate_record(record, n_samples)

    try:
        annotations = preprocess.read_annotations(record)
        score = detect.score_detection(encoded.detection.r_peaks, annotations)
        sensitivity, ppv = score.sensitivity, score.ppv
    except Exception:  # noqa: BLE001  (annotation file may be absent)
        sensitivity = ppv = float("nan")

    if emit_golden:
        pipeline.write_golden(golden_name or record, encoded)

    qrs_bits = sum(p.bit_count for p in encoded.qrs_payloads)
    nonqrs_bits = sum(p.bit_count for p in encoded.nonqrs_payloads)
    nonqrs_samples = evaluation.sample_count - evaluation.qrs_sample_count

    return {
        "record": record,
        "cr": evaluation.compression_ratio,
        "prd_strict": evaluation.prd_overall,
        "prd_baseline": evaluation.prd_overall_baseline,
        "prd_qrs": evaluation.prd_qrs,
        "prd_nonqrs": evaluation.prd_nonqrs,
        "beats": evaluation.qrs_count,
        "segments": evaluation.segment_count,
        "bits": evaluation.compressed_bits,
        "qrs_bits_per_sample": qrs_bits / max(1, evaluation.qrs_sample_count),
        "nonqrs_bits_per_sample": nonqrs_bits / max(1, nonqrs_samples),
        "escapes": evaluation.escapes,
        "saturated": evaluation.saturated,
        "checksum_ok": evaluation.checksum_ok,
        "max_abs_error": evaluation.max_abs_error,
        "sensitivity": sensitivity,
        "ppv": ppv,
        "cr_pass": evaluation.cr_pass,
        "prd_pass": evaluation.prd_pass,
        "qrs_pass": evaluation.qrs_pass,
        "passed": evaluation.passed,
    }


def write_markdown(rows: list[dict], path) -> None:
    lines = [
        "# Reference Model Verification Matrix",
        "",
        f"Gate: CR > {config.TARGET_CR}, PRD < {config.TARGET_PRD_OVERALL}% "
        f"({config.PRD_GATE_METRIC}-referenced), PRD_qrs = 0%.",
        "",
        "Thresholds (global, frozen): "
        + ", ".join(f"{b}={config.BAND_THRESHOLD[b]}" for b in config.BAND_ORDER),
        "",
        "| Record | CR | PRD (base) | PRD (strict) | PRD QRS | Beats | Se% | PPV% "
        "| Sat | Esc | Result |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['record']} | {r['cr']:.2f} | {r['prd_baseline']:.4f} | "
            f"{r['prd_strict']:.3f} | {r['prd_qrs']:.3f} | {r['beats']} | "
            f"{r['sensitivity']:.2f} | {r['ppv']:.2f} | {r['saturated']} | "
            f"{r['escapes']} | {'PASS' if r['passed'] else 'FAIL'} |")

    passed = sum(1 for r in rows if r["passed"])
    lines += [
        "",
        f"**{passed}/{len(rows)} records pass.**",
        "",
        f"- mean CR {np.mean([r['cr'] for r in rows]):.2f} "
        f"(min {np.min([r['cr'] for r in rows]):.2f})",
        f"- mean PRD baseline {np.mean([r['prd_baseline'] for r in rows]):.4f}% "
        f"(max {np.max([r['prd_baseline'] for r in rows]):.4f}%)",
        f"- mean PRD strict {np.mean([r['prd_strict'] for r in rows]):.3f}% "
        f"(max {np.max([r['prd_strict'] for r in rows]):.3f}%)",
        f"- QRS lossless on every record: "
        f"{all(r['prd_qrs'] == 0.0 for r in rows)}",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", nargs="*", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--golden", action="store_true",
                        help="emit golden vectors for the HLS testbenches")
    parser.add_argument("--report", default=None, help="markdown report filename")
    parser.add_argument("--json", default=None)
    parser.add_argument("--samples", type=int, default=None,
                        help="truncate to the first N samples (short golden vectors "
                             "for RTL co-simulation, which cannot afford 108k samples)")
    parser.add_argument("--golden-name", default=None,
                        help="write golden vectors under this name instead of the "
                             "record number; only valid for a single record")
    args = parser.parse_args()

    if args.golden_name and len(args.records) != 1:
        parser.error("--golden-name requires exactly one record")

    if args.records:
        records = args.records
    else:
        records = [r for r in config.MLII_RECORDS
                   if (config.MITDB_DIR / f"{r}.hea").exists()]
    if not records:
        print("no records available; run download_mitdb.py first")
        return 1

    print(f"Reference model over {len(records)} record(s)")
    print(f"gate: PRD < {config.TARGET_PRD_OVERALL}% "
          f"({config.PRD_GATE_METRIC}), CR > {config.TARGET_CR}\n")

    rows = []
    t0 = time.time()
    for record in records:
        try:
            row = analyse(record, args.golden, args.samples, args.golden_name)
        except Exception as exc:  # noqa: BLE001
            print(f"  {record}: ERROR {type(exc).__name__}: {exc}")
            continue
        rows.append(row)
        print(f"  {row['record']}: CR={row['cr']:5.2f} "
              f"PRDbase={row['prd_baseline']:.4f}% "
              f"PRDstrict={row['prd_strict']:6.3f}% "
              f"PRDqrs={row['prd_qrs']:.3f}% "
              f"Se={row['sensitivity']:6.2f}% "
              f"{'PASS' if row['passed'] else 'FAIL'}")

    if not rows:
        return 1
    print(f"\nelapsed {time.time() - t0:.1f}s")
    passed = sum(1 for r in rows if r["passed"])
    print(f"{passed}/{len(rows)} records pass")
    print(f"mean CR {np.mean([r['cr'] for r in rows]):.2f}, "
          f"max PRD(base) {np.max([r['prd_baseline'] for r in rows]):.4f}%, "
          f"QRS lossless everywhere: "
          f"{all(r['prd_qrs'] == 0.0 for r in rows)}")

    if args.report:
        path = config.REPORT_DIR / args.report
        write_markdown(rows, path)
        print(f"wrote {path}")
    if args.json:
        path = config.REPORT_DIR / args.json
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"wrote {path}")

    return 0 if passed == len(rows) else 2


if __name__ == "__main__":
    sys.exit(main())
