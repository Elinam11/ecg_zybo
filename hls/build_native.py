"""Compile and run every HLS testbench natively with g++.

Purpose: prove the HLS C++ sources are bit-exact against the golden vectors on any
machine, including ones without the Xilinx toolchain. The module sources are compiled
unmodified; only hls::stream is substituted (see hls/shim/hls_stream_shim.h), and the
arithmetic is plain fixed-width integers with explicit shifts, so the results are
identical to what Vitis HLS C-simulation produces.

This complements rather than replaces `vitis_hls -f hls/tcl/run_all.tcl`, which
additionally runs csynth and cosim against the generated RTL.

Usage:
    python hls/build_native.py                 # record 100
    python hls/build_native.py --records 100 101 208 --clean
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys

HLS_DIR = pathlib.Path(__file__).resolve().parent
REPO = HLS_DIR.parent
SRC = HLS_DIR / "src"
TB = HLS_DIR / "tb"
SHIM = HLS_DIR / "shim"
BUILD = HLS_DIR / "build"

MODULES = {
    "tb_packer": ["bitpack.cpp", "detect.cpp", "lossless.cpp", "lossy.cpp",
                  "ecg_top.cpp"],
    "tb_detect": ["detect.cpp"],
    "tb_lossless": ["bitpack.cpp", "lossless.cpp"],
    "tb_lossy": ["bitpack.cpp", "lossy.cpp"],
    "tb_top": ["bitpack.cpp", "detect.cpp", "lossless.cpp", "lossy.cpp",
               "ecg_top.cpp"],
}

# Testbenches that do not depend on a specific record's golden vectors.
RECORD_INDEPENDENT = {"tb_packer"}

CXXFLAGS = ["-std=c++14", "-O2", "-Wall", "-Wextra",
            "-Wno-unused-parameter",
            # HLS loop labels and pragmas are inert in a native build.
            "-Wno-unknown-pragmas", "-Wno-unused-label",
            "-DECG_NATIVE"]


def compile_tb(name: str, sources: list[str]) -> pathlib.Path:
    BUILD.mkdir(parents=True, exist_ok=True)
    exe = BUILD / (name + (".exe" if os.name == "nt" else ""))
    cmd = ["g++", *CXXFLAGS,
           f"-I{SRC}", f"-I{SHIM}", f"-I{TB}",
           str(TB / f"{name}.cpp"),
           *[str(SRC / s) for s in sources],
           "-o", str(exe)]
    print(f"  compiling {name}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout.strip():
        print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        raise SystemExit(f"compilation of {name} failed")
    if result.stderr.strip():
        print(result.stderr)
    return exe


def run_tb(exe: pathlib.Path, record: str | None) -> int:
    env = dict(os.environ)
    env["ECG_GOLDEN_DIR"] = str(REPO / "data" / "golden")
    env["ECG_COE_DIR"] = str(REPO / "data" / "coe")
    cmd = [str(exe)] + ([record] if record else [])
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    sys.stdout.write(result.stdout)
    if result.stderr.strip():
        sys.stdout.write(result.stderr)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", nargs="*", default=["100"])
    parser.add_argument("--only", nargs="*", default=None,
                        help="run only these testbenches")
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()

    if args.clean and BUILD.exists():
        shutil.rmtree(BUILD)

    names = args.only or list(MODULES)
    print("Building HLS testbenches natively (g++)")
    executables = {}
    for name in names:
        if name not in MODULES:
            print(f"unknown testbench {name}")
            return 1
        executables[name] = compile_tb(name, MODULES[name])

    failures = 0
    print("\nRunning:")
    for name, exe in executables.items():
        if name in RECORD_INDEPENDENT:
            if run_tb(exe, None) != 0:
                failures += 1
            continue
        for record in args.records:
            if run_tb(exe, record) != 0:
                failures += 1

    print()
    if failures:
        print(f"{failures} testbench run(s) FAILED")
        return 1
    print("all testbench runs PASSED (bit-exact against the golden vectors)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
