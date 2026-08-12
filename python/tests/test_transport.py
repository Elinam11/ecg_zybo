"""UART transport framing: sync, length, resynchronisation.

The compressed frame itself is covered by test_packer/test_roundtrip. What is tested
here is the thin envelope the PS wraps around it, which is the only part of the link
that has to survive a receiver being started midway through the board's boot output.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))
sys.path.insert(0, str(REPO / "python" / "scripts"))

from ecg_ref import config  # noqa: E402


def _load_serial_rx():
    path = REPO / "python" / "scripts" / "serial_rx.py"
    spec = importlib.util.spec_from_file_location("serial_rx", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


serial_rx = _load_serial_rx()


WORDS = [0xEC601001, 108000, 0x01700168, 0xDEADBEEF, 0x11112222, 0x33334444]


def test_frame_round_trips():
    stream = serial_rx.build_transport_frame(WORDS, latency_cycles=832800)
    frame, latency = serial_rx.read_transport_frame(
        serial_rx.reader_from_bytes(stream))

    assert latency == 832800
    assert len(frame) == len(WORDS) * 4
    assert [int.from_bytes(frame[i:i + 4], "big")
            for i in range(0, len(frame), 4)] == WORDS


def test_preamble_length_is_what_the_firmware_sends():
    stream = serial_rx.build_transport_frame(WORDS)
    assert len(stream) == serial_rx.PREAMBLE_BYTES + len(WORDS) * 4
    assert config.UART_PREAMBLE_WORDS == 3


def test_resynchronises_past_boot_chatter():
    # A real capture almost always starts mid-sentence in the board's console output.
    noise = b"Zynq boot chatter, no sync word here...\r\n"
    stream = noise + serial_rx.build_transport_frame(WORDS)
    frame, _ = serial_rx.read_transport_frame(serial_rx.reader_from_bytes(stream))
    assert int.from_bytes(frame[0:4], "big") == WORDS[0]


def test_sync_word_appearing_inside_the_noise_is_still_found():
    # A partial sync (three of the four bytes) must not derail the byte-wise search.
    partial = config.UART_SYNC_MAGIC.to_bytes(4, "big")[:3]
    stream = partial + serial_rx.build_transport_frame(WORDS)
    frame, _ = serial_rx.read_transport_frame(serial_rx.reader_from_bytes(stream))
    assert int.from_bytes(frame[0:4], "big") == WORDS[0]


def test_truncated_stream_is_an_error_not_a_short_frame():
    stream = serial_rx.build_transport_frame(WORDS)[:-8]
    with pytest.raises(serial_rx.FrameError):
        serial_rx.read_transport_frame(serial_rx.reader_from_bytes(stream))


def test_absent_sync_word_gives_up():
    with pytest.raises(serial_rx.FrameError):
        serial_rx.read_transport_frame(
            serial_rx.reader_from_bytes(b"\x00" * 4096), resync_limit=64)


def test_implausible_length_is_rejected():
    # A corrupted length field must not make the receiver block forever on a read.
    bad = (config.UART_SYNC_MAGIC.to_bytes(4, "big")
           + (1 << 30).to_bytes(4, "big") + b"\x00" * 4)
    with pytest.raises(serial_rx.FrameError):
        serial_rx.read_transport_frame(serial_rx.reader_from_bytes(bad))
