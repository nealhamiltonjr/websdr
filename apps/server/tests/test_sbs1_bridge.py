"""Tests for the SBS1 → NDJSON bridge (slice-16).

The bridge lets a stock dump1090 / readsb binary (which speaks SBS1 CSV
on TCP) drop into the OpenWebRX+ subprocess decoder contract (which
expects NDJSON on stdout). These tests cover the pure-parser surface
(SBS1 field parsing, IQ format conversion, row merging) plus an
end-to-end run against a fake SBS1 server (no real dump1090 binary
needed; the bridge accepts --no-spawn to bridge to an external SBS1
emitter).

The bridge script lives at ``scripts/sbs1_to_ndjson.py`` (repo-root
level, alongside ``generate_iq_fixtures.py``). It's a standalone Python
3.12 script — importable as a module for testing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import sbs1_to_ndjson as m  # noqa: E402  # type: ignore[import-not-found]

# ---------------------------------------------------------------------------
# Pure-parser unit tests (no I/O)
# ---------------------------------------------------------------------------


class TestSbs1LineParsing:
    def test_non_msg_line_returns_none(self) -> None:
        assert m._parse_sbs1_line("AIR,,,") is None
        assert m._parse_sbs1_line("") is None
        assert m._parse_sbs1_line("SELECT,1,2") is None

    def test_empty_icao_returns_none(self) -> None:
        """An MSG row with no hex_ident is untranslatable — we'd lose
        the row's identity, so the caller skips."""
        line = (
            "MSG,3,1,1,1,,1,2024/01/01,00:00:00.000,2024/01/01,"
            "00:00:00.000,,40000,300,90,37.0,-122.0,-1000,1234,,0,0,0,0"
        )
        assert m._parse_sbs1_line(line) is None

    def test_airborne_position_msg_translates_full(self) -> None:
        """MSG type 3 = airborne position. Standard dump1090 layout:
        MSG,3,<tx>,<session>,<aircraft_id>,<hex_ident>,<flight_id>,
        <date>,<time>,<date>,<time>,<callsign>,<altitude>,<gs>,<trk>,
        <lat>,<lon>,<vrate>,<squawk>,<alert>,<emergency>,<spi>,<spi_flags>
        """
        line = (
            "MSG,3,1,1,1,4D22AA,1,2024/01/01,00:00:00.000,2024/01/01,"
            "00:00:00.000,UAL456,40000,300,90,37.5,-122.5,-1000,1234,,"
            "0,0,0,0"
        )
        ev = m._parse_sbs1_line(line)
        assert ev is not None
        assert ev["kind"] == "frame"
        assert ev["icao"] == "4D22AA"
        assert ev["df"] == 17  # ADS-B analogue
        assert ev["callsign"] == "UAL456"
        assert ev["altitude_ft"] == 40000
        assert ev["groundspeed_kt"] == 300
        assert ev["track_deg"] == 90.0
        assert ev["lat"] == 37.5
        assert ev["lon"] == -122.5
        assert ev["vertical_rate_fpm"] == -1000
        assert ev["squawk"] == "1234"
        assert ev["raw"] == line

    def test_pipe_separator_fork_accepted(self) -> None:
        """Some dump1090 forks emit pipe-separated SBS1. The parser
        detects the dominant separator automatically."""
        line = (
            "MSG|3|1|1|1|A1B2C3|1|2024/01/01|00:00:00.000|2024/01/01|"
            "00:00:00.000||40000|300|90|37.0|-122.0|-1000||||0|0|0|0"
        )
        ev = m._parse_sbs1_line(line)
        assert ev is not None
        assert ev["icao"] == "A1B2C3"
        assert ev["altitude_ft"] == 40000
        assert ev["lat"] == 37.0

    def test_callsign_only_msg_type_1(self) -> None:
        """MSG type 1 = identification (callsign only)."""
        line = (
            "MSG,1,1,1,1,ABC123,1,2024/01/01,00:00:00.000,2024/01/01,"
            "00:00:00.000,DLH451,,,,,,,,,,,,0,0,0,0"
        )
        ev = m._parse_sbs1_line(line)
        assert ev is not None
        assert ev["icao"] == "ABC123"
        assert ev["callsign"] == "DLH451"
        # No position/altitude in type 1.
        assert "altitude_ft" not in ev
        assert "lat" not in ev

    def test_truncated_row_padded_with_none(self) -> None:
        """A fork that omits trailing empty fields still parses —
        we pad to the expected field count."""
        line = "MSG,3,1,1,1,4D22AA,1,2024/01/01,00:00:00.000"
        ev = m._parse_sbs1_line(line)
        assert ev is not None
        assert ev["icao"] == "4D22AA"

    def test_icao_normalized_uppercase(self) -> None:
        """The hex_ident comes through as lowercase from some forks;
        the row schema in fake_dump1090 uses uppercase. Normalize."""
        line = "MSG,3,1,1,1,4d22aa,1,2024/01/01,00:00:00.000,2024/01/01,00:00:00.000,,,,,,,,,0,0,0,0"
        ev = m._parse_sbs1_line(line)
        assert ev is not None
        assert ev["icao"] == "4D22AA"


