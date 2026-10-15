"""Phase A: histogram, round-trip, T/a sweep, Rice retrain, Haar baseline."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecg_ref import config, detect, lossy_bw, metrics, pipeline, pipeline_bw, preprocess
from ecg_ref.fixedpoint import floor_log2, zigzag_map

OUT = ROOT.parent / "results" / "phase_a"
OUT.mkdir(parents=True, exist_ok=True)


def _records() -> tuple[str, ...]:
    return config.MLII_RECORDS


def extract_lossy_lengths() -> dict:
    lengths: list[int] = []
    per_record: dict[str, list[int]] = {}
    n_lossy_samp = 0
    n_total_samp = 0
    n_qrs_seg = 0
    n_lossy_seg = 0
    for rec in _records():
        samples = preprocess.preprocess_record(rec)
        segs, _det = detect.segment_record(samples)
        rec_len = [s.length for s in segs if s.kind == "nonqrs"]
        per_record[rec] = rec_len
        lengths.extend(rec_len)
        n_lossy_seg += len(rec_len)
        n_qrs_seg += sum(1 for s in segs if s.kind == "qrs")
        n_lossy_samp += sum(rec_len)
        n_total_samp += len(samples)
    arr = np.asarray(lengths, dtype=np.int64)
    hist = {
        "n_records": len(_records()),
        "n_lossy_segments": int(arr.size),
        "n_qrs_segments": n_qrs_seg,
        "min": int(arr.min()) if arr.size else 0,
        "median": float(np.median(arr)) if arr.size else 0,
        "max": int(arr.max()) if arr.size else 0,
        "mean": float(arr.mean()) if arr.size else 0,
        "frac_lt_32": float(np.mean(arr < 32)) if arr.size else 0,
        "frac_lt_64": float(np.mean(arr < 64)) if arr.size else 0,
        "frac_lt_128": float(np.mean(arr < 128)) if arr.size else 0,
        "frac_lt_256": float(np.mean(arr < 256)) if arr.size else 0,
        "n_eq_1": int(np.sum(arr == 1)),
        "frac_samples_lossy": n_lossy_samp / n_total_samp if n_total_samp else 0,
        "n_lossy_samples": n_lossy_samp,
        "n_total_samples": n_total_samp,
        "longest_record": max(per_record, key=lambda r: max(per_record[r] or [0])),
    }
    (OUT / "segment_histogram.json").write_text(json.dumps(hist, indent=2), encoding="utf-8")
    with (OUT / "segment_lengths.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["record", "seg_index", "length"])
        for rec, ls in per_record.items():
            for i, n in enumerate(ls):
                w.writerow([rec, i, n])
    print(json.dumps(hist, indent=2))
    return hist


def _verify_record(record: str, t_adu: float = 4.0, a: float = 1.4) -> dict:
    encoded = pipeline_bw.encode_record_bw(record, t_adu, a, method="b",
                                           margin=config.BW_BND_MARGIN)
    rec = pipeline_bw.decode_bitstream_bw(encoded.payload, encoded)
    rec_bytes, mask = pipeline_bw.decode_from_bytes_bw(
        encoded.payload, t_adu, a, margin=encoded.margin, method="b")
    # entropy round-trip
    n_mismatch = 0
    for bw in encoded.bw_payloads:
        got = lossy_bw.entropy_roundtrip(bw.coded, bw.levels)
        for name, vals in bw.coded.items():
            if got.get(name) != vals:
                n_mismatch += 1
    # independence: reconstruct lossy segments in reverse order
    rec2 = encoded.samples.astype(np.float64).copy()
    rec2[~encoded.qrs_mask] = 0
    for seg in encoded.segments:
        if seg.kind == "qrs":
            rec2[seg.start:seg.end + 1] = encoded.samples[seg.start:seg.end + 1]
    bw_by_seg = []
    it = iter(encoded.bw_payloads)
    for i, seg in enumerate(encoded.segments):
        if seg.kind == "nonqrs":
            bw_by_seg.append((i, seg, next(it)))
    rng = np.random.default_rng(0)
    order = list(range(len(bw_by_seg)))
    rng.shuffle(order)
    for j in order:
        i, seg, bw = bw_by_seg[j]
        left, right = pipeline_bw._neighbors(encoded.segments, i, rec2)
        x_u = lossy_bw.reconstruct_segment(bw, left, right, encoded.delta_real)
        rec2[seg.start:seg.end + 1] = x_u[:seg.length]
    ev = pipeline_bw.evaluate_bw(record, encoded, rec)
    max_ind = float(np.max(np.abs(np.rint(rec2) - np.rint(rec))))
    max_bytes = float(np.max(np.abs(np.rint(rec_bytes) - np.rint(rec))))
    out = {
        "record": record,
        "entropy_mismatches": n_mismatch,
        "independence_max_delta": max_ind,
        "bytes_decoder_max_delta": max_bytes,
        **ev,
    }
    print(json.dumps(out, indent=2))
    return out


def _analyze_record(record: str, margin: int, method: str, l_max: int,
                    wavelet: str = "97"):
    samples = preprocess.preprocess_record(record)
    segs, det = detect.segment_record(samples)
    analyzed = []
    qrs_mask = np.zeros(len(samples), dtype=bool)
    for i, seg in enumerate(segs):
        block = samples[seg.start:seg.end + 1]
        if seg.kind == "qrs":
            qrs_mask[seg.start:seg.end + 1] = True
            analyzed.append(("qrs", seg, block, None))
        else:
            left, right = pipeline_bw._neighbors(segs, i, samples)
            an = lossy_bw.analyze_segment(block, left, right, margin=margin,
                                          l_max=l_max, method=method,
                                          wavelet=wavelet)
            analyzed.append(("nonqrs", seg, block, an))
    return samples, segs, det, qrs_mask, analyzed


def _score_analyzed(record, samples, segs, qrs_mask, analyzed, qp, margin, method, l_max):
    rec = samples.astype(np.float64).copy()
    data_bits = 0
    lossy_full = 0
    lossy_data = 0
    n_lossy = 0
    rice_stats = defaultdict(list)  # band -> list of mapped values / runs
    # QRS packets (unchanged coder) — count bits from real lossless encode
    from ecg_ref import lossless, packer as pk
    for kind, seg, block, an in analyzed:
        if kind == "qrs":
            payload = lossless.encode_qrs(block)
            data_bits += 1 + config.LEN_FIELD_BITS + payload.bit_count
            rec[seg.start:seg.end + 1] = block
            continue
        bw = lossy_bw.quantize_analyzed(an, qp["t_q6"], qp["inv_d_q16"])
        body = lossy_bw._rice_body(bw.coded, bw.levels)
        rice_bits = body.tell()
        header_fields = 1 + config.LEN_FIELD_BITS + config.SYMCOUNT_FIELD_BITS
        data_bits += header_fields + rice_bits
        lossy_data += header_fields + rice_bits
        raw = header_fields + rice_bits
        # packet: 33 bits, align32, then rice padded to words
        hdr_pad = (-header_fields) % config.WORD_BITS
        rice_pad = (-rice_bits) % config.WORD_BITS
        lossy_full += header_fields + hdr_pad + rice_bits + rice_pad
        n_lossy += an.n
        x_u = lossy_bw.reconstruct_segment(bw, an.left, an.right, qp["delta_real"])
        rec[seg.start:seg.end + 1] = x_u[:an.n]
        for name, vals in bw.coded.items():
            toks = lossy_bw.rle_tokens(vals)
            rice_stats[name + "_run"].extend(t[0] for t in toks)
            rice_stats[name + "_lvl"].extend(zigzag_map(t[1]) for t in toks)

    rec_i = np.rint(rec).astype(np.int64)
    orig = samples.astype(np.int64)
    full_bits = config.HEADER_BITS + data_bits
    # plus per-packet alignment: use lossy_full - lossy_data plus QRS pads unknown;
    # CR full is measured from an actual encode of one point. For the sweep we
    # approximate full bits as header + aligned packets.
    qrs_pad_est = 0
    for kind, seg, block, an in analyzed:
        if kind != "qrs":
            continue
        from ecg_ref import lossless
        payload = lossless.encode_qrs(block)
        raw = 1 + config.LEN_FIELD_BITS + payload.bit_count
        qrs_pad_est += (-raw) % config.WORD_BITS
    full_bits = config.HEADER_BITS + (lossy_full) + (
        data_bits - lossy_data) + qrs_pad_est
    ev = metrics.evaluate(
        record=record, original=orig, reconstructed=rec_i, qrs_mask=qrs_mask,
        compressed_bits=full_bits,
        qrs_count=sum(1 for k, *_ in analyzed if k == "qrs"),
        segment_count=len(segs), checksum_ok=True,
    )
    return {
        "record": record,
        "prd_overall": ev.prd_overall,
        "prd_nonqrs": ev.prd_nonqrs,
        "prd_qrs": ev.prd_qrs,
        "prd_baseline": ev.prd_overall_baseline,
        "cr_full": metrics.compression_ratio(len(orig), full_bits),
        "cr_without_tx": metrics.compression_ratio(len(orig), data_bits),
        "cr_lossy_full": metrics.compression_ratio(n_lossy, lossy_full),
        "cr_lossy_without_tx": metrics.compression_ratio(n_lossy, lossy_data),
        "snr_db": metrics.snr_db(orig, rec_i),
        "rmse": metrics.rmse(orig, rec_i),
        "n_lossy_samples": n_lossy,
        "bit_count": full_bits,
        "data_bits": data_bits,
        "rice_stats": rice_stats,
    }


def sweep(t_values, a_values, margin=config.BW_BND_MARGIN, method="b",
          l_max=config.BW_L_MAX, records=None):
    records = list(records or _records())
    cache = {}
    print(f"analyzing {len(records)} records ...")
    for rec in records:
        cache[rec] = _analyze_record(rec, margin, method, l_max)
        print(f"  analyzed {rec}")

    rows = []
    for a in a_values:
        for t in t_values:
            qp = lossy_bw.make_quant_params(t, a)
            per = []
            for rec in records:
                samples, segs, _det, mask, analyzed = cache[rec]
                stats = _score_analyzed(rec, samples, segs, mask, analyzed,
                                        qp, margin, method, l_max)
                stats.pop("rice_stats", None)
                stats.update({"t_adu": qp["t_real"], "a": a,
                              "t_q6": qp["t_q6"], "delta_real": qp["delta_real"]})
                per.append(stats)
            worst = max(per, key=lambda r: r["prd_overall"])
            mean_prd = float(np.mean([r["prd_overall"] for r in per]))
            mean_cr = float(np.mean([r["cr_full"] for r in per]))
            all_ok = all(r["prd_overall"] <= 2.0 for r in per)
            summary = {
                "t_adu": qp["t_real"], "a": a, "t_q6": qp["t_q6"],
                "delta_real": qp["delta_real"],
                "mean_prd_overall": mean_prd,
                "worst_prd_overall": worst["prd_overall"],
                "worst_record": worst["record"],
                "mean_prd_nonqrs": float(np.mean([r["prd_nonqrs"] for r in per])),
                "worst_prd_nonqrs": max(r["prd_nonqrs"] for r in per),
                "mean_cr_full": mean_cr,
                "mean_cr_without_tx": float(np.mean([r["cr_without_tx"] for r in per])),
                "min_cr_full": min(r["cr_full"] for r in per),
                "gate_prd2_all": all_ok,
                "gate_cr5": min(r["cr_full"] for r in per) >= 5.0,
            }
            print(summary)
            rows.append({"summary": summary, "per": per})
    path = OUT / "sweep.json"
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    with (OUT / "sweep_summary.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]["summary"].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r["summary"])
    return rows, cache


def choose_frozen(rows) -> dict:
    """Largest T whose whole-record strict PRD <= 2% on every record; prefer higher CR."""
    ok = [r["summary"] for r in rows if r["summary"]["gate_prd2_all"]]
    if not ok:
        # fall back to lowest worst-PRD
        best = min((r["summary"] for r in rows),
                   key=lambda s: (s["worst_prd_overall"], -s["mean_cr_full"]))
        best = dict(best)
        best["gate_met"] = False
        return best
    best = max(ok, key=lambda s: (s["t_adu"], s["mean_cr_full"]))
    best = dict(best)
    best["gate_met"] = True
    return best


def retrain_rice(cache, qp) -> dict[str, tuple[int, int]]:
    runs = defaultdict(list)
    lvls = defaultdict(list)
    for rec, (_s, _segs, _d, _m, analyzed) in cache.items():
        for kind, seg, block, an in analyzed:
            if kind != "nonqrs":
                continue
            bw = lossy_bw.quantize_analyzed(an, qp["t_q6"], qp["inv_d_q16"])
            for name, vals in bw.coded.items():
                toks = lossy_bw.rle_tokens(vals)
                runs[name].extend(t[0] for t in toks)
                lvls[name].extend(zigzag_map(t[1]) for t in toks)

    def pick_k(vals):
        if not vals:
            return 2
        mean = int(sum(vals) // max(len(vals), 1))
        return int(min(max(floor_log2(max(mean, 1)), 0), config.BW_RICE_K_MAX))

    table = {}
    for name in sorted(set(runs) | set(lvls)):
        table[name] = (pick_k(runs.get(name, [])), pick_k(lvls.get(name, [])))
    (OUT / "rice_defaults.json").write_text(json.dumps(table, indent=2), encoding="utf-8")
    print("retrained Rice k", table)
    return table


def old_baseline(records=None):
    records = list(records or _records())
    rows = []
    for rec in records:
        ev, encoded, _dec = pipeline.evaluate_record(rec)
        data_bits, lossy_full, lossy_data = pipeline_bw.old_path_cr_without_tx(encoded)
        n_lossy = int(np.count_nonzero(~encoded.qrs_mask))
        row = {
            "record": rec,
            "prd_overall": ev.prd_overall,
            "prd_nonqrs": ev.prd_nonqrs,
            "prd_qrs": ev.prd_qrs,
            "prd_baseline": ev.prd_overall_baseline,
            "cr_full": ev.compression_ratio,
            "cr_without_tx": metrics.compression_ratio(ev.sample_count, data_bits),
            "cr_lossy_full": metrics.compression_ratio(n_lossy, lossy_full),
            "cr_lossy_without_tx": metrics.compression_ratio(n_lossy, lossy_data),
            "snr_db": metrics.snr_db(encoded.samples, _dec.samples),
            "rmse": metrics.rmse(encoded.samples, _dec.samples),
        }
        rows.append(row)
        print(rec, f"PRD={row['prd_overall']:.2f} CR={row['cr_full']:.2f}")
    with (OUT / "old_path_baseline.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return rows


def write_checkpoint(frozen, new_rows, old_rows):
    # new_rows is per-record at frozen T,a
    path = OUT / "checkpoint1_per_record.csv"
    fields = ["record", "path", "prd_overall", "prd_nonqrs", "prd_qrs",
              "prd_baseline", "cr_full", "cr_without_tx", "cr_lossy_full",
              "cr_lossy_without_tx", "snr_db", "rmse"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in new_rows:
            w.writerow({**{k: r.get(k) for k in fields}, "path": "bior4.4"})
        for r in old_rows:
            w.writerow({**{k: r.get(k) for k in fields}, "path": "haar"})
    summary = {
        "frozen": frozen,
        "word_lengths": {
            "datapath": "s25 Q19.6",
            "lift_const": "s18 Q2.16",
            "inv_delta": "u16 Q0.16",
            "rice_k": "0..15",
            "rice_escape": "Q_ESC=31, ESC_BITS=32",
        },
        "gate": "whole-record strict PRD <= 2 at CR_full >= 5",
        "new_mean_prd": float(np.mean([r["prd_overall"] for r in new_rows])),
        "new_worst_prd": max(r["prd_overall"] for r in new_rows),
        "new_mean_cr": float(np.mean([r["cr_full"] for r in new_rows])),
        "new_min_cr": min(r["cr_full"] for r in new_rows),
        "old_mean_prd": float(np.mean([r["prd_overall"] for r in old_rows])),
        "old_worst_prd": max(r["prd_overall"] for r in old_rows),
        "old_mean_cr": float(np.mean([r["cr_full"] for r in old_rows])),
        "gate_met": bool(frozen.get("gate_met")) and min(
            r["cr_full"] for r in new_rows) >= 5.0
        and max(r["prd_overall"] for r in new_rows) <= 2.0,
    }
    (OUT / "checkpoint1_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--stage", default="all",
                   choices=["hist", "verify", "sweep", "baseline", "all"])
    p.add_argument("--quick", action="store_true",
                   help="3 records, coarse T grid (debug)")
    args = p.parse_args()
    recs = ("100", "117", "233") if args.quick else None
    t_grid = [2, 4, 8, 16] if args.quick else [
        0.5, 1, 1.5, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24]
    a_grid = [1.4] if args.quick else [1.20, 1.40, 1.60, 1.80]

    if args.stage in ("hist", "all"):
        extract_lossy_lengths()
    if args.stage in ("verify", "all"):
        for rec in (recs or ("100", "117")):
            _verify_record(rec)
    if args.stage in ("sweep", "all"):
        rows, cache = sweep(t_grid, a_grid, records=recs)
        frozen = choose_frozen(rows)
        (OUT / "frozen.json").write_text(json.dumps(frozen, indent=2), encoding="utf-8")
        print("FROZEN", frozen)
        qp = lossy_bw.make_quant_params(frozen["t_adu"], frozen["a"])
        rice = retrain_rice(cache, qp)
        # re-score at frozen T,a (defaults; rice table stored for CR re-report)
        config.BW_RICE_K_RUN = int(np.median([v[0] for v in rice.values()])) if rice else 2
        new_rows = []
        for rec, pack in cache.items():
            samples, segs, _d, mask, analyzed = pack
            st = _score_analyzed(rec, samples, segs, mask, analyzed, qp,
                                 config.BW_BND_MARGIN, "b", config.BW_L_MAX)
            st.pop("rice_stats", None)
            new_rows.append(st)
        with (OUT / "new_path_frozen.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=[k for k in new_rows[0] if k != "rice_stats"])
            w.writeheader()
            w.writerows(new_rows)
        old = old_baseline(records=recs)
        write_checkpoint(frozen, new_rows, old)
    elif args.stage == "baseline":
        old_baseline(records=recs)


if __name__ == "__main__":
    main()
