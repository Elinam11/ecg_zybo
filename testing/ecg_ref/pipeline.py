"""End-to-end encode/decode pipeline and golden-vector emission.

`encode_record` is the exact algorithm the FPGA implements; `decode_bitstream` is the
host-side reconstruction. Together they define the contract every later phase is
verified against.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import config, detect, lossless, lossy, metrics, packer, preprocess


@dataclass
class EncodeResult:
    samples: np.ndarray                 # preprocessed int16 input (BRAM contents)
    payload: bytes                      # complete frame, header included
    words: list[int]
    segments: list[detect.Segment]
    detection: detect.DetectResult
    qrs_mask: np.ndarray
    escapes: int = 0
    saturated: int = 0
    qrs_payloads: list[lossless.QrsPayload] = field(default_factory=list)
    nonqrs_payloads: list[lossy.NonQrsPayload] = field(default_factory=list)

    @property
    def bit_count(self) -> int:
        return len(self.payload) * 8

    @property
    def qrs_count(self) -> int:
        return sum(1 for s in self.segments if s.kind == "qrs")


def encode_samples(samples: np.ndarray) -> EncodeResult:
    """Compress a preprocessed int16 record into a complete frame."""
    segments, detection = detect.segment_record(samples)

    body = packer.BitWriter()
    qrs_payloads: list[lossless.QrsPayload] = []
    nonqrs_payloads: list[lossy.NonQrsPayload] = []
    qrs_mask = np.zeros(len(samples), dtype=bool)
    escapes = 0
    saturated = 0

    for segment in segments:
        block = samples[segment.start:segment.end + 1]
        if segment.kind == "qrs":
            payload = lossless.encode_qrs(block)
            packer.write_qrs_packet(body, payload)
            qrs_payloads.append(payload)
            escapes += payload.escapes
            qrs_mask[segment.start:segment.end + 1] = True
        else:
            payload = lossy.encode_nonqrs(block)
            packer.write_nonqrs_packet(body, payload)
            nonqrs_payloads.append(payload)
            saturated += payload.saturated

    body_words = body.to_words()
    header = packer.FrameHeader(
        sample_count=len(samples),
        qrs_count=sum(1 for s in segments if s.kind == "qrs"),
        checksum=packer.payload_checksum(body_words),
    )
    words = header.to_words() + body_words
    payload_bytes = b"".join(w.to_bytes(4, "big") for w in words)

    return EncodeResult(samples=samples, payload=payload_bytes, words=words,
                        segments=segments, detection=detection, qrs_mask=qrs_mask,
                        escapes=escapes, saturated=saturated,
                        qrs_payloads=qrs_payloads, nonqrs_payloads=nonqrs_payloads)


def encode_record(record: str, n_samples: int | None = None) -> EncodeResult:
    return encode_samples(preprocess.preprocess_record(record, n_samples))


def decode_bitstream(payload: bytes) -> packer.DecodedFrame:
    return packer.decode_frame(payload)


def evaluate_record(record: str, n_samples: int | None = None,
                    encoded: EncodeResult | None = None) -> tuple[metrics.Evaluation,
                                                                  EncodeResult,
                                                                  packer.DecodedFrame]:
    """Encode, decode, and score one record in the reference model."""
    encoded = encode_record(record, n_samples) if encoded is None else encoded
    decoded = decode_bitstream(encoded.payload)

    if len(decoded.samples) != len(encoded.samples):
        raise AssertionError(f"decoded {len(decoded.samples)} samples, "
                             f"expected {len(encoded.samples)}")

    evaluation = metrics.evaluate(
        record=record,
        original=encoded.samples,
        reconstructed=decoded.samples,
        qrs_mask=decoded.qrs_mask,
        compressed_bits=encoded.bit_count,
        qrs_count=encoded.qrs_count,
        segment_count=len(encoded.segments),
        escapes=encoded.escapes,
        saturated=encoded.saturated,
        checksum_ok=decoded.checksum_ok,
    )
    return evaluation, encoded, decoded


# ---------------------------------------------------------------------------
# Golden vector emission for the HLS testbenches
# ---------------------------------------------------------------------------

def write_golden(record: str, encoded: EncodeResult, out_dir=None) -> "dict[str, str]":
    """Dump every pipeline-internal vector as plain text for the C++ testbenches.

    Plain integers, one per line, so the testbenches need no parsing library and the
    files diff cleanly when something drifts.
    """
    out_dir = (config.GOLDEN_DIR / record) if out_dir is None else out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, str] = {}

    def dump(name: str, values) -> None:
        path = out_dir / name
        path.write_text("\n".join(str(int(v)) for v in values) + "\n",
                        encoding="ascii")
        written[name] = str(path)

    detection = encoded.detection
    dump("input_samples.txt", encoded.samples)
    dump("bandpass.txt", detection.bandpass)
    dump("tkeo.txt", detection.tkeo)
    dump("smooth.txt", detection.smooth)
    dump("threshold.txt", detection.threshold)
    dump("triggers.txt", detection.triggers)
    dump("r_peaks.txt", detection.r_peaks)
    dump("bitstream_words.txt", encoded.words)

    # Segment table: kind(0=nonqrs,1=qrs), start, end
    seg_path = out_dir / "segments.txt"
    seg_path.write_text(
        "\n".join(f"{1 if s.kind == 'qrs' else 0} {s.start} {s.end}"
                  for s in encoded.segments) + "\n", encoding="ascii")
    written["segments.txt"] = str(seg_path)

    # Per-QRS-window coder state, for the lossless module testbench.
    qrs_path = out_dir / "qrs_params.txt"
    qrs_path.write_text(
        "\n".join(f"{p.length} {p.anchor0} {p.anchor1} {p.k} "
                  f"{p.residual_sum} {p.escapes} {p.bit_count}"
                  for p in encoded.qrs_payloads) + "\n", encoding="ascii")
    written["qrs_params.txt"] = str(qrs_path)

    # Per-non-QRS-segment token stream, for the lossy module testbench.
    nq_path = out_dir / "nonqrs_tokens.txt"
    lines = []
    for payload in encoded.nonqrs_payloads:
        packed = " ".join(str(lossy.pack_token(r, v)) for r, v in payload.tokens)
        lines.append(f"{payload.length} {payload.padded_length} "
                     f"{len(payload.tokens)}" + (f" {packed}" if packed else ""))
    nq_path.write_text("\n".join(lines) + "\n", encoding="ascii")
    written["nonqrs_tokens.txt"] = str(nq_path)

    return written
