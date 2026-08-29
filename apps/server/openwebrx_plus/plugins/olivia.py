"""Olivia MFSK decoder plugin (ADR-003 family #9).

Mirrors :mod:`.psk31` — wraps the pure-Python MFSK demodulator
(:mod:`.olivia_demod`) + the character decoder (:mod:`.olivia_protocol`)
and emits decoded text as decoder events.

Tap point: rf_band (audio-band decoders need the demodulated audio stream).
The session's AudioChain pushes complex-float IQ; the plugin treats the I
component as the audio-band signal.

The plugin emits one "frame" event per decoded character and one "text"
snapshot event periodically. Defaults: Olivia 32-1000 (32 tones, 1000 Hz
bandwidth, 31.25 baud, center 1500 Hz).
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import numpy as np

from .base import DecoderManifest, DecoderPlugin
from .olivia_demod import (
    DEFAULT_SAMPLE_RATE,
    OliviaReceiver,
)
from .olivia_protocol import OliviaDecoder
from .registry import DecoderRegistry


@DecoderRegistry.register
class OliviaDecoderPlugin(DecoderPlugin):
    """Olivia MFSK decoder: int16 PCM → multi-tone Goertzel → ASCII text."""

    manifest: ClassVar[DecoderManifest] = DecoderManifest(
        name="olivia",
        version="0.1.0",
        label="Olivia (MFSK, 32-1000)",
        tap_point="rf_band",
        description=(
            "Olivia MFSK decoder — 32-tone Goertzel detector at "
            "1000 Hz bandwidth (31.25 Hz tone spacing), 31.25 baud "
            "symbol rate, center 1500 Hz. Robust weak-signal mode. "
            "Emits 'frame' events per decoded character and 'text' "
            "snapshots periodically. v1: no FEC (works on strong signals; "
            "Golay + interleaving deferred to v2)."
        ),
        required_sample_rate=DEFAULT_SAMPLE_RATE,
        events=("frame", "text"),
    )

    _SNAPSHOT_INTERVAL = 0.5  # s — coalesce text snapshots

    def __init__(self) -> None:
        self._rx = OliviaReceiver()
        self._decoder = OliviaDecoder()
        self._text = ""
        self._last_snapshot = 0.0

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
        # Demodulate MFSK → symbols.
        symbols = self._rx.feed(pcm)
        if symbols:
            # Decode symbols → characters.
            for sym in symbols:
                char = self._decoder.feed_symbol(sym)
                if char:
                    self._text += char
                    events.append(self._frame_event(char, now))
            # Emit a text snapshot if enough time has passed.
            if now - self._last_snapshot >= self._SNAPSHOT_INTERVAL:
                events.append(self._snapshot_event(now))
        return events

    def stop(self) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {
            "text_length": len(self._text),
            "symbols_decoded": self._rx.symbol_count,
            "num_tones": self._rx.num_tones,
            "bandwidth": self._rx.bandwidth,
            "center_hz": self._rx.center_hz,
        }

    # -- event builders ------------------------------------------------------

    @staticmethod
    def _frame_event(char: str, now: float) -> dict[str, Any]:
        return {
            "kind": "frame",
            "ts": now,
            "char": char,
        }

    def _snapshot_event(self, now: float) -> dict[str, Any]:
        self._last_snapshot = now
        return {
            "kind": "text",
            "ts": now,
            "text": self._text,
            "symbols_decoded": self._rx.symbol_count,
        }
