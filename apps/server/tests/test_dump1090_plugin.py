"""Tests for the dump1090 plugin's slice-31 auto-discovery behavior.

Verifies:
  * ``_probe_local_sbs1`` returns True when a server is listening at
    (host, port); False on refused / timeout.
  * ``_bridge_script_path`` resolves to the absolute path of
    ``scripts/sbs1_to_ndjson.py`` when the script exists; falls back to
    the bare filename otherwise.
  * ``_default_spec`` chooses the bridge auto-discovery path when
    ``OPENWEBRX_PLUS_DUMP1090_BIN`` is unset AND 127.0.0.1:30003 is
    reachable; falls through to the legacy default otherwise.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from openwebrx_plus.plugins.dump1090 import (
    _BIN_ENV,
    _BRIDGE_SCRIPT,
    _SBS1_DEFAULT_HOST,
    _SBS1_DEFAULT_PORT,
    _bridge_script_path,
    _default_spec,
    _probe_local_sbs1,
)

# ---------------------------------------------------------------------------
# _probe_local_sbs1: best-effort TCP probe
# ---------------------------------------------------------------------------


class _TinyTcpServer:
    """Minimal TCP listener on an ephemeral port, for probe tests."""

    def __init__(self) -> None:
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
        # The server doesn't need to send any data — the probe just
        # connects and immediately closes. We accept connections and
        # close them right away.
        try:
            while not self._stop.is_set():
                try:
                    self._sock.settimeout(0.2)
                    conn, _ = self._sock.accept()
                    conn.close()
                except (TimeoutError, OSError):
                    continue
        finally:
            with __import__("contextlib").suppress(OSError):
                self._sock.close()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            with __import__("contextlib").suppress(OSError):
                self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


class TestProbeLocalSbs1:
    def test_returns_true_when_server_listening(self) -> None:
        srv = _TinyTcpServer()
        srv.start()
        try:
            # Tiny delay to ensure accept loop is ready.
            time.sleep(0.05)
            assert _probe_local_sbs1("127.0.0.1", srv.port, 0.5) is True
        finally:
            srv.stop()

    def test_returns_false_on_connection_refused(self) -> None:
        # An ephemeral port that's almost certainly not listening: pick
        # one in the dynamic range and connect with a tight timeout.
        # No server is started.
        # Use port 1 (privileged, almost never accepting on a non-root
        # process) to ensure refused.
        assert _probe_local_sbs1("127.0.0.1", 1, 0.25) is False

    def test_returns_false_on_timeout(self) -> None:
        # Use a non-routable address (TEST-NET-1 / RFC 5737) so the
        # connection attempt times out instead of refusing immediately.
        # 192.0.2.0/24 is reserved for documentation; nothing listens
        # there, and the route is black-holed on most systems — the
        # connect will time out within our 0.25s budget.
        assert _probe_local_sbs1("192.0.2.1", 30003, 0.25) is False

    def test_returns_false_on_dns_failure(self) -> None:
        # An invalid hostname → socket.gaierror → caught → False.
        assert _probe_local_sbs1("invalid.invalid.invalid", 30003, 0.25) is False


# ---------------------------------------------------------------------------
# _bridge_script_path: resolve scripts/sbs1_to_ndjson.py from plugin
# ---------------------------------------------------------------------------


class TestBridgeScriptPath:
    def test_returns_absolute_path_when_script_exists(self) -> None:
        # The repo does ship scripts/sbs1_to_ndjson.py — the resolver
        # must return its absolute path.
        result = _bridge_script_path()
        assert result.endswith(_BRIDGE_SCRIPT)
        # Absolute path means it starts with "/".
        assert os.path.isabs(result)
        assert Path(result).is_file()

    def test_returns_bare_filename_when_script_missing(self) -> None:
        # Force the resolver to fail by patching the BRIDGE_SCRIPT const
        # to a name that doesn't exist in the scripts/ dir. The resolver
        # falls back to the bare filename (operator's PATH must include
        # it).
        with patch("openwebrx_plus.plugins.dump1090._BRIDGE_SCRIPT", "does-not-exist.py"):
            result = _bridge_script_path()
        assert result == "does-not-exist.py"
        assert not os.path.isabs(result)


# ---------------------------------------------------------------------------
# _default_spec: auto-discovery vs legacy default
# ---------------------------------------------------------------------------


class TestDefaultSpecAutoDiscovery:
    """Tests the auto-discovery branch in _default_spec."""

    def test_bridge_mode_when_env_unset_and_sbs1_reachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Make _probe_local_sbs1 return True regardless of network state.
        monkeypatch.delenv(_BIN_ENV, raising=False)
        with patch(
            "openwebrx_plus.plugins.dump1090._probe_local_sbs1",
            return_value=True,
        ):
            spec = _default_spec()
        # Bridge mode: argv[0] is python3; argv[1] is the bridge script
        # path; argv contains --no-spawn + --connect-host + --connect-port.
        assert spec.argv[0] == "python3"
        assert spec.argv[1].endswith(_BRIDGE_SCRIPT)
        assert "--no-spawn" in spec.argv
        assert "--connect-host" in spec.argv
        assert _SBS1_DEFAULT_HOST in spec.argv
        assert "--connect-port" in spec.argv
        assert str(_SBS1_DEFAULT_PORT) in spec.argv
        # ready_timeout is 5.0 in bridge mode (vs None in legacy mode).
        assert spec.ready_timeout == 5.0

    def test_legacy_mode_when_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # When the operator explicitly sets OPENWEBRX_PLUS_DUMP1090_BIN,
        # auto-discovery must not run — use the operator's binary as-is.
        monkeypatch.setenv(_BIN_ENV, "/usr/local/bin/dump1090-special")
        # Even if SBS1 would be reachable, the probe must not fire.
        with patch(
            "openwebrx_plus.plugins.dump1090._probe_local_sbs1",
            side_effect=AssertionError("probe must not be called when env is set"),
        ):
            spec = _default_spec()
        assert spec.argv[0] == "/usr/local/bin/dump1090-special"
        # Legacy mode: ready_timeout is None (no handshake expected from
        # a stock binary that doesn't speak the OpenWebRX+ contract).
        assert spec.ready_timeout is None

    def test_legacy_mode_when_probe_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Env unset + probe says no SBS1 server → fall through to legacy.
        monkeypatch.delenv(_BIN_ENV, raising=False)
        with patch(
            "openwebrx_plus.plugins.dump1090._probe_local_sbs1",
            return_value=False,
        ):
            spec = _default_spec()
        # Legacy default: argv[0] is "dump1090" (the default stock binary
        # name; operator's PATH must include it).
        assert spec.argv[0] == "dump1090"
        assert spec.ready_timeout is None

    def test_legacy_mode_when_env_empty_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An empty OPENWEBRX_PLUS_DUMP1090_BIN env var should be treated
        # as unset (env.get returns "" which is falsy). The probe must
        # run and (if True) trigger bridge mode.
        monkeypatch.setenv(_BIN_ENV, "")
        with patch(
            "openwebrx_plus.plugins.dump1090._probe_local_sbs1",
            return_value=True,
        ):
            spec = _default_spec()
        assert spec.argv[0] == "python3"
        assert "--no-spawn" in spec.argv
