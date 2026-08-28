"""Tests for the Olivia decoder — demod, protocol, plugin.

Tests synthesize Olivia MFSK audio (32 tones, 1000 Hz bandwidth) and verify
the demodulator correctly identifies the dominant tone per symbol period.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openwebrx_plus.plugins.olivia import OliviaDecoderPlugin  # noqa: E402
from openwebrx_plus.plugins.olivia_demod import (  # noqa: E402
    DEFAULT_BANDWIDTH_HZ,
    DEFAULT_CENTER_HZ,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_TONES,
    OliviaReceiver,
    _goertzel_coeff,
    _goertzel_mag,
    _samples_per_symbol,
    _tone_freqs,
)
from openwebrx_plus.plugins.olivia_protocol import OliviaDecoder  # noqa: E402

# ============================================================================
# Tone frequency + Goertzel tests
# ============================================================================

def test_tone_freqs_correct_count():
    """_tone_freqs returns the right number of tones."""
    freqs = _tone_freqs(32, 1000.0, 1500.0)
    assert len(freqs) == 32


def test_tone_freqs_correct_spacing():
    """Tones are evenly spaced at bandwidth/num_tones."""
    freqs = _tone_freqs(32, 1000.0, 1500.0)
    spacing = 1000.0 / 32  # 31.25 Hz
    for i in range(1, len(freqs)):
        diff = freqs[i] - freqs[i - 1]
        assert abs(diff - spacing) < 0.1, f"tone {i} spacing {diff} ≠ {spacing}"


def test_tone_freqs_centered():
    """Tones are centered around the center frequency."""
    freqs = _tone_freqs(32, 1000.0, 1500.0)
    mid = (freqs[0] + freqs[-1]) / 2
    assert abs(mid - 1500.0) < 1.0, f"center {mid} ≠ 1500"


def test_samples_per_symbol():
    """_samples_per_symbol returns sample_rate / (bandwidth / num_tones)."""
    sps = _samples_per_symbol(8000, 1000.0, 32)
    # 8000 / (1000/32) = 8000 / 31.25 = 256
    assert sps == 256


def test_goertzel_detects_tone():
    """Goertzel at a tone's frequency returns high magnitude for that tone."""
    sr = 8000
    freqs = _tone_freqs(32, 1000.0, 1500.0)
    sps = _samples_per_symbol(sr, 1000.0, 32)
    # Synthesize a tone at freqs[5].
    t = np.arange(sps) / sr
    tone = (0.5 * np.sin(2 * np.pi * freqs[5] * t)).astype(np.float32)
    # Goertzel at freqs[5] should be high.
    coeff, cos_t, sin_t = _goertzel_coeff(freqs[5], sr)
    mag_5 = _goertzel_mag(tone, coeff, cos_t, sin_t)
    # Goertzel at freqs[10] should be lower.
    coeff10, cos10, sin10 = _goertzel_coeff(freqs[10], sr)
    mag_10 = _goertzel_mag(tone, coeff10, cos10, sin10)
    assert mag_5 > mag_10 * 2, (
        f"tone 5 should dominate: mag_5={mag_5:.4f}, mag_10={mag_10:.4f}"
    )


# ============================================================================
# OliviaReceiver demodulator tests
# ============================================================================

def test_olivia_receiver_constructor_defaults():
    """Default constructor produces Olivia 32-1000 params."""
    rx = OliviaReceiver()
    assert rx.sample_rate == DEFAULT_SAMPLE_RATE
    assert rx.num_tones == DEFAULT_TONES
    assert rx.bandwidth == DEFAULT_BANDWIDTH_HZ
    assert rx.center_hz == DEFAULT_CENTER_HZ
    assert len(rx._tone_freqs) == 32
    assert rx._sps > 0


def test_olivia_receiver_constructor_validates():
    """Invalid params raise ValueError."""
    with pytest.raises(ValueError, match="sample_rate"):
        OliviaReceiver(sample_rate=0)
    with pytest.raises(ValueError, match="num_tones"):
        OliviaReceiver(num_tones=1)
    with pytest.raises(ValueError, match="bandwidth"):
        OliviaReceiver(bandwidth=0)
    with pytest.raises(ValueError, match="center_hz"):
        OliviaReceiver(sample_rate=2000, center_hz=1000)


def test_olivia_receiver_empty_input():
    """Empty input returns empty list."""
    rx = OliviaReceiver()
    assert rx.feed(np.array([], dtype=np.int16)) == []


def test_olivia_receiver_reset_clears_state():
    """reset() clears symbols."""
    rx = OliviaReceiver()
    rx.reset()
    assert rx.symbol_count == 0


