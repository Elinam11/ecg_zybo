"""Compute the on-chip memory and latency budget for the Zybo Z7-10 implementation.

Block RAM is the binding resource on the XC7Z010: the 108,000-sample store alone eats
most of it. This script derives the block count from config.py rather than quoting a
number in prose, so the budget cannot silently go stale when a constant changes.

Everything here is an analytical bound. LUT/FF/DSP figures come from `csynth_design`
and are checked in Phase 3; this script exists to catch an over-budget design before
spending an hour in synthesis to learn the same thing.

Usage:
    python python/scripts/resource_budget.py
    python python/scripts/resource_budget.py --report resource_budget.md
"""

from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401

from ecg_ref import config

# 7-series block RAM geometry. A BRAM36 holds 36,864 bits and can be configured with a
# maximum word width of 36 bits (72 in a single-port cascade, which HLS does not use for
# these arrays). Depth beyond the configured width's limit costs another block.
BRAM36_BITS = 36 * 1024
BRAM36_MAX_WIDTH = 36
DEVICE_BRAM36 = 60          # XC7Z010
DEVICE_LUT = 17_600
DEVICE_FF = 35_200
DEVICE_DSP = 80

# Distributed RAM: one 6-input LUT provides 64 bits of single-port RAM.
LUTRAM_BITS_PER_LUT = 64


