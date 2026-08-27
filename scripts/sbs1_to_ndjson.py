#!/usr/bin/env python3
"""SBS1 → NDJSON bridge for stock dump1090-class binaries (slice-16).

**Why this exists.** The OpenWebRX+ subprocess decoder contract
(``plugins/dump1090.py`` / ADR-003) expects a child binary to:

  * read IQ on stdin (``cf32`` / ``cs16`` / ``cu8``),
  * emit NDJSON events on stdout (``{"kind": "ready" | "frame" |
    "aircraft" | "decoder_state" | "iq_stats" | "warning"}``).

Stock dump1090 / dump1090-mutability / readsb releases speak **SBS1
(BaseStation)** CSV on TCP port 30003 instead of NDJSON on stdout —
they have no stdout-NDJSON mode at all. ``plugins/dump1090.py`` ships a
``tests/fakes/fake_dump1090.py`` that pins the OpenWebRX+ contract;
the roadmap ("dump1090 real-binary bring-up") asked for a thin SBS1
translator so a real binary can drop in.

This script IS that translator. Point ``OPENWEBRX_PLUS_DUMP1090_BIN`` at
it:

.. code-block:: bash

    OPENWEBRX_PLUS_DUMP1090_BIN=python3 \\
        scripts/sbs1_to_ndjson.py

What the wrapper does:

  1. Reads the OpenWebRX+ env vars (``OWRX_RX_ID``, ``OWRX_SAMPLE_RATE``,
     ``OWRX_CENTER_FREQ``, ``OWRX_IQ_FORMAT``).
  2. Picks an ephemeral TCP port for the SBS1 socket (avoids colliding
     with any host dump1090 already running).
  3. Spawns the real dump1090 binary with ``--ifile - --net
     --net-sbs-port <ephemeral> --net-only`` (configurable via
     ``OPENWEBRX_PLUS_DUMP1090_REAL_BIN`` + ``_REAL_ARGS``).
  4. Forwards stdin IQ (any of cf32/cs16/cu8) → the child's stdin
     (``cs16`` by default, matching dump1090's ``--iformat SC16``).
  5. Connects to the SBS1 socket, parses each ``MSG,...`` CSV line,
     emits ``{"kind": "frame", ...}`` matching the fake binary's
     schema. Coalesces aircraft snapshots every ~300 ms.
  6. Emits ``{"kind": "ready", ...}`` immediately after spawning the
     child — the OpenWebRX+ runner's handshake (consumed, not
     forwarded to clients).

Teardown:

  * On stdin EOF → close child stdin → wait up to 2 s for child exit →
    SIGKILL if still alive → emit final aircraft snapshot → exit 0.
  * On child crash → emit ``{"kind": "decoder_state", "state":
    "failed", "reason": "sbs1_child_exited_early"}`` and exit 1 (the
    runner's bounded crash-restart will respawn us).
  * On SIGTERM/SIGINT → forward to child, drain SBS1 socket for up to
    200 ms, exit 0.

**SBS1 format reference** (BaseStation-1 / SBS-1, used by dump1090's
``--net-sbs-port``):

    MSG,<msg_type>,<tx_type>,<session>,<aircraft_id>,<hex_ident>,\\
<flight_id>,<date_msg>,<time_msg>,<date_log>,<time_log>,<callsign>,\\
<altitude>,<groundspeed>,<track>,<latitude>,<longitude>,\\
<vertical_rate>,<squawk>,<alert>,<emergency>,<spi>,<spi_flags>

Fields by msg_type (1-indexed after the leading ``MSG``):
  - 1, 2 → callsign only
  - 3 → altitude, groundspeed, track, lat, lon
  - 4 → groundspeed, track, vertical_rate
  - 5, 6 → surface position (altitude=0, lat, lon)
  - 7, 8 → airborne position reset

CSV separator: ``,`` (canonical) or ``|`` (some forks). We accept both.
Empty fields stay empty (caller treats as None).
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import os
import signal
import socket
import sys
import threading
import time
from typing import Any

# --- env vars (matching plugins/subprocess.py + dump1090.py) ----------------
_RX_ID = os.environ.get("OWRX_RX_ID", "")
_SAMPLE_RATE = int(os.environ.get("OWRX_SAMPLE_RATE", "2000000"))
_CENTER_FREQ = int(os.environ.get("OWRX_CENTER_FREQ", "0"))
_IQ_FORMAT = os.environ.get("OWRX_IQ_FORMAT", "cs16")
_REAL_BIN = os.environ.get("OPENWEBRX_PLUS_DUMP1090_REAL_BIN", "dump1090")
_REAL_ARGS = os.environ.get(
    "OPENWEBRX_PLUS_DUMP1090_REAL_ARGS",
    "--ifile - --iformat SC16 --sample-rate 2000000 --quiet --net --net-only",
)
_SBS_PORT_HINT = int(os.environ.get("OPENWEBRX_PLUS_DUMP1090_SBS_PORT", "0"))
_SNAPSHOT_INTERVAL_S = float(
    os.environ.get("OPENWEBRX_PLUS_DUMP1090_SNAPSHOT_INTERVAL", "0.3")
)

# SBS1 fields after the leading "MSG" token, 1-indexed (see module docstring).
_SBS_FIELDS = (
    "msg_type",          # 1
    "tx_type",           # 2
    "session_id",        # 3
    "aircraft_id",       # 4
    "icao",              # 5  ← hex tail
    "flight_id",         # 6
    "date_msg",          # 7
    "time_msg",          # 8
    "date_log",          # 9
    "time_log",          # 10
    "callsign",          # 11
    "altitude",          # 12  ← feet
    "groundspeed",       # 13  ← knots
    "track",             # 14  ← degrees
    "latitude",          # 15
    "longitude",         # 16
    "vertical_rate",     # 17  ← ft/min
    "squawk",            # 18  ← octal string
    "alert",             # 19
    "emergency",         # 20
    "spi",               # 21
    "spi_flags",         # 22
)


def _emit(obj: dict[str, object]) -> None:
    """Print one JSON object + newline, flush immediately."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _emit_ready(child_pid: int, sbs_port: int, argv: list[str]) -> None:
    _emit(
        {
            "kind": "ready",
            "software": "sbs1-bridge/1.0",
            "pid": os.getpid(),
            "child_pid": child_pid,
            "iq_format": _IQ_FORMAT,
            "receiver_id": _RX_ID,
            "sample_rate": _SAMPLE_RATE,
            "center_freq": _CENTER_FREQ,
            "sbs_port": sbs_port,
            "real_binary": _REAL_BIN,
            "argv": argv,
        }
    )


