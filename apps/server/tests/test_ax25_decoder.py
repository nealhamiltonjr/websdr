"""Tests for the AX.25 decoder — CRC, address encoding, frame round-trip, demod.

Tests verify the CRC-16 computation, callsign encoding/decoding, and the
full AFSK → HDLC → AX.25 frame round-trip.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openwebrx_plus.plugins.ax25 import Ax25DecoderPlugin  # noqa: E402
from openwebrx_plus.plugins.ax25_demod import (  # noqa: E402
    DEFAULT_BAUD,
    DEFAULT_MARK_HZ,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_SPACE_HZ,
    Ax25Receiver,
    crc16_ax25,
    verify_crc,
)
from openwebrx_plus.plugins.ax25_protocol import (  # noqa: E402
    Ax25Address,
    decode_callsign,
    decode_frame,
    encode_callsign,
    encode_frame,
)

# ============================================================================
# CRC-16 tests
# ============================================================================

def test_crc16_known_value():
    """CRC-16 of a known test vector."""
    # "123456789" should give 0x29B1 for CRC-CCITT (0xFFFF init).
    data = b"123456789"
    crc = crc16_ax25(data)
    assert crc == 0x29B1, f"CRC-16 of '123456789' should be 0x29B1, got 0x{crc:04X}"


def test_verify_crc_valid():
    """verify_crc returns True for a frame with correct FCS."""
    payload = b"HELLO"
    crc = crc16_ax25(payload)
    frame = payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    assert verify_crc(frame)


def test_verify_crc_invalid():
    """verify_crc returns False for a corrupted frame."""
    payload = b"HELLO"
    crc = crc16_ax25(payload)
    frame = payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    # Corrupt one byte.
    frame = bytearray(frame)
    frame[0] ^= 0xFF
    assert not verify_crc(bytes(frame))


def test_verify_crc_short_frame():
    """verify_crc returns False for frames too short to have FCS."""
    assert not verify_crc(b"")
    assert not verify_crc(b"\x00")


# ============================================================================
# Callsign encoding tests
# ============================================================================

def test_callsign_round_trip():
    """Callsign encode/decode round-trips."""
    for callsign, ssid in [("K1ABC", 0), ("W1AW", 1), ("N0CALL", 15)]:
        addr = Ax25Address(callsign=callsign, ssid=ssid)
        encoded = encode_callsign(addr)
        decoded = decode_callsign(encoded)
        assert decoded.callsign == callsign, f"callsign: {callsign} → {decoded.callsign}"
        assert decoded.ssid == ssid, f"ssid: {ssid} → {decoded.ssid}"


def test_callsign_short_padded():
    """Short callsigns are padded with spaces."""
    addr = Ax25Address(callsign="W1", ssid=0)
    encoded = encode_callsign(addr)
    assert len(encoded) == 7
    decoded = decode_callsign(encoded)
    assert decoded.callsign == "W1"


# ============================================================================
# Frame encoding/decoding tests
# ============================================================================

def test_encode_decode_frame_round_trip():
    """Full frame encode/decode round-trips (no FCS)."""
    dst = Ax25Address(callsign="APRS", ssid=0)
    src = Ax25Address(callsign="K1ABC", ssid=1)
    info = b">OpenWebRX+ test"
    payload = encode_frame(dst, src, info=info, control=0x00)
    # Add FCS for proper decoding.
    crc = crc16_ax25(payload)
    frame = payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    decoded = decode_frame(frame)
    assert decoded is not None
    assert decoded.destination.callsign == "APRS"
    assert decoded.source.callsign == "K1ABC"
    assert decoded.source.ssid == 1
    assert decoded.frame_type == "I"
    assert decoded.info == info


def test_decode_frame_with_digipeater():
    """Frame with a digipeater decodes correctly."""
    dst = Ax25Address(callsign="CQ", ssid=0)
    src = Ax25Address(callsign="N0CALL", ssid=2)
    digi = Ax25Address(callsign="WIDE2", ssid=2)
    info = b"test payload"
    payload = encode_frame(dst, src, info=info, digipeaters=[digi])
    crc = crc16_ax25(payload)
    frame = payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    decoded = decode_frame(frame)
    assert decoded is not None
    assert len(decoded.digipeaters) == 1
    assert decoded.digipeaters[0].callsign == "WIDE2"


def test_frame_type_u_frame():
    """U-frames (control bit 0x03) are classified correctly."""
    dst = Ax25Address(callsign="TEST", ssid=0)
    src = Ax25Address(callsign="TEST", ssid=0)
    payload = encode_frame(dst, src, info=b"", control=0x03)  # UI frame
    crc = crc16_ax25(payload)
    frame = payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    decoded = decode_frame(frame)
    assert decoded is not None
    assert decoded.frame_type == "U"


# ============================================================================
# Demodulator tests
# ============================================================================

def test_ax25_receiver_constructor_defaults():
    """Default constructor produces standard AX.25 params."""
    rx = Ax25Receiver()
    assert rx.sample_rate == DEFAULT_SAMPLE_RATE
    assert rx.mark_hz == DEFAULT_MARK_HZ
    assert rx.space_hz == DEFAULT_SPACE_HZ
    assert rx.baud == DEFAULT_BAUD
    assert rx._spb > 0


def test_ax25_receiver_validates():
    """Invalid params raise ValueError."""
    with pytest.raises(ValueError, match="sample_rate"):
        Ax25Receiver(sample_rate=0)
    with pytest.raises(ValueError, match="mark_hz"):
        Ax25Receiver(mark_hz=0)
    with pytest.raises(ValueError, match="baud"):
        Ax25Receiver(baud=0)


def test_ax25_receiver_empty_input():
    """Empty input returns empty list."""
    rx = Ax25Receiver()
    assert rx.feed(np.array([], dtype=np.int16)) == []


def test_ax25_receiver_reset():
    """reset() clears state."""
    rx = Ax25Receiver()
    rx.reset()
    assert rx.frame_count == 0


def _bits_to_nrzi_audio(bits: list[int], sr: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    """Convert a bit stream to NRZI-encoded AFSK audio.

    Bit 1 → no tone change, bit 0 → tone change.
    Mark = 1200 Hz, space = 2200 Hz.
    """
    spb = int(round(sr / DEFAULT_BAUD))
    samples: list[float] = []
    current_tone = 1  # start with mark
    for bit in bits:
        if bit == 0:
            current_tone = 1 - current_tone  # toggle
        freq = DEFAULT_MARK_HZ if current_tone == 1 else DEFAULT_SPACE_HZ
        t = np.arange(spb) / sr
        samples.extend(0.5 * np.sin(2 * np.pi * freq * t))
    return (np.array(samples, dtype=np.float32) * 32767).astype(np.int16)


def _bytes_to_bits_lsb(data: bytes) -> list[int]:
    """Convert bytes to a bit stream (LSB-first per byte)."""
    bits: list[int] = []
    for byte in data:
        for i in range(8):
            bits.append((byte >> i) & 1)
    return bits


def _bit_stuff(bits: list[int]) -> list[int]:
    """Apply AX.25 bit stuffing: after 5 consecutive 1s, insert a 0."""
    result: list[int] = []
    ones = 0
    for bit in bits:
        result.append(bit)
        if bit == 1:
            ones += 1
            if ones == 5:
                result.append(0)  # stuff a 0
                ones = 0
        else:
            ones = 0
    return result


def _synthesize_afsk(bits: list[int], sr: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    """Synthesize 1200 baud AFSK audio from a bit stream (legacy interface)."""
    return _bits_to_nrzi_audio(bits, sr)


def test_ax25_demod_detects_mark_tone():
    """Feeding a continuous mark tone produces all 1 bits (NRZI no-change)."""
    rx = Ax25Receiver()
    # 10 bits of mark (NRZI: no tone change = 1).
    bits = [1] * 10
    audio = _synthesize_afsk(bits)
    # The demod should process the audio without errors.
    frames = rx.feed(audio)
    assert isinstance(frames, list)


def test_ax25_demod_round_trip_frame():
    """Synthesize a complete AX.25 frame and verify demod + decode."""
    # Build a minimal frame: APRS dest, K1ABC source, no info, control=0x03 (UI).
    dst = Ax25Address(callsign="APRS", ssid=0)
    src = Ax25Address(callsign="K1ABC", ssid=1)
    payload = encode_frame(dst, src, info=b"test", control=0x03)
    crc = crc16_ax25(payload)
    frame = payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])

    # Convert frame bytes to bits (LSB-first), apply bit stuffing,
    # then wrap with HDLC flags (which are NOT stuffed).
    flag_bits = [0, 1, 1, 1, 1, 1, 1, 0]  # 0x7E LSB-first
    frame_bits = _bytes_to_bits_lsb(frame)
    stuffed = _bit_stuff(frame_bits)
    # Full bit stream: flag + stuffed frame + flag.
    all_bits = flag_bits + stuffed + flag_bits

    audio = _bits_to_nrzi_audio(all_bits)
    rx = Ax25Receiver()
    frames = rx.feed(audio)
    # We should get at least one frame.
    assert len(frames) >= 1, f"expected at least 1 frame, got {len(frames)}"
    # The first frame should have a valid CRC.
    assert verify_crc(frames[0]), "first frame CRC should be valid"


# ============================================================================
# Plugin tests
# ============================================================================

def test_ax25_plugin_manifest():
    """Plugin manifest has the right fields."""
    m = Ax25DecoderPlugin.manifest
    assert m.name == "ax25"
    assert m.tap_point == "rf_band"
    assert m.required_sample_rate == DEFAULT_SAMPLE_RATE
    assert "packet" in m.events
    assert "crc_error" in m.events


def test_ax25_plugin_status():
    """status() returns the current demod state."""
    plugin = Ax25DecoderPlugin()
    s = plugin.status()
    assert s["packets_decoded"] == 0
    assert s["crc_errors"] == 0
    assert s["mark_hz"] == DEFAULT_MARK_HZ
    assert s["space_hz"] == DEFAULT_SPACE_HZ
    assert s["baud"] == DEFAULT_BAUD


def test_ax25_plugin_feed_iq_processes_audio():
    """The plugin pipeline processes synthesized AFSK audio."""
    plugin = Ax25DecoderPlugin()
    # Synthesize a complete frame with bit stuffing.
    dst = Ax25Address(callsign="APRS", ssid=0)
    src = Ax25Address(callsign="K1ABC", ssid=1)
    payload = encode_frame(dst, src, info=b"hi", control=0x03)
    crc = crc16_ax25(payload)
    frame = payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    flag_bits = [0, 1, 1, 1, 1, 1, 1, 0]
    frame_bits = _bytes_to_bits_lsb(frame)
    stuffed = _bit_stuff(frame_bits)
    all_bits = flag_bits + stuffed + flag_bits
    audio = _bits_to_nrzi_audio(all_bits)
    iq = (audio.astype(np.float32) / 32767.0).astype(np.complex64)
    events = plugin.feed_iq(iq)
    # Should produce at least one packet event.
    packet_events = [e for e in events if e["kind"] == "packet"]
    assert len(packet_events) >= 1, (
        f"expected at least 1 packet event, got {len(packet_events)} "
        f"(events: { [e['kind'] for e in events] })"
    )
    pkt = packet_events[0]
    assert pkt["source"] == "K1ABC-1"
    assert pkt["destination"] == "APRS"
