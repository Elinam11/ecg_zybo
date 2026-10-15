"""Encode/decode a record with the bior4.4 lossy path; pass 1 and QRS unchanged."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import config, detect, lossless, lossy_bw, metrics, packer, preprocess, pipeline


@dataclass
class BwEncodeResult:
    samples: np.ndarray
    payload: bytes
    words: list[int]
    segments: list[detect.Segment]
    detection: detect.DetectResult
    qrs_mask: np.ndarray
    bw_payloads: list[lossy_bw.BwPayload] = field(default_factory=list)
    qrs_payloads: list[lossless.QrsPayload] = field(default_factory=list)
    data_bits: int = 0          # tag+length[+count]+entropy, no header/pad
    lossy_full_bits: int = 0    # TAG_NONQRS packets including pad
    lossy_data_bits: int = 0    # TAG_NONQRS tag+length+count+entropy
    n_lossy_samples: int = 0
    t_q6: int = 0
    inv_d_q16: int = 0
    delta_real: float = 0.0
    margin: int = config.BW_BND_MARGIN
    method: str = config.BW_DEFAULT_METHOD

    @property
    def bit_count(self) -> int:
        return len(self.payload) * 8

    @property
    def qrs_count(self) -> int:
        return sum(1 for s in self.segments if s.kind == "qrs")

    @property
    def cr_full(self) -> float:
        return metrics.compression_ratio(len(self.samples), self.bit_count)

    @property
    def cr_without_tx(self) -> float:
        return metrics.compression_ratio(len(self.samples), self.data_bits)

    @property
    def cr_lossy_full(self) -> float:
        return metrics.compression_ratio(self.n_lossy_samples, self.lossy_full_bits)

    @property
    def cr_lossy_without_tx(self) -> float:
        return metrics.compression_ratio(self.n_lossy_samples, self.lossy_data_bits)


def _neighbors(segments: list[detect.Segment], index: int,
               samples: np.ndarray) -> tuple[lossy_bw.EdgeSpec, lossy_bw.EdgeSpec]:
    """Immediate adjacent packet: QRS -> method (b) on that side, else mirror."""
    left = lossy_bw.EdgeSpec("a", np.zeros(0, dtype=np.int64))
    right = lossy_bw.EdgeSpec("a", np.zeros(0, dtype=np.int64))
    if index > 0 and segments[index - 1].kind == "qrs":
        s = segments[index - 1]
        left = lossy_bw.EdgeSpec("b", samples[s.start:s.end + 1].astype(np.int64))
    if index + 1 < len(segments) and segments[index + 1].kind == "qrs":
        s = segments[index + 1]
        right = lossy_bw.EdgeSpec("b", samples[s.start:s.end + 1].astype(np.int64))
    return left, right


def encode_samples_bw(samples: np.ndarray, t_adu: float, a: float,
                      margin: int = config.BW_BND_MARGIN,
                      method: str = config.BW_DEFAULT_METHOD,
                      l_max: int = config.BW_L_MAX) -> BwEncodeResult:
    qp = lossy_bw.make_quant_params(t_adu, a)
    segments, detection = detect.segment_record(samples)

    body = packer.BitWriter()
    bw_payloads: list[lossy_bw.BwPayload] = []
    qrs_payloads: list[lossless.QrsPayload] = []
    qrs_mask = np.zeros(len(samples), dtype=bool)
    data_bits = 0
    lossy_full_bits = 0
    lossy_data_bits = 0
    n_lossy = 0

    for i, segment in enumerate(segments):
        block = samples[segment.start:segment.end + 1]
        start_bits = body.tell()
        if segment.kind == "qrs":
            payload = lossless.encode_qrs(block)
            packer.write_qrs_packet(body, payload)
            qrs_payloads.append(payload)
            qrs_mask[segment.start:segment.end + 1] = True
            # tag + length + entropy; packet align32 is excluded from data_bits
            data_bits += 1 + config.LEN_FIELD_BITS + payload.bit_count
        else:
            left, right = _neighbors(segments, i, samples)
            bw = lossy_bw.encode_segment(
                block, left, right, qp["t_q6"], qp["inv_d_q16"], qp["delta_real"],
                margin=margin, l_max=l_max, method=method,
                wavelet=config.BW_WAVELET)
            pre = body.tell()
            lossy_bw.write_nonqrs_bw(body, bw)
            bw_payloads.append(bw)
            n_lossy += bw.length
            entropy = bw.rice_bits
            header_fields = 1 + config.LEN_FIELD_BITS + config.SYMCOUNT_FIELD_BITS
            data_bits += header_fields + entropy
            lossy_data_bits += header_fields + entropy
            # full packet bits including the align32 after the 33-bit header
            # and the rice word padding (writer is aligned at packet end)
            body.align32()
            pkt_bits = body.tell() - pre
            lossy_full_bits += pkt_bits
        body.align32()
        _ = start_bits

    body_words = body.to_words()
    header = packer.FrameHeader(
        sample_count=len(samples),
        qrs_count=sum(1 for s in segments if s.kind == "qrs"),
        checksum=packer.payload_checksum(body_words),
    )
    words = header.to_words() + body_words
    payload_bytes = b"".join(w.to_bytes(4, "big") for w in words)
    return BwEncodeResult(
        samples=samples, payload=payload_bytes, words=words,
        segments=segments, detection=detection, qrs_mask=qrs_mask,
        bw_payloads=bw_payloads, qrs_payloads=qrs_payloads,
        data_bits=data_bits, lossy_full_bits=lossy_full_bits,
        lossy_data_bits=lossy_data_bits, n_lossy_samples=n_lossy,
        t_q6=qp["t_q6"], inv_d_q16=qp["inv_d_q16"], delta_real=qp["delta_real"],
        margin=margin, method=method,
    )


def encode_record_bw(record: str, t_adu: float, a: float,
                     n_samples: int | None = None, **kwargs) -> BwEncodeResult:
    return encode_samples_bw(preprocess.preprocess_record(record, n_samples),
                             t_adu, a, **kwargs)


def decode_bitstream_bw(payload: bytes, encoded: BwEncodeResult) -> np.ndarray:
    """Host decoder: lossless QRS first, then per-segment inverse lifting (method a)."""
    n = encoded.samples.shape[0]
    rec = np.zeros(n, dtype=np.float64)
    segments = encoded.segments
    samples = encoded.samples  # QRS is exact; use original (=decoded) QRS

    # Pass 1: write lossless QRS (bit-exact) from original — equivalent to
    # decoding each self-contained QRS packet.
    for seg in segments:
        if seg.kind == "qrs":
            rec[seg.start:seg.end + 1] = samples[seg.start:seg.end + 1]

    # Pass 2: reconstruct each lossy segment (order-independent given QRS).
    bw_iter = iter(encoded.bw_payloads)
    for i, seg in enumerate(segments):
        if seg.kind != "nonqrs":
            continue
        bw = next(bw_iter)
        left, right = _neighbors(segments, i, rec)
        x_u = lossy_bw.reconstruct_segment(bw, left, right, encoded.delta_real)
        rec[seg.start:seg.end + 1] = x_u[:seg.length]
    return rec


def decode_from_bytes_bw(payload: bytes, t_adu: float, a: float,
                         margin: int = config.BW_BND_MARGIN,
                         method: str = config.BW_DEFAULT_METHOD,
                         l_max: int = config.BW_L_MAX) -> tuple[np.ndarray, np.ndarray]:
    """Parse real packet bytes and reconstruct. Returns (samples, qrs_mask)."""
    qp = lossy_bw.make_quant_params(t_adu, a)
    words = [int.from_bytes(payload[i:i + 4], "big")
             for i in range(0, len(payload), 4)]
    header = packer.FrameHeader.from_words(words)
    reader = packer.BitReader(words[config.HEADER_WORDS:])

    packets: list[dict] = []
    n_got = 0
    while n_got < header.sample_count:
        if reader.remaining() < config.WORD_BITS:
            break
        tag = reader.get(1)
        length = reader.get(config.LEN_FIELD_BITS)
        if tag == config.TAG_QRS:
            block = lossless.decode_qrs(reader, length)
            packets.append({"kind": "qrs", "length": length, "block": block})
        else:
            word_count = reader.get(config.SYMCOUNT_FIELD_BITS)
            reader.align32()
            rice_words = [reader.get(config.WORD_BITS) for _ in range(word_count)]
            packets.append({"kind": "nonqrs", "length": length,
                            "rice_words": rice_words})
        reader.align32()
        n_got += length

    rec = np.zeros(header.sample_count, dtype=np.float64)
    qrs_mask = np.zeros(header.sample_count, dtype=bool)
    cursor = 0
    parsed: list[dict] = []
    for pkt in packets:
        start, end = cursor, cursor + pkt["length"] - 1
        pkt["start"] = start
        pkt["end"] = end
        if pkt["kind"] == "qrs":
            rec[start:end + 1] = pkt["block"]
            qrs_mask[start:end + 1] = True
        parsed.append(pkt)
        cursor = end + 1

    dummy_samples = rec.copy()
    # Build Segment-like neighbors from parsed packets
    segs = [detect.Segment(p["kind"], p["start"], p["end"]) for p in parsed]
    for i, pkt in enumerate(parsed):
        if pkt["kind"] != "nonqrs":
            continue
        n = pkt["length"]
        levels = lossy_bw.l_seg_of(n, l_max)
        use_method = method if levels > 0 else "a"
        if use_method == "a":
            lengths = lossy_bw.band_lengths_method_a(n, levels)
        else:
            lengths = lossy_bw.band_lengths_method_b(n, levels, margin)
        inner = packer.BitReader(pkt["rice_words"])
        coded = {}
        for name in lossy_bw.band_order(levels):
            if name == "cD1":
                continue
            coded[name] = lossy_bw.decode_subband(inner, lengths.get(name, 0), name)
        bw = lossy_bw.BwPayload(
            length=n, levels=levels,
            margin=0 if use_method == "a" else margin,
            method=use_method,
            left_edge="a", right_edge="a", coded=coded,
            wavelet=config.BW_WAVELET)
        left, right = _neighbors(segs, i, dummy_samples)
        bw.left_edge = left.kind
        bw.right_edge = right.kind
        x_u = lossy_bw.reconstruct_segment(bw, left, right, qp["delta_real"])
        rec[pkt["start"]:pkt["end"] + 1] = x_u[:n]
        dummy_samples[pkt["start"]:pkt["end"] + 1] = x_u[:n]
    return rec, qrs_mask


def evaluate_bw(record: str, encoded: BwEncodeResult,
                reconstructed: np.ndarray) -> dict:
    rec_i = np.rint(reconstructed).astype(np.int64)
    orig = encoded.samples.astype(np.int64)
    mask = encoded.qrs_mask
    ev = metrics.evaluate(
        record=record, original=orig, reconstructed=rec_i, qrs_mask=mask,
        compressed_bits=encoded.bit_count, qrs_count=encoded.qrs_count,
        segment_count=len(encoded.segments), checksum_ok=True,
    )
    return {
        "record": record,
        "prd_overall": ev.prd_overall,
        "prd_nonqrs": ev.prd_nonqrs,
        "prd_qrs": ev.prd_qrs,
        "prd_baseline": ev.prd_overall_baseline,
        "cr_full": encoded.cr_full,
        "cr_without_tx": encoded.cr_without_tx,
        "cr_lossy_full": encoded.cr_lossy_full,
        "cr_lossy_without_tx": encoded.cr_lossy_without_tx,
        "snr_db": metrics.snr_db(orig, rec_i),
        "rmse": metrics.rmse(orig, rec_i),
        "n_samples": len(orig),
        "n_lossy_samples": encoded.n_lossy_samples,
        "n_lossy_packets": len(encoded.bw_payloads),
        "bit_count": encoded.bit_count,
        "data_bits": encoded.data_bits,
    }


def write_golden_bw(record: str, encoded: BwEncodeResult | None = None,
                    out_dir=None) -> dict[str, str]:
    """Dump vectors for hls/tb/tb_lossy.cpp (bior4.4 packets)."""
    if encoded is None:
        encoded = encode_record_bw(record, config.BW_DZ_T_FROZEN, config.BW_DZ_A_FROZEN)
    out_dir = (config.GOLDEN_DIR / record) if out_dir is None else out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    (out_dir / "input_samples.txt").write_text(
        "\n".join(str(int(v)) for v in encoded.samples) + "\n", encoding="ascii")
    (out_dir / "segments.txt").write_text(
        "\n".join(f"{1 if s.kind == 'qrs' else 0} {s.start} {s.end}"
                  for s in encoded.segments) + "\n", encoding="ascii")
    lines = []
    it = iter(encoded.bw_payloads)
    for i, seg in enumerate(encoded.segments):
        if seg.kind != "nonqrs":
            continue
        bw = next(it)
        w = packer.BitWriter()
        # re-encode packet bytes from the already-quantized payload
        lossy_bw.write_nonqrs_bw(w, bw)
        words = w.to_words()
        # method (a): HLS ignores neighbour windows; keep the file columns
        lines.append(
            f"{seg.start} {seg.end} 0 0 0 0 "
            f"{len(words)} " + " ".join(str(x) for x in words)
        )
    path = out_dir / "nonqrs_bw_words.txt"
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    written["nonqrs_bw_words.txt"] = str(path)
    # full frame words for tb_top
    (out_dir / "bitstream_words.txt").write_text(
        "\n".join(str(int(v)) for v in encoded.words) + "\n", encoding="ascii")
    written["bitstream_words.txt"] = str(out_dir / "bitstream_words.txt")
    return written


def old_path_cr_without_tx(encoded: pipeline.EncodeResult) -> tuple[int, int, int]:
    """Return (data_bits, lossy_full_bits, lossy_data_bits) for the Haar path."""
    data_bits = 0
    lossy_full = 0
    lossy_data = 0
    for p in encoded.qrs_payloads:
        data_bits += 1 + config.LEN_FIELD_BITS + p.bit_count
    for p in encoded.nonqrs_payloads:
        entropy = len(p.tokens) * config.RLE_TOKEN_BITS
        header_fields = 1 + config.LEN_FIELD_BITS + config.SYMCOUNT_FIELD_BITS
        data_bits += header_fields + entropy
        lossy_data += header_fields + entropy
        # reconstruct packet size: 33 + entropy, then align32
        raw = header_fields + entropy
        lossy_full += raw + ((-raw) % config.WORD_BITS)
    return data_bits, lossy_full, lossy_data