def _emit_decoder_state(state: str, reason: str) -> None:
    _emit({"kind": "decoder_state", "state": state, "reason": reason})


# --- IQ format conversion (cf32 ⇄ cs16 / cu8) --------------------------------
def _stdin_to_cs16(raw: bytes, fmt: str) -> bytes:
    """Convert a chunk of bytes in the OpenWebRX+ runner format ``fmt``
    to interleaved int16 (SC16, dump1090's ``--iformat SC16``).

    ``cs16`` → passthrough (just memcpy).
    ``cf32`` → complex64 view, scaled by 32767, clipped to int16 range.
    ``cu8`` → uint8 → [-128, 127] → int16 range.
    """
    import numpy as np

    if fmt == "cs16":
        return raw
    if fmt == "cf32":
        if len(raw) % 8:
            raw = raw[: -(len(raw) % 8)]
        if not raw:
            return b""
        iq = np.frombuffer(raw, dtype=np.complex64)
        if not iq.size:
            return b""
        re = iq.real * 32767.0
        im = iq.imag * 32767.0
        out = np.empty(iq.size * 2, dtype=np.int16)
        out[0::2] = np.clip(re, -32768, 32767).astype(np.int16)
        out[1::2] = np.clip(im, -32768, 32767).astype(np.int16)
        return out.tobytes()
    if fmt == "cu8":
        if not raw:
            return b""
        u = np.frombuffer(raw, dtype=np.uint8)
        if not u.size:
            return b""
        center = 127.5
        i16 = ((u - center) * 257.0).astype(np.int16)
        if i16.size % 2:
            i16 = i16[:-1]
        return i16.tobytes()
    raise ValueError(f"unsupported OWRX_IQ_FORMAT={fmt!r}")


