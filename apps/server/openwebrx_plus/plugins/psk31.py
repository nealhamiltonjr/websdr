"""PSK31 (Phase Shift Keying, 31.25 baud) decoder plugin (ADR-003 family #7).

Mirrors :mod:`.cw` and :mod:`.rtty` — wraps the pure-Python BPSK demodulator
(:mod:`.psk31_demod`) + the Varicode decoder (:mod:`.psk31_protocol`) and
emits decoded text characters as decoder events.

Tap point: rf_band (audio-band decoders need the demodulated audio stream).
The session's AudioChain pushes complex-float IQ; the plugin treats the I
component as the audio-band signal (the BPSK carrier beat note lives in
the audio band already).

Sample-rate contract: accepts any positive rate; the carrier Hz defaults
to 1000 (the standard PSK31 audio tone), baud defaults to 31.25. The
plugin emits one "frame" event per decoded character and one "text"
snapshot event periodically.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import numpy as np

from .base import DecoderManifest, DecoderPlugin
from .psk31_demod import DEFAULT_SAMPLE_RATE, Psk31Receiver
from .psk31_protocol import VaricodeDecoder
from .registry import DecoderRegistry


@DecoderRegistry.register
class Psk31DecoderPlugin(DecoderPlugin):
    """PSK31 decoder: int16 PCM → BPSK demod → Varicode → text."""

    manifest: ClassVar[DecoderManifest] = DecoderManifest(
        name="psk31",
        version="0.1.0",
        label="PSK31 (Phase Shift Keying 31.25 baud)",
        tap_point="rf_band",
        description=(
            "PSK31 decoder — coherent BPSK demodulation at 1000 Hz carrier "
            "+ 31.25 baud clock recovery + Varicode character decoder. "
            "Emits 'frame' events per decoded character and 'text' "
            "snapshots periodically. Defaults: 1000 Hz carrier, 31.25 baud."
        ),
        required_sample_rate=DEFAULT_SAMPLE_RATE,
        events=("frame", "text"),
    )

    _SNAPSHOT_INTERVAL = 0.5  # s — coalesce text snapshots

    def __init__(self) -> None:
        self._rx = Psk31Receiver()
        self._decoder = VaricodeDecoder()
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
        # Demodulate BPSK → bits.
        new_bits = self._rx.feed(pcm)
        if new_bits:
            # Feed bits to the Varicode decoder.
            for bit in new_bits:
                char = self._decoder.feed_bit(bit)
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
            "center_hz": self._rx.center_hz,
            "baud": self._rx.baud,
            "bits_buffered": len(self._rx.bit_stream),
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
            "center_hz": self._rx.center_hz,
            "baud": self._rx.baud,
        }