def bram36_blocks(depth: int, width: int) -> int:
    """Blocks needed for a `depth` x `width` memory, honouring the 36-bit port limit.

    Wide words are split across parallel blocks; deep arrays are stacked. This is the
    same tiling the Vivado memory generator performs.
    """
    if depth <= 0 or width <= 0:
        return 0
    lanes = -(-width // BRAM36_MAX_WIDTH)           # parallel blocks for the width
    per_lane_width = -(-width // lanes)
    words_per_block = BRAM36_BITS // per_lane_width
    # 7-series ports address a power-of-two depth, so round the usable depth down.
    words_per_block = 1 << (words_per_block.bit_length() - 1)
    return lanes * -(-depth // words_per_block)


def lutram_luts(depth: int, width: int) -> int:
    return -(-(depth * width) // LUTRAM_BITS_PER_LUT)


def memory_budget() -> list[dict]:
    """Every memory in the system, with where it physically lives."""
    coeff_width = 32        # ecg_coeff_t
    index_width = 32        # ecg_index_t
    token_width = 32 + 8 + 8

    entries = [
        {
            "name": "sample store (PL BRAM, .coe initialised)",
            "where": "block design",
            "depth": config.N_SAMPLES,
            "width": config.SAMPLE_BITS,
            "impl": "bram",
        },
        {
            "name": "Haar coefficient buffer",
            "where": "ecg_top",
            "depth": config.MAX_LOSSY_SEGMENT,
            "width": coeff_width,
            "impl": "bram",
        },
        {
            "name": "Haar lifting scratch",
            "where": "ecg_top",
            "depth": config.MAX_LOSSY_SEGMENT,
            "width": coeff_width,
            "impl": "bram",
        },
        {
            "name": "R-peak index array",
            "where": "ecg_top",
            "depth": (config.N_SAMPLES // config.REFRACTORY) + 2,
            "width": index_width,
            "impl": "bram",
        },
        {
            "name": "coder -> packer token FIFO",
            "where": "module TBs only (ecg_top packs inline)",
            "depth": config.TOKEN_FIFO_DEPTH,
            "width": token_width,
            "impl": "srl",
        },
        {
            "name": "moving-average window",
            "where": "detect",
            "depth": config.MA_LEN,
            "width": 64,
            "impl": "lutram",
        },
    ]

    for e in entries:
        e["bits"] = e["depth"] * e["width"]
        e["bram36"] = bram36_blocks(e["depth"], e["width"]) if e["impl"] == "bram" else 0
        e["luts"] = 0 if e["impl"] == "bram" else lutram_luts(e["depth"], e["width"])
    return entries


def latency_estimate() -> dict:
    """Cycle-count bound for one full record, from the loop structure of ecg_top.

    Per-region costs are read off the source: each loop is either II=1 pipelined or a
    fixed number of passes over its data. The numbers are upper bounds, so the real
    csynth latency should come in under them.
    """
    n = config.N_SAMPLES
    beats = 400                      # a busy 5-minute record; 233 is the worst at 514
    qrs_samples = beats * config.QRS_WINDOW
    nonqrs_samples = n - qrs_samples

    # Detection: one II=1 pass over the record, plus an apex search per trigger.
    detect = n + beats * (config.PEAK_SEARCH_BACK + config.PEAK_SEARCH_FWD)

    # Lossless: two passes over each window (k selection, then coding), plus a
    # sequential divide per window for the Rice parameter.
    lossless = 2 * qrs_samples + beats * 40

    # Lossy: load + 4 transform levels (each a pass and a copy-back over a halving
    # length, so < 4x the segment) + quantise + count + emit == about 8 passes.
    lossy = 8 * nonqrs_samples

    total = detect + lossless + lossy
    seconds = total / config.CLK_FREQ_HZ
    signal_seconds = n / config.FS
    return {
        "detect": detect,
        "lossless": lossless,
        "lossy": lossy,
        "total": total,
        "seconds": seconds,
        "signal_seconds": signal_seconds,
        "realtime_factor": signal_seconds / seconds,
        "per_sample_cycles": total / n,
        "cycles_available_per_sample": config.CLK_FREQ_HZ / config.FS,
    }


def render(entries: list[dict], latency: dict) -> str:
    hls_bram = sum(e["bram36"] for e in entries if e["where"] != "block design")
    bd_bram = sum(e["bram36"] for e in entries if e["where"] == "block design")
    total_bram = hls_bram + bd_bram
    total_luts = sum(e["luts"] for e in entries)

    lines = [
        "# Resource and Latency Budget (XC7Z010-1CLG400C)",
        "",
        "Generated by `python/scripts/resource_budget.py`. Block RAM and latency are",
        "analytical bounds derived from `config.py`; LUT/FF/DSP totals come from",
        "`csynth_design` in Phase 3.",
        "",
        "## On-chip memory",
        "",
        "| Memory | Lives in | Depth | Width | Bits | BRAM36 | LUTs |",
        "|---|---|---|---|---|---|---|",
    ]
    for e in entries:
        lines.append(
            f"| {e['name']} | {e['where']} | {e['depth']:,} | {e['width']} | "
            f"{e['bits']:,} | {e['bram36']} | {e['luts']} |")

    lines += [
        "",
        f"- block design (sample store): **{bd_bram} / {DEVICE_BRAM36} BRAM36**",
        f"- ecg_top IP: **{hls_bram} / {DEVICE_BRAM36} BRAM36**",
        f"- total: **{total_bram} / {DEVICE_BRAM36} BRAM36** "
        f"({100.0 * total_bram / DEVICE_BRAM36:.0f}%), "
        f"{DEVICE_BRAM36 - total_bram} blocks spare",
        f"- distributed RAM: about **{total_luts} / {DEVICE_LUT} LUTs** "
        f"({100.0 * total_luts / DEVICE_LUT:.1f}%)",
        "",
        "The sample store dominates and is irreducible: 108,000 x 16 bits stored in",
        "18-bit-wide block RAM wastes two bits per word, which is why it costs more",
        "blocks than a naive bit count suggests. Peaks use the spare BRAM36s so their",
        "~750 LUTRAM cost is avoided; the token FIFO stays off-chip / module-TB only.",
        "",
        "## Multipliers",
        "",
        "The data path is deliberately multiplier-free except for the energy operator:",
        "",
        "- IIR bandpass: two shift-based EMAs, no multiply.",
        "- Haar DWT: reversible lifting, adds and a shift only.",
        "- Golomb-Rice and RLE: shifts, compares and a single divide per QRS window.",
        f"- TKEO: two products per sample, so a handful of the {DEVICE_DSP} DSP48s.",
        "",
        "## Latency for one 108,000-sample record",
        "",
        "| Stage | Cycles (bound) |",
        "|---|---|",
        f"| detection pass | {latency['detect']:,} |",
        f"| lossless QRS coding | {latency['lossless']:,} |",
        f"| lossy non-QRS coding | {latency['lossy']:,} |",
        f"| **total** | **{latency['total']:,}** |",
        "",
        f"At {config.CLK_FREQ_HZ / 1e6:.0f} MHz that is "
        f"{latency['seconds'] * 1e3:.1f} ms to compress "
        f"{latency['signal_seconds']:.0f} s of signal, i.e. about "
        f"**{latency['realtime_factor']:.0f}x real time**.",
        "",
        f"Streaming-mode headroom: the core needs about "
        f"{latency['per_sample_cycles']:.1f} cycles per sample against the "
        f"{latency['cycles_available_per_sample']:,.0f} cycles available between",
        "samples at 360 Hz, so a live-input version of the same pipeline would idle",
        "almost all the time.",
        "",
        "## Gate",
        "",
        f"- BRAM within budget: {'PASS' if total_bram <= DEVICE_BRAM36 else 'FAIL'}",
        f"- real time: {'PASS' if latency['realtime_factor'] > 1 else 'FAIL'}",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=None,
                        help="write the markdown table to docs/<name>")
    args = parser.parse_args()

    entries = memory_budget()
    latency = latency_estimate()
    text = render(entries, latency)
    print(text)

    total_bram = sum(e["bram36"] for e in entries)
    if args.report:
        path = config.REPO_ROOT / "docs" / args.report
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")

    return 0 if total_bram <= DEVICE_BRAM36 else 1


if __name__ == "__main__":
    sys.exit(main())
