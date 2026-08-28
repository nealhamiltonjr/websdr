"""JT9 (Joe Taylor 9-tone) decoder plugin (ADR-003 family #13).

Mirrors :mod:`.jt65` — wraps the pure-Python 9-tone MFSK demodulator
(:mod:`.jt9_demod`) + the protocol decoder (:mod:`.jt9_protocol`) and
emits decoded JT9 messages as decoder events.

JT9 is a weak-signal digital mode for LF/MF/HF, using 9 tones instead
of JT65's 65. It offers sub-modes from 1 minute (JT9-1) to 30 minutes
(JT9-30) for extreme weak-signal work.

The plugin emits:
  * "message" events when a complete 85-symbol JT9 message is decoded.
  * "progress" events periodically during decoding.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import numpy as np

from .base import DecoderManifest, DecoderPlugin
from .jt9_demod import (
    DEFAULT_NUM_SYMBOLS,
    DEFAULT_SAMPLE_RATE,
    Jt9Receiver,
)
from .jt9_protocol import unpack_payload
from .registry import DecoderRegistry


@DecoderRegistry.register
class Jt9DecoderPlugin(DecoderPlugin):
    """JT9 decoder: int16 PCM → 9-tone MFSK → callsign/grid/report."""

    manifest: ClassVar[DecoderManifest] = DecoderManifest(
        name="jt9",
        version="0.1.0",
        label="JT9 (Weak-Signal LF/MF/HF)",
        tap_point="rf_band",
        description=(
            "JT9 decoder — 9-tone MFSK Goertzel detector at "
            "1.4648 Hz spacing (12000/4096 baud), 85-symbol "
            "transmissions (~1 min for JT9-1). Decodes callsign1 + "
            "callsign2 + grid/report. Emits 'message' events on "
            "complete decode and 'progress' events. v1: no FEC."
        ),
        required_sample_rate=DEFAULT_SAMPLE_RATE,
        events=("message", "progress"),
    )

    _PROGRESS_INTERVAL = 10

    def __init__(self) -> None:
        self._rx = Jt9Receiver()
        self._last_progress = 0
        self._message_count = 0

    def feed_iq(self, iq: np.ndarray) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        now = time.time()
        if iq.dtype == np.complex64:
            pcm = iq.real.astype(np.float32)
            pcm = (pcm * 32767.0).astype("<i2")
        else:
            pcm = iq.astype("<i2", copy=False)
        new_symbols = self._rx.feed(pcm)
        if not new_symbols:
            return events
        total = self._rx.symbol_count
        if total - self._last_progress >= self._PROGRESS_INTERVAL:
            self._last_progress = total
            events.append(self._progress_event(total, now))
        if self._rx.is_complete:
            symbols = self._rx.consume_symbols(DEFAULT_NUM_SYMBOLS)
            msg = self._decode_symbols(symbols, now)
            if msg is not None:
                events.append(msg)
                self._message_count += 1
        return events

    def stop(self) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {
            "symbols_decoded": self._rx.symbol_count,
            "is_complete": self._rx.is_complete,
            "messages_decoded": self._message_count,
            "base_freq_hz": self._rx.base_freq_hz,
            "tone_spacing_hz": self._rx.tone_spacing,
            "num_tones": self._rx.num_tones,
        }

    def _decode_symbols(self, symbols: list[int], now: float) -> dict[str, Any] | None:
        if len(symbols) < DEFAULT_NUM_SYMBOLS:
            return None
        callsign1, callsign2, grid_report = unpack_payload(symbols[:DEFAULT_NUM_SYMBOLS])
        if not callsign1.strip() and not callsign2.strip():
            return None
        return {
            "kind": "message",
            "ts": now,
            "callsign1": callsign1.strip(),
            "callsign2": callsign2.strip(),
            "grid_report": grid_report,
            "snr_db": 0.0,
            "freq_hz": 0,
            "message_index": self._message_count,
        }

    def _progress_event(self, symbol_count: int, now: float) -> dict[str, Any]:
        return {
            "kind": "progress",
            "ts": now,
            "symbols": symbol_count,
            "total": DEFAULT_NUM_SYMBOLS,
        }
