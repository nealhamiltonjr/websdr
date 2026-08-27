"""CW (Morse code) decoder plugin (ADR-003 in-process plugin family #5).

Mirrors :mod:`.ais` and :mod:`.dump978` — wraps the pure-Python
Goertzel-based CW demodulator (:mod:`.cw_demod`) and emits decoded
text characters as decoder events. A simple text viz renders them
in a terminal-like panel (the same decoderStream plumbing as the
aircraft/vessel tables).

Tap point: rf_band (audio-band decoders need the demodulated audio
stream, not raw IQ — the session's AudioChain output feeds this
plugin). v1 takes the IQ bytes that the FFT chain also consumes;
the Goertzel sidetone filter replaces the FM demod (CW is essentially
"AM at one tone" — the carrier beat note IS the signal).

Sample-rate contract: accepts any positive rate; the sidetone Hz
defaults to 600 (the operator's comfortable offset) and must be in
(50, sample_rate/2). The plugin emits one "frame" event per decoded
character and one "text" snapshot event when a word gap closes.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import numpy as np

from .base import DecoderManifest, DecoderPlugin
from .cw_demod import DEFAULT_SAMPLE_RATE, CwReceiver
from .registry import DecoderRegistry


@DecoderRegistry.register
class CwDecoderPlugin(DecoderPlugin):
    """CW / Morse code decoder: int16 PCM → Goertzel sidetone → text."""

    manifest: ClassVar[DecoderManifest] = DecoderManifest(
        name="cw",
        version="0.1.0",
        label="CW / Morse",
        tap_point="rf_band",
        description=(
            "Morse code (CW) decoder — Goertzel-filter sidetone detector "
            "with adaptive noise floor + hysteresis, streaming interval "
            "to-character decoder with adaptive WPM estimate. Emits "
            "'frame' events per character and 'text' snapshots at word "
            "gaps. Defaults: 600 Hz sidetone, 20 WPM starting estimate."
        ),
        required_sample_rate=DEFAULT_SAMPLE_RATE,
        events=("frame", "text"),
    )

    _SNAPSHOT_INTERVAL = 0.5  # s — coalesce text snapshots for busy traffic

    def __init__(self) -> None:
        self._rx = CwReceiver()
        self._last_snapshot = 0.0
        self._last_text_len = 0

    # -- DecoderPlugin contract ---------------------------------------------

    def feed_iq(self, iq: np.ndarray) -> list[dict[str, Any]]:
        """Feed IQ (interpreted as int16 PCM for the audio-band path).

        The session's AudioChain pushes complex-float IQ through the FFT
        and demod paths; for the CW plugin we treat the I component as
        the audio-band signal (the Goertzel sidetone detector works on
        any real-valued audio stream).
        """
        events: list[dict[str, Any]] = []
        now = time.time()
        # Treat IQ complex samples as their real (I) component — for
        # CW the carrier beat note lives in the audio band already.
        if iq.dtype == np.complex64:
            pcm = iq.real.astype(np.float32)
            # Scale to int16 range (|I| ≤ 1.0 → multiply by 32767).
            pcm = (pcm * 32767.0).astype("<i2")
        else:
            pcm = iq.astype("<i2", copy=False)
        new_text = self._rx.feed(pcm)
        if new_text:
            # One "frame" event per decoded character (split the new text).
            for ch in new_text:
                events.append(self._frame_event(ch, now))
        # Word gap → "text" snapshot event coalesced at 0.5 s.
        if new_text and (now - self._last_snapshot >= self._SNAPSHOT_INTERVAL or " " in new_text):
            events.append(self._snapshot_event(now))
        return events

    def stop(self) -> None:
        # Flush any pending char so the stream doesn't lose a partial letter.
        self._rx.flush()

    def status(self) -> dict[str, Any]:
        return {
            "text_length": len(self._rx.text),
            "wpm": self._rx._decoder.wpm,
            "sidetone_hz": self._rx.sidetone_hz,
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
            "text": self._rx.text,
            "wpm": self._rx._decoder.wpm,
            "sidetone_hz": self._rx.sidetone_hz,
        }