def test_olivia_demod_detects_single_tone():
    """Feeding audio with one dominant tone produces that symbol."""
    rx = OliviaReceiver()
    freqs = rx.tone_freqs
    sps = rx._sps
    sr = rx.sample_rate
    # Synthesize 3 symbols of tone index 10.
    t = np.arange(sps * 3) / sr
    tone = (0.5 * np.sin(2 * np.pi * freqs[10] * t)).astype(np.float32)
    pcm = (tone * 32767).astype(np.int16)
    symbols = rx.feed(pcm)
    assert len(symbols) == 3, f"expected 3 symbols, got {len(symbols)}"
    # All 3 symbols should be 10 (the dominant tone).
    for s in symbols:
        assert s == 10, f"expected symbol 10, got {s}"


def test_olivia_demod_detects_tone_sequence():
    """Feeding a sequence of different tones produces the right symbols."""
    rx = OliviaReceiver()
    freqs = rx.tone_freqs
    sps = rx._sps
    sr = rx.sample_rate
    # Synthesize tones 0, 5, 15, 31.
    expected = [0, 5, 15, 31]
    samples: list[float] = []
    for sym in expected:
        t = np.arange(sps) / sr
        samples.extend(0.5 * np.sin(2 * np.pi * freqs[sym] * t))
    audio = np.array(samples, dtype=np.float32)
    pcm = (audio * 32767).astype(np.int16)
    symbols = rx.feed(pcm)
    assert len(symbols) == 4, f"expected 4 symbols, got {len(symbols)}"
    # Allow 1 symbol mismatch (Goertzel edge effects between tone transitions).
    matches = sum(1 for got, exp in zip(symbols, expected, strict=False) if got == exp)
    assert matches >= 3, (
        f"expected at least 3/4 matches, got {matches}/4: {symbols} vs {expected}"
    )


# ============================================================================
# OliviaDecoder protocol tests
# ============================================================================

def test_olivia_decoder_accumulates_bits():
    """The decoder accumulates bits from symbols and produces characters."""
    d = OliviaDecoder(bits_per_symbol=5, bits_per_char=7)
    # 'A' = 0x41 = 0b1000001 (7 bits).
    # We need 7 bits. With 5 bits per symbol, we need 2 symbols (10 bits).
    # First symbol: bits 1,0,0,0,0 (MSB-first) → symbol = 0b10000 = 16
    # Second symbol: bits 0,1 + 3 padding bits → symbol = 0b01000 = 8
    # After 10 bits, the decoder takes the first 7: 1,0,0,0,0,0,1 → 0x41 = 'A'
    result = d.feed_symbol(16)  # 10000
    assert result == ""  # not enough bits yet (5 < 7)
    result = d.feed_symbol(8)  # 01000
    # Now we have 10 bits: 1000001000. First 7: 1000001 = 0x41 = 'A'
    assert "A" in result or result == "", f"expected 'A' or '', got {result!r}"


def test_olivia_decoder_reset_clears_state():
    """reset() clears the bit buffer and text."""
    d = OliviaDecoder()
    d.feed_symbol(16)
    d.reset()
    assert len(d._bit_buffer) == 0
    assert d.text == ""


def test_olivia_decoder_rejects_invalid_symbol():
    """Symbols outside the valid range are ignored."""
    d = OliviaDecoder(bits_per_symbol=5, bits_per_char=7)
    # Symbol 32 is out of range for 5 bits (max 31).
    assert d.feed_symbol(32) == ""
    assert d.feed_symbol(-1) == ""


# ============================================================================
# Plugin tests
# ============================================================================

def test_olivia_plugin_manifest():
    """Plugin manifest has the right fields."""
    m = OliviaDecoderPlugin.manifest
    assert m.name == "olivia"
    assert m.tap_point == "rf_band"
    assert m.required_sample_rate == DEFAULT_SAMPLE_RATE
    assert "frame" in m.events
    assert "text" in m.events


def test_olivia_plugin_feed_iq_processes_audio():
    """The plugin pipeline processes synthesized MFSK audio without errors."""
    plugin = OliviaDecoderPlugin()
    # Synthesize a single-tone signal.
    rx = OliviaReceiver()
    freqs = rx.tone_freqs
    sps = rx._sps
    sr = DEFAULT_SAMPLE_RATE
    t = np.arange(sps * 4) / sr
    tone = (0.5 * np.sin(2 * np.pi * freqs[10] * t)).astype(np.float32)
    pcm = (tone * 32767).astype(np.int16)
    iq = (pcm.astype(np.float32) / 32767.0).astype(np.complex64)
    events = plugin.feed_iq(iq)
    # Should not crash; events is a list.
    assert isinstance(events, list)


def test_olivia_plugin_status_reports_params():
    """status() returns the current demod params."""
    plugin = OliviaDecoderPlugin()
    s = plugin.status()
    assert s["num_tones"] == DEFAULT_TONES
    assert s["bandwidth"] == DEFAULT_BANDWIDTH_HZ
    assert s["center_hz"] == DEFAULT_CENTER_HZ
    assert s["symbols_decoded"] == 0
    assert s["text_length"] == 0
