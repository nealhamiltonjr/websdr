"""FT8 audio-band decoder plugin (ADR-003 in-process plugin family #6).

Slice-21 status: **manifest + wire-format scaffolding**. The plugin is
registered in DecoderRegistry, advertised in GET /api/decoders, and
the DigiMessageListViz component on the frontend renders its events.
The actual FT8 demodulator (8-FSK + LDPC + CRC + message decode) is a
substantial undertaking; this slice ships the contract surface so
operators see the FT8 option in the +viz dropdown today.

Wire format (this module → frontend):

  * ``{"kind": "message", "ts": float, "mode": "FT8",
      "text": "K1ABC KO51 -12", "callsign": "K1ABC",
      "grid_locator": "KO51", "snr_db": -12, ...}`` per decoded message.
  * ``{"kind": "messages", "ts": float, "messages": [...]}`` snapshot
    of the most recent N messages (default 50; a ring buffer).

This mirrors the ADS-B / AIS pattern (per-message "frame" / per-table
"snapshot" events) but with a free-form ``text`` field for arbitrary
audio-band digital modes (FT8 / FT4 / WSPR / JT65 / JT9 / PSK31 /
RTTY — all feed the same DigiMessageListViz).

Future implementation plan (per the roadmap):

  1. FSK demodulator at 6.25 baud (numpy FFT magnitude bin tracking).
  2. Soft-decision LDPC decoder (174-bit codeword; reuse the same
     algorithm as the existing rs_correct in uat_protocol.py for the
     syndrome calc, then a BCJR or sum-product for the actual decode).
  3. CRC-14 check (the last 14 bits of the 77-bit message).
  4. Message unpack: callsign (28-bit packed), grid locator (15-bit),
     signal report (5-bit signed).
  5. Emit "message" event per decoded frame + coalesce snapshots
     every 15 seconds (one FT8 slot).

The module below is a minimal stub that the manifest + status methods
work today. ``feed_iq`` returns an empty list — no decoding yet. The
tap_point is ``rf_band`` because the FT8 demodulator operates on the
audio-band IQ (after the session's AudioChain demod), not raw RF IQ.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from .base import DecoderManifest, DecoderPlugin
from .registry import DecoderRegistry

# FT8 spec constants (DO-282B-style — the actual demodulator will use
# these; documented here so the manifest's required_sample_rate is
# traceable to the protocol).
FT8_SAMPLE_RATE = 12_000  # 12 kHz audio band — WSJT-X standard
FT8_TONE_SPACING_HZ = 6.25  # 8-FSK, 6.25 Hz between tones
FT8_BIT_RATE_BAUD = 6.25  # 6.25 baud (one symbol per 0.16 s)
FT8_SLOT_SECONDS = 15  # 15-second slot alignment (UTC-locked)


@DecoderRegistry.register
class FT8DecoderPlugin(DecoderPlugin):
    """FT8 / audio-band digital-mode decoder (slice-21 stub).

    Registered in DecoderRegistry; advertised in GET /api/decoders;
    feed_iq returns no events until the real demodulator lands in a
    future slice. The wire-format types in
    ``packages/shared-types/src/decoder.ts`` (DigiMessageEvent,
    DigiMessageListEvent, DIGI_MESSAGE_DECODERS) let the frontend
    DigiMessageListViz component render the events when they arrive.
    """

    manifest: ClassVar[DecoderManifest] = DecoderManifest(
        name="ft8",
        version="0.1.0",
        label="FT8 (audio-band digi modes)",
        tap_point="rf_band",
        description=(
            "FT8 / FT4 / WSPR / JT65 / JT9 / PSK31 / RTTY decoder "
            "(ADR-003 in-process plugin family #6). Slice-21 status: "
            "manifest + wire-format scaffolding only — the actual FSK "
            "demodulator + LDPC + CRC + message unpack lands in a "
            "future slice. The DigiMessageListViz component on the "
            "frontend renders events when the decoder produces them. "
            "Tap point: rf_band (operates on the audio-band IQ, not "
            "raw RF). Defaults: 12 kHz audio rate, 6.25 Hz tone "
            "spacing, 15-second slot alignment."
        ),
        required_sample_rate=FT8_SAMPLE_RATE,
        events=("message", "messages"),
    )

    def feed_iq(self, iq: np.ndarray) -> list[dict[str, Any]]:
        """Consume one complex-float32 IQ chunk; return decoded events.

        Slice-21 stub: returns an empty list — the demodulator is not
        yet implemented. Operators who want FT8 today can run a local
        WSJT-X instance and feed it via a virtual audio cable (the
        scope of this slice doesn't include the VAC wiring).
        """
        _ = iq  # accepted, ignored — stub.
        return []

    def feed_audio(self, pcm: np.ndarray, sample_rate: int) -> list[dict[str, Any]]:
        """Audio-band path — same stub as feed_iq (no demod yet)."""
        _ = pcm, sample_rate
        return []

    def status(self) -> dict[str, Any]:
        """Live counters for GET /api/receivers/{id}/decoders.

        Zero across the board — the stub hasn't decoded anything yet.
        The real impl will populate ``messages_decoded``,
        ``crc_failures``, ``slot_count`` here.
        """
        return {
            "messages_decoded": 0,
            "crc_failures": 0,
            "slot_count": 0,
            "stub": True,
            "note": "slice-21 manifest stub — demodulator not implemented",
        }

    def stop(self) -> None:
        """Nothing to release (no streaming state yet)."""
        return None
