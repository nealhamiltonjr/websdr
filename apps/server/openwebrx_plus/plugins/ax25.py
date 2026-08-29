"""AX.25 packet radio decoder plugin (ADR-003 family #11).

Mirrors :mod:`.rtty` and :mod:`.psk31` — wraps the pure-Python AFSK
demodulator (:mod:`.ax25_demod`) + the protocol decoder
(:mod:`.ax25_protocol`) and emits decoded packet frames as decoder events.

Tap point: rf_band (audio-band decoders need the demodulated audio stream).
The session's AudioChain pushes complex-float IQ; the plugin treats the I
component as the audio-band signal (the AFSK tones live in the audio band).

The plugin emits:
  * "packet" events when a complete AX.25 frame is decoded (with source/
    destination callsigns, digipeaters, control byte, info payload).
  * "crc_error" events when a frame is received but the CRC check fails
    (diagnostic — the frame is corrupted but the raw bytes may be useful).

Sample-rate contract: accepts any positive rate; defaults to 8000 Hz.
Mark/space tones default to 1200/2200 Hz (standard 1200 baud AFSK).
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import numpy as np

from .ax25_demod import (
    DEFAULT_SAMPLE_RATE,
    Ax25Receiver,
    verify_crc,
)
from .ax25_protocol import decode_frame
from .base import DecoderManifest, DecoderPlugin
from .registry import DecoderRegistry


@DecoderRegistry.register
class Ax25DecoderPlugin(DecoderPlugin):
    """AX.25 packet radio decoder: int16 PCM → AFSK → HDLC → packets."""

    manifest: ClassVar[DecoderManifest] = DecoderManifest(
        name="ax25",
        version="0.1.0",
        label="AX.25 Packet Radio",
        tap_point="rf_band",
        description=(
            "AX.25 packet decoder — 1200 baud AFSK Goertzel mark/space "
            "detector (1200/2200 Hz) + NRZI decode + HDLC frame sync "
            "(0x7E flag) + bit de-stuffing + CRC-16-CCITT verification. "
            "Decodes destination/source callsigns + SSID + digipeaters + "
            "control + info payload. Emits 'packet' events on valid "
            "frames and 'crc_error' events on corrupted frames."
        ),
        required_sample_rate=DEFAULT_SAMPLE_RATE,
        events=("packet", "crc_error"),
    )

    def __init__(self) -> None:
        self._rx = Ax25Receiver()
        self._packet_count = 0
        self._crc_error_count = 0

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
        # Demodulate AFSK → HDLC frames.
        frames = self._rx.feed(pcm)
        for frame_bytes in frames:
            if verify_crc(frame_bytes):
                # Valid CRC — decode the frame.
                decoded = decode_frame(frame_bytes)
                if decoded is not None:
                    self._packet_count += 1
                    events.append(self._packet_event(decoded, now))
                else:
                    # Frame parse error — count as CRC error.
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
            "packets_decoded": self._packet_count,
            "crc_errors": self._crc_error_count,
            "mark_hz": self._rx.mark_hz,
            "space_hz": self._rx.space_hz,
            "baud": self._rx.baud,
        }

    # -- event builders ------------------------------------------------------

    def _packet_event(self, frame: Any, now: float) -> dict[str, Any]:
        return {
            "kind": "packet",
            "ts": now,
            "source": str(frame.source),
            "destination": str(frame.destination),
            "digipeaters": [str(d) for d in frame.digipeaters],
            "control": frame.control,
            "frame_type": frame.frame_type,
            "info_hex": frame.info.hex(),
            "info_text": frame.info.decode("ascii", errors="replace"),
            "packet_index": self._packet_count,
        }

    @staticmethod
    def _crc_error_event(frame_bytes: bytes, reason: str, now: float) -> dict[str, Any]:
        return {
            "kind": "crc_error",
            "ts": now,
            "reason": reason,
            "raw_hex": frame_bytes[:64].hex(),  # first 64 bytes for diagnostics
            "length": len(frame_bytes),
        }
