"""SSTV (Slow-Scan Television) decoder plugin (ADR-003 family #8).

Mirrors :mod:`.rtty` and :mod:`.psk31` — wraps the pure-Python FM-based
SSTV demodulator (:mod:`.sstv_demod`) and emits decoded image frames as
decoder events.

Tap point: rf_band (audio-band decoders need the demodulated audio stream).
The session's AudioChain pushes complex-float IQ; the plugin treats the I
component as the audio-band signal (the SSTV FM tones live in the audio
band already).

The plugin emits:
  * "image" events when a complete image is decoded (with base64-encoded
    PNG data for the frontend to render).
  * "scanline" events periodically during decoding (progress indicator).
  * "mode" events when the VIS code is detected (announces the SSTV mode).

Sample-rate contract: accepts any positive rate; defaults to 8000 Hz.
"""

from __future__ import annotations

import base64
import time
from typing import Any, ClassVar

import numpy as np

from .base import DecoderManifest, DecoderPlugin
from .registry import DecoderRegistry
from .sstv_demod import DEFAULT_SAMPLE_RATE, SstvImage, SstvMode, SstvReceiver


@DecoderRegistry.register
class SstvDecoderPlugin(DecoderPlugin):
    """SSTV decoder: int16 PCM → FM demod → image frames."""

    manifest: ClassVar[DecoderManifest] = DecoderManifest(
        name="sstv",
        version="0.1.0",
        label="SSTV (Slow-Scan Television)",
        tap_point="rf_band",
        description=(
            "SSTV image decoder — FM frequency-to-pixel mapping "
            "(1500 Hz = black, 2300 Hz = white) + VIS mode detection "
            "+ scanline assembly. Supports Scottie 1/2, Martin 1/2, "
            "Robot 36 (320×256 or 320×240). Emits 'image' events with "
            "base64-encoded PNG data, 'scanline' progress events, and "
            "'mode' events on VIS detection. Defaults: 8000 Hz sample rate."
        ),
        required_sample_rate=DEFAULT_SAMPLE_RATE,
        events=("image", "scanline", "mode"),
    )

    _SCANLINE_EVENT_INTERVAL = 10  # emit a scanline event every N scanlines

    def __init__(self) -> None:
        self._rx = SstvReceiver()
        self._last_scanline_event = 0
        self._image_count = 0

    # -- DecoderPlugin contract ---------------------------------------------

    def feed_iq(self, iq: np.ndarray) -> list[dict[str, Any]]:
        """Feed IQ (interpreted as int16 PCM for the audio-band path)."""
        events: list[dict[str, Any]] = []
        now = time.time()
        if iq.dtype == np.complex64:
            pcm = iq.real.astype(np.float32)
            pcm = (pcm * 32767.0).astype("<i2")
        else:
            pcm = iq.astype("<i2", copy=False)
        # Emit a "mode" event when the VIS code is first detected.
        prev_state = self._rx.state
        images = self._rx.feed(pcm)
        # Check for mode transition (idle → scanning with a mode).
        if prev_state != self._rx.state and self._rx.state == "scanning" and self._rx.current_mode is not None:
            events.append(self._mode_event(self._rx.current_mode, now))
        # Emit periodic scanline progress events.
        scanline_count = self._rx.scanline_count
        if scanline_count > 0 and scanline_count - self._last_scanline_event >= self._SCANLINE_EVENT_INTERVAL:
            self._last_scanline_event = scanline_count
            events.append(self._scanline_event(scanline_count, now))
        # Emit image events.
        for img in images:
            self._image_count += 1
            events.append(self._image_event(img, now))
        return events

    def stop(self) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {
            "state": self._rx.state,
            "mode": self._rx.current_mode.name if self._rx.current_mode else None,
            "scanlines_decoded": self._rx.scanline_count,
            "images_decoded": self._image_count,
        }

    # -- event builders ------------------------------------------------------

    @staticmethod
    def _mode_event(mode: SstvMode, now: float) -> dict[str, Any]:
        return {
            "kind": "mode",
            "ts": now,
            "mode": mode.name,
            "vis_code": int(mode),
        }

    @staticmethod
    def _scanline_event(count: int, now: float) -> dict[str, Any]:
        return {
            "kind": "scanline",
            "ts": now,
            "scanline": count,
        }

    def _image_event(self, img: SstvImage, now: float) -> dict[str, Any]:
        """Build an image event with base64-encoded PNG data."""
        # Encode the image as PNG using a minimal pure-numpy encoder.
        # We don't have PIL in the live path (ADR-004 — no extra deps),
        # so we encode as raw RGB bytes with a simple header. The
        # frontend can reconstruct the image from width × height × 3 bytes.
        png_b64 = _encode_image_b64(img.pixels)
        return {
            "kind": "image",
            "ts": now,
            "mode": img.mode.name,
            "width": img.width,
            "height": img.height,
            "data": png_b64,
            "image_index": self._image_count,
        }


def _encode_image_b64(pixels: np.ndarray) -> str:
    """Encode an RGB pixel array as base64-encoded raw bytes.

    The format is simple: raw RGB bytes (row-major, height × width × 3).
    The frontend reconstructs via `new Uint8Array(atob(data))` →
    `ImageData(width, height)` → `putImageData`. No PNG compression
    (keeps the encoder pure-numpy, no PIL dependency in the live path).
    """
    # Ensure uint8, C-contiguous.
    arr = np.ascontiguousarray(pixels, dtype=np.uint8)
    raw = arr.tobytes()
    return base64.b64encode(raw).decode("ascii")
