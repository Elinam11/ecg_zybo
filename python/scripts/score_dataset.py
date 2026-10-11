"""Beat/rhythm histograms (5-min window) and MIT-BIH Directory age/sex.

Writes:
    paper/dataset_beats.csv
    paper/dataset_rhythms.csv
    paper/dataset_demographics.csv

Usage:
    python python/scripts/score_dataset.py
"""

from __future__ import annotations

import csv
import re
import sys
from collections import Counter

import _bootstrap  # noqa: F401
import wfdb

from ecg_ref import config, preprocess

OUT_DIR = config.REPO_ROOT / "paper"
HEADINGS = OUT_DIR / "mitbih_directory_headings.txt"

# AAMI / MIT-BIH beat labels used in this window (plus non-beat markers).
BEAT_GROUPS = {
    "N": "normal",
    "L": "LBBB",
    "R": "RBBB",
    "B": "bundle",
    "A": "APC",
    "a": "aberrated_APC",
    "J": "junctional_premature",
    "S": "SV_premature",
    "V": "PVC",
    "r": "ventricular",
    "F": "fusion_ventricular",
    "e": "atrial_escape",
    "j": "junctional_escape",
    "n": "supraventricular_escape",
    "E": "ventricular_escape",
    "/": "paced",
    "f": "fusion_paced",
    "Q": "unclassifiable",
    "?": "unclassifiable",
    "x": "blocked_APC",
    "!": "flutter_wave",
}

RANDOM_SERIES = tuple(
    r for r in config.MLII_RECORDS if int(r) < 200
)
SELECTED_SERIES = tuple(
    r for r in config.MLII_RECORDS if int(r) >= 200
)


def read_full_ann(record: str):
    start = config.RECORD_START
    end = start + config.N_SAMPLES
    return wfdb.rdann(preprocess.record_path(record), "atr",
                      sampfrom=start, sampto=end)


def beat_counts(record: str) -> Counter:
    """Count morphology labels only (not rhythm '+' or quality '~' / '|')."""
    ann = read_full_ann(record)
    return Counter(sym for sym in ann.symbol if sym in BEAT_GROUPS)


def rhythm_durations(record: str) -> Counter:
    """Samples spent in each rhythm label inside the analysis window."""
    ann = read_full_ann(record)
    start = config.RECORD_START
    end = start + config.N_SAMPLES
    events: list[tuple[int, str]] = []
    for samp, sym, aux in zip(ann.sample, ann.symbol, ann.aux_note):
        note = (aux or "").strip().strip("\x00")
        if not note.startswith("("):
            continue
        events.append((int(samp) - start, note))
    if not events:
        return Counter({"(unknown)": config.N_SAMPLES})
    # A rhythm that began before sampfrom may be missing; first event then
    # only covers the suffix. Assign the prefix to the first seen label
    # if it starts after 0 — labelled unknown_prefix.
    events.sort()
    durations: Counter = Counter()
    if events[0][0] > 0:
        durations["(unknown_prefix)"] += events[0][0]
    for (t0, lab), (t1, _) in zip(events, events[1:] + [(config.N_SAMPLES, "")]):
        durations[lab] += max(0, t1 - t0)
    return durations


def parse_directory_headings(text: str) -> dict[str, dict]:
    """Age/sex/leads from transcribed PhysioNet Directory headings."""
    pat = re.compile(
        r"Record\s+(\d{3})\s+\(([^;]+);\s*(male|female),\s*age\s+(.+?)\)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    out: dict[str, dict] = {}
    for rec, leads, sex, age_s in pat.findall(text):
        age_s = age_s.strip().lower()
        age = "" if "not recorded" in age_s else int(age_s)
        out[rec] = {
            "record": rec,
            "leads": re.sub(r"\s+", " ", leads.strip()),
            "sex": sex.lower(),
            "age": age,
            "cohort": "selected" if int(rec) >= 200 else "random",
        }
    return out


def main() -> int:
    missing = [r for r in config.MLII_RECORDS
               if not (config.MITDB_DIR / f"{r}.hea").exists()]
    if missing:
        print("MIT-BIH files missing:", ", ".join(missing), file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    symbols = sorted(BEAT_GROUPS)
    beat_path = OUT_DIR / "dataset_beats.csv"
    with beat_path.open("w", newline="", encoding="ascii") as fh:
        writer = csv.DictWriter(fh, fieldnames=["record", "cohort", "total"] + symbols)
        writer.writeheader()
        for rec in config.MLII_RECORDS:
            c = beat_counts(rec)
            row = {"record": rec,
                   "cohort": "selected" if int(rec) >= 200 else "random",
                   "total": sum(c.values())}
            for s in symbols:
                row[s] = c.get(s, 0)
            writer.writerow(row)
    print(f"wrote {beat_path}")

    all_rhythms: set[str] = set()
    per_rec_rhythm: dict[str, Counter] = {}
    for rec in config.MLII_RECORDS:
        d = rhythm_durations(rec)
        per_rec_rhythm[rec] = d
        all_rhythms.update(d)
    rhythm_cols = sorted(all_rhythms)
    rhythm_path = OUT_DIR / "dataset_rhythms.csv"
    with rhythm_path.open("w", newline="", encoding="ascii") as fh:
        writer = csv.DictWriter(fh, fieldnames=["record", "cohort"] + rhythm_cols)
        writer.writeheader()
        for rec in config.MLII_RECORDS:
            d = per_rec_rhythm[rec]
            row = {"record": rec,
                   "cohort": "selected" if int(rec) >= 200 else "random"}
            for lab in rhythm_cols:
                row[lab] = d.get(lab, 0)
            writer.writerow(row)
    print(f"wrote {rhythm_path}")

    demo = parse_directory_headings(HEADINGS.read_text(encoding="utf-8"))
    if len(demo) != len(config.ALL_MITDB_RECORDS):
        print(f"heading parse: {len(demo)} records, expected "
              f"{len(config.ALL_MITDB_RECORDS)}", file=sys.stderr)
        return 1

    demo_path = OUT_DIR / "dataset_demographics.csv"
    fields = ["record", "cohort", "sex", "age", "leads", "in_evaluation"]
    with demo_path.open("w", newline="", encoding="ascii") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for rec in config.ALL_MITDB_RECORDS:
            if rec not in demo:
                writer.writerow({
                    "record": rec,
                    "cohort": "selected" if int(rec) >= 200 else "random",
                    "sex": "",
                    "age": "",
                    "leads": "",
                    "in_evaluation": rec in config.MLII_RECORDS,
                })
                continue
            row = dict(demo[rec])
            row["in_evaluation"] = rec in config.MLII_RECORDS
            writer.writerow(row)
    print(f"wrote {demo_path}")
    print(f"directory rows: {len(demo)}; "
          f"MLII matched: {sum(1 for r in config.MLII_RECORDS if r in demo)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