class TestSbs1RowMerging:
    def _frame(self, icao: str = "4D22AA", **overrides: object) -> dict[str, object]:
        ev: dict[str, object] = {
            "kind": "frame",
            "ts": 0.0,
            "df": 17,
            "icao": icao,
            "raw": "MSG,...",
            "parity": None,
            "rssi_dbfs": -10.0,
        }
        ev.update(overrides)
        return ev

    def test_row_from_frame_carries_optional_fields(self) -> None:
        ev = self._frame(
            callsign="UAL456", altitude_ft=40000, lat=37.5, lon=-122.5,
            groundspeed_kt=300, vertical_rate_fpm=-1000, squawk="1234",
        )
        row = m._row_from_frame(ev)
        assert row["icao"] == "4D22AA"
        assert row["callsign"] == "UAL456"
        assert row["altitude_ft"] == 40000
        assert row["lat"] == 37.5
        assert row["lon"] == -122.5
        assert row["groundspeed_kt"] == 300
        assert row["vertical_rate_fpm"] == -1000
        assert row["squawk"] == "1234"
        assert row["frames"] == 1
        assert row["rssi_dbfs"] == -10.0

    def test_merge_updates_only_changed_fields(self) -> None:
        """The aircraft snapshot only re-emits when something changes
        (matching dump1090's aircraft.json refresh behavior)."""
        ev1 = self._frame(callsign="UAL456", altitude_ft=40000)
        row = m._row_from_frame(ev1)
        # Same values → merge returns False (no change), but frames++.
        ev2 = self._frame(callsign="UAL456", altitude_ft=40000)
        changed = m._merge_row(row, ev2)
        assert not changed
        assert row["frames"] == 2
        # New altitude → merge returns True.
        ev3 = self._frame(callsign="UAL456", altitude_ft=41000)
        changed = m._merge_row(row, ev3)
        assert changed
        assert row["altitude_ft"] == 41000
        assert row["frames"] == 3


# ---------------------------------------------------------------------------
# IQ format conversion
# ---------------------------------------------------------------------------


