"""Tests for the JT9 decoder — protocol, demod, plugin."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openwebrx_plus.plugins.jt9 import Jt9DecoderPlugin  # noqa: E402
from openwebrx_plus.plugins.jt9_demod import (  # noqa: E402
    DEFAULT_BASE_FREQ_HZ,
    DEFAULT_NUM_SYMBOLS,
    DEFAULT_NUM_TONES,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_TONE_SPACING_HZ,
    Jt9Receiver,
    _tone_freqs,
)
from openwebrx_plus.plugins.jt9_protocol import (  # noqa: E402
    bits_to_symbols,
    symbols_to_bits,
    unpack_payload,
)

# ============================================================================
# Protocol tests
# ============================================================================

def test_symbols_to_bits_round_trip():
    """symbols_to_bits + bits_to_symbols round-trips for 4-bit symbols."""
    symbols = [0, 1, 2, 3, 5, 8]
    bits = symbols_to_bits(symbols)
    assert len(bits) == 24  # 4 bits per symbol × 6
    reconverted = bits_to_symbols(bits)
    assert reconverted == symbols


def test_symbols_to_bits_msb_first():
    """Symbol 1 = [0,0,0,1], symbol 8 = [1,0,0,0]."""
    bits = symbols_to_bits([1, 8])
    assert bits == [0, 0, 0, 1, 1, 0, 0, 0]


def test_unpack_payload_returns_strings():
    """unpack_payload returns a 3-tuple of strings."""
    symbols = list(range(18))  # 18 symbols × 4 bits = 72 bits
    c1, c2, gr = unpack_payload(symbols)
    assert isinstance(c1, str)
    assert isinstance(c2, str)
    assert isinstance(gr, str)


# ============================================================================
# Demodulator tests
# ============================================================================

def test_tone_freqs_correct_count():
    """_tone_freqs returns 9 tones."""
    freqs = _tone_freqs(1000.0, 1.4648, 9)
    assert len(freqs) == 9


def test_tone_freqs_spacing():
    """Tones are evenly spaced."""
    freqs = _tone_freqs(1000.0, 1.4648, 9)
    for i in range(1, len(freqs)):
        diff = freqs[i] - freqs[i - 1]
        assert abs(diff - 1.4648) < 0.01


def test_jt9_receiver_defaults():
    rx = Jt9Receiver()
    assert rx.sample_rate == DEFAULT_SAMPLE_RATE
    assert rx.base_freq_hz == DEFAULT_BASE_FREQ_HZ
    assert rx.tone_spacing == DEFAULT_TONE_SPACING_HZ
    assert rx.num_tones == DEFAULT_NUM_TONES
    assert len(rx._tone_freqs) == 9


def test_jt9_receiver_validates():
    with pytest.raises(ValueError, match="sample_rate"):
        Jt9Receiver(sample_rate=0)
    with pytest.raises(ValueError, match="base_freq_hz"):
        Jt9Receiver(sample_rate=2000, base_freq_hz=1000)
    with pytest.raises(ValueError, match="tone_spacing"):
        Jt9Receiver(tone_spacing=0)
    with pytest.raises(ValueError, match="num_tones"):
        Jt9Receiver(num_tones=1)


def test_jt9_receiver_empty():
    rx = Jt9Receiver()
    assert rx.feed(np.array([], dtype=np.int16)) == []


def test_jt9_receiver_reset():
    rx = Jt9Receiver()
    rx.reset()
    assert rx.symbol_count == 0
    assert not rx.is_complete


def test_jt9_demod_single_tone():
    """Feeding one dominant tone produces that symbol."""
    rx = Jt9Receiver(base_freq_hz=1000.0, tone_spacing=100.0, num_tones=9)
    freqs = rx._tone_freqs
    sps = rx._sps
    sr = rx.sample_rate
    t = np.arange(sps * 3) / sr
    tone = (0.5 * np.sin(2 * np.pi * freqs[5] * t)).astype(np.float32)
    pcm = (tone * 32767).astype(np.int16)
    symbols = rx.feed(pcm)
    assert len(symbols) == 3
    for s in symbols:
        assert s == 5


def test_jt9_demod_tone_sequence():
    """Feeding a sequence of different tones produces the right symbols."""
    rx = Jt9Receiver(base_freq_hz=1000.0, tone_spacing=100.0, num_tones=9)
    freqs = rx._tone_freqs
    sps = rx._sps
    sr = rx.sample_rate
    expected = [0, 3, 6, 8]
    samples: list[float] = []
    for sym in expected:
        t = np.arange(sps) / sr
        samples.extend(0.5 * np.sin(2 * np.pi * freqs[sym] * t))
    audio = np.array(samples, dtype=np.float32)
    pcm = (audio * 32767).astype(np.int16)
    symbols = rx.feed(pcm)
    assert len(symbols) == 4
    matches = sum(1 for g, e in zip(symbols, expected, strict=False) if g == e)
    assert matches >= 3


def test_jt9_is_complete_after_85_symbols():
    rx = Jt9Receiver(base_freq_hz=1000.0, tone_spacing=100.0, num_tones=9)
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

def test_jt9_plugin_manifest():
    m = Jt9DecoderPlugin.manifest
    assert m.name == "jt9"
    assert m.tap_point == "rf_band"
    assert m.required_sample_rate == DEFAULT_SAMPLE_RATE
    assert "message" in m.events
    assert "progress" in m.events


def test_jt9_plugin_status():
    plugin = Jt9DecoderPlugin()
    s = plugin.status()
    assert s["symbols_decoded"] == 0
    assert s["is_complete"] is False
    assert s["messages_decoded"] == 0
    assert s["base_freq_hz"] == DEFAULT_BASE_FREQ_HZ
    assert s["num_tones"] == DEFAULT_NUM_TONES


def test_jt9_plugin_feed_iq():
    plugin = Jt9DecoderPlugin()
    plugin._rx = Jt9Receiver(base_freq_hz=1000.0, tone_spacing=100.0, num_tones=9)
    freqs = plugin._rx._tone_freqs
    sps = plugin._rx._sps
    sr = DEFAULT_SAMPLE_RATE
    t = np.arange(sps * 5) / sr
    tone = (0.5 * np.sin(2 * np.pi * freqs[0] * t)).astype(np.float32)
    pcm = (tone * 32767).astype(np.int16)
    iq = (pcm.astype(np.float32) / 32767.0).astype(np.complex64)
    events = plugin.feed_iq(iq)
    assert isinstance(events, list)
