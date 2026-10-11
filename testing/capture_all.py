"""Capture one hardware frame per MIT-BIH record into captures/.

For each record this script:

    1. runs XSCT: stop, rst -processor, ps7_init, ps7_post_config, memmap
    2. writes data/coe/<record>.bin into BRAM with mwr -bin
    3. downloads ecg_app.elf
    4. opens COM15 (or --port) and starts listening
    5. only then tells XSCT to con
    6. saves captures/capture_<id>.bin and the latency sidecar

Usage (close any other COM-port terminal first):

    python testing/capture_all.py --port COM15

On Windows the script uses ``C:\\Xilinx\\Vitis\\2023.2\\bin\\xsct.bat`` when present;
override with ``--xsct`` if your install path differs.

    python testing/capture_all.py --port COM15 --records 100 101 103
    python testing/capture_all.py --port COM15 --no-program
    python testing/score_captures.py captures
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import _bootstrap  # noqa: F401

from ecg_ref import config

TESTING_DIR = Path(__file__).resolve().parent
REPO = TESTING_DIR.parent
TCL = TESTING_DIR / "run_record.tcl"


def _load_serial_rx():
    spec = importlib.util.spec_from_file_location(
        "serial_rx", TESTING_DIR / "serial_rx.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def available_records(coe_dir: Path) -> list[str]:
    found = []
    for record in config.MLII_RECORDS:
        if (coe_dir / f"{record}.bin").is_file():
            found.append(record)
    return found


def pump_xsct_output(proc: subprocess.Popen) -> threading.Thread:
    def _run() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            print(f"  xsct: {line.rstrip()}")

    thread = threading.Thread(target=_run, name="xsct-log", daemon=True)
    thread.start()
    return thread


def wait_for_prepared(proc: subprocess.Popen, stamp: Path, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stamp.is_file():
            print("  board ready; opening serial port")
            return
        if proc.poll() is not None:
            raise RuntimeError(
                f"xsct exited {proc.returncode} before the board was ready")
        time.sleep(0.1)
    raise TimeoutError(f"xsct did not finish prepare within {timeout:.0f}s")


def save_frame(path: Path, frame: bytes, latency_cycles: int, record: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(frame)
    meta = {"latency_cycles": latency_cycles, "record": record}
    meta_path = path.with_name(f"{path.stem}.meta.json")
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote {path}")
    print(f"  wrote {meta_path}")


def xsct_command(xsct: str, *args: str) -> list[str]:
    """Windows .bat launchers need cmd /c when invoked from Python."""
    if Path(xsct).suffix.lower() in (".bat", ".cmd"):
        return ["cmd", "/c", xsct, *args]
    return [xsct, *args]


def capture_record(record: str, args, serial_rx, program: bool) -> Path:
    save_path = Path(args.out) / f"capture_{record}.bin"
    ready = TESTING_DIR / f".ready_{record}"
    prepared = TESTING_DIR / f".prepared_{record}"
    for stamp in (ready, prepared):
        if stamp.exists():
            stamp.unlink()

    cmd = xsct_command(
        args.xsct, str(TCL),
        "-record", record,
        "-ready", str(ready.resolve()).replace("\\", "/"),
        "-prepared", str(prepared.resolve()).replace("\\", "/"),
        "-coe_dir", str(Path(args.coe_dir).resolve()).replace("\\", "/"),
        "-bit", str(Path(args.bit).resolve()).replace("\\", "/"),
        "-xsa", str(Path(args.xsa).resolve()).replace("\\", "/"),
        "-elf", str(Path(args.elf).resolve()).replace("\\", "/"),
        "-ps7", str(Path(args.ps7).resolve()).replace("\\", "/"),
        "-wait_ms", str(int(args.hw_timeout * 1000)),
    )
    if program:
        cmd.append("-program")

    print(f"\n=== record {record} ===")
    print(f"  starting xsct ({'program PL' if program else 'reuse bitstream'})")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    log_thread = pump_xsct_output(proc)
    link = None
    try:
        wait_for_prepared(proc, prepared, args.hw_timeout)
        read_exact, link = serial_rx.reader_from_serial(
            args.port, config.UART_BAUD, args.timeout)
        ready.write_text("listen\n", encoding="utf-8")
        t0 = time.time()
        frame, latency = serial_rx.read_transport_frame(read_exact)
        print(f"  received {len(frame)} bytes in {time.time() - t0:.1f}s")
        save_frame(save_path, frame, latency, record)
    except Exception:
        if proc.poll() is None:
            proc.terminate()
        raise
    finally:
        if link is not None:
            link.close()
        if proc.poll() is None:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        log_thread.join(timeout=2)
        for stamp in (ready, prepared):
            if stamp.exists():
                stamp.unlink()

    if proc.returncode not in (0, None):
        print(f"  warning: xsct exit {proc.returncode} (frame already saved)")
    return save_path


DEFAULT_XSCT = Path(r"C:\Xilinx\Vitis\2023.2\bin\xsct.bat")


def resolve_xsct(explicit: str | None) -> str:
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return str(path)
        found = shutil.which(explicit)
        if found:
            return found
        return explicit

    if DEFAULT_XSCT.is_file():
        return str(DEFAULT_XSCT)
    found = shutil.which("xsct")
    return found if found else "xsct"


def xsct_available(xsct: str) -> bool:
    path = Path(xsct)
    if path.is_file():
        return True
    return shutil.which(xsct) is not None


def resolve_elf(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    kit = TESTING_DIR / "ecg_app.elf"
    if kit.is_file():
        return kit
    ws = REPO / "ps" / "workspace" / "ecg_app" / "Debug" / "ecg_app.elf"
    return ws


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM15",
                        help="Digilent / PROG COM port (default COM15)")
    parser.add_argument("--records", nargs="*", default=None,
                        help="record ids (default: every MLII .bin in --coe-dir)")
    parser.add_argument("--coe-dir", default=str(REPO / "data" / "coe"))
    parser.add_argument("--out", default="captures",
                        help="folder for capture_<id>.bin (default: captures)")
    parser.add_argument("--xsct", default=None,
                        help="xsct launcher (default: Vitis 2023.2 xsct.bat, else PATH)")
    parser.add_argument("--bit", default=str(TESTING_DIR / "ecg_system_wrapper.bit"))
    parser.add_argument("--xsa", default=str(TESTING_DIR / "ecg_system_wrapper.xsa"))
    parser.add_argument("--elf", default=None)
    parser.add_argument("--ps7", default=str(TESTING_DIR / "ps7_init.tcl"))
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="serial read timeout per record (seconds)")
    parser.add_argument("--hw-timeout", type=float, default=180.0,
                        help="how long to wait for XSCT to finish prepare / con")
    parser.add_argument("--no-program", action="store_true",
                        help="never program the PL (board already configured)")
    parser.add_argument("--program-each", action="store_true",
                        help="re-program the bitstream before every record")
    parser.add_argument("--score", action="store_true",
                        help="run score_captures.py after all captures")
    args = parser.parse_args()

    args.xsct = resolve_xsct(args.xsct)
    if not xsct_available(args.xsct):
        print(f"xsct not found: {args.xsct}")
        print("Pass --xsct C:\\Xilinx\\Vitis\\2023.2\\bin\\xsct.bat")
        return 1

    args.elf = str(resolve_elf(args.elf))
    if not Path(args.elf).is_file():
        print(f"ELF not found: {args.elf}")
        print("Copy ecg_app.elf into testing/ or rebuild with xsct ps/build_ps.tcl")
        return 1

    coe_dir = Path(args.coe_dir)
    records = args.records or available_records(coe_dir)
    if not records:
        print(f"no record .bin files in {coe_dir}")
        return 1
    missing = [r for r in records if not (coe_dir / f"{r}.bin").is_file()]
    if missing:
        print("missing bins: " + ", ".join(missing))
        return 1

    serial_rx = _load_serial_rx()
    print(f"capturing {len(records)} record(s) on {args.port}")
    print(f"  xsct {args.xsct}")
    print(f"  bins {coe_dir}")
    print(f"  elf  {args.elf}")
    print(f"  out  {Path(args.out).resolve()}")

    failures: list[str] = []
    for i, record in enumerate(records):
        program = args.program_each or (not args.no_program and i == 0)
        try:
            capture_record(record, args, serial_rx, program)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED {record}: {type(exc).__name__}: {exc}")
            failures.append(record)

    print()
    print(f"done: {len(records) - len(failures)}/{len(records)} captured")
    if failures:
        print("failed: " + ", ".join(failures))

    if args.score:
        score = TESTING_DIR / "score_captures.py"
        rc = subprocess.call(
            [sys.executable, str(score), args.out], cwd=str(REPO))
        if rc != 0 and not failures:
            return rc

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
