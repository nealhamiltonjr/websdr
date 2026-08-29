"""ACARS decoder plugin (ADR-003 family #15).

Wraps the pure-Python MSK demodulator (:mod:`.acars_demod`) + the
protocol decoder (:mod:`.acars_protocol`) and emits decoded aircraft
messages as decoder events.

Tap point: rf_band (audio-band decoders need the demodulated audio).
The session's AudioChain pushes complex-float IQ; the plugin treats the
I component as the audio-band signal.

Emits:
  * "message" events on valid frames (address + label + text).
  * "crc_error" events on corrupted frames.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import numpy as np

from .acars_demod import (
    DEFAULT_SAMPLE_RATE,
    AcarsReceiver,
    verify_crc,
)
from .acars_protocol import decode_frame
from .base import DecoderManifest, DecoderPlugin
from .registry import DecoderRegistry


@DecoderRegistry.register
class AcarsDecoderPlugin(DecoderPlugin):
    """ACARS decoder: int16 PCM → MSK demod → aircraft text messages."""

    manifest: ClassVar[DecoderManifest] = DecoderManifest(
        name="acars",
        version="0.1.0",
        label="ACARS (Aircraft Messaging)",
        tap_point="rf_band",
        description=(
            "ACARS decoder — 1200 baud MSK Goertzel mark/space detector "
            "(1200/2400 Hz) + HDLC-like frame sync (0xEB 0x90) + "
            "CRC-16-CCITT verification. Decodes aircraft address + mode + "
            "ACK + message label + text payload. Emits 'message' events "
            "on valid frames and 'crc_error' events on corrupted frames. "
            "Operates on 131.550 MHz (worldwide VHF airline datalink)."
        ),
        required_sample_rate=DEFAULT_SAMPLE_RATE,
        events=("message", "crc_error"),
    )

    def __init__(self) -> None:
        self._rx = AcarsReceiver()
        self._message_count = 0
        self._crc_error_count = 0

    def feed_iq(self, iq: np.ndarray) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        now = time.time()
        if iq.dtype == np.complex64:
            pcm = iq.real.astype(np.float32)
            pcm = (pcm * 32767.0).astype("<i2")
        else:
            pcm = iq.astype("<i2", copy=False)
        frames = self._rx.feed(pcm)
        for frame_bytes in frames:
            if verify_crc(frame_bytes):
                decoded = decode_frame(frame_bytes)
                if decoded is not None:
                    self._message_count += 1
                    events.append(self._message_event(decoded, now))
                else:
                    self._crc_error_count += 1
                    events.append(self._crc_error_event(frame_bytes, "parse error", now))
            else:
                self._crc_error_count += 1
                events.append(self._crc_error_event(frame_bytes, "CRC mismatch", now))
        return events

    def stop(self) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {
            "messages_decoded": self._message_count,
            "crc_errors": self._crc_error_count,
            "mark_hz": self._rx.mark_hz,
            "space_hz": self._rx.space_hz,
            "baud": self._rx.baud,
        }

    def _message_event(self, msg: Any, now: float) -> dict[str, Any]:
        return {
            "kind": "message",
            "ts": now,
            "address": msg.address,
            "mode": msg.mode,
            "ack": msg.ack,
            "label": msg.label,
            "block_id": msg.block_id,
            "text": msg.text,
            "raw_hex": msg.raw_hex,
            "message_index": self._message_count,
        }

    @staticmethod
    def _crc_error_event(frame_bytes: bytes, reason: str, now: float) -> dict[str, Any]:
        return {
            "kind": "crc_error",
            "ts": now,
            "reason": reason,
            "raw_hex": frame_bytes[:64].hex(),
            "length": len(frame_bytes),
        }
