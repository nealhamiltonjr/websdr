#!/usr/bin/env python3
"""Protocol-faithful fake dump1090 — implements the OpenWebRX+ subprocess
decoder contract exactly (ADR-003 / plugins/dump1090.py):

  * reads IQ on stdin in the format named by ``OWRX_IQ_FORMAT``
    (cf32 / cs16 / cu8), tolerating arbitrary chunk boundaries
  * emits ``{"kind": "ready", …}`` first (consumed by the runner as the
    handshake; carries argv/env echoes the tests assert on)
  * emits ``{"kind": "frame", …}`` per CRC-valid Mode S frame and
    ``{"kind": "aircraft", …}`` snapshots — same row schema as the
    in-process adsb plugin PLUS synthetic lat/lon/groundspeed/vertical
    rate fields (marked ``position_source: "synthetic"`` — the baked
    fixture carries no real CPR data; real binaries decode real ones)
  * failure modes for the runner's crash/restart/garbage/stall tests:
    ``--crash-after N [--crash-marker PATH]``, ``--garbage-lines N``,
    ``--stall-secs S``, and ``--echo-stats`` (per-chunk RMS instead of
    demodulation, for the IQ format-conversion tests)

The demodulator is the REAL production one (openwebrx_plus.plugins.modes)
— this fake is only "fake" about where positions come from.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

# Make openwebrx_plus importable when spawned from anywhere. Under the
# project venv the editable install already covers this; the sys.path
# entry is belt-and-suspenders for manual runs with a bare python.
_REPO_ROOT = Path(__file__).resolve().parents[2]  # → apps/server
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from openwebrx_plus.plugins.modes import (  # noqa: E402
    MODE_S_SAMPLE_RATE,
    ModeSFrame,
    ModeSReceiver,
)

_CHUNK = 16384  # stdin read granularity (bytes)
_SNAPSHOT_INTERVAL = 0.3  # s — coalesce aircraft snapshots


def stdin_to_cf32(buf: bytearray, fmt: str) -> np.ndarray:
    """Consume whole samples from ``buf``; return cf32 (leftover bytes stay)."""
    if fmt == "cf32":
        step = 8
    elif fmt == "cs16":
        step = 4
    elif fmt == "cu8":
        step = 2
    else:
        raise SystemExit(f"unknown OWRX_IQ_FORMAT: {fmt}")
    usable = (len(buf) // step) * step
    raw = bytes(buf[:usable])
    del buf[:usable]
    if fmt == "cf32":
        return np.frombuffer(raw, dtype="<c8").astype(np.complex64)
    if fmt == "cs16":
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0
    else:  # cu8
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5) / 127.5
    return (a[0::2] + 1j * a[1::2]).astype(np.complex64)


def synthetic_track(icao: str) -> dict[str, object]:
    """Deterministic pseudo-position per ICAO (fixture has no CPR data)."""
    seed = int(icao, 16)
    return {
        "lat": round(37.0 + (seed % 1000) / 100.0, 5),
        "lon": round(-122.0 + (seed % 777) / 100.0, 5),
        "groundspeed_kt": 200 + seed % 150,
        "vertical_rate_fpm": -800 + (seed % 1600),
        "position_source": "synthetic",
    }


def frame_event(frame: ModeSFrame, now: float) -> dict[str, object]:
    event: dict[str, object] = {
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


def main() -> None:
    parser = argparse.ArgumentParser(description="fake dump1090 (OpenWebRX+ contract)")
    parser.add_argument("--crash-after", type=int, default=0,
                        help="exit(1) after N stdin reads (0 = never)")
    parser.add_argument("--crash-marker", default="",
                        help="only crash if this file does NOT exist yet (created on crash)")
    parser.add_argument("--garbage-lines", type=int, default=0,
                        help="emit N unparseable stdout lines after the ready line")
    parser.add_argument("--stall-secs", type=float, default=0.0,
                        help="sleep before reading any stdin (backpressure tests)")
    parser.add_argument("--echo-stats", action="store_true",
                        help="emit iq_stats events instead of demodulating")
    args = parser.parse_args()

    fmt = os.environ.get("OWRX_IQ_FORMAT", "cs16")

    def emit(obj: dict[str, object]) -> None:
        print(json.dumps(obj), flush=True)

    if args.stall_secs:
        # Stalls BEFORE the ready line: exercises both the ready-timeout
        # path (runner gives up waiting) and the drop-counter path (runner
        # feeds a child that isn't reading).
        time.sleep(args.stall_secs)

    emit({
        "kind": "ready",
        "software": "fake-dump1090/1.0",
        "pid": os.getpid(),
        "iq_format": fmt,
        "receiver_id": os.environ.get("OWRX_RX_ID"),
        "sample_rate": os.environ.get("OWRX_SAMPLE_RATE"),
        "expected_rate": MODE_S_SAMPLE_RATE,
        "argv": sys.argv[1:],
    })
    for _ in range(args.garbage_lines):
        print("warning: this line is deliberately not JSON", flush=True)

    rx = ModeSReceiver()
    aircraft: dict[str, dict[str, object]] = {}
    last_snapshot = 0.0
    reads = 0
    buf = bytearray()

    while True:
        # read1: return whatever the pipe holds now — a streaming consumer
        # must not block for the full chunk while the server paces writes.
        raw = sys.stdin.buffer.read1(_CHUNK)
        if not raw:
            break  # EOF — server closed stdin (graceful stop)
        reads += 1
        if args.echo_stats:
            buf.extend(raw)
            iq = stdin_to_cf32(buf, fmt)
            if iq.size:
                rms = float(np.sqrt(np.mean(np.square(np.abs(iq)))))
                emit({"kind": "iq_stats", "count": int(iq.size), "rms": rms})
            continue
        if args.crash_after and reads >= args.crash_after and (
            not args.crash_marker or not os.path.exists(args.crash_marker)
        ):
            if args.crash_marker:
                Path(args.crash_marker).write_text(str(os.getpid()))
            sys.stderr.write("fake_dump1090: simulated crash\n")
            sys.exit(1)
        buf.extend(raw)
        iq = stdin_to_cf32(buf, fmt)
        if not iq.size:
            continue
        now = time.time()
        new_info = False
        for frame in rx.feed(iq):
            emit(frame_event(frame, now))
            icao = frame.icao
            if icao is None:
                continue
            row = aircraft.get(icao)
            if row is None:
                row = {"icao": icao, "callsign": None, "altitude_ft": None,
                       "frames": 0, "rssi_dbfs": 0.0, **synthetic_track(icao)}
                aircraft[icao] = row
                new_info = True
            if frame.callsign is not None and row["callsign"] != frame.callsign:
                row["callsign"] = frame.callsign
                new_info = True
            if frame.altitude_ft is not None and row["altitude_ft"] != frame.altitude_ft:
                row["altitude_ft"] = frame.altitude_ft
                new_info = True
            row["frames"] = int(row["frames"]) + 1
            row["rssi_dbfs"] = round(frame.rssi_dbfs, 1)
        # Emit on new information (mirrors dump1090's aircraft.json refresh)
        # or at a floor rate for "still here" heartbeats.
        if aircraft and (new_info or now - last_snapshot >= _SNAPSHOT_INTERVAL):
            last_snapshot = now
            emit({"kind": "aircraft", "ts": now, "aircraft": list(aircraft.values())})

    # Final flush on clean EOF
    if aircraft:
        emit({"kind": "aircraft", "ts": time.time(), "aircraft": list(aircraft.values())})


if __name__ == "__main__":
    main()
