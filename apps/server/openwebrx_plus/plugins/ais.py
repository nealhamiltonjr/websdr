"""AIS decoder plugin (ADR-003 bundled plugin #2).

Wraps the pure-Python AIS demodulator (:mod:`.ais_demod`) and layers
the per-receiver *vessel table* on top — exactly the ADS-B plugin
pattern, but for ships instead of aircraft. Events down the wire:

  ``frame``    one event per CRC-verified AIS message (the message feed)
  ``vessel``   a snapshot of the vessel table (the list/map viz)

Snapshots fire immediately when a NEW vessel appears, otherwise at
most every ``_SNAPSHOT_INTERVAL`` seconds so heavy marine traffic
can't flood the WS broadcast queues.

Sample-rate contract: the AIS demodulator needs an integer multiple
of 9600 baud (19200, 28800, 38400, 48000, 96000…). The default
fixture rate is 48 kS/s (5 samples/bit). Real receivers should use a
VFO tap (ADR-005) decimated to 48 kS/s centered on 162 MHz, OR pair
with the subprocess ``rtl-ais`` plugin (ADR-003 family #2) for the
production demod.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import numpy as np

from .ais_demod import AIS_SAMPLE_RATE, AisReceiver
from .ais_protocol import AisMessage
from .base import DecoderManifest, DecoderPlugin
from .registry import DecoderRegistry


@DecoderRegistry.register
class AisDecoderPlugin(DecoderPlugin):
    """Marine AIS (ITU-R M.1371-5) GMSK decoder → vessel tracks."""

    manifest: ClassVar[DecoderManifest] = DecoderManifest(
        name="ais",
        version="0.1.0",
        label="AIS (Marine)",
        tap_point="rf_band",
        description=(
            "ITU-R M.1371-5 AIS decoder — GMSK demodulator + HDLC "
            "deframer + CRC-16-CCITT-verified message decode. Handles "
            "the four most common message types on a busy water: "
            "Type 1/2/3 (Class A position report), Type 4 (base "
            "station), Type 5 (static & voyage), Type 18 (Class B "
            "position), Type 21 (Aid-to-Navigation). Sample rate must "
            "be an integer multiple of 9600 (default fixture: 48 kS/s "
            "= 5 samples/bit). Live receivers pair with the subprocess "
            "rtl-ais plugin for the production demod."
        ),
        required_sample_rate=AIS_SAMPLE_RATE,
        events=("frame", "vessel"),
    )

    _SNAPSHOT_INTERVAL = 1.0  # s — coalesce table updates for busy waters

    def __init__(self) -> None:
        self._rx = AisReceiver()
        self._vessels: dict[str, dict[str, Any]] = {}
        self._last_snapshot = 0.0

    # -- DecoderPlugin contract ---------------------------------------------

    def feed_iq(self, iq: np.ndarray) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        now = time.time()
        new_vessel = False
        for msg in self._rx.feed(iq):
            events.append(self._frame_event(msg, now))
            new_vessel |= self._update_vessel(msg, now)
        if events and (new_vessel or now - self._last_snapshot >= self._SNAPSHOT_INTERVAL):
            events.append(self._snapshot_event(now))
        return events

    def stop(self) -> None:
        # Pure in-process; no buffers to flush.
        pass

    def status(self) -> dict[str, Any]:
        return {
            "frames": self._rx.frames,
            "crc_failures": self._rx.crc_failures,
            "vessels": len(self._vessels),
            "feed_samples": self._rx.stats.feed_samples,
        }

    # -- event builders ------------------------------------------------------

    @staticmethod
    def _frame_event(msg: AisMessage, now: float) -> dict[str, Any]:
        event: dict[str, Any] = {
            "kind": "frame",
            "ts": now,
            "type": msg.type,
            "mmsi": msg.mmsi,
            "raw": msg.raw,
            "rssi_dbfs": round(msg.rssi_dbfs, 1),
        }
        # Type-specific fields surfaced in the frame event:
        if msg.speed_kn is not None:
            event["speed_kn"] = msg.speed_kn
        if msg.longitude is not None:
            event["longitude"] = msg.longitude
        if msg.latitude is not None:
            event["latitude"] = msg.latitude
        if msg.course_deg is not None:
            event["course_deg"] = msg.course_deg
        if msg.heading_deg is not None:
            event["heading_deg"] = msg.heading_deg
        if msg.timestamp_sec is not None:
            event["timestamp_sec"] = msg.timestamp_sec
        if msg.vessel_name is not None:
            event["vessel_name"] = msg.vessel_name
        if msg.callsign is not None:
            event["callsign"] = msg.callsign
        if msg.imo is not None:
            event["imo"] = msg.imo
        if msg.destination is not None:
            event["destination"] = msg.destination
        if msg.nav_status is not None:
            event["nav_status"] = msg.nav_status
        if msg.ship_type is not None:
            event["ship_type"] = msg.ship_type
        return event

    def _update_vessel(self, msg: AisMessage, now: float) -> bool:
        """Fold one message into the table; True when a NEW MMSI appeared."""
        row = self._vessels.get(msg.mmsi)
        if row is None:
            row = {
                "mmsi": msg.mmsi,
                "type": msg.type,
                "vessel_name": None,
                "callsign": None,
                "imo": None,
                "ship_type": None,
                "speed_kn": None,
                "longitude": None,
                "latitude": None,
                "course_deg": None,
                "heading_deg": None,
                "timestamp_sec": None,
                "nav_status": None,
                "destination": None,
                "frames": 0,
                "first_seen": now,
                "last_seen": now,
                "rssi_dbfs": msg.rssi_dbfs,
            }
            self._vessels[msg.mmsi] = row
            new = True
        else:
            new = False
        # Update only the non-None fields — the most recent message that
        # carried each field wins (Type 5 has name/callsign, Type 1 has
        # position; they alternate).
        if msg.vessel_name is not None:
            row["vessel_name"] = msg.vessel_name
        if msg.callsign is not None:
            row["callsign"] = msg.callsign
        if msg.imo is not None:
            row["imo"] = msg.imo
        if msg.ship_type is not None:
            row["ship_type"] = msg.ship_type
        if msg.speed_kn is not None:
            row["speed_kn"] = msg.speed_kn
        if msg.longitude is not None:
            row["longitude"] = msg.longitude
        if msg.latitude is not None:
            row["latitude"] = msg.latitude
        if msg.course_deg is not None:
            row["course_deg"] = msg.course_deg
        if msg.heading_deg is not None:
            row["heading_deg"] = msg.heading_deg
        if msg.timestamp_sec is not None:
            row["timestamp_sec"] = msg.timestamp_sec
        if msg.nav_status is not None:
            row["nav_status"] = msg.nav_status
        if msg.destination is not None:
            row["destination"] = msg.destination
        # The most recent message's TYPE stamps the row (so a Type 5
        # update doesn't overwrite a Type 1's position but does update
        # the "last message type" field for the UI to display).
        row["type"] = msg.type
        row["frames"] += 1
        row["last_seen"] = now
        row["rssi_dbfs"] = round(msg.rssi_dbfs, 1)
        return new

    def _snapshot_event(self, now: float) -> dict[str, Any]:
        self._last_snapshot = now
        vessels = sorted(self._vessels.values(), key=lambda r: -r["last_seen"])
        return {
            "kind": "vessel",
            "ts": now,
            "vessels": [
                {
                    "mmsi": r["mmsi"],
                    "type": r["type"],
                    "vessel_name": r["vessel_name"],
                    "callsign": r["callsign"],
                    "imo": r["imo"],
                    "ship_type": r["ship_type"],
                    "speed_kn": r["speed_kn"],
                    "longitude": r["longitude"],
                    "latitude": r["latitude"],
                    "course_deg": r["course_deg"],
                    "heading_deg": r["heading_deg"],
                    "nav_status": r["nav_status"],
                    "destination": r["destination"],
                    "frames": r["frames"],
                    "last_seen": r["last_seen"],
                    "rssi_dbfs": r["rssi_dbfs"],
                }
                for r in vessels
            ],
        }
