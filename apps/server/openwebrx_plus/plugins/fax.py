"""FAX (Facsimile) decoder plugin (ADR-003 family #14).

Mirrors :mod:`.sstv` — wraps the pure-Python FM-based FAX demodulator
(:mod:`.fax_demod`) and emits decoded weather map images as decoder events.

Tap point: rf_band (audio-band decoders need the demodulated audio stream).
The session's AudioChain pushes complex-float IQ; the plugin treats the I
component as the audio-band signal.

The plugin emits:
  * "image" events when a complete FAX image is decoded (with base64-
    encoded grayscale pixel data).
  * "scanline" events periodically during decoding.
  * "start" events when the start tone is detected.
  * "stop" events when the stop tone is detected.

Sample-rate contract: accepts any positive rate; defaults to 8000 Hz.
"""

from __future__ import annotations

import base64
import time
from typing import Any, ClassVar

import numpy as np

from .base import DecoderManifest, DecoderPlugin
from .fax_demod import DEFAULT_SAMPLE_RATE, FaxImage, FaxReceiver
from .registry import DecoderRegistry


@DecoderRegistry.register
class FaxDecoderPlugin(DecoderPlugin):
    """FAX decoder: int16 PCM → FM demod → weather map images."""

    manifest: ClassVar[DecoderManifest] = DecoderManifest(
        name="fax",
        version="0.1.0",
        label="FAX (Weather Facsimile)",
        tap_point="rf_band",
        description=(
            "FAX image decoder — FM frequency-to-pixel mapping "
            "(1500 Hz = black, 2300 Hz = white) + start/stop tone "
            "detection (300 Hz / 450 Hz). Standard IOC 576, 120 LPM. "
            "Emits 'image' events with base64-encoded grayscale pixel "
            "data, 'scanline' progress, and 'start'/'stop' events."
        ),
        required_sample_rate=DEFAULT_SAMPLE_RATE,
        events=("image", "scanline", "start", "stop"),
    )

    _SCANLINE_EVENT_INTERVAL = 10

    def __init__(self) -> None:
        self._rx = FaxReceiver()
        self._last_scanline_event = 0
        self._image_count = 0

    def feed_iq(self, iq: np.ndarray) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        now = time.time()
        if iq.dtype == np.complex64:
            pcm = iq.real.astype(np.float32)
            pcm = (pcm * 32767.0).astype("<i2")
        else:
            pcm = iq.astype("<i2", copy=False)
        prev_state = self._rx.state
        images = self._rx.feed(pcm)
        # Emit start/stop events on state transitions.
        if prev_state != self._rx.state:
            if self._rx.state == "receiving":
                events.append({"kind": "start", "ts": now})
            elif prev_state == "receiving" and self._rx.state == "idle":
                events.append({"kind": "stop", "ts": now})
        # Emit periodic scanline progress events.
        sc = self._rx.scanline_count
        if sc > 0 and sc - self._last_scanline_event >= self._SCANLINE_EVENT_INTERVAL:
            self._last_scanline_event = sc
            events.append({"kind": "scanline", "ts": now, "scanline": sc})
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
            "scanlines_decoded": self._rx.scanline_count,
            "images_decoded": self._image_count,
            "ioc": self._rx.ioc,
            "lpm": self._rx.lpm,
        }

    def _image_event(self, img: FaxImage, now: float) -> dict[str, Any]:
        """Build an image event with base64-encoded grayscale pixel data."""
        arr = np.ascontiguousarray(img.pixels, dtype=np.uint8)
        raw = arr.tobytes()
        data_b64 = base64.b64encode(raw).decode("ascii")
        return {
            "kind": "image",
            "ts": now,
            "width": img.width,
            "height": img.height,
            "data": data_b64,
            "image_index": self._image_count,
        }
