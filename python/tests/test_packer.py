"""Bitstream packing: bit ordering, word alignment, header fields, checksum."""

import numpy as np
import pytest

from ecg_ref import config, lossless, lossy, packer


def test_bit_writer_is_msb_first():
    writer = packer.BitWriter()
    writer.put(0b1011, 4)
    writer.put(0, 28)
    assert writer.to_words() == [0b1011 << 28]


def test_bit_writer_reader_roundtrip():
    rng = np.random.default_rng(1)
    fields = []
    writer = packer.BitWriter()
    for _ in range(500):
        nbits = int(rng.integers(1, 33))
        value = int(rng.integers(0, 1 << nbits))
        fields.append((value, nbits))
        writer.put(value, nbits)
    reader = packer.BitReader(writer.to_words())
    for value, nbits in fields:
        assert reader.get(nbits) == value


def test_align32_pads_to_word_boundary():
    writer = packer.BitWriter()
    writer.put(1, 1)
    pad = writer.align32()
    assert pad == 31
    assert writer.tell() % config.WORD_BITS == 0
    assert writer.align32() == 0, "already aligned"


def test_to_bytes_is_big_endian():
    writer = packer.BitWriter()
    writer.put(0xDEADBEEF, 32)
    assert writer.to_bytes() == bytes([0xDE, 0xAD, 0xBE, 0xEF])


def test_header_roundtrip():
    header = packer.FrameHeader(sample_count=108000, qrs_count=368, checksum=0x12345678)
    restored = packer.FrameHeader.from_words(header.to_words())
    assert restored.magic == config.MAGIC_WORD
    assert restored.sample_count == 108000
    assert restored.qrs_count == 368
    assert restored.fs == config.FS
    assert restored.checksum == 0x12345678


def test_header_magic_word_value():
    header = packer.FrameHeader(sample_count=1, qrs_count=0)
    assert header.to_words()[0] == 0xEC601001


def test_header_rejects_bad_magic():
    words = [0x00000000, 1, 0, 0]
    with pytest.raises(ValueError, match="magic"):
        packer.FrameHeader.from_words(words)


def test_checksum_detects_corruption():
    words = [1, 2, 3, 4, 5]
    good = packer.payload_checksum(words)
    words[2] ^= 0x40
    assert packer.payload_checksum(words) != good


def test_qrs_packet_is_word_aligned():
    samples = np.arange(config.WS, dtype=np.int64) * 3
    payload = lossless.encode_qrs(samples)
    writer = packer.BitWriter()
    packer.write_qrs_packet(writer, payload)
    assert writer.tell() % config.WORD_BITS == 0


def test_nonqrs_packet_is_word_aligned():
    samples = np.arange(128, dtype=np.int64)
    payload = lossy.encode_nonqrs(samples)
    writer = packer.BitWriter()
    packer.write_nonqrs_packet(writer, payload)
    assert writer.tell() % config.WORD_BITS == 0


def test_frame_decode_reconstructs_mixed_segments():
    """A two-segment frame exercises tag dispatch and per-packet realignment."""
    rng = np.random.default_rng(5)
    qrs = rng.integers(-500, 500, size=config.WS).astype(np.int64)
    nonqrs = np.zeros(96, dtype=np.int64)

    body = packer.BitWriter()
    packer.write_nonqrs_packet(body, lossy.encode_nonqrs(nonqrs))
    packer.write_qrs_packet(body, lossless.encode_qrs(qrs))
    body_words = body.to_words()

    header = packer.FrameHeader(sample_count=len(nonqrs) + len(qrs), qrs_count=1,
                               checksum=packer.payload_checksum(body_words))
    words = header.to_words() + body_words
    payload = b"".join(w.to_bytes(4, "big") for w in words)

    frame = packer.decode_frame(payload)
    assert frame.checksum_ok
    assert frame.segment_count == 2
    assert len(frame.samples) == len(nonqrs) + len(qrs)
    # The QRS half is lossless, so it must come back bit-exact.
    assert np.array_equal(frame.samples[len(nonqrs):], qrs)
    assert np.array_equal(frame.qrs_mask[len(nonqrs):], np.ones(config.WS, bool))
    assert not frame.qrs_mask[:len(nonqrs)].any()


def test_frame_flags_checksum_mismatch():
    qrs = np.arange(config.WS, dtype=np.int64)
    body = packer.BitWriter()
    packer.write_qrs_packet(body, lossless.encode_qrs(qrs))
    body_words = body.to_words()
    header = packer.FrameHeader(sample_count=config.WS, qrs_count=1,
                                checksum=0xDEADBEEF)
    words = header.to_words() + body_words
    payload = b"".join(w.to_bytes(4, "big") for w in words)
    assert packer.decode_frame(payload).checksum_ok is False


def test_reader_raises_at_end_of_stream():
    reader = packer.BitReader([0])
    reader.get(32)
    with pytest.raises(EOFError):
        reader.get(1)