# --- SBS1 CSV parsing --------------------------------------------------------
def _split_sbs1(line: str) -> list[str] | None:
    """Parse one SBS1 line into a list of field values (no leading
    'MSG' token). Returns None for non-MSG lines (AIR, ID, etc.) which
    we don't translate.
    """
    line = line.strip()
    if not line:
        return None
    if not line.startswith("MSG"):
        return None
    body = line[3:].lstrip(",|")
    # Tolerate comma OR pipe as separator (forks differ).
    sep = "|" if "|" in body and body.count("|") >= body.count(",") - 1 else ","
    try:
        rows = list(csv.reader(io.StringIO(body), delimiter=sep))
    except csv.Error:
        return None
    if not rows:
        return None
    return rows[0]


def _parse_sbs1_line(line: str) -> dict[str, object] | None:
    """Translate one SBS1 MSG line into an OpenWebRX+ ``frame`` event.

    Returns None for non-MSG lines or truncated MSG rows. The ICAO hex
    is mandatory (we skip rows without it — they can't join an aircraft
    table row).
    """
    fields = _split_sbs1(line)
    if fields is None:
        return None
    while len(fields) < len(_SBS_FIELDS):
        fields.append("")
    rec = {
        name: (val.strip() or None)
        for name, val in zip(_SBS_FIELDS, fields, strict=False)
    }
    if not rec["icao"]:
        return None
    try:
        mt = int(rec["msg_type"]) if rec["msg_type"] else 0
    except ValueError:
        mt = 0
    df = 17 if 1 <= mt <= 4 else (20 if mt == 5 else 0)

    event: dict[str, object] = {
        "kind": "frame",
        "ts": time.time(),
        "df": df,
        "icao": rec["icao"].upper(),
        "raw": line,
        "parity": None,
        "rssi_dbfs": 0.0,
    }
    if rec["callsign"]:
        event["callsign"] = rec["callsign"].strip()
    if rec["altitude"]:
        try:
            event["altitude_ft"] = int(rec["altitude"])
        except ValueError:
            pass
    if rec["groundspeed"]:
        try:
            event["groundspeed_kt"] = int(rec["groundspeed"])
        except ValueError:
            pass
    if rec["track"]:
        try:
            event["track_deg"] = float(rec["track"])
        except ValueError:
            pass
    if rec["latitude"] and rec["longitude"]:
        try:
            event["lat"] = float(rec["latitude"])
            event["lon"] = float(rec["longitude"])
        except ValueError:
            pass
    if rec["vertical_rate"]:
        try:
            event["vertical_rate_fpm"] = int(rec["vertical_rate"])
        except ValueError:
            pass
    if rec["squawk"]:
        event["squawk"] = rec["squawk"].strip()
    return event


def _row_from_frame(frame: dict[str, object]) -> dict[str, object]:
    """Project a frame event into an aircraft-table row (the fake binary
    emits the same shape — icao, callsign, altitude_ft, lat, lon,
    groundspeed_kt, vertical_rate_fpm, frames, rssi_dbfs).
    """
    row: dict[str, object] = {
        "icao": frame["icao"],
        "callsign": frame.get("callsign"),
        "altitude_ft": frame.get("altitude_ft"),
        "frames": 1,
        "rssi_dbfs": float(frame.get("rssi_dbfs", 0.0) or 0.0),
    }
    if "lat" in frame:
        row["lat"] = frame["lat"]
        row["lon"] = frame["lon"]
    if "groundspeed_kt" in frame:
        row["groundspeed_kt"] = frame["groundspeed_kt"]
    if "vertical_rate_fpm" in frame:
        row["vertical_rate_fpm"] = frame["vertical_rate_fpm"]
    if "squawk" in frame:
        row["squawk"] = frame["squawk"]
    return row


def _merge_row(old: dict[str, object], frame: dict[str, object]) -> bool:
    """Update an aircraft row in place with new frame fields. Returns
    True if any field changed (used to decide snapshot emission).
    """
    changed = False
    for key in ("callsign", "altitude_ft", "lat", "lon",
                "groundspeed_kt", "vertical_rate_fpm", "squawk"):
        v = frame.get(key)
        if v is not None and old.get(key) != v:
            old[key] = v
            changed = True
    old["frames"] = int(old.get("frames", 0)) + 1
    old["rssi_dbfs"] = float(frame.get("rssi_dbfs", 0.0) or 0.0)
    return changed


