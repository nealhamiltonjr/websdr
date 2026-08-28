"""ADS-B / Mode S decoder plugin (ADR-003 bundled plugin #1).

Wraps the pure-Python Mode S demodulator (:mod:`.modes`) and layers the
per-receiver *aircraft table* on top: every CRC-valid frame updates a
row, and the plugin emits two event kinds downstream —

  ``frame``    one event per decoded Mode S frame (the message feed)
  ``aircraft`` a snapshot of the aircraft table (the list viz)

Snapshots fire immediately when a NEW aircraft appears, otherwise at
most every ``_SNAPSHOT_INTERVAL`` seconds so heavy traffic can't flood
the WS broadcast queues.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import numpy as np

from .base import DecoderManifest, DecoderPlugin
from .modes import MODE_S_SAMPLE_RATE, ModeSFrame, ModeSReceiver
from .registry import DecoderRegistry


@DecoderRegistry.register
class AdsbDecoderPlugin(DecoderPlugin):
    """Mode S / ADS-B: 1090 MHz, 2 MSPS IQ in, aircraft tracks out."""

    manifest: ClassVar[DecoderManifest] = DecoderManifest(
        name="adsb",
        version="0.1.0",
        label="ADS-B / Mode S",
        tap_point="rf_band",
        description=(
            "Mode S extended squitter decoder — DF11 all-call and DF17 "
            "ADS-B frames with CRC-24 verification, callsign and altitude "
            "extraction, per-receiver aircraft table. Requires exactly "
            "2 MSPS IQ (the classic RTL-SDR 1090 MHz rate). Position "
            "(CPR) and velocity decode land with the dump1090 subprocess "
            "plugin (ADR-003 roadmap)."
        ),
        required_sample_rate=MODE_S_SAMPLE_RATE,
        events=("frame", "aircraft"),
    )

    _SNAPSHOT_INTERVAL = 0.5  # s — coalesce table updates for busy skies

    def __init__(self) -> None:
        self._rx = ModeSReceiver()
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
        # The session cancels the feed loop before calling stop(); there is
        # no buffered state left to flush (every decoded frame was already
        # emitted, and snapshots only coalesce within 0.5 s).
        pass

    def status(self) -> dict[str, Any]:
        return {
            "frames": self._rx.frames,
            "crc_failures": self._rx.crc_failures,
            "aircraft": len(self._aircraft),
        }

    # -- event builders ------------------------------------------------------

    @staticmethod
    def _frame_event(frame: ModeSFrame, now: float) -> dict[str, Any]:
        event: dict[str, Any] = {
            "kind": "frame",
            "ts": now,
            "df": frame.df,
            "icao": frame.icao,
            "raw": frame.raw,
            "parity": frame.parity,
            "rssi_dbfs": round(frame.rssi_dbfs, 1),
        }
        if frame.callsign is not None:
            event["callsign"] = frame.callsign
        if frame.altitude_ft is not None:
            event["altitude_ft"] = frame.altitude_ft
        return event

    def _update_aircraft(self, frame: ModeSFrame, now: float) -> bool:
        """Fold one frame into the table; True when a NEW ICAO appeared."""
        icao = frame.icao
        if icao is None:
            return False  # formats without an ICAO can't build a track
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
                }
                for r in aircraft
            ],
        }
