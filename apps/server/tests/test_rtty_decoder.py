"""Tests for the RTTY decoder — demod core, ITA2 protocol, plugin.

Tests use synthesized FSK audio (mark/space tones at 2125/2295 Hz, 45.45 baud)
to verify the round-trip: text → ITA2 codes → FSK audio → demod → codes → text.
No live RTTY signals needed — the generator + demod are both pure-numpy.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

# Ensure the server package is importable when run via run-server-tests.sh
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openwebrx_plus.plugins.rtty import RttyDecoderPlugin  # noqa: E402
from openwebrx_plus.plugins.rtty_demod import (  # noqa: E402
    DEFAULT_BAUD,
    DEFAULT_MARK_HZ,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_SPACE_HZ,
    RttyReceiver,
    _goertzel_coeff,
    _goertzel_mag,
)
from openwebrx_plus.plugins.rtty_protocol import Ita2Decoder  # noqa: E402

# ============================================================================
# ITA2 / Baudot decoder tests
# ============================================================================

def test_ita2_decode_letters_basic():
    """Basic letter codes decode correctly in letters mode (default)."""
    d = Ita2Decoder()
    # ITA2: E=1, A=3, T=16, space=4
    assert d.decode(1) == "E"
    assert d.decode(3) == "A"
    assert d.decode(16) == "T"
    assert d.decode(4) == " "


def test_ita2_decode_figures_after_shift():
    """After FIGS shift (code 27), figure codes decode as digits."""
    d = Ita2Decoder()
    assert d.decode(27) == ""  # FIGS shift — no char emitted
    # In figures mode: code 1 = "3", code 3 = "-", code 16 = "5"
    assert d.decode(1) == "3"
    assert d.decode(3) == "-"
    assert d.decode(16) == "5"


def test_ita2_decode_back_to_letters():
    """LTRS shift (code 31) returns to letter mode."""
    d = Ita2Decoder()
    d.decode(27)  # FIGS
    assert d.decode(1) == "3"  # figure
    d.decode(31)  # LTRS
    assert d.decode(1) == "E"  # letter


def test_ita2_decode_cr_lf():
    """CR (code 8) and LF (code 2) produce their respective characters."""
    d = Ita2Decoder()
    assert d.decode(8) == "\r"
    assert d.decode(2) == "\n"


def test_ita2_decode_null_and_shifts_emit_empty():
    """Null (0), LTRS (31), FIGS (27) emit empty strings."""
    d = Ita2Decoder()
    assert d.decode(0) == ""
    assert d.decode(31) == ""
    assert d.decode(27) == ""


def test_ita2_decode_many_batch():
    """decode_many concatenates all characters."""
    d = Ita2Decoder()
    # "CQ " in ITA2 letters: C=14, Q=23, space=4
    codes = [14, 23, 4]
    assert d.decode_many(codes) == "CQ "


def test_ita2_decode_invalid_code_ignored():
    """Codes outside 0-31 are silently ignored."""
    d = Ita2Decoder()
    assert d.decode(32) == ""
    assert d.decode(-1) == ""
    assert d.decode(100) == ""


def test_ita2_reset_returns_to_letters():
    """reset() restores letters mode."""
    d = Ita2Decoder()
    d.decode(27)  # FIGS
    assert not d.in_letters_mode
    d.reset()
    assert d.in_letters_mode
    assert d.decode(1) == "E"


# ============================================================================
# Goertzel filter tests
# ============================================================================

def test_goertzel_detects_mark_tone():
    """Goertzel at 2125 Hz should return high magnitude for a 2125 Hz tone."""
    sr = DEFAULT_SAMPLE_RATE
    t = np.arange(176) / sr
    tone = (0.5 * np.sin(2 * np.pi * DEFAULT_MARK_HZ * t)).astype(np.float32)
    coeff, cos_t, sin_t = _goertzel_coeff(DEFAULT_MARK_HZ, sr)
    mag = _goertzel_mag(tone, coeff, cos_t, sin_t)
    assert mag > 0.1, f"mark tone should have high magnitude, got {mag}"


def test_goertzel_rejects_off_frequency():
    """Goertzel at 2125 Hz should return low magnitude for a 2295 Hz tone."""
    sr = DEFAULT_SAMPLE_RATE
    t = np.arange(176) / sr
    # Generate a SPACE tone (2295 Hz) and measure with the MARK filter.
    tone = (0.5 * np.sin(2 * np.pi * DEFAULT_SPACE_HZ * t)).astype(np.float32)
    coeff, cos_t, sin_t = _goertzel_coeff(DEFAULT_MARK_HZ, sr)
    mag_mark = _goertzel_mag(tone, coeff, cos_t, sin_t)
    # Now measure with the SPACE filter — should be much higher.
    coeff_s, cos_s, sin_s = _goertzel_coeff(DEFAULT_SPACE_HZ, sr)
    mag_space = _goertzel_mag(tone, coeff_s, cos_s, sin_s)
    assert mag_space > mag_mark * 2, (
        f"space filter should dominate for space tone: "
        f"mark_mag={mag_mark:.4f}, space_mag={mag_space:.4f}"
    )


# ============================================================================
# RttyReceiver demodulator tests
# ============================================================================

def test_rtty_receiver_constructor_defaults():
    """Default constructor produces standard ham RTTY params."""
    rx = RttyReceiver()
    assert rx.sample_rate == DEFAULT_SAMPLE_RATE
    assert rx.mark_hz == DEFAULT_MARK_HZ
    assert rx.space_hz == DEFAULT_SPACE_HZ
    assert rx.baud == DEFAULT_BAUD
    assert rx._spb > 0


def test_rtty_receiver_constructor_validates():
    """Invalid params raise ValueError."""
    with pytest.raises(ValueError, match="sample_rate"):
        RttyReceiver(sample_rate=0)
    with pytest.raises(ValueError, match="mark_hz"):
        RttyReceiver(mark_hz=0)
    with pytest.raises(ValueError, match="baud"):
        RttyReceiver(baud=0)
    with pytest.raises(ValueError, match="differ"):
        RttyReceiver(mark_hz=2125.0, space_hz=2125.5)  # < 1 Hz diff


def test_rtty_receiver_rejects_aliasing_freq():
    """Mark/space above Nyquist should raise."""
    with pytest.raises(ValueError, match="sample_rate/2"):
        RttyReceiver(sample_rate=4000, mark_hz=2125, space_hz=2295)


def _synthesize_rtty_audio(
    codes: list[int],
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    mark_hz: float = DEFAULT_MARK_HZ,
    space_hz: float = DEFAULT_SPACE_HZ,
    baud: float = DEFAULT_BAUD,
) -> np.ndarray:
    """Synthesize RTTY FSK audio from a list of ITA2 5-bit codes.

    Frame format: 1 start bit (0=space) + 5 data bits LSB first + 1 stop bit (1=mark).
    """
    spb = int(round(sample_rate / baud))
    samples: list[float] = []
    for code in codes:
        # Build the 7-bit frame: start(0) + 5 data LSB-first + stop(1).
        bits = [0]  # start bit
        for b in range(5):
            bits.append((code >> b) & 1)
        bits.append(1)  # stop bit
        for bit in bits:
            freq = mark_hz if bit == 1 else space_hz
            t = np.arange(spb) / sample_rate
            tone = 0.5 * np.sin(2 * np.pi * freq * t)
            samples.extend(tone.tolist())
    # Add a few mark bits of idle before and after.
    idle = int(round(spb * 2))
    t_idle = np.arange(idle) / sample_rate
    idle_tone = 0.5 * np.sin(2 * np.pi * mark_hz * t_idle)
    audio = np.concatenate([idle_tone, np.array(samples, dtype=np.float32), idle_tone])
    return (audio * 32767.0).astype(np.int16)


def test_rtty_demod_round_trip_single_char():
    """Synthesize one ITA2 code, demod it, verify the code comes back."""
    rx = RttyReceiver()
    # Code 14 = 'C' in letters mode.
    audio = _synthesize_rtty_audio([14])
    codes = rx.feed(audio)
    assert 14 in codes, f"expected code 14 in decoded codes, got {codes}"


def test_rtty_demod_round_trip_multiple_chars():
    """Synthesize 'CQ' (codes 14, 23) and verify both decode."""
    rx = RttyReceiver()
    audio = _synthesize_rtty_audio([14, 23])  # C Q
    codes = rx.feed(audio)
    assert 14 in codes, f"expected code 14 (C), got {codes}"
    assert 23 in codes, f"expected code 23 (Q), got {codes}"


def test_rtty_demod_round_trip_with_shifts():
    """Synthesize FIGS+code1+LTRS+code1 and verify figure then letter."""
    # FIGS(27) + code1 → "3", LTRS(31) + code1 → "E"
    rx = RttyReceiver()
    audio = _synthesize_rtty_audio([27, 1, 31, 1])
    codes = rx.feed(audio)
    assert 27 in codes, f"expected FIGS code 27, got {codes}"
    assert 1 in codes, f"expected code 1 (at least once), got {codes}"


def test_rtty_demod_reset_clears_state():
    """reset() clears the frame state."""
    rx = RttyReceiver()
    audio = _synthesize_rtty_audio([14])
    rx.feed(audio)
    rx.reset()
    assert not rx._in_frame
    assert rx._last_bit == 1  # idle


def test_rtty_demod_empty_input():
    """Empty input returns empty list."""
    rx = RttyReceiver()
    assert rx.feed(np.array([], dtype=np.int16)) == []


# ============================================================================
# Plugin tests
# ============================================================================

def test_rtty_plugin_manifest():
    """Plugin manifest has the right fields."""
    m = RttyDecoderPlugin.manifest
    assert m.name == "rtty"
    assert m.tap_point == "rf_band"
    assert m.required_sample_rate == DEFAULT_SAMPLE_RATE
    assert "frame" in m.events
    assert "text" in m.events


def test_rtty_plugin_feed_iq_decodes_synthesized_audio():
    """The full plugin pipeline: synthesized audio → decoder events."""
    plugin = RttyDecoderPlugin()
    # Synthesize 'CQ' in letters mode: C=14, Q=23.
    audio = _synthesize_rtty_audio([14, 23])
    # Convert to complex64 (what the session feeds). Use astype (cast)
    # not view (reinterpret) — astype fills imaginary with 0, keeping
    # the same sample count; view would halve the length and alias.
    iq = (audio.astype(np.float32) / 32767.0).astype(np.complex64)
    events = plugin.feed_iq(iq)
    # Should have at least 2 frame events (one per char) + possibly a text snapshot.
    frame_events = [e for e in events if e["kind"] == "frame"]
    chars = [e["char"] for e in frame_events]
    assert "C" in chars, f"expected 'C' in decoded chars, got {chars}"
    assert "Q" in chars, f"expected 'Q' in decoded chars, got {chars}"


def test_rtty_plugin_status_reports_params():
    """status() returns the current demod params."""
    plugin = RttyDecoderPlugin()
    s = plugin.status()
    assert s["mark_hz"] == DEFAULT_MARK_HZ
    assert s["space_hz"] == DEFAULT_SPACE_HZ
    assert s["baud"] == DEFAULT_BAUD
    assert s["in_letters_mode"] is True  # default
    assert s["text_length"] == 0
