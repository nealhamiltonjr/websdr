"""Tests for the ATC voice activity detector — demod + plugin."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openwebrx_plus.plugins.atc import AtcDecoderPlugin  # noqa: E402
from openwebrx_plus.plugins.atc_demod import (  # noqa: E402
    DEFAULT_DEBOUNCE_S,
    DEFAULT_HANG_TIME_S,
    DEFAULT_RSSI_INTERVAL_S,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_SQUELCH_DBFS,
    AtcReceiver,
    AtcVoiceEvent,
)

# ============================================================================
# AtcVoiceEvent tests
# ============================================================================

def test_voice_event_to_dict():
    """to_dict returns the expected fields."""
    evt = AtcVoiceEvent(kind="voice_start", ts=100.0, rssi_dbfs=-20.5, frequency_hz=118000000)
    d = evt.to_dict()
    assert d["kind"] == "voice_start"
    assert d["ts"] == 100.0
    assert d["rssi_dbfs"] == -20.5
    assert d["frequency_hz"] == 118000000


# ============================================================================
# AtcReceiver tests
# ============================================================================

def test_atc_receiver_defaults():
    rx = AtcReceiver()
    assert rx.sample_rate == DEFAULT_SAMPLE_RATE
    assert rx.squelch_dbfs == DEFAULT_SQUELCH_DBFS
    assert rx.hang_time_s == DEFAULT_HANG_TIME_S
    assert rx.debounce_s == DEFAULT_DEBOUNCE_S
    assert rx.rssi_interval_s == DEFAULT_RSSI_INTERVAL_S
    assert not rx.is_active


def test_atc_receiver_validates():
    with pytest.raises(ValueError, match="sample_rate"):
        AtcReceiver(sample_rate=0)
    with pytest.raises(ValueError, match="squelch_dbfs"):
        AtcReceiver(squelch_dbfs=10)


def test_atc_receiver_empty():
    rx = AtcReceiver()
    assert rx.feed(np.array([], dtype=np.int16)) == []


def test_atc_receiver_reset():
    rx = AtcReceiver()
    rx.reset()
    assert not rx.is_active
    assert rx.last_rssi == -60.0


def _synth_tone(amplitude: float = 0.5, duration_s: float = 0.2, sr: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    """Synthesize a 1 kHz tone at the given amplitude."""
    n = int(sr * duration_s)
    t = np.arange(n) / sr
    return (amplitude * np.sin(2 * np.pi * 1000 * t) * 32767).astype(np.int16)


def test_atc_detects_voice_start():
    """Feeding loud audio triggers voice_start after the debounce period."""
    rx = AtcReceiver(squelch_dbfs=-40.0, debounce_s=0.05)
    # Feed 0.2 s of loud audio (well above debounce).
    audio = _synth_tone(amplitude=0.5, duration_s=0.2)
    events = rx.feed(audio)
    # Should have at least one voice_start event.
    starts = [e for e in events if e.kind == "voice_start"]
    assert len(starts) >= 1, f"expected voice_start, got {[e.kind for e in events]}"
    assert rx.is_active


def test_atc_detects_voice_end():
    """Feeding silence after voice triggers voice_end after hang time."""
    rx = AtcReceiver(squelch_dbfs=-40.0, debounce_s=0.05, hang_time_s=0.1)
    # Start with loud audio.
    loud = _synth_tone(amplitude=0.5, duration_s=0.2)
    rx.feed(loud)
    assert rx.is_active
    # Feed silence (0.2 s, longer than hang_time).
    silence = np.zeros(int(DEFAULT_SAMPLE_RATE * 0.2), dtype=np.int16)
    events = rx.feed(silence)
    ends = [e for e in events if e.kind == "voice_end"]
    assert len(ends) >= 1, f"expected voice_end, got {[e.kind for e in events]}"
    assert not rx.is_active


def test_atc_emits_rssi_periodically():
    """RSSI events are emitted at the configured interval."""
    rx = AtcReceiver(rssi_interval_s=0.01)  # 10ms for fast testing
    audio = _synth_tone(amplitude=0.1, duration_s=0.1)
    events = rx.feed(audio)
    rssi_events = [e for e in events if e.kind == "rssi"]
    assert len(rssi_events) >= 1, f"expected at least 1 rssi event, got {len(rssi_events)}"


def test_atc_squelch_stays_closed_below_threshold():
    """Quiet audio (below squelch) doesn't trigger voice_start."""
    rx = AtcReceiver(squelch_dbfs=-10.0, debounce_s=0.01)  # high threshold
    # Very quiet audio (amplitude 0.001 → ~-60 dBFS, well below -10 dBFS).
    audio = _synth_tone(amplitude=0.001, duration_s=0.2)
    events = rx.feed(audio)
    starts = [e for e in events if e.kind == "voice_start"]
    assert len(starts) == 0, f"expected no voice_start below squelch, got {len(starts)}"
    assert not rx.is_active


# ============================================================================
# Plugin tests
# ============================================================================

def test_atc_plugin_manifest():
    m = AtcDecoderPlugin.manifest
    assert m.name == "atc"
    assert m.tap_point == "rf_band"
    assert m.required_sample_rate == DEFAULT_SAMPLE_RATE
    assert "voice_start" in m.events
    assert "voice_end" in m.events
    assert "rssi" in m.events


def test_atc_plugin_status():
    plugin = AtcDecoderPlugin()
    s = plugin.status()
    assert s["is_active"] is False
    assert s["voice_starts"] == 0
    assert s["voice_ends"] == 0
    assert s["squelch_dbfs"] == DEFAULT_SQUELCH_DBFS


def test_atc_plugin_feed_iq():
    """The plugin pipeline processes synthesized audio."""
    plugin = AtcDecoderPlugin()
    # Feed loud audio to trigger voice_start.
    audio = _synth_tone(amplitude=0.5, duration_s=0.2)
    iq = (audio.astype(np.float32) / 32767.0).astype(np.complex64)
    events = plugin.feed_iq(iq)
    starts = [e for e in events if e["kind"] == "voice_start"]
    assert len(starts) >= 1, f"expected voice_start, got {[e['kind'] for e in events]}"
    assert plugin.status()["voice_starts"] >= 1
