"""JT65 (Joe Taylor 65-tone) decoder plugin (ADR-003 family #12).

Mirrors :mod:`.wspr` — wraps the pure-Python 65-tone MFSK demodulator
(:mod:`.jt65_demod`) + the protocol decoder (:mod:`.jt65_protocol`) and
emits decoded JT65 messages as decoder events.

JT65 is a weak-signal digital mode designed for Earth-Moon-Earth (EME)
communications. It uses 65-tone MFSK with Reed-Solomon FEC, making it
readable at -24 dB SNR.

Tap point: rf_band (audio-band decoders need the demodulated audio stream).
The session's AudioChain pushes complex-float IQ; the plugin treats the I
component as the audio-band signal.

The plugin emits:
  * "message" events when a complete 126-symbol JT65 message is decoded
    (with callsign1, callsign2, grid/report).
  * "progress" events periodically during decoding (symbol count).

Sample-rate contract: accepts any positive rate; defaults to 8000 Hz.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import numpy as np

from .base import DecoderManifest, DecoderPlugin
from .jt65_demod import DEFAULT_NUM_SYMBOLS, DEFAULT_SAMPLE_RATE, Jt65Receiver
from .jt65_protocol import strip_sync, symbols_to_bits, unpack_payload
from .registry import DecoderRegistry


@DecoderRegistry.register
class Jt65DecoderPlugin(DecoderPlugin):
    """JT65 decoder: int16 PCM → 65-tone MFSK → callsign/grid/report."""

    manifest: ClassVar[DecoderManifest] = DecoderManifest(
        name="jt65",
        version="0.1.0",
        label="JT65 (Weak-Signal EME)",
        tap_point="rf_band",
        description=(
            "JT65 decoder — 65-tone MFSK Goertzel detector at "
            "2.6917 Hz spacing (11025/4096 baud), 126-symbol "
            "transmissions (~46.8 s). Designed for EME (moonbounce). "
            "Decodes callsign1 + callsign2 + grid/report. Emits "
            "'message' events on complete decode and 'progress' events. "
            "v1: no Reed-Solomon FEC (works on strong signals; RS "
            "decode deferred to v2)."
        ),
        required_sample_rate=DEFAULT_SAMPLE_RATE,
        events=("message", "progress"),
    )

    _PROGRESS_INTERVAL = 10  # emit a progress event every N symbols

    def __init__(self) -> None:
        self._rx = Jt65Receiver()
        self._last_progress = 0
        self._message_count = 0

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
        new_symbols = self._rx.feed(pcm)
        if not new_symbols:
            return events
        # Emit periodic progress events.
        total = self._rx.symbol_count
        if total - self._last_progress >= self._PROGRESS_INTERVAL:
            self._last_progress = total
            events.append(self._progress_event(total, now))
        # Check if we have a complete 126-symbol message.
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

    # -- internal -----------------------------------------------------------

    def _decode_symbols(self, symbols: list[int], now: float) -> dict[str, Any] | None:
        """Attempt to decode a 126-symbol JT65 message.

        v1: strip sync tones, convert to bits, extract the 72-bit payload,
        unpack. No RS FEC — works only on error-free symbols.
        """
        if len(symbols) < DEFAULT_NUM_SYMBOLS:
            return None
        # Strip sync tones → 63 data symbols.
        data_symbols = strip_sync(symbols[:DEFAULT_NUM_SYMBOLS])
        # Convert to bits (63 symbols × 6 bits = 378 bits; we need only 72).
        bits = symbols_to_bits(data_symbols)
        # Unpack the 72-bit payload.
        callsign1, callsign2, grid_report = unpack_payload(bits[:72])
        # Sanity check: at least one callsign should be non-empty.
        if not callsign1.strip() and not callsign2.strip():
            return None
        return {
            "kind": "message",
            "ts": now,
            "callsign1": callsign1.strip(),
            "callsign2": callsign2.strip(),
            "grid_report": grid_report,
            "snr_db": 0.0,  # SNR estimation deferred to v2
            "freq_hz": 0,  # frequency set by the receiver's center_freq
            "message_index": self._message_count,
        }

    def _progress_event(self, symbol_count: int, now: float) -> dict[str, Any]:
        return {
            "kind": "progress",
            "ts": now,
            "symbols": symbol_count,
            "total": DEFAULT_NUM_SYMBOLS,
        }
