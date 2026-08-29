"""DAB (Digital Audio Broadcasting) decoder plugin (ADR-003 family #16).

This v1 implements the FIC (Fast Information Channel) service label
extraction layer. It takes decoded FIB bytes (from a future OFDM
front-end) and extracts service labels + program types.

The full DAB decoder (OFDM + DQPSK + MSC audio) is a substantial
undertaking — this v1 provides the FIC decoding building block so the
frontend can render a DAB service list.

Tap point: rf_band (the FIC is extracted from the DAB signal's fast
information channel, which is a separate logical channel from the audio).

Emits:
  * "service" events when a service label is decoded.
  * "ensemble" events with the full service list.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import numpy as np

from .base import DecoderManifest, DecoderPlugin
from .dab_demod import DabService, decode_fic
from .registry import DecoderRegistry


@DecoderRegistry.register
class DabDecoderPlugin(DecoderPlugin):
    """DAB decoder: FIC bytes → service labels + program types."""

    manifest: ClassVar[DecoderManifest] = DecoderManifest(
        name="dab",
        version="0.1.0",
        label="DAB (Digital Audio Broadcasting)",
        tap_point="rf_band",
        description=(
            "DAB FIC decoder — extracts service labels + program types "
            "from the Fast Information Channel. Decodes FIG 0/1 service "
            "labels (16-char ASCII + PTy). CRC-16-CCITT verification on "
            "each FIB. v1: FIC decoding only (OFDM + DQPSK + MSC audio "
            "deferred to v2). Emits 'service' events per decoded station "
            "and 'ensemble' events with the full service list."
        ),
        required_sample_rate=8000,
        events=("service", "ensemble"),
    )

    def __init__(self) -> None:
        self._services: dict[int, DabService] = {}
        self._ensemble_count = 0

    def feed_iq(self, iq: np.ndarray) -> list[dict[str, Any]]:
        """Feed IQ (interpreted as FIC bytes for this v1).

        In a full DAB decoder, the IQ would be OFDM-demodulated to
        extract the FIC. This v1 takes the I component as a raw byte
        stream (for testing — the frontend would need to supply decoded
        FIC bytes, or a future OFDM front-end would produce them).
        """
        events: list[dict[str, Any]] = []
        now = time.time()
        if iq.dtype == np.complex64:
            pcm = iq.real.astype(np.float32)
            pcm = (pcm * 32767.0).astype("<i2")
        else:
            pcm = iq.astype("<i2", copy=False)
        # Convert int16 samples to bytes (each sample → 2 bytes, big-endian).
        # For testing, we interpret the raw bytes as FIC data.
        fic_bytes = pcm.tobytes()
        services = decode_fic(fic_bytes)
        for svc in services:
            if svc.service_id not in self._services or self._services[svc.service_id].label != svc.label:
                self._services[svc.service_id] = svc
                events.append(self._service_event(svc, now))
        if services:
            self._ensemble_count += 1
            events.append(self._ensemble_event(now))
        return events

    def stop(self) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {
            "services_known": len(self._services),
            "ensembles_decoded": self._ensemble_count,
        }

    def _service_event(self, svc: DabService, now: float) -> dict[str, Any]:
        return {
            "kind": "service",
            "ts": now,
            "service_id": svc.service_id,
            "label": svc.label,
            "program_type": svc.program_type,
            "subchannel_id": svc.subchannel_id,
        }

    def _ensemble_event(self, now: float) -> dict[str, Any]:
        return {
            "kind": "ensemble",
            "ts": now,
            "services": [s.to_dict() for s in self._services.values()],
            "ensemble_index": self._ensemble_count,
        }
