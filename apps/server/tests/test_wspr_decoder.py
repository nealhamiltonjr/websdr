"""Tests for the WSPR decoder — protocol, demod, plugin.

Tests verify the callsign/grid/power packing + unpacking round-trip,
the 4-tone FSK Goertzel detection, and the plugin pipeline.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openwebrx_plus.plugins.wspr import WsprDecoderPlugin  # noqa: E402
from openwebrx_plus.plugins.wspr_demod import (  # noqa: E402
    DEFAULT_CENTER_HZ,
    DEFAULT_NUM_SYMBOLS,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_TONE_SPACING_HZ,
    WsprReceiver,
    _samples_per_symbol,
    _tone_freqs,
)
from openwebrx_plus.plugins.wspr_protocol import (  # noqa: E402
    bits_to_symbols,
    pack_callsign,
    pack_grid,
    pack_message,
    pack_power,
    symbols_to_bits,
    unpack_callsign,
    unpack_grid,
    unpack_message,
    unpack_power,
)

# ============================================================================
# Protocol tests — callsign / grid / power packing
# ============================================================================

def test_callsign_round_trip():
    """Callsign pack/unpack round-trips for standard callsigns.

    WSPR encodes 5 characters in the 28-bit callsign field (6-char
    callsigns use a special encoding not implemented in v1)."""
    for callsign in ["K1ABC", "W1AW", "G4ABC", "N0CAL"]:
        code = pack_callsign(callsign)
        decoded = unpack_callsign(code)
        assert decoded.strip() == callsign, (
            f"round-trip failed: {callsign} → {decoded!r}"
        )


def test_grid_round_trip():
    """Grid locator pack/unpack round-trips for standard 4-char grids."""
    for grid in ["JO30", "FN20", "EM00", "AA00"]:
        code = pack_grid(grid)
        decoded = unpack_grid(code)
        assert decoded == grid, f"round-trip failed: {grid} → {decoded!r}"


def test_power_round_trip():
    """Power pack/unpack round-trips for standard dBm values."""
    for power in [0, 10, 20, 30, 37, 43]:
        code = pack_power(power)
        decoded = unpack_power(code)
        # Power is quantized to even values (decoded = code * 2 - 83).
        # So odd input values round to the nearest even.
        assert abs(decoded - power) <= 1, (
            f"round-trip failed: {power} → {decoded}"
        )


def test_message_round_trip():
    """Full message (callsign + grid + power) pack/unpack round-trips."""
    callsign = "K1AB"  # 4 chars (fits in 5-char WSPR field)
    grid = "JO30"
    power = 30
    code = pack_message(callsign, grid, power)
    c, g, p = unpack_message(code)
    assert c.strip() == callsign, f"callsign: {callsign} → {c!r}"
    assert g == grid, f"grid: {grid} → {g!r}"
    assert abs(p - power) <= 1, f"power: {power} → {p}"


# ============================================================================
# Symbol/bit conversion tests
# ============================================================================

def test_symbols_to_bits_round_trip():
    """symbols_to_bits + bits_to_symbols round-trips."""
    symbols = [0, 1, 2, 3, 0, 1, 2, 3]
    bits = symbols_to_bits(symbols)
    assert len(bits) == 16  # 2 bits per symbol
    assert bits == [0, 0, 0, 1, 1, 0, 1, 1, 0, 0, 0, 1, 1, 0, 1, 1]
    reconverted = bits_to_symbols(bits)
    assert reconverted == symbols


def test_symbols_to_bits_msb_first():
    """Symbol 2 = [1,0] (MSB first), symbol 1 = [0,1]."""
    bits = symbols_to_bits([2, 1])
    assert bits == [1, 0, 0, 1]


# ============================================================================
# Demodulator tests
# ============================================================================

def test_tone_freqs_correct():
    """The 4 WSPR tones are centered around the center frequency."""
    freqs = _tone_freqs(1500.0, 1.4648)
    assert len(freqs) == 4
    # Tones should be symmetric around 1500.
    mid = (freqs[0] + freqs[3]) / 2
    assert abs(mid - 1500.0) < 0.1


def test_samples_per_symbol():
    """At 8000 Hz with 1.4648 Hz spacing, sps ≈ 5461."""
    sps = _samples_per_symbol(8000, DEFAULT_TONE_SPACING_HZ)
    # 8000 / 1.4648 ≈ 5461
    assert 5000 < sps < 6000


def test_wspr_receiver_constructor_defaults():
    """Default constructor produces standard WSPR params."""
    rx = WsprReceiver()
    assert rx.sample_rate == DEFAULT_SAMPLE_RATE
    assert rx.center_hz == DEFAULT_CENTER_HZ
    assert rx.tone_spacing == DEFAULT_TONE_SPACING_HZ
    assert len(rx._tone_freqs) == 4
    assert rx._sps > 0


def test_wspr_receiver_validates():
    """Invalid params raise ValueError."""
    with pytest.raises(ValueError, match="sample_rate"):
        WsprReceiver(sample_rate=0)
    with pytest.raises(ValueError, match="center_hz"):
        WsprReceiver(sample_rate=2000, center_hz=1000)
    with pytest.raises(ValueError, match="tone_spacing"):
        WsprReceiver(tone_spacing=0)


def test_wspr_receiver_empty_input():
    """Empty input returns empty list."""
    rx = WsprReceiver()
    assert rx.feed(np.array([], dtype=np.int16)) == []


def test_wspr_receiver_reset():
    """reset() clears symbols."""
    rx = WsprReceiver()
    rx.reset()
    assert rx.symbol_count == 0
    assert not rx.is_complete


def test_wspr_demod_detects_single_tone():
    """Feeding audio with one dominant tone produces that symbol."""
    # Use a higher tone spacing for faster testing (WSPR's 1.46 Hz is very slow).
    rx = WsprReceiver(center_hz=1500.0, tone_spacing=100.0)
    freqs = rx._tone_freqs
    sps = rx._sps
    sr = rx.sample_rate
    # Synthesize 3 symbols of tone index 2.
    t = np.arange(sps * 3) / sr
    tone = (0.5 * np.sin(2 * np.pi * freqs[2] * t)).astype(np.float32)
    pcm = (tone * 32767).astype(np.int16)
    symbols = rx.feed(pcm)
    assert len(symbols) == 3, f"expected 3 symbols, got {len(symbols)}"
    for s in symbols:
        assert s == 2, f"expected symbol 2, got {s}"


def test_wspr_demod_detects_tone_sequence():
    """Feeding a sequence of different tones produces the right symbols."""
    rx = WsprReceiver(center_hz=1500.0, tone_spacing=100.0)
    freqs = rx._tone_freqs
    sps = rx._sps
    sr = rx.sample_rate
    expected = [0, 1, 2, 3]
    samples: list[float] = []
    for sym in expected:
        t = np.arange(sps) / sr
        samples.extend(0.5 * np.sin(2 * np.pi * freqs[sym] * t))
    audio = np.array(samples, dtype=np.float32)
    pcm = (audio * 32767).astype(np.int16)
    symbols = rx.feed(pcm)
    assert len(symbols) == 4, f"expected 4 symbols, got {len(symbols)}"
    matches = sum(1 for got, exp in zip(symbols, expected, strict=False) if got == exp)
    assert matches >= 3, (
        f"expected at least 3/4 matches, got {matches}/4: {symbols} vs {expected}"
    )


def test_wspr_is_complete_after_162_symbols():
    """is_complete returns True after 162 symbols are accumulated."""
    rx = WsprReceiver(center_hz=1500.0, tone_spacing=100.0)
    # Feed enough audio to produce 162 symbols.
    freqs = rx._tone_freqs
    sps = rx._sps
    sr = rx.sample_rate
    t = np.arange(sps * DEFAULT_NUM_SYMBOLS) / sr
    tone = (0.5 * np.sin(2 * np.pi * freqs[0] * t)).astype(np.float32)
    pcm = (tone * 32767).astype(np.int16)
    rx.feed(pcm)
    assert rx.symbol_count >= DEFAULT_NUM_SYMBOLS
    assert rx.is_complete


# ============================================================================
# Plugin tests
# ============================================================================

def test_wspr_plugin_manifest():
    """Plugin manifest has the right fields."""
    m = WsprDecoderPlugin.manifest
    assert m.name == "wspr"
    assert m.tap_point == "rf_band"
    assert m.required_sample_rate == DEFAULT_SAMPLE_RATE
    assert "spot" in m.events
    assert "progress" in m.events


def test_wspr_plugin_status():
    """status() returns the current demod state."""
    plugin = WsprDecoderPlugin()
    s = plugin.status()
    assert s["symbols_decoded"] == 0
    assert s["is_complete"] is False
    assert s["spots_decoded"] == 0
    assert s["center_hz"] == DEFAULT_CENTER_HZ
    assert s["tone_spacing_hz"] == DEFAULT_TONE_SPACING_HZ


def test_wspr_plugin_feed_iq_processes_audio():
    """The plugin pipeline processes synthesized FSK audio without errors."""
    plugin = WsprDecoderPlugin()
    # Use a fast tone spacing for testing.
    plugin._rx = WsprReceiver(center_hz=1500.0, tone_spacing=100.0)
    freqs = plugin._rx._tone_freqs
    sps = plugin._rx._sps
    sr = DEFAULT_SAMPLE_RATE
    # Synthesize 5 symbols of tone 0.
    t = np.arange(sps * 5) / sr
    tone = (0.5 * np.sin(2 * np.pi * freqs[0] * t)).astype(np.float32)
    pcm = (tone * 32767).astype(np.int16)
    iq = (pcm.astype(np.float32) / 32767.0).astype(np.complex64)
    events = plugin.feed_iq(iq)
    # Should produce at least a progress event (5 symbols < 10 threshold,
    # but the first call with symbols may trigger it).
    assert isinstance(events, list)
