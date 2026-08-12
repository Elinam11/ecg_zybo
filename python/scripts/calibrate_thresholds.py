"""Phase 1h: choose ONE global band-threshold set for every record, then freeze it.

The three thresholds (T_A4, T_D_LOW, T_D_HIGH) are searched jointly. There is no
per-record tuning: a candidate is only admissible if it satisfies the constraints on
every record in the calibration set, and the winner is the admissible candidate with
the highest mean compression ratio.

The forward DWT of every non-QRS segment is computed once and reused across all
candidates, which is what makes an exhaustive sweep affordable.

Usage:
    python python/scripts/calibrate_thresholds.py --records 100 101 103 --frontier
    python python/scripts/calibrate_thresholds.py --prd-metric strict --prd-limit 2.0
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time

import _bootstrap  # noqa: F401
import numpy as np

from ecg_ref import config, detect, lossless, lossy, metrics, packer, preprocess


def _align32(bits: int) -> int:
    return bits + (-bits % config.WORD_BITS)


class RecordCache:
    """Everything about a record that does not depend on the thresholds."""

    def __init__(self, record: str) -> None:
        self.record = record
        self.samples = preprocess.preprocess_record(record)
        segments, _ = detect.segment_record(self.samples)
        self.segments = segments

        self.qrs_mask = np.zeros(len(self.samples), dtype=bool)
        self.qrs_bits = 0
        self.qrs_count = 0
        # The lossless path is threshold independent, so encode it once.
        for seg in segments:
            if seg.kind != "qrs":
                continue
            payload = lossless.encode_qrs(self.samples[seg.start:seg.end + 1])
            self.qrs_bits += _align32(1 + config.LEN_FIELD_BITS + payload.bit_count)
            self.qrs_mask[seg.start:seg.end + 1] = True
            self.qrs_count += 1

        # Cache the forward transform of every non-QRS segment.
        self.nonqrs: list[tuple[int, int, int, dict[str, np.ndarray]]] = []
        for seg in segments:
            if seg.kind != "nonqrs":
                continue
            block = self.samples[seg.start:seg.end + 1]
            padded = lossy.pad_segment(block)
            self.nonqrs.append((seg.start, seg.length, len(padded),
                                lossy.haar_forward(padded)))

        self.reference = self.samples.astype(np.int64)
        self.energy = float(np.sum(self.reference.astype(np.float64) ** 2))
        shifted = self.reference.astype(np.float64) + config.MITBIH_ADC_ZERO
        self.energy_baseline = float(np.sum(shifted * shifted))

    def evaluate(self, thresholds: dict[str, int]) -> dict[str, float]:
        """Score one candidate threshold set on this record."""
        total_bits = config.HEADER_BITS + self.qrs_bits
        error_sq = 0.0
        tokens_total = 0
        saturated = 0

        for start, length, padded_length, bands in self.nonqrs:
            payload = lossy.encode_from_bands(bands, length, padded_length, thresholds)
            total_bits += _align32(1 + config.LEN_FIELD_BITS
                                   + config.SYMCOUNT_FIELD_BITS
                                   + config.RLE_TOKEN_BITS * len(payload.tokens))
            tokens_total += len(payload.tokens)
            saturated += payload.saturated

            recon = lossy.decode_nonqrs(payload.tokens, length)
            diff = (self.reference[start:start + length] - recon).astype(np.float64)
            error_sq += float(np.sum(diff * diff))

        return {
            "cr": (len(self.samples) * config.SAMPLE_BITS) / total_bits,
            "prd_strict": 100.0 * float(np.sqrt(error_sq / self.energy))
            if self.energy else 0.0,
            "prd_baseline": 100.0 * float(np.sqrt(error_sq / self.energy_baseline))
            if self.energy_baseline else 0.0,
            "bits": float(total_bits),
            "tokens": float(tokens_total),
            "saturated": float(saturated),
        }


def expand(t_a4: int, t_low: int, t_high: int) -> dict[str, int]:
    """Map the three global knobs onto the five bands (spec 4.3.2 grouping)."""
    return {"A4": t_a4, "D4": t_low, "D3": t_low, "D2": t_high, "D1": t_high}


def sweep(caches: list[RecordCache], candidates: list[tuple[int, int, int]],
          prd_metric: str, prd_limit: float, verbose: bool = True) -> list[dict]:
    key = "prd_strict" if prd_metric == "strict" else "prd_baseline"
    rows = []
    for candidate in candidates:
        thresholds = expand(*candidate)
        per_record = [cache.evaluate(thresholds) for cache in caches]
        mean_cr = float(np.mean([r["cr"] for r in per_record]))
        min_cr = float(np.min([r["cr"] for r in per_record]))
        max_prd = float(np.max([r[key] for r in per_record]))
        mean_prd = float(np.mean([r[key] for r in per_record]))
        row = {
            "thresholds": candidate,
            "mean_cr": mean_cr,
            "min_cr": min_cr,
            "max_prd": max_prd,
            "mean_prd": mean_prd,
            "mean_prd_strict": float(np.mean([r["prd_strict"] for r in per_record])),
            "mean_prd_baseline": float(np.mean([r["prd_baseline"]
                                                for r in per_record])),
            "saturated": float(np.sum([r["saturated"] for r in per_record])),
            "admissible": bool(max_prd < prd_limit
                               and min_cr > config.TARGET_CR),
        }
        rows.append(row)
        if verbose:
            print(f"  T={str(candidate):<14} meanCR={mean_cr:5.2f} minCR={min_cr:5.2f} "
                  f"maxPRD({prd_metric})={max_prd:7.3f}% "
                  f"{'OK' if row['admissible'] else ''}")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", nargs="*", default=None,
                        help="calibration set (default: every downloaded record)")
    parser.add_argument("--prd-metric", choices=("strict", "baseline"),
                        default="strict")
    parser.add_argument("--prd-limit", type=float, default=config.TARGET_PRD_OVERALL)
    parser.add_argument("--frontier", action="store_true",
                        help="print the full CR/PRD trade-off curve")
    parser.add_argument("--candidates", nargs="*", default=None,
                        help="explicit candidates as T_A4,T_D_LOW,T_D_HIGH triples; "
                             "makes an all-record sweep affordable")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--out", default=None, help="write the result as JSON")
    args = parser.parse_args()

    records = args.records
    if not records:
        records = [r for r in config.MLII_RECORDS
                   if (config.MITDB_DIR / f"{r}.hea").exists()]
    if not records:
        print("no records available; run download_mitdb.py first")
        return 1

    print(f"Calibrating on {len(records)} record(s): {' '.join(records)}")
    t0 = time.time()
    caches = []
    for record in records:
        caches.append(RecordCache(record))
        print(f"  cached {record} "
              f"({caches[-1].qrs_count} beats, {len(caches[-1].nonqrs)} non-QRS)")
    print(f"cache built in {time.time() - t0:.1f}s\n")

    if args.candidates:
        candidates = []
        for spec in args.candidates:
            parts = spec.split(",")
            if len(parts) != 3:
                print(f"bad candidate '{spec}', expected T_A4,T_D_LOW,T_D_HIGH")
                return 1
            candidates.append(tuple(int(p) for p in parts))
    else:
        a4_grid = (0, 1, 2, 3, 4, 6, 8, 12)
        low_grid = (0, 1, 2, 3, 4, 6, 8, 12, 16)
        high_grid = (0, 1, 2, 4, 6, 8, 12, 16, 24, 32)
        candidates = [c for c in itertools.product(a4_grid, low_grid, high_grid)]

    print(f"Sweeping {len(candidates)} candidates "
          f"(constraint: {args.prd_metric} PRD < {args.prd_limit}%, "
          f"CR > {config.TARGET_CR})")
    rows = sweep(caches, candidates, args.prd_metric, args.prd_limit,
                 verbose=args.verbose or bool(args.candidates))

    admissible = [r for r in rows if r["admissible"]]
    admissible.sort(key=lambda r: -r["mean_cr"])

    if args.frontier:
        print("\nPareto frontier (max CR for each PRD ceiling):")
        for ceiling in (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 8.0):
            feasible = [r for r in rows if r["max_prd"] < ceiling]
            if not feasible:
                print(f"  PRD < {ceiling:4.1f}%: none")
                continue
            best = max(feasible, key=lambda r: r["mean_cr"])
            print(f"  PRD < {ceiling:4.1f}%: T={str(best['thresholds']):<14} "
                  f"meanCR={best['mean_cr']:5.2f} minCR={best['min_cr']:5.2f} "
                  f"maxPRD={best['max_prd']:6.3f}% "
                  f"(strict {best['mean_prd_strict']:.3f}% / "
                  f"baseline {best['mean_prd_baseline']:.4f}%)")

    print()
    if not admissible:
        print("NO candidate satisfies both constraints simultaneously.")
        best_cr = max(rows, key=lambda r: r["mean_cr"] if r["max_prd"] < args.prd_limit
                      else -1)
        print(f"  best CR meeting PRD only: T={best_cr['thresholds']} "
              f"meanCR={best_cr['mean_cr']:.2f} maxPRD={best_cr['max_prd']:.3f}%")
        cr_ok = [r for r in rows if r["min_cr"] > config.TARGET_CR]
        if cr_ok:
            best_prd = min(cr_ok, key=lambda r: r["max_prd"])
            print(f"  best PRD meeting CR only: T={best_prd['thresholds']} "
                  f"meanCR={best_prd['mean_cr']:.2f} maxPRD={best_prd['max_prd']:.3f}%")
        result = {"status": "infeasible", "metric": args.prd_metric}
    else:
        winner = admissible[0]
        print(f"WINNER: T_A4={winner['thresholds'][0]} "
              f"T_D_LOW={winner['thresholds'][1]} T_D_HIGH={winner['thresholds'][2]}")
        print(f"  mean CR {winner['mean_cr']:.2f}, min CR {winner['min_cr']:.2f}, "
              f"max {args.prd_metric} PRD {winner['max_prd']:.3f}%")
        result = {"status": "ok", "metric": args.prd_metric,
                  "prd_limit": args.prd_limit,
                  "thresholds": expand(*winner["thresholds"]),
                  "mean_cr": winner["mean_cr"], "min_cr": winner["min_cr"],
                  "max_prd": winner["max_prd"], "records": records}

    if args.out:
        path = config.REPORT_DIR / args.out
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"result": result, "sweep": rows}, indent=2),
                        encoding="utf-8")
        print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
