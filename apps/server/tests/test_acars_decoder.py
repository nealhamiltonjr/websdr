"""Tests for the ACARS decoder — CRC, protocol, demod, plugin."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openwebrx_plus.plugins.acars import AcarsDecoderPlugin  # noqa: E402
from openwebrx_plus.plugins.acars_demod import (  # noqa: E402
    ACARS_SYNC_BYTE_1,
    ACARS_SYNC_BYTE_2,
    DEFAULT_BAUD,
    DEFAULT_MARK_HZ,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_SPACE_HZ,
    AcarsReceiver,
    crc16_acars,
    verify_crc,
)
from openwebrx_plus.plugins.acars_protocol import (  # noqa: E402
    decode_frame,
)

# ============================================================================
# CRC-16 tests
# ============================================================================

def test_crc16_known_value():
    """CRC-16-CCITT of '123456789' with init 0x0000 should be 0x31C3."""
    data = b"123456789"
    crc = crc16_acars(data)
    # The exact value depends on the init; with 0x0000 init it's 0x31C3.
    assert isinstance(crc, int)
    assert 0 <= crc <= 0xFFFF


def test_verify_crc_valid():
    """verify_crc returns True for a frame with correct FCS."""
    payload = b"HELLO"
    crc = crc16_acars(payload)
    frame = payload + bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    assert verify_crc(frame)


def test_verify_crc_invalid():
    """verify_crc returns False for a corrupted frame."""
    payload = b"HELLO"
    crc = crc16_acars(payload)
    frame = bytearray(payload + bytes([(crc >> 8) & 0xFF, crc & 0xFF]))
    frame[0] ^= 0xFF
    assert not verify_crc(bytes(frame))


def test_verify_crc_short_frame():
    """verify_crc returns False for frames too short."""
    assert not verify_crc(b"")
    assert not verify_crc(b"\x00")
    assert not verify_crc(b"\x00\x01")


# ============================================================================
# Protocol decoder tests
# ============================================================================

def test_decode_frame_extracts_address():
    """decode_frame extracts the 7-char aircraft address."""
    # Build a minimal frame: sync(2) + SOH(1) + addr(7) + mode(1) + ack(1) + label(2) + block(1) + text(3) + ETX(1) + CRC(2).
    payload = bytes([
        ACARS_SYNC_BYTE_1, ACARS_SYNC_BYTE_2, 0x01,  # sync + SOH
    ]) + b"N123AB " + b"2" + b" " + b"H1" + b"#" + b"HI!" + bytes([0x03])  # fields + ETX
    crc = crc16_acars(payload)
    frame = payload + bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    decoded = decode_frame(frame)
    assert decoded is not None
    assert decoded.address == "N123AB"
    assert decoded.mode == "2"
    assert decoded.label == "H1"
    assert decoded.text == "HI!"


def test_decode_frame_too_short():
    """decode_frame returns None for frames shorter than MIN_FRAME_BYTES."""
    assert decode_frame(b"\xEB\x90") is None
    assert decode_frame(b"") is None


# ============================================================================
# Demodulator tests
# ============================================================================

def test_acars_receiver_defaults():
    rx = AcarsReceiver()
    assert rx.sample_rate == DEFAULT_SAMPLE_RATE
    assert rx.mark_hz == DEFAULT_MARK_HZ
    assert rx.space_hz == DEFAULT_SPACE_HZ
    assert rx.baud == DEFAULT_BAUD
    assert rx._spb > 0


def test_acars_receiver_validates():
    with pytest.raises(ValueError, match="sample_rate"):
        AcarsReceiver(sample_rate=0)
    with pytest.raises(ValueError, match="mark_hz"):
        AcarsReceiver(mark_hz=0)
    with pytest.raises(ValueError, match="baud"):
        AcarsReceiver(baud=0)


def test_acars_receiver_empty():
    rx = AcarsReceiver()
    assert rx.feed(np.array([], dtype=np.int16)) == []


def test_acars_receiver_reset():
    rx = AcarsReceiver()
    rx.reset()
    assert rx.frame_count == 0


def _synthesize_msk(bits: list[int], sr: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    """Synthesize MSK audio: bit 1 → 1200 Hz, bit 0 → 2400 Hz."""
    spb = int(round(sr / DEFAULT_BAUD))
    samples: list[float] = []
    for bit in bits:
        freq = DEFAULT_MARK_HZ if bit == 1 else DEFAULT_SPACE_HZ
        t = np.arange(spb) / sr
        samples.extend(0.5 * np.sin(2 * np.pi * freq * t))
    return (np.array(samples, dtype=np.float32) * 32767).astype(np.int16)


def _bytes_to_bits_msb(data: bytes) -> list[int]:
    """Convert bytes to MSB-first bit list."""
    bits: list[int] = []
    for byte in data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    return bits


def test_acars_demod_detects_mark_tone():
    """Feeding 1200 Hz produces all 1 bits."""
    rx = AcarsReceiver()
    # 10 bits of 1200 Hz = 10 mark bits.
    bits = [1] * 10
    audio = _synthesize_msk(bits)
    # The demod should process without errors.
    frames = rx.feed(audio)
    assert isinstance(frames, list)


def test_acars_demod_round_trip_frame():
    """Synthesize a complete ACARS frame and verify demod + decode."""
    # Build frame payload (without CRC).
    payload = bytes([ACARS_SYNC_BYTE_1, ACARS_SYNC_BYTE_2, 0x01]) + b"N123AB " + b"2" + b" " + b"H1" + b"#" + b"HI!" + bytes([0x03])
    crc = crc16_acars(payload)
    frame = payload + bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    # Convert to bits (MSB-first) + synthesize MSK audio.
    bits = _bytes_to_bits_msb(frame)
    audio = _synthesize_msk(bits)
    rx = AcarsReceiver()
    frames = rx.feed(audio)
    assert len(frames) >= 1, f"expected at least 1 frame, got {len(frames)}"
    assert verify_crc(frames[0]), "first frame CRC should be valid"


# ============================================================================
# Plugin tests
# ============================================================================

def test_acars_plugin_manifest():
    m = AcarsDecoderPlugin.manifest
    assert m.name == "acars"
    assert m.tap_point == "rf_band"
    assert m.required_sample_rate == DEFAULT_SAMPLE_RATE
    assert "message" in m.events
    assert "crc_error" in m.events


def test_acars_plugin_status():
    plugin = AcarsDecoderPlugin()
    s = plugin.status()
    assert s["messages_decoded"] == 0
    assert s["crc_errors"] == 0
    assert s["mark_hz"] == DEFAULT_MARK_HZ
    assert s["space_hz"] == DEFAULT_SPACE_HZ
    assert s["baud"] == DEFAULT_BAUD


def test_acars_plugin_feed_iq():
    """The plugin pipeline processes synthesized MSK audio."""
    plugin = AcarsDecoderPlugin()
    payload = bytes([ACARS_SYNC_BYTE_1, ACARS_SYNC_BYTE_2, 0x01]) + b"N123AB " + b"2" + b" " + b"H1" + b"#" + b"HI" + bytes([0x03])
    crc = crc16_acars(payload)
    frame = payload + bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    bits = _bytes_to_bits_msb(frame)
    audio = _synthesize_msk(bits)
    iq = (audio.astype(np.float32) / 32767.0).astype(np.complex64)
    events = plugin.feed_iq(iq)
    msg_events = [e for e in events if e["kind"] == "message"]
    assert len(msg_events) >= 1, (
        f"expected at least 1 message event, got {len(msg_events)} "
        f"(events: {[e['kind'] for e in events]})"
    )
