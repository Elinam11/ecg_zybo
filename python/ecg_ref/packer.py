"""Bitstream packing and parsing (spec 6).

Layout: a 128-bit frame header followed by tagged segment packets, each padded to a
32-bit boundary so the AXI4-Stream master can emit whole words and the PS never has to
realign anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import config, lossless, lossy
from .fixedpoint import sign_extend


class BitWriter:
    """MSB-first bit packer producing 32-bit words."""

    def __init__(self) -> None:
        self._bits: list[int] = []

    def put(self, value: int, nbits: int) -> None:
        if nbits < 0:
            raise ValueError("nbits must be non-negative")
        for shift in range(nbits - 1, -1, -1):
            self._bits.append((value >> shift) & 1)

    def tell(self) -> int:
        return len(self._bits)

    def align32(self) -> int:
        """Zero-pad to the next 32-bit boundary; returns the number of pad bits."""
        pad = (-len(self._bits)) % config.WORD_BITS
        self._bits.extend([0] * pad)
        return pad

    def to_words(self) -> list[int]:
        self.align32()
        words = []
        for offset in range(0, len(self._bits), config.WORD_BITS):
            word = 0
            for bit in self._bits[offset:offset + config.WORD_BITS]:
                word = (word << 1) | bit
            words.append(word)
        return words

    def to_bytes(self) -> bytes:
        """Big-endian byte order, matching the order the PS shifts words onto UART."""
        return b"".join(w.to_bytes(4, "big") for w in self.to_words())


class BitReader:
    """MSB-first bit reader over a list of 32-bit words."""

    def __init__(self, words: list[int]) -> None:
        self._bits: list[int] = []
        for word in words:
            for shift in range(config.WORD_BITS - 1, -1, -1):
                self._bits.append((word >> shift) & 1)
        self._pos = 0

    @classmethod
    def from_bytes(cls, payload: bytes) -> "BitReader":
        if len(payload) % 4:
            raise ValueError("payload must be a whole number of 32-bit words")
        words = [int.from_bytes(payload[i:i + 4], "big")
                 for i in range(0, len(payload), 4)]
        return cls(words)

    def get(self, nbits: int) -> int:
        if nbits == 0:
            return 0
        if self._pos + nbits > len(self._bits):
            raise EOFError("bitstream exhausted")
        value = 0
        for _ in range(nbits):
            value = (value << 1) | self._bits[self._pos]
            self._pos += 1
        return value

    def tell(self) -> int:
        return self._pos

    def align32(self) -> None:
        self._pos += (-self._pos) % config.WORD_BITS

    def remaining(self) -> int:
        return len(self._bits) - self._pos


# ---------------------------------------------------------------------------
# Frame header
# ---------------------------------------------------------------------------

@dataclass
class FrameHeader:
    sample_count: int
    qrs_count: int
    fs: int = config.FS
    checksum: int = 0
    magic: int = config.MAGIC_WORD

    def to_words(self) -> list[int]:
        word2 = (self.fs & 0xFFFF) | ((self.qrs_count & 0xFFFF) << 16)
        return [self.magic, self.sample_count & 0xFFFFFFFF, word2,
                self.checksum & config.CHECKSUM_MASK]

    @classmethod
    def from_words(cls, words: list[int]) -> "FrameHeader":
        if len(words) < config.HEADER_WORDS:
            raise ValueError("truncated frame header")
        if words[0] != config.MAGIC_WORD:
            raise ValueError(f"bad magic word 0x{words[0]:08X}")
        return cls(sample_count=words[1],
                   qrs_count=(words[2] >> 16) & 0xFFFF,
                   fs=words[2] & 0xFFFF,
                   checksum=words[3],
                   magic=words[0])


def payload_checksum(words: list[int]) -> int:
    """32-bit additive checksum over the payload words (one adder in hardware)."""
    total = config.CHECKSUM_INIT
    for word in words:
        total = (total + word) & config.CHECKSUM_MASK
    return total


# ---------------------------------------------------------------------------
# Segment packets
# ---------------------------------------------------------------------------

def write_qrs_packet(writer: BitWriter, payload: lossless.QrsPayload) -> None:
    writer.put(config.TAG_QRS, 1)
    writer.put(payload.length, config.LEN_FIELD_BITS)
    for value, nbits in payload.bits:
        writer.put(value, nbits)
    writer.align32()


def write_nonqrs_packet(writer: BitWriter, payload: lossy.NonQrsPayload) -> None:
    writer.put(config.TAG_NONQRS, 1)
    writer.put(payload.length, config.LEN_FIELD_BITS)
    writer.put(len(payload.tokens), config.SYMCOUNT_FIELD_BITS)
    for run, value in payload.tokens:
        writer.put(lossy.pack_token(run, value), config.RLE_TOKEN_BITS)
    writer.align32()


@dataclass
class DecodedFrame:
    header: FrameHeader
    samples: np.ndarray
    qrs_mask: np.ndarray
    segment_count: int = 0
    checksum_ok: bool = True
    segments: list[tuple[str, int, int]] = field(default_factory=list)


def decode_frame(payload: bytes) -> DecodedFrame:
    """Parse a complete frame and reconstruct the signal.

    Also returns a boolean mask marking which samples came from the lossless path, so
    PRD can be evaluated separately over QRS and non-QRS regions.
    """
    if len(payload) % 4:
        raise ValueError("payload must be a whole number of 32-bit words")
    words = [int.from_bytes(payload[i:i + 4], "big")
             for i in range(0, len(payload), 4)]
    header = FrameHeader.from_words(words)

    body = words[config.HEADER_WORDS:]
    checksum_ok = payload_checksum(body) == header.checksum

    reader = BitReader(body)
    samples: list[int] = []
    qrs_flags: list[bool] = []
    segments: list[tuple[str, int, int]] = []
    segment_count = 0

    while len(samples) < header.sample_count:
        if reader.remaining() < config.WORD_BITS:
            break
        start = len(samples)
        tag = reader.get(1)
        length = reader.get(config.LEN_FIELD_BITS)

        if tag == config.TAG_QRS:
            block = lossless.decode_qrs(reader, length)
            samples.extend(int(v) for v in block)
            qrs_flags.extend([True] * len(block))
            segments.append(("qrs", start, start + len(block) - 1))
        else:
            symbol_count = reader.get(config.SYMCOUNT_FIELD_BITS)
            tokens = [lossy.unpack_token(reader.get(config.RLE_TOKEN_BITS))
                      for _ in range(symbol_count)]
            block = lossy.decode_nonqrs(tokens, length)
            samples.extend(int(v) for v in block)
            qrs_flags.extend([False] * len(block))
            segments.append(("nonqrs", start, start + len(block) - 1))

        reader.align32()
        segment_count += 1

    return DecodedFrame(header=header,
                        samples=np.asarray(samples, dtype=np.int64),
                        qrs_mask=np.asarray(qrs_flags, dtype=bool),
                        segment_count=segment_count,
                        checksum_ok=checksum_ok,
                        segments=segments)


def parse_tokens_from_words(words: list[int]) -> list[int]:
    """Helper for testbench comparison: raw 32-bit words as unsigned ints."""
    return [w & 0xFFFFFFFF for w in words]


__all__ = [
    "BitWriter", "BitReader", "FrameHeader", "DecodedFrame",
    "payload_checksum", "write_qrs_packet", "write_nonqrs_packet",
    "decode_frame", "sign_extend",
]
