"""dump978 UAT decoder plugin (ADR-003 in-process plugin family #4).

Mirrors :mod:`.adsb` (the in-process Mode S plugin) but for 978 MHz
UAT downlink messages. Wraps the pure-Python 2-GFSK demodulator
(:mod:`.uat_demod`) and layers a per-receiver *aircraft table* on top,
emitting the same ``frame`` + ``aircraft`` event schema as ADS-B so the
frontend :class:`AircraftListViz` renders UAT aircraft alongside ADS-B.

Sample-rate contract: requires :data:`.uat_protocol.UAT_SAMPLE_RATE`
(2.083333 MSPS = 2 samples/symbol of 1.0416667 Mbps). Real RTL-SDR
sources produce 2 MSPS exactly; until a DDC resampler is added in
front, the plugin is best driven by a fixture-backed 2.083333 MSPS
receiver (the synthetic IQ fixture ships for testing) or a SpyServer
that's already at the right rate.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import numpy as np

from .base import DecoderManifest, DecoderPlugin
from .registry import DecoderRegistry
from .uat_demod import UAT_SAMPLE_RATE, UatReceiver
from .uat_protocol import UatFrame


@DecoderRegistry.register
class Dump978Plugin(DecoderPlugin):
    """UAT / dump978: 978 MHz, 2.083333 MSPS IQ in, aircraft tracks out."""

    manifest: ClassVar[DecoderManifest] = DecoderManifest(
        name="dump978",
        version="0.1.0",
        label="UAT / dump978",
        tap_point="rf_band",
        description=(
            "FAA UAT (Universal Access Transceiver) decoder — 978 MHz "
            "2-GFSK demodulator + RS-CRC verification + basic downlink "
            "message field decode (ICAO + callsign + altitude). "
            "Per-receiver aircraft table; same wire schema as the in-"
            "process 'adsb' plugin so the aircraft viz works with both."
            "Position (lat/lon) decode for TIS-B/CPR messages lands in "
            "a later slice — frames still emit raw for downstream tools."
        ),
        required_sample_rate=UAT_SAMPLE_RATE,
        events=("frame", "aircraft"),
    )

    _SNAPSHOT_INTERVAL = 0.5  # s — coalesce table updates for busy skies

    def __init__(self) -> None:
        self._rx = UatReceiver()
        self._aircraft: dict[str, dict[str, Any]] = {}
        self._last_snapshot = 0.0

    # -- DecoderPlugin contract ---------------------------------------------

    def feed_iq(self, iq: np.ndarray) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        now = time.time()
        new_aircraft = False
        for frame in self._rx.feed(iq):
            events.append(self._frame_event(frame, now))
            if frame.icao is not None:
                new_aircraft |= self._update_aircraft(frame, now)
        if events and (new_aircraft or now - self._last_snapshot >= self._SNAPSHOT_INTERVAL):
            events.append(self._snapshot_event(now))
        return events

    def stop(self) -> None:
        # Nothing to flush — frames emit immediately, snapshots coalesce
        # within 0.5 s. The session's feed loop is already cancelled.
        pass

    def status(self) -> dict[str, Any]:
        return {
            "frames": self._rx.frames,
            "crc_failures": self._rx.crc_failures,
            "aircraft": len(self._aircraft),
        }

    # -- event builders ------------------------------------------------------

    @staticmethod
    def _frame_event(frame: UatFrame, now: float) -> dict[str, Any]:
        event: dict[str, Any] = {
            "kind": "frame",
            "ts": now,
            "frame_length": frame.frame_length,
            "icao": frame.icao,
            "raw": frame.raw,
            "rssi_dbfs": round(frame.rssi_dbfs, 1),
        }
        if frame.callsign is not None:
            event["callsign"] = frame.callsign
        if frame.altitude_ft is not None:
            event["altitude_ft"] = frame.altitude_ft
        if frame.lat is not None:
            event["lat"] = frame.lat
            event["lon"] = frame.lon
        return event

    def _update_aircraft(self, frame: UatFrame, now: float) -> bool:
        """Fold one frame into the table; True when a NEW ICAO appeared."""
        icao = frame.icao
        if icao is None:
            return False
        row = self._aircraft.get(icao)
        if row is None:
            row = {
                "icao": icao,
                "callsign": None,
                "altitude_ft": None,
                "frames": 0,
                "first_seen": now,
                "last_seen": now,
                "rssi_dbfs": frame.rssi_dbfs,
            }
            self._aircraft[icao] = row
            new = True
        else:
            new = False
        if frame.callsign is not None:
            row["callsign"] = frame.callsign
        if frame.altitude_ft is not None:
            row["altitude_ft"] = frame.altitude_ft
        if frame.lat is not None:
            row["lat"] = frame.lat
            row["lon"] = frame.lon
        row["frames"] += 1
        row["last_seen"] = now
        row["rssi_dbfs"] = round(frame.rssi_dbfs, 1)
        return new

    def _snapshot_event(self, now: float) -> dict[str, Any]:
        self._last_snapshot = now
        aircraft = sorted(self._aircraft.values(), key=lambda r: -r["last_seen"])
        return {
            "kind": "aircraft",
            "ts": now,
            "aircraft": [
                {
                    "icao": r["icao"],
                    "callsign": r["callsign"],
                    "altitude_ft": r["altitude_ft"],
                    "frames": r["frames"],
                    "last_seen": r["last_seen"],
                    "rssi_dbfs": r["rssi_dbfs"],
                    **({"lat": r["lat"], "lon": r["lon"]} if "lat" in r else {}),
                }
                for r in aircraft
            ],
        }