# --- SBS1 socket pump (background thread) -----------------------------------
class _Sbs1Reader(threading.Thread):
    """Read SBS1 lines from a TCP socket, translate to NDJSON dicts,
    queue them for the main thread to emit.

    Threads (not asyncio) keep this script self-contained — no external
    dependency on asyncio's complexity, no shared event loop with the
    dump1090 child.
    """

    def __init__(self, host: str, port: int, connect_timeout: float) -> None:
        super().__init__(daemon=True)
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._sock: socket.socket | None = None
        self._buf = bytearray()
        self._queue: list[dict[str, object]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._connected = threading.Event()

    def connect(self) -> bool:
        """Try to connect; return True on success. Retries for up to
        ``connect_timeout`` seconds (the child takes a moment to bind)."""
        deadline = time.monotonic() + self._connect_timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                s = socket.create_connection(
                    (self._host, self._port), timeout=2.0
                )
                s.settimeout(0.2)
                self._sock = s
                self._connected.set()
                return True
            except OSError:
                time.sleep(0.05)
        return False

    def run(self) -> None:
        assert self._sock is not None
        sock = self._sock
        try:
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                self._buf.extend(chunk)
                while b"\n" in self._buf:
                    nl = self._buf.index(b"\n")
                    line = self._buf[:nl].decode("ascii", errors="replace")
                    del self._buf[: nl + 1]
                    ev = _parse_sbs1_line(line)
                    if ev is not None:
                        with self._lock:
                            self._queue.append(ev)
        finally:
            with contextlib.suppress(OSError):
                sock.close()

    def drain(self) -> list[dict[str, object]]:
        with self._lock:
            if not self._queue:
                return []
            out = self._queue[:]
            self._queue.clear()
            return out

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.shutdown(socket.SHUT_RDWR)


# --- Main loop ---------------------------------------------------------------
def _spawn_child(sbs_port: int) -> "Any | None":
    import shlex
    import subprocess

    argv = [_REAL_BIN, *shlex.split(_REAL_ARGS), "--net-sbs-port", str(sbs_port)]
    try:
        return subprocess.Popen(  # noqa: S603 — argv is user-configurable
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=sys.stderr,
            close_fds=True,
        )
    except FileNotFoundError:
        return None


def _pump_stdin_to_child(child_stdin: Any, stop: threading.Event) -> None:
    """Background thread: read our stdin → translate format → write to
    the dump1090 child's stdin.

    Reads in 64 KB chunks; the OpenWebRX+ runner pipes binary IQ bytes
    through, and our format converter handles cf32 → cs16 (the child's
    --iformat SC16 expectation).
    """
    try:
        while not stop.is_set():
            chunk = sys.stdin.buffer.read1(65536)
            if not chunk:
                break
            converted = _stdin_to_cs16(chunk, _IQ_FORMAT)
            if not converted:
                continue
            try:
                child_stdin.write(converted)
                child_stdin.flush()
            except (BrokenPipeError, OSError):
                break
    finally:
        with contextlib.suppress(Exception):
            child_stdin.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="SBS1 → NDJSON bridge")
    parser.add_argument(
        "--connect-host",
        default=os.environ.get("OPENWEBRX_PLUS_DUMP1090_SBS_HOST", "127.0.0.1"),
        help="host to connect to the SBS1 socket on (default 127.0.0.1)",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=float(os.environ.get("OPENWEBRX_PLUS_DUMP1090_CONNECT_TIMEOUT", "5.0")),
        help="seconds to wait for the child's SBS1 socket to bind",
    )
    parser.add_argument(
        "--no-spawn",
        action="store_true",
        help="don't spawn a child; just bridge to an existing SBS1 "
        "server at --connect-host:--connect-port",
    )
    parser.add_argument(
        "--connect-port",
        type=int,
        default=0,
        help="explicit SBS1 port (else: ephemeral, picked by the OS)",
    )
    args = parser.parse_args()

    # Pick the SBS1 port: explicit > env hint > ephemeral (kernel picks).
    sbs_port = args.connect_port or _SBS_PORT_HINT
    child_proc = None
    if not args.no_spawn:
        # Pick an ephemeral port: bind a socket, read its port, close.
        # Pass it to the child's --net-sbs-port so the bind succeeds.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", sbs_port))
            sbs_port = probe.getsockname()[1]
        child_proc = _spawn_child(sbs_port)
        if child_proc is None:
            _emit_decoder_state("failed", "child_binary_missing")
            sys.stderr.write(
                f"sbs1_to_ndjson: child binary {_REAL_BIN!r} not found on PATH\n"
            )
            return 1
    else:
        if sbs_port == 0:
            sys.stderr.write(
                "sbs1_to_ndjson: --no-spawn requires --connect-port or "
                "OPENWEBRX_PLUS_DUMP1090_SBS_PORT\n"
            )
            return 1

    # Emit ready immediately so the runner's handshake completes.
    _emit_ready(
        child_pid=child_proc.pid if child_proc else 0,
        sbs_port=sbs_port,
        argv=[_REAL_BIN, _REAL_ARGS],
    )

    # Start the SBS1 reader thread (connects with retry).
    reader = _Sbs1Reader(args.connect_host, sbs_port, args.connect_timeout)
    if not reader.connect():
        _emit_decoder_state("failed", "sbs1_socket_connect_timeout")
        sys.stderr.write(
            f"sbs1_to_ndjson: could not connect to SBS1 socket at "
            f"{args.connect_host}:{sbs_port} within {args.connect_timeout:g}s\n"
        )
        if child_proc is not None:
            child_proc.terminate()
            try:
                child_proc.wait(timeout=2.0)
            except Exception:  # noqa: BLE001
                with contextlib.suppress(ProcessLookupError):
                    child_proc.kill()
        return 1
    reader.start()

    # Start the stdin pump (background thread).
    stop_pump = threading.Event()
    pump: threading.Thread | None = None
    if child_proc is not None and child_proc.stdin is not None:
        pump = threading.Thread(
            target=_pump_stdin_to_child,
            args=(child_proc.stdin, stop_pump),
            daemon=True,
        )
        pump.start()

    # Forward SIGTERM/SIGINT to the child and trigger clean shutdown.
    def _sigterm(signum: int, frame: Any) -> None:  # noqa: ARG001
        stop_pump.set()
        if child_proc is not None:
            with contextlib.suppress(ProcessLookupError):
                child_proc.send_signal(signal.SIGTERM)
        reader.stop()

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    # Main loop: drain the SBS1 reader queue → emit NDJSON, watch the
    # child for early exit, coalesce aircraft snapshots every ~300 ms.
    aircraft: dict[str, dict[str, object]] = {}
    last_snapshot = 0.0
    exit_code = 0

    try:
        while True:
            # Drain the SBS1 reader queue.
            for ev in reader.drain():
                _emit(ev)
                icao = ev.get("icao")
                if isinstance(icao, str):
                    row = aircraft.get(icao)
                    if row is None:
                        aircraft[icao] = _row_from_frame(ev)
                    else:
                        _merge_row(row, ev)

            now = time.time()
            if aircraft and now - last_snapshot >= _SNAPSHOT_INTERVAL_S:
                last_snapshot = now
                _emit(
                    {"kind": "aircraft", "ts": now,
                     "aircraft": list(aircraft.values())}
                )

            # Check the child for early exit.
            if child_proc is not None:
                rc = child_proc.poll()
                if rc is not None:
                    if rc != 0:
                        _emit_decoder_state(
                            "failed", f"sbs1_child_exited_code_{rc}"
                        )
                        exit_code = 1
                    break

            # Check our own stdin — if the runner closed it, we're done.
            if pump is not None and not pump.is_alive() and not reader._stop.is_set():
                time.sleep(0.2)  # let the reader tail drain
                for ev in reader.drain():
                    _emit(ev)
                    icao = ev.get("icao")
                    if isinstance(icao, str):
                        row = aircraft.get(icao)
                        if row is None:
                            aircraft[icao] = _row_from_frame(ev)
                        else:
                            _merge_row(row, ev)
                if aircraft:
                    _emit(
                        {"kind": "aircraft", "ts": time.time(),
                         "aircraft": list(aircraft.values())}
                    )
                break

            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        stop_pump.set()
        reader.stop()
        reader.join(timeout=2.0)
        if child_proc is not None:
            try:
                child_proc.terminate()
                child_proc.wait(timeout=2.0)
            except Exception:  # noqa: BLE001
                with contextlib.suppress(ProcessLookupError):
                    child_proc.kill()
        if aircraft:
            _emit(
                {"kind": "aircraft", "ts": time.time(),
                 "aircraft": list(aircraft.values())}
            )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