class TestIqFormatConversion:
    def test_cs16_passthrough(self) -> None:
        data = b"\x00\x01\x00\x02\x00\x03"
        assert m._stdin_to_cs16(data, "cs16") == data

    def test_cf32_to_cs16_scales_and_clips(self) -> None:
        """cf32 → cs16: real/imag scaled by 32767 (symmetric), clipped
        to int16 range. Complex 1+0j → I=32767, Q=0; -1+0j → I=-32767.
        (Scaling factor 32767, not 32768 — keeps the int16 range
        symmetric so |x| ≤ 1.0 maps cleanly.)"""
        iq = np.array(
            [1 + 0j, 0 + 1j, -1 + 0j, 0 - 1j], dtype=np.complex64
        )
        out = m._stdin_to_cs16(iq.tobytes(), "cf32")
        arr = np.frombuffer(out, dtype=np.int16)
        # 1+0j → I=32767, Q=0
        assert arr[0] == 32767 and arr[1] == 0
        # 0+1j → I=0, Q=32767
        assert arr[2] == 0 and arr[3] == 32767
        # -1+0j → I=-32767, Q=0
        assert arr[4] == -32767 and arr[5] == 0
        # 0-1j → I=0, Q=-32767
        assert arr[6] == 0 and arr[7] == -32767

    def test_cf32_odd_byte_count_truncated(self) -> None:
        """A trailing partial sample (not a multiple of 8 bytes) is
        dropped — no buffer-overrun risk."""
        iq = np.array([1 + 0j], dtype=np.complex64)
        data = iq.tobytes() + b"\x00\x00\x00"  # 11 bytes total
        out = m._stdin_to_cs16(data, "cf32")
        # Only the first complete sample survives.
        assert len(out) == 4  # 1 sample × 2 int16 × 2 bytes = 4 bytes

    def test_cu8_to_cs16_centers_and_scales(self) -> None:
        """cu8 → cs16: subtract 127.5 (zero-center), scale by 257.
        255 → +max; 0 → -max; 128 → ~+0; 127 → ~-0."""
        u8 = np.array([255, 0, 128, 127], dtype=np.uint8)
        out = m._stdin_to_cs16(u8.tobytes(), "cu8")
        arr = np.frombuffer(out, dtype=np.int16)
        assert arr[0] == 32767   # 255 → +max
        assert arr[1] == -32767  # 0   → -max (truncated toward 0)
        assert arr[2] == 128     # 128 → +0.5 × 257 ≈ +128
        assert arr[3] == -128    # 127 → -0.5 × 257 ≈ -128

    def test_unsupported_format_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported OWRX_IQ_FORMAT"):
            m._stdin_to_cs16(b"", "f32-ieee")


# ---------------------------------------------------------------------------
# Fork auto-detection (slice-31): _probe_fork + _resolve_fork
# ---------------------------------------------------------------------------


class _FakeVersionBinary:
    """Tiny shim that writes a fixed string to stdout when invoked with
    any argument starting with ``--`` (so ``--version`` and ``-V`` both
    work). Implemented as a temp file because the bridge's _probe_fork
    runs subprocess.run([binary, arg]) — needs a real executable.

    Usage in a test:

        with _FakeVersionBinary("dump1090-fa v1.0-1") as binpath:
            assert m._probe_fork(binpath) == "fa"
    """

    def __init__(self, version_output: str) -> None:
        self._out = version_output
        self._path: Path | None = None

    def __enter__(self) -> str:
        import tempfile

        # Write a tiny shell script that echoes the canned output. The
        # shell is portable across Linux/macOS; this is a test-only path.
        script = f'#!/bin/sh\necho "{self._out}"\n'
        fd, path = tempfile.mkstemp(suffix=".sh", prefix="fake-dump1090-")
        import os

        with os.fdopen(fd, "w") as f:
            f.write(script)
        os.chmod(path, 0o755)
        self._path = Path(path)
        return str(self._path)

    def __exit__(self, *_: object) -> None:
        if self._path is not None:
            with contextlib.suppress(OSError):
                self._path.unlink()


