"""Fetch MIT-BIH Arrhythmia Database records (signal + header + annotations).

Usage:
    python python/scripts/download_mitdb.py            # all MLII-bearing records
    python python/scripts/download_mitdb.py 100 101    # selected records
"""

from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401  (sys.path side effect)
import wfdb

from ecg_ref import config


def download(records: list[str]) -> int:
    config.MITDB_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    for rec in records:
        target = config.MITDB_DIR / f"{rec}.hea"
        if target.exists():
            print(f"[skip] {rec} already present")
            continue
        try:
            wfdb.dl_database("mitdb", str(config.MITDB_DIR), records=[rec],
                             annotators=["atr"], overwrite=False)
            print(f"[ok]   {rec}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[FAIL] {rec}: {type(exc).__name__}: {exc}")
    return failures


def report_leads(records: list[str]) -> None:
    """Print the lead layout of each downloaded record and flag missing MLII."""
    print("\nLead inventory:")
    missing = []
    for rec in records:
        hea = config.MITDB_DIR / f"{rec}.hea"
        if not hea.exists():
            continue
        header = wfdb.rdheader(str(config.MITDB_DIR / rec))
        has_mlii = config.TARGET_LEAD in header.sig_name
        if not has_mlii:
            missing.append(rec)
        print(f"  {rec}: fs={header.fs} sig={header.sig_name} "
              f"len={header.sig_len} MLII={'yes' if has_mlii else 'NO'}")
    if missing:
        print(f"\nRecords without {config.TARGET_LEAD}: {missing}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", nargs="*", default=None,
                        help="record IDs (default: every MLII-bearing record)")
    parser.add_argument("--all", action="store_true",
                        help="download all 48 records including the non-MLII ones")
    parser.add_argument("--leads", action="store_true",
                        help="print the lead inventory after downloading")
    args = parser.parse_args()

    if args.records:
        records = args.records
    elif args.all:
        records = list(config.ALL_MITDB_RECORDS)
    else:
        records = list(config.MLII_RECORDS)

    print(f"Downloading {len(records)} record(s) into {config.MITDB_DIR}")
    failures = download(records)
    if args.leads:
        report_leads(records)
    if failures:
        print(f"\n{failures} record(s) failed to download")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
