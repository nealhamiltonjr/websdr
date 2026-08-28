"""RTTY (Radio Teletype) decoder plugin (ADR-003 in-process plugin family #6).

Mirrors :mod:`.cw` — wraps the pure-Python Goertzel-based FSK demodulator
(:mod:`.rtty_demod`) + the ITA2/Baudot decoder (:mod:`.rtty_protocol`) and
emits decoded text characters as decoder events.

Tap point: rf_band (audio-band decoders need the demodulated audio stream,
not raw IQ — the session's AudioChain output feeds this plugin). v1 takes
the IQ bytes that the FFT chain also consumes; the Goertzel mark/space
filter replaces the FM demod (RTTY is FSK — two tones, one per bit).

Sample-rate contract: accepts any positive rate; the mark/space Hz
default to 2125/2295 (standard ham RTTY with 170 Hz shift), baud
defaults to 45.45 (amateur standard). The plugin emits one "frame"
event per decoded character and one "text" snapshot event periodically.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import numpy as np

from .base import DecoderManifest, DecoderPlugin
from .registry import DecoderRegistry
from .rtty_demod import DEFAULT_SAMPLE_RATE, RttyReceiver
from .rtty_protocol import Ita2Decoder


@DecoderRegistry.register
class RttyDecoderPlugin(DecoderPlugin):
    """RTTY / Radio Teletype decoder: int16 PCM → Goertzel FSK → ITA2 text."""

    manifest: ClassVar[DecoderManifest] = DecoderManifest(
        name="rtty",
        version="0.1.0",
        label="RTTY (Radio Teletype)",
        tap_point="rf_band",
        description=(
            "RTTY decoder — dual-Goertzel FSK mark/space detector at "
            "2125/2295 Hz (170 Hz shift) + 45.45 baud clock recovery + "
            "ITA2/Baudot 5-bit decoder with letter/figure shift state. "
            "Emits 'frame' events per decoded character and 'text' "
            "snapshots periodically. Defaults: 2125 Hz mark, 2295 Hz "
            "space, 45.45 baud."
        ),
        required_sample_rate=DEFAULT_SAMPLE_RATE,
        events=("frame", "text"),
    )

    _SNAPSHOT_INTERVAL = 0.5  # s — coalesce text snapshots

    def __init__(self) -> None:
        self._rx = RttyReceiver()
        self._decoder = Ita2Decoder()
        self._text = ""
        self._last_snapshot = 0.0

    # -- DecoderPlugin contract ---------------------------------------------

    def feed_iq(self, iq: np.ndarray) -> list[dict[str, Any]]:
        """Feed IQ (interpreted as int16 PCM for the audio-band path).

        The session's AudioChain pushes complex-float IQ through the FFT
        and demod paths; for the RTTY plugin we treat the I component as
        the audio-band signal (the Goertzel mark/space detector works on
        any real-valued audio stream).
        """
        events: list[dict[str, Any]] = []
        now = time.time()
        # Treat IQ complex samples as their real (I) component.
        if iq.dtype == np.complex64:
            pcm = iq.real.astype(np.float32)
            pcm = (pcm * 32767.0).astype("<i2")
        else:
            pcm = iq.astype("<i2", copy=False)
        # Demodulate FSK → 5-bit codes.
        codes = self._rx.feed(pcm)
        if codes:
            # Decode each code through the ITA2 table.
            for code in codes:
                char = self._decoder.decode(code)
                if char:
                    self._text += char
                    events.append(self._frame_event(char, code, now))
            # Emit a text snapshot if enough time has passed.
            if now - self._last_snapshot >= self._SNAPSHOT_INTERVAL:
                events.append(self._snapshot_event(now))
        return events

    def stop(self) -> None:
        # Nothing to flush — the demodulator is stateless between calls
        # (all state is in the receiver object, which persists).
        pass

    def status(self) -> dict[str, Any]:
        return {
            "text_length": len(self._text),
            "mark_hz": self._rx.mark_hz,
            "space_hz": self._rx.space_hz,
            "baud": self._rx.baud,
            "in_letters_mode": self._decoder.in_letters_mode,
        }

    # -- event builders ------------------------------------------------------

    @staticmethod
    def _frame_event(char: str, code: int, now: float) -> dict[str, Any]:
        return {
            "kind": "frame",
            "ts": now,
            "char": char,
            "code": code,
        }

    def _snapshot_event(self, now: float) -> dict[str, Any]:
        self._last_snapshot = now
        return {
            "kind": "text",
            "ts": now,
            "text": self._text,
            "mark_hz": self._rx.mark_hz,
            "space_hz": self._rx.space_hz,
            "baud": self._rx.baud,
        }
