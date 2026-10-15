"""Receive a compressed ECG frame over UART, decode it, and score it.

The decoder is the bior4.4 (method-a, cA DPCM) reference decoder. The hardware
passes only if that same model reconstructs the board's output, and the received
frame is byte-identical to the golden bitstream.

Three sources, all feeding the same parser:

    --port COM5            live board over the PS UART (the PROG micro-USB COM port)
    --file capture.bin     a previously captured byte stream
    --loopback 100         a frame synthesised from the reference model (no hardware)

The loopback mode is the desktop gate for Phase 5: it proves the receiver, the
transport framing and the decoder agree with run_reference.py before any board is
involved.

Usage:
    python python/scripts/serial_rx.py --loopback 100
    python python/scripts/serial_rx.py --port COM5 --record 100 --save capture.bin
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np

from ecg_ref import config, metrics, packer, pipeline_bw, preprocess

PREAMBLE_BYTES = config.UART_PREAMBLE_WORDS * 4


class FrameError(Exception):
    pass


def build_transport_frame(words: list[int], latency_cycles: int = 0) -> bytes:
    """Wrap a compressed frame the way ps/src/main.c does.

    Kept here rather than only in the firmware so the loopback path exercises the exact
    byte layout the board emits.
    """
    preamble = [config.UART_SYNC_MAGIC, len(words), latency_cycles]
    return b"".join(w.to_bytes(4, "big") for w in preamble + words)


def read_transport_frame(read_exact, resync_limit: int = 1 << 16) -> tuple[bytes, int]:
    """Find the sync word, then read one complete frame.

    Returns (frame_bytes, latency_cycles). `read_exact(n)` must return exactly n bytes
    or raise. Resynchronising byte by byte matters on a real link, where the receiver
    is usually started midway through the board's boot chatter.
    """
    sync = config.UART_SYNC_MAGIC.to_bytes(4, "big")
    window = read_exact(4)
    skipped = 0
    while window != sync:
        if skipped >= resync_limit:
            raise FrameError(f"no sync word in the first {resync_limit} bytes")
        window = window[1:] + read_exact(1)
        skipped += 1
    if skipped:
        print(f"  resynchronised after {skipped} stray byte(s)")

    frame_words = int.from_bytes(read_exact(4), "big")
    latency_cycles = int.from_bytes(read_exact(4), "big")
    if not 0 < frame_words <= (1 << 22):
        raise FrameError(f"implausible frame length: {frame_words} words")

    return read_exact(frame_words * 4), latency_cycles


def reader_from_bytes(payload: bytes):
    """read_exact over an in-memory buffer."""
    state = {"pos": 0}

    def read_exact(n: int) -> bytes:
        start = state["pos"]
        chunk = payload[start:start + n]
        if len(chunk) != n:
            raise FrameError(f"stream truncated: wanted {n} bytes at offset {start}")
        state["pos"] = start + n
        return chunk

    return read_exact


def reader_from_serial(port: str, baud: int, timeout: float):
    try:
        import serial  # noqa: PLC0415  (optional dependency; only the board path needs it)
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("pyserial is required for --port; pip install pyserial") from exc

    link = serial.Serial(port, baud, timeout=timeout)
    print(f"listening on {port} at {baud} baud")

    def read_exact(n: int) -> bytes:
        chunk = link.read(n)
        if len(chunk) != n:
            raise FrameError(f"serial timeout: got {len(chunk)} of {n} bytes")
        return chunk

    return read_exact, link


def evaluate(frame: bytes, record: str | None) -> dict:
    """Decode a received frame and, if the record is known, score it."""
    words = [int.from_bytes(frame[i:i + 4], "big")
             for i in range(0, len(frame), 4)]
    header = packer.FrameHeader.from_words(words)
    body = words[config.HEADER_WORDS:]
    checksum_ok = packer.payload_checksum(body) == header.checksum
    rec, qrs_mask = pipeline_bw.decode_from_bytes_bw(
        frame, config.BW_DZ_T_FROZEN, config.BW_DZ_A_FROZEN)
    rec_i = np.rint(rec).astype(np.int64)
    result = {
        "sample_count": header.sample_count,
        "qrs_count": header.qrs_count,
        "fs": header.fs,
        "checksum_ok": checksum_ok,
        "segments": 0,
        "frame_bytes": len(frame),
        "cr": header.sample_count * config.SAMPLE_BITS / max(1, len(frame) * 8),
    }
    if record is None:
        return result

    original = preprocess.preprocess_record(record)
    n = min(len(original), len(rec_i))
    golden = pipeline_bw.encode_record_bw(
        record, config.BW_DZ_T_FROZEN, config.BW_DZ_A_FROZEN)
    evaluation = metrics.evaluate(
        record=record,
        original=original[:n],
        reconstructed=rec_i[:n],
        qrs_mask=qrs_mask[:n],
        compressed_bits=len(frame) * 8,
        qrs_count=header.qrs_count,
        segment_count=len(golden.segments),
        checksum_ok=checksum_ok,
    )
    result.update({
        "segments": len(golden.segments),
        "cr": evaluation.compression_ratio,
        "prd_strict": evaluation.prd_overall,
        "prd_baseline": evaluation.prd_overall_baseline,
        "prd_qrs": evaluation.prd_qrs,
        "passed": evaluation.passed,
    })

    result["matches_golden"] = (frame == golden.payload)
    if not result["matches_golden"]:
        got = [int.from_bytes(frame[i:i + 4], "big") for i in range(0, len(frame), 4)]
        result["first_divergence"] = _first_divergence(got, golden.words)
    return result


def _first_divergence(got: list[int], expected: list[int]) -> str:
    for i in range(min(len(got), len(expected))):
        if got[i] != expected[i]:
            return (f"word {i}: received 0x{got[i]:08X}, "
                    f"golden 0x{expected[i]:08X}")
    return f"length differs: received {len(got)} words, golden {len(expected)}"


def report(result: dict, latency_cycles: int) -> bool:
    print(f"  samples      {result['sample_count']}")
    print(f"  beats        {result['qrs_count']}")
    print(f"  segments     {result['segments']}")
    print(f"  frame        {result['frame_bytes']} bytes")
    print(f"  checksum     {'OK' if result['checksum_ok'] else 'MISMATCH'}")
    print(f"  CR           {result['cr']:.2f}")
    if "prd_qrs" in result:
        print(f"  PRD (base)   {result['prd_baseline']:.4f}%")
        print(f"  PRD (strict) {result['prd_strict']:.3f}%")
        print(f"  PRD QRS      {result['prd_qrs']:.3f}%")
    if latency_cycles:
        seconds = latency_cycles / config.CLK_FREQ_HZ
        signal = result["sample_count"] / config.FS
        print(f"  latency      {latency_cycles} PL cycles "
              f"({seconds * 1e3:.2f} ms, {signal / seconds:.0f}x real time)")
    if "matches_golden" in result:
        print(f"  vs golden    "
              f"{'BIT-IDENTICAL' if result['matches_golden'] else 'DIFFERS'}")
        if not result["matches_golden"]:
            print(f"               {result['first_divergence']}")

    ok = result["checksum_ok"] and result.get("passed", True) \
        and result.get("matches_golden", True)
    print(f"  result       {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--port", help="serial port the board is attached to")
    source.add_argument("--file", help="previously captured byte stream")
    source.add_argument("--loopback", metavar="RECORD",
                        help="synthesise the frame from the reference model")
    parser.add_argument("--baud", type=int, default=config.UART_BAUD)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--record", default=None,
                        help="record the board is running, so CR/PRD can be scored")
    parser.add_argument("--save", default=None, help="write the raw frame to this file")
    args = parser.parse_args()

    record = args.record
    link = None

    if args.loopback:
        record = record or args.loopback
        encoded = pipeline_bw.encode_record_bw(
            args.loopback, config.BW_DZ_T_FROZEN, config.BW_DZ_A_FROZEN)
        stream = build_transport_frame(encoded.words, latency_cycles=0)
        print(f"loopback: record {args.loopback}, "
              f"{len(encoded.words)} words synthesised")
        read_exact = reader_from_bytes(stream)
    elif args.file:
        with open(args.file, "rb") as handle:
            read_exact = reader_from_bytes(handle.read())
        print(f"replaying {args.file}")
    else:
        read_exact, link = reader_from_serial(args.port, args.baud, args.timeout)

    t0 = time.time()
    try:
        frame, latency_cycles = read_transport_frame(read_exact)
    except FrameError as exc:
        print(f"receive failed: {exc}")
        return 1
    finally:
        if link is not None:
            link.close()
    print(f"received {len(frame)} bytes in {time.time() - t0:.1f}s")

    if args.save:
        save_path = Path(args.save)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with save_path.open("wb") as handle:
            handle.write(frame)
        meta = {"latency_cycles": latency_cycles}
        if record:
            meta["record"] = record
        meta_path = save_path.with_name(f"{save_path.stem}.meta.json")
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {save_path}")
        print(f"wrote {meta_path}")

    try:
        result = evaluate(frame, record)
    except Exception as exc:  # noqa: BLE001
        print(f"decode failed: {type(exc).__name__}: {exc}")
        return 1

    return 0 if report(result, latency_cycles) else 2


if __name__ == "__main__":
    sys.exit(main())
