"""Generate BRAM initialisation files (.coe/.dat) for every record.

Dataset swapping (spec 2.1) works by regenerating these files and re-running the
Vivado BRAM initialisation; no HLS or RTL source changes and no re-synthesis of the
top-level code are required.

Usage:
    python python/scripts/gen_coe.py            # every downloaded record
    python python/scripts/gen_coe.py 100 101
"""

from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401
import numpy as np

from ecg_ref import config, preprocess


def generate(record: str, verify: bool = True) -> dict:
    samples = preprocess.preprocess_record(record)

    coe_path = config.COE_DIR / f"{record}.coe"
    coe32_path = config.COE_DIR / f"{record}_x32.coe"
    dat_path = config.COE_DIR / f"{record}.dat"
    bin_path = config.COE_DIR / f"{record}.bin"
    preprocess.write_coe(samples, coe_path)
    preprocess.write_coe32(samples, coe32_path)
    preprocess.write_bin32(samples, bin_path)
    preprocess.write_dat(samples, dat_path)

    if verify:
        # Round-trip check: what Vivado will load must equal what Python compressed.
        if not np.array_equal(preprocess.read_coe(coe_path), samples):
            raise AssertionError(f"{record}: .coe round-trip mismatch")
        if not np.array_equal(preprocess.read_coe32(coe32_path)[:len(samples)], samples):
            raise AssertionError(f"{record}: 32-bit .coe round-trip mismatch")
        if not np.array_equal(preprocess.read_bin32(bin_path), samples):
            raise AssertionError(f"{record}: .bin round-trip mismatch")
        if not np.array_equal(preprocess.read_dat(dat_path), samples):
            raise AssertionError(f"{record}: .dat round-trip mismatch")

    return {
        "record": record,
        "samples": len(samples),
        "bits": len(samples) * config.SAMPLE_BITS,
        "peak": int(np.abs(samples).max()),
        "rms": float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", nargs="*", default=None)
    parser.add_argument("--no-verify", action="store_true")
    args = parser.parse_args()

    records = args.records or [r for r in config.MLII_RECORDS
                               if (config.MITDB_DIR / f"{r}.hea").exists()]
    if not records:
        print("no records found; run download_mitdb.py first")
        return 1

    config.COE_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    for record in records:
        try:
            info = generate(record, verify=not args.no_verify)
            print(f"  {info['record']}: {info['samples']} samples "
                  f"({info['bits'] / 1024:.0f} Kb) peak={info['peak']} "
                  f"rms={info['rms']:.1f}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  {record}: FAILED {type(exc).__name__}: {exc}")

    usage = config.N_SAMPLES * config.SAMPLE_BITS
    print(f"\nBRAM per record: {usage} bits "
          f"({100.0 * usage / config.BRAM_TOTAL_BITS:.1f}% of the "
          f"{config.BRAM_TOTAL_BITS // 1024} Kb device total)")
    print(f"wrote {len(records) - failures} file pair(s) to {config.COE_DIR}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