class TestForkAutoDetect:
    """Tests for the dump1090-fork auto-detection probe (slice-31)."""

    def test_probe_returns_fa_for_dump1090_fa_signature(self) -> None:
        with _FakeVersionBinary("dump1090-fa v1.0-1 (rbbranch)") as p:
            assert m._probe_fork(p) == "fa"

    def test_probe_returns_fa_for_bare_dump1090_version(self) -> None:
        # Some fa builds print just "dump1090 1.0.x" without the -fa suffix.
        with _FakeVersionBinary("dump1090 1.0.8-1") as p:
            assert m._probe_fork(p) == "fa"

    def test_probe_returns_mutability_for_mutability_signature(self) -> None:
        with _FakeVersionBinary("dump1090-mutability v1.15dev") as p:
            assert m._probe_fork(p) == "mutability"

    def test_probe_returns_readsb_for_readsb_signature(self) -> None:
        with _FakeVersionBinary("readsb 2.0 proto") as p:
            assert m._probe_fork(p) == "readsb"

    def test_probe_returns_none_for_unrecognized_output(self) -> None:
        with _FakeVersionBinary("hello world this is not dump1090") as p:
            assert m._probe_fork(p) is None

    def test_probe_returns_none_for_missing_binary(self) -> None:
        # A binary path that doesn't exist; probe must not raise.
        assert m._probe_fork("/nonexistent/dump1090-binary-xyz") is None

    def test_resolve_fork_honors_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Override skips the probe entirely.
        monkeypatch.setenv("OPENWEBRX_PLUS_DUMP1090_FORK", "mutability")
        # Re-import the module-level _FORK_OVERRIDE by reloading — the
        # module reads it at import time. Simpler: call _resolve_fork
        # and assert it honors the live monkeypatched env. Because
        # _resolve_fork reads _FORK_OVERRIDE at module level, we patch
        # the module attribute directly.
        monkeypatch.setattr(m, "_FORK_OVERRIDE", "mutability")
        assert m._resolve_fork() == "mutability"

    def test_resolve_fork_returns_unknown_when_probe_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No override + probe returns None → "unknown" (never None).
        monkeypatch.setattr(m, "_FORK_OVERRIDE", "")
        monkeypatch.setattr(m, "_REAL_BIN", "/nonexistent/dump1090-binary-xyz")
        assert m._resolve_fork() == "unknown"

    def test_resolve_fork_warns_on_invalid_override(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # An invalid override value falls back to "unknown" and writes a
        # warning to stderr; the operator knows the override was bad.
        monkeypatch.setattr(m, "_FORK_OVERRIDE", "custom-fork-name")
        monkeypatch.setattr(m, "_REAL_BIN", "/nonexistent")
        result = m._resolve_fork()
        assert result == "unknown"
        captured = capsys.readouterr()
        assert "OPENWEBRX_PLUS_DUMP1090_FORK" in captured.err
        assert "custom-fork-name" in captured.err


# ---------------------------------------------------------------------------
# End-to-end: bridge ↔ fake SBS1 server (no real dump1090 binary needed)
# ---------------------------------------------------------------------------


class _FakeSbs1Server:
    """A minimal TCP server that emits SBS1 lines on connect.

    The bridge is run with ``--no-spawn --connect-host 127.0.0.1
    --connect-port <port>`` so it bridges straight to this fake — no
    dump1090 binary needed.
    """

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self._sock: socket.socket | None = None
        self._port = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        assert self._sock is not None
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = self._sock.accept()
                except OSError:
                    return
                try:
                    for line in self._lines:
                        if self._stop.is_set():
                            break
                        conn.sendall((line + "\n").encode("ascii"))
                    # Keep the socket open a moment so the bridge drains.
                    for _ in range(20):
                        if self._stop.is_set():
                            break
                        import time
                        time.sleep(0.05)
                finally:
                    with contextlib_suppress():
                        conn.close()
        finally:
            with contextlib_suppress():
                self._sock.close()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            with contextlib_suppress():
                self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def contextlib_suppress():  # noqa: ANN202
    return contextlib.suppress(OSError)


class TestBridgeEndToEnd:
    async def test_bridge_translates_sbs1_to_ndjson(self) -> None:
        """End-to-end: bridge spawns, connects to a fake SBS1 server,
        translates MSG lines → NDJSON events."""
        sbs1_lines = [
            # Type 1: callsign only
            "MSG,1,1,1,1,4D22AA,1,2024/01/01,00:00:00.000,2024/01/01,00:00:00.000,UAL456,,,,,,,,,,,,0,0,0,0",
            # Type 3: airborne position
            "MSG,3,1,1,1,4D22AA,1,2024/01/01,00:00:00.000,2024/01/01,00:00:00.000,UAL456,40000,300,90,37.5,-122.5,-1000,1234,,0,0,0,0",
            # Type 4: velocity
            "MSG,4,1,1,1,A1B2C3,1,2024/01/01,00:00:00.000,2024/01/01,00:00:00.000,DLH451,,450,270,,,,,,,,0,0,0,0",
        ]
        server = _FakeSbs1Server(sbs1_lines)
        server.start()
        try:
            env = {**os.environ, "OWRX_RX_ID": "rx-test", "OWRX_SAMPLE_RATE": "2000000"}
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(_SCRIPTS / "sbs1_to_ndjson.py"),
                "--no-spawn",
                "--connect-host", "127.0.0.1",
                "--connect-port", str(server.port),
                "--connect-timeout", "3.0",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                # Close stdin immediately — we don't feed IQ; the bridge
                # exits when its stdin pump thread observes EOF.
                assert proc.stdin is not None
                proc.stdin.close()

                # Read stdout until we see at least one aircraft snapshot.
                events: list[dict[str, object]] = []
                assert proc.stdout is not None
                while True:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=5.0
                    )
                    if not line:
                        break
                    line_str = line.decode("ascii", errors="replace").strip()
                    if not line_str:
                        continue
                    try:
                        ev = json.loads(line_str)
                    except json.JSONDecodeError:
                        continue
                    events.append(ev)
                    if ev.get("kind") == "aircraft":
                        # Wait briefly for any final flush, then break.
                        await asyncio.sleep(0.3)
                        break
                    if ev.get("kind") == "decoder_state" and ev.get("state") == "failed":
                        pytest.fail(f"bridge failed early: {ev}")

                # Validate the translated events.
                ready_events = [e for e in events if e.get("kind") == "ready"]
                assert ready_events, "expected a ready handshake"
                ready = ready_events[0]
                assert ready["software"] == "sbs1-bridge/1.0"
                assert ready["receiver_id"] == "rx-test"
                assert ready["sbs_port"] == server.port
                # Slice-31: ready event must carry a fork field (even if
                # "unknown" — the probe runs against _REAL_BIN which
                # defaults to "dump1090"; in this --no-spawn test path
                # the probe likely returns None since there's no real
                # binary in CI, but the field MUST exist).
                assert "fork" in ready
                assert ready["fork"] in {"fa", "mutability", "readsb", "unknown"}

                frame_events = [e for e in events if e.get("kind") == "frame"]
                icaos = {e.get("icao") for e in frame_events}
                assert "4D22AA" in icaos
                assert "A1B2C3" in icaos
                # At least one aircraft snapshot with a row carrying lat/lon.
                aircraft_events = [
                    e for e in events if e.get("kind") == "aircraft"
                ]
                assert aircraft_events, "expected at least one aircraft snapshot"
                snapshot = aircraft_events[-1]
                rows = snapshot.get("aircraft") or []
                row_by_icao = {r.get("icao"): r for r in rows}
                assert "4D22AA" in row_by_icao
                assert row_by_icao["4D22AA"].get("callsign") == "UAL456"
                assert row_by_icao["4D22AA"].get("lat") == 37.5
                assert row_by_icao["4D22AA"].get("lon") == -122.5
            finally:
                with contextlib_suppress():
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except (TimeoutError, ProcessLookupError):
                    with contextlib_suppress():
                        proc.kill()
        finally:
            server.stop()
