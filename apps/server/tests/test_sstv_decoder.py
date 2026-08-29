"""Tests for the SSTV decoder — demod core + plugin pipeline.

Tests synthesize SSTV audio (VIS leader + VIS code + scanlines with
frequency-mapped pixel intensities) and verify the demodulator correctly
detects the mode and decodes image data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openwebrx_plus.plugins.sstv import SstvDecoderPlugin  # noqa: E402
from openwebrx_plus.plugins.sstv_demod import (  # noqa: E402
    _MODE_PARAMS,
    DEFAULT_SAMPLE_RATE,
    FREQ_BLACK,
    FREQ_LEADER,
    FREQ_SYNC,
    FREQ_WHITE,
    SstvImage,
    SstvMode,
    SstvReceiver,
    _estimate_freq,
)

# ============================================================================
# Frequency estimation tests
# ============================================================================

def test_estimate_freq_detects_1200hz():
    """_estimate_freq correctly identifies a 1200 Hz tone."""
    sr = DEFAULT_SAMPLE_RATE
    t = np.arange(800) / sr
    tone = (0.5 * np.sin(2 * np.pi * 1200 * t)).astype(np.float32)
    freq = _estimate_freq(tone, sr)
    assert abs(freq - 1200) < 50, f"expected ~1200 Hz, got {freq:.1f}"


def test_estimate_freq_detects_1900hz():
    """_estimate_freq correctly identifies a 1900 Hz tone."""
    sr = DEFAULT_SAMPLE_RATE
    t = np.arange(800) / sr
    tone = (0.5 * np.sin(2 * np.pi * 1900 * t)).astype(np.float32)
    freq = _estimate_freq(tone, sr)
    assert abs(freq - 1900) < 50, f"expected ~1900 Hz, got {freq:.1f}"


def test_estimate_freq_empty_input():
    """Empty input returns 0.0."""
    assert _estimate_freq(np.array([], dtype=np.float32), DEFAULT_SAMPLE_RATE) == 0.0


# ============================================================================
# SstvReceiver tests
# ============================================================================

def test_sstv_receiver_constructor():
    """Default constructor produces standard params."""
    rx = SstvReceiver()
    assert rx.sample_rate == DEFAULT_SAMPLE_RATE
    assert rx.state == "idle"
    assert rx.current_mode is None
    assert rx.scanline_count == 0


def test_sstv_receiver_rejects_invalid_sample_rate():
    """Invalid sample_rate raises ValueError."""
    with pytest.raises(ValueError, match="sample_rate"):
        SstvReceiver(sample_rate=0)


def test_sstv_receiver_empty_input():
    """Empty input returns empty list."""
    rx = SstvReceiver()
    assert rx.feed(np.array([], dtype=np.int16)) == []


def test_sstv_receiver_reset_clears_state():
    """reset() returns to idle state."""
    rx = SstvReceiver()
    rx.reset()
    assert rx.state == "idle"
    assert rx.current_mode is None
    assert rx.scanline_count == 0


# ============================================================================
# SSTV audio synthesis + round-trip tests
# ============================================================================

def _synth_tone(freq: float, duration_ms: float, sr: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    """Synthesize a pure tone at the given frequency for duration_ms."""
    n = int(sr * duration_ms / 1000)
    t = np.arange(n) / sr
    return (0.5 * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)


def _synth_vis_leader(sr: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    """Synthesize the VIS leader: 1200 Hz for 100 ms + 1900 Hz for 100 ms."""
    return np.concatenate([
        _synth_tone(FREQ_LEADER, 100, sr),
        _synth_tone(FREQ_SYNC, 100, sr),
    ])


def _synth_vis_code(code: int, sr: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    """Synthesize the 10-bit VIS code for a given mode.

    Format: start bit (1200 Hz, 30 ms) + 7 data bits LSB-first
    (1100 Hz = 1, 1300 Hz = 0, 30 ms each) + parity bit + stop bit (1200 Hz).
    """
    bits = [0]  # start bit (1200 Hz)
    for i in range(7):
        bits.append((code >> i) & 1)
    # Parity bit (even parity over the 7 data bits).
    parity = sum(bits[1:8]) % 2
    bits.append(parity)
    bits.append(0)  # stop bit (1200 Hz)
    samples: list[np.ndarray] = []
    for bit in bits:
        freq = 1100 if bit == 1 else 1300
        # But start/stop use 1200 Hz.
        if bit == 0 and (len(samples) == 0 or len(bits) - len(samples) - 1 == 0):
            freq = 1200  # start or stop bit
        samples.append(_synth_tone(freq, 30, sr))
    return np.concatenate(samples)


def _synth_scanline(
    width: int,
    pixel_us: float,
    sync_ms: float = 9.0,
    porch_ms: float = 1.5,
    sr: int = DEFAULT_SAMPLE_RATE,
    intensity_pattern: np.ndarray | None = None,
) -> np.ndarray:
    """Synthesize one SSTV scanline.

    The scanline: sync pulse (1900 Hz, sync_ms) + porch (1900 Hz, porch_ms)
    + 3 color channels (R, G, B), each `width` pixels at `pixel_us` per pixel.

    intensity_pattern: optional (width, 3) array of intensities 0-255.
    If None, all pixels are mid-gray (128).
    """
    if intensity_pattern is None:
        intensity_pattern = np.full((width, 3), 128, dtype=np.uint8)
    samples: list[np.ndarray] = []
    # Sync pulse.
    samples.append(_synth_tone(FREQ_SYNC, sync_ms, sr))
    # Porch.
    samples.append(_synth_tone(FREQ_SYNC, porch_ms, sr))
    # 3 color channels.
    for ch in range(3):
        for x in range(width):
            intensity = intensity_pattern[x, ch]
            # Map intensity (0-255) → frequency (1500-2300 Hz).
            freq = FREQ_BLACK + (intensity / 255.0) * (FREQ_WHITE - FREQ_BLACK)
            samples.append(_synth_tone(freq, pixel_us / 1000.0, sr))
    return np.concatenate(samples)


def test_sstv_detects_vis_leader():
    """The receiver detects the VIS leader and transitions to vis state."""
    rx = SstvReceiver()
    leader = _synth_vis_leader()
    # Feed more than enough samples to trigger detection.
    audio = np.concatenate([leader, _synth_vis_code(0x3C)])  # Scottie 1
    rx.feed(audio)
    # After feeding the leader + VIS code, the receiver should have
    # transitioned past idle (either to vis or scanning).
    assert rx.state != "idle" or rx.current_mode is not None, (
        f"expected state != idle or mode detected, got state={rx.state}, mode={rx.current_mode}"
    )


def test_sstv_decodes_scottie_1_mode():
    """The receiver correctly identifies Scottie 1 from its VIS code."""
    rx = SstvReceiver()
    # Scottie 1 VIS code = 0x3C (60).
    audio = np.concatenate([_synth_vis_leader(), _synth_vis_code(0x3C)])
    rx.feed(audio)
    # The receiver should be in scanning state with Scottie 1 mode.
    assert rx.state == "scanning", f"expected scanning, got {rx.state}"
    assert rx.current_mode == SstvMode.SCOTTIE_1, (
        f"expected SCOTTIE_1, got {rx.current_mode}"
    )


def test_sstv_decodes_martin_1_mode():
    """The receiver correctly identifies Martin 1 from its VIS code."""
    rx = SstvReceiver()
    # Martin 1 VIS code = 0x2C (44).
    audio = np.concatenate([_synth_vis_leader(), _synth_vis_code(0x2C)])
    rx.feed(audio)
    assert rx.state == "scanning", f"expected scanning, got {rx.state}"
    assert rx.current_mode == SstvMode.MARTIN_1


def test_sstv_rejects_unknown_vis_code():
    """An unknown VIS code returns the receiver to idle."""
    rx = SstvReceiver()
    # VIS code 0x7F (127) is not a valid SSTV mode.
    audio = np.concatenate([_synth_vis_leader(), _synth_vis_code(0x7F)])
    rx.feed(audio)
    # Should be back to idle (unknown mode rejected).
    assert rx.state == "idle"


def test_sstv_decodes_partial_scanlines():
    """After VIS detection, feeding scanline audio produces scanline progress."""
    rx = SstvReceiver()
    # Use Scottie 2 (faster scan) for shorter test audio.
    audio = np.concatenate([_synth_vis_leader(), _synth_vis_code(0x38)])
    rx.feed(audio)
    assert rx.current_mode == SstvMode.SCOTTIE_2
    # Synthesize one scanline and feed it.
    params = _MODE_PARAMS[SstvMode.SCOTTIE_2]
    width = int(params["width"])  # type: ignore[index]
    pixel_us = float(params["pixel_us"])  # type: ignore[index]
    scanline = _synth_scanline(width=width, pixel_us=pixel_us, sync_ms=9.0, porch_ms=1.5)
    rx.feed(scanline)
    assert rx.scanline_count >= 1, f"expected at least 1 scanline, got {rx.scanline_count}"


# ============================================================================
# Plugin tests
# ============================================================================

def test_sstv_plugin_manifest():
    """Plugin manifest has the right fields."""
    m = SstvDecoderPlugin.manifest
    assert m.name == "sstv"
    assert m.tap_point == "rf_band"
    assert m.required_sample_rate == DEFAULT_SAMPLE_RATE
    assert "image" in m.events
    assert "scanline" in m.events
    assert "mode" in m.events


def test_sstv_plugin_feed_iq_processes_audio():
    """The plugin pipeline processes synthesized SSTV audio without errors."""
    plugin = SstvDecoderPlugin()
    # Synthesize VIS leader + code + one scanline (Scottie 2 for speed).
    audio = np.concatenate([
        _synth_vis_leader(),
        _synth_vis_code(0x38),
        _synth_scanline(width=320, pixel_us=276, sync_ms=9.0, porch_ms=1.5),
    ])
    iq = (audio.astype(np.float32) / 32767.0).astype(np.complex64)
    events = plugin.feed_iq(iq)
    # Should have at least a mode event (on VIS detection).
    kinds = [e["kind"] for e in events]
    assert "mode" in kinds or "scanline" in kinds, (
        f"expected mode or scanline events, got {kinds}"
    )


def test_sstv_plugin_status_reports_state():
    """status() returns the current demod state."""
    plugin = SstvDecoderPlugin()
    s = plugin.status()
    assert s["state"] == "idle"
    assert s["mode"] is None
    assert s["scanlines_decoded"] == 0
    assert s["images_decoded"] == 0


def test_sstv_plugin_emits_mode_event_on_vis_detection():
    """The plugin emits a 'mode' event when the VIS code is detected."""
    plugin = SstvDecoderPlugin()
    audio = np.concatenate([_synth_vis_leader(), _synth_vis_code(0x3C)])
    iq = (audio.astype(np.float32) / 32767.0).astype(np.complex64)
    events = plugin.feed_iq(iq)
    mode_events = [e for e in events if e["kind"] == "mode"]
    assert len(mode_events) >= 1, f"expected at least 1 mode event, got {len(mode_events)}"
    assert mode_events[0]["mode"] == "SCOTTIE_1"
    assert mode_events[0]["vis_code"] == 0x3C


# ============================================================================
# SstvImage tests
# ============================================================================

def test_sstv_image_dataclass():
    """SstvImage holds the expected fields."""
    pixels = np.zeros((10, 20, 3), dtype=np.uint8)
    img = SstvImage(
        mode=SstvMode.SCOTTIE_1,
        width=20,
        height=10,
        pixels=pixels,
    )
    assert img.mode == SstvMode.SCOTTIE_1
    assert img.width == 20
    assert img.height == 10
    assert img.pixels.shape == (10, 20, 3)
    assert img.pixels.dtype == np.uint8
