"""Score saved hardware captures offline and write a summary table.

Use this after capturing frames from the board with serial_rx.py --save:

    python testing/serial_rx.py --port COM15 --record 101 --save captures/capture_101.bin

That also writes ``capture_101.meta.json`` with the measured PL latency so
``score_captures.py`` can include it in the CSV.

Then score every file in the folder (no board needed):

    python testing/score_captures.py captures
    python testing/score_captures.py captures --csv results/hw_matrix.csv

Each input file may be either:

    - a raw compressed frame (what --save writes: starts with the frame magic word), or
    - a full transport stream (sync word + length + latency + frame), as received live.

Record ids are taken from the filename: ``capture_101.bin`` -> record ``101``.
Override with ``--record 101`` when scoring a single file whose name does not match.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import sys
from pathlib import Path

import _bootstrap  # noqa: F401

from ecg_ref import config

TESTING_DIR = Path(__file__).resolve().parent
RECORD_RE = re.compile(r"(?:capture[_-])?(\d+)\.bin$", re.IGNORECASE)


def _load_serial_rx():
    spec = importlib.util.spec_from_file_location(
        "serial_rx", TESTING_DIR / "serial_rx.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record_from_name(path: Path) -> str | None:
    match = RECORD_RE.search(path.name)
    return match.group(1) if match else None


def meta_path_for(bin_path: Path) -> Path:
    return bin_path.with_name(f"{bin_path.stem}.meta.json")


def load_sidecar(bin_path: Path) -> dict:
    """Latency written by serial_rx.py --save lives in a sibling .meta.json file."""
    sidecar = meta_path_for(bin_path)
    if not sidecar.is_file():
        return {}
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"_error": f"invalid sidecar: {exc}"}


def load_capture(path: Path, serial_rx) -> tuple[bytes, int]:
    """Return (frame_bytes, latency_cycles)."""
    data = path.read_bytes()
    if len(data) < 4:
        raise serial_rx.FrameError("file too short")

    sync = config.UART_SYNC_MAGIC.to_bytes(4, "big")
    magic = config.MAGIC_WORD.to_bytes(4, "big")

    if data[:4] == sync:
        read_exact = serial_rx.reader_from_bytes(data)
        return serial_rx.read_transport_frame(read_exact)

    if data[:4] == magic:
        sidecar = load_sidecar(path)
        latency = int(sidecar.get("latency_cycles") or 0)
        return data, latency

    raise serial_rx.FrameError(
        f"unrecognised format (first word 0x{int.from_bytes(data[:4], 'big'):08X}); "
        "expected transport sync or frame magic")


def score_file(path: Path, record: str | None, serial_rx) -> dict:
    row: dict = {
        "record": record or "",
        "file": path.name,
        "status": "ok",
        "error": "",
    }
    try:
        frame, latency = load_capture(path, serial_rx)
        row["latency_cycles"] = latency
        row["frame_bytes"] = len(frame)

        if not record:
            raise ValueError(
                f"cannot infer record id from {path.name}; "
                "rename to capture_<id>.bin or pass --record")

        result = serial_rx.evaluate(frame, record)
        beats = result["qrs_count"]
        row.update({
            "sample_count": result["sample_count"],
            "beats": beats,
            "qrs_count": beats,
            "segments": result["segments"],
            "checksum_ok": result["checksum_ok"],
            "cr": result["cr"],
            "prd_baseline": result.get("prd_baseline"),
            "prd_strict": result.get("prd_strict"),
            "prd_qrs": result.get("prd_qrs"),
            "matches_golden": result.get("matches_golden"),
            "first_divergence": result.get("first_divergence", ""),
            "passed": result.get("passed"),
        })
        ok = (
            result["checksum_ok"]
            and result.get("passed", True)
            and result.get("matches_golden", True)
        )
        row["result"] = "PASS" if ok else "FAIL"
    except Exception as exc:  # noqa: BLE001
        row["status"] = "error"
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["result"] = "ERROR"

    latency = row.get("latency_cycles") or 0
    if latency:
        seconds = latency / config.CLK_FREQ_HZ
        samples = row.get("sample_count") or config.N_SAMPLES
        row["latency_ms"] = round(seconds * 1e3, 3)
        row["realtime_factor"] = round((samples / config.FS) / seconds, 1) if seconds else ""
    else:
        row["latency_ms"] = ""
        row["realtime_factor"] = ""

    return row


def write_csv(rows: list[dict], path: Path) -> None:
    fields = [
        "record", "beats", "latency_cycles", "latency_ms", "realtime_factor",
        "result", "status", "error", "file",
        "frame_bytes", "checksum_ok", "matches_golden", "first_divergence",
        "cr", "prd_baseline", "prd_strict", "prd_qrs",
        "segments", "sample_count",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def print_table(rows: list[dict]) -> None:
    print(f"{'Record':>6}  {'Beats':>5}  {'Latency ms':>10}  {'Result':<5}  "
          f"{'CR':>5}  {'PRD base':>9}  {'Golden':<14}  File")
    print("-" * 88)
    for row in rows:
        if row["status"] != "ok":
            print(f"{row['record']:>6}  {'':>5}  {'':>10}  {'ERROR':<5}  "
                  f"{'':>5}  {'':>9}  {'':<14}  {row['file']}  ({row['error']})")
            continue
        golden = "BIT-IDENTICAL" if row.get("matches_golden") else "DIFFERS"
        lat = row.get("latency_ms")
        lat_s = f"{lat:10.3f}" if lat != "" else f"{'—':>10}"
        print(f"{row['record']:>6}  {row.get('beats', ''):>5}  {lat_s}  "
              f"{row['result']:<5}  {row['cr']:5.2f}  "
              f"{row['prd_baseline']:9.4f}  {golden:<14}  {row['file']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "captures",
        nargs="?",
        default="captures",
        help="folder of capture_*.bin files (default: captures)",
    )
    parser.add_argument(
        "--csv",
        default="results/hw_matrix.csv",
        help="write results here (default: results/hw_matrix.csv)",
    )
    parser.add_argument(
        "--record",
        default=None,
        help="record id when scoring a single file with a non-standard name",
    )
    parser.add_argument(
        "--no-csv",
        action="store_true",
        help="print the table only; do not write a CSV file",
    )
    args = parser.parse_args()

    serial_rx = _load_serial_rx()
    capture_path = Path(args.captures)

    if capture_path.is_file():
        files = [capture_path]
    elif capture_path.is_dir():
        files = sorted(capture_path.glob("*.bin"))
        if not files:
            print(f"no .bin files in {capture_path}")
            return 1
    else:
        print(f"not found: {capture_path}")
        return 1

    rows: list[dict] = []
    for path in files:
        record = args.record or record_from_name(path)
        rows.append(score_file(path, record, serial_rx))

    rows.sort(key=lambda r: (r["record"], r["file"]))
    print_table(rows)

    passed = sum(1 for r in rows if r.get("result") == "PASS")
    failed = sum(1 for r in rows if r.get("result") == "FAIL")
    errors = sum(1 for r in rows if r.get("result") == "ERROR")
    print()
    print(f"scored {len(rows)} file(s): {passed} PASS, {failed} FAIL, {errors} ERROR")

    if not args.no_csv:
        csv_path = Path(args.csv)
        write_csv(rows, csv_path)
        print(f"wrote {csv_path}")

    if errors:
        return 1
    if failed:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
