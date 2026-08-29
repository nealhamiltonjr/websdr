"""ATC (Air Traffic Control) voice activity detector plugin (ADR-003 family #17).

This isn't a protocol decoder — it's a voice activity detector for AM
voice communications on VHF ATC frequencies (118–137 MHz). It emits
"voice_start" / "voice_end" / "rssi" events as the controller or pilot
starts/stops talking.

Tap point: rf_band (the detector needs the demodulated audio stream).
The session's AudioChain pushes complex-float IQ; the plugin treats the
I component as the audio-band signal.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from .atc_demod import DEFAULT_SAMPLE_RATE, DEFAULT_SQUELCH_DBFS, AtcReceiver
from .base import DecoderManifest, DecoderPlugin
from .registry import DecoderRegistry


@DecoderRegistry.register
class AtcDecoderPlugin(DecoderPlugin):
    """ATC voice activity detector: int16 PCM → squelch → voice events."""

    manifest: ClassVar[DecoderManifest] = DecoderManifest(
        name="atc",
        version="0.1.0",
        label="ATC (Air Traffic Control Voice)",
        tap_point="rf_band",
        description=(
            "ATC voice activity detector — RSSI-based squelch with "
            "configurable threshold (-40 dBFS default), debounce (100 ms), "
            "and hang time (500 ms). Emits 'voice_start' when the squelch "
            "opens, 'voice_end' when it closes, and periodic 'rssi' "
            "events (1 s interval) with the current signal strength in "
            "dBFS. For AM voice on VHF ATC frequencies (118–137 MHz)."
        ),
        required_sample_rate=DEFAULT_SAMPLE_RATE,
        events=("voice_start", "voice_end", "rssi"),
    )

    def __init__(self) -> None:
        self._rx = AtcReceiver()
        self._voice_start_count = 0
        self._voice_end_count = 0

    def feed_iq(self, iq: np.ndarray) -> list[dict[str, Any]]:
        """Feed IQ (interpreted as int16 PCM for the audio-band path)."""
        events: list[dict[str, Any]] = []
        if iq.dtype == np.complex64:
            pcm = iq.real.astype(np.float32)
            pcm = (pcm * 32767.0).astype("<i2")
        else:
            pcm = iq.astype("<i2", copy=False)
        # Get the frequency from the session (if available — passed via
        # a thread-local or attribute). For now, pass 0; the session
        # could set this via an attribute on the plugin.
        freq = getattr(self, "_frequency_hz", 0)
        voice_events = self._rx.feed(pcm, frequency_hz=freq)
        for evt in voice_events:
            if evt.kind == "voice_start":
                self._voice_start_count += 1
            elif evt.kind == "voice_end":
                self._voice_end_count += 1
            events.append(evt.to_dict())
        return events

    def stop(self) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {
            "is_active": self._rx.is_active,
            "last_rssi_dbfs": round(self._rx.last_rssi, 1),
            "squelch_dbfs": DEFAULT_SQUELCH_DBFS,
            "voice_starts": self._voice_start_count,
            "voice_ends": self._voice_end_count,
        }
