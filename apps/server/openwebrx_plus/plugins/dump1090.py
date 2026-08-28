"""dump1090-family ADS-B decoder plugin — ADR-003 subprocess bundled plugin #1.

Production-grade ADS-B/Mode S decoding lives in C (dump1090 / readsb /
dump1090-mutability forks); this plugin drives such a binary through the
generic :mod:`.subprocess` runner instead of re-implementing the world in
Python. Compared to the bundled in-process ``adsb`` plugin this unlocks
full CPR position decode, velocity, and Gilliam-altitude — whatever the
child binary emits flows through as event fields.

**Binary contract (OpenWebRX+ subprocess decoder convention, ADR-003):**

  * IQ arrives on **stdin** — interleaved samples in the format named by
    the ``OWRX_IQ_FORMAT`` env var (this plugin requests ``cs16``, the
    dump1090 ``--iformat SC16`` layout; the runner converts hub cf32).
  * Events leave on **stdout** as NDJSON, one JSON object per line:
    ``{"kind": "frame", "df": 17, "icao": "4D22AA", "raw": "…", …}`` and
    ``{"kind": "aircraft", "aircraft": [{…}]}`` snapshots with the row
    schema the frontend ``AircraftListViz`` already renders (icao,
    callsign, altitude_ft, frames, last_seen, rssi_dbfs + optional lat,
    lon, groundspeed_kt, vertical_rate_fpm).
  * An optional ``{"kind": "ready", …}`` first line is consumed as a
    handshake (never broadcast).
  * Environment: ``OWRX_RX_ID``, ``OWRX_SAMPLE_RATE``, ``OWRX_CENTER_FREQ``,
    ``OWRX_IQ_FORMAT``.

Stock dump1090 releases speak SBS1 CSV on TCP ports instead of stdout
NDJSON; either point ``OPENWEBRX_PLUS_DUMP1090_BIN`` at a
convention-speaking build, OR (slice-16) ship the SBS1 → NDJSON bridge
at ``scripts/sbs1_to_ndjson.py`` (repo root). The bridge spawns a
stock dump1090 with ``--net --net-sbs-port <ephemeral> --net-only``,
connects to its SBS1 socket, and translates each ``MSG,...`` line into
the OpenWebRX+ ``frame`` / ``aircraft`` event schema that this plugin
expects (matches ``tests/fakes/fake_dump1090.py`` row shape). Example
config:

    OPENWEBRX_PLUS_DUMP1090_BIN=python3 scripts/sbs1_to_ndjson.py

Live bring-up remains the operator's job: the fake binary pins the
contract; the bridge only translates SBS1's CSV into the NDJSON schema.
"""

from __future__ import annotations

import os
import shlex
from typing import ClassVar

from .base import DecoderManifest
from .modes import MODE_S_SAMPLE_RATE
from .registry import DecoderRegistry
from .subprocess import SubprocessDecoderPlugin, SubprocessSpec

_BIN_ENV = "OPENWEBRX_PLUS_DUMP1090_BIN"
_ARGS_ENV = "OPENWEBRX_PLUS_DUMP1090_ARGS"

# Stock-ish defaults; dump1090-class binaries read stdin with --ifile -.
# A convention-speaking build ignores flags it doesn't need.
_DEFAULT_BIN = "dump1090"
_DEFAULT_ARGS = "--ifile - --iformat SC16 --sample-rate 2000000 --quiet"

# Standard SBS1 (BaseStation) TCP port that dump1090-fa, dump1090-mutability,
# and readsb all bind by default. Used by the auto-discovery probe below.
_SBS1_DEFAULT_HOST = "127.0.0.1"
_SBS1_DEFAULT_PORT = 30003
_SBS1_PROBE_TIMEOUT_S = 0.25
# Path to the SBS1 bridge script (relative to repo root). Resolved at
# runtime via __file__ so the plugin works under editable install +
# under pytest's CWD-shim. Falls back to bare `sbs1_to_ndjson.py` if
# the path can't be resolved (operator's PATH must include it).
_BRIDGE_SCRIPT = "sbs1_to_ndjson.py"


def _bridge_script_path() -> str:
    """Resolve the absolute path to scripts/sbs1_to_ndjson.py from this
    plugin's location. The plugin lives at
    ``apps/server/openwebrx_plus/plugins/dump1090.py``; the script lives
    at ``<repo>/scripts/sbs1_to_ndjson.py`` — that's five parents up:
    parents[0]=plugins, [1]=openwebrx_plus, [2]=server, [3]=apps,
    [4]=repo root.

    Returns the absolute path if it exists; otherwise returns the bare
    ``sbs1_to_ndjson.py`` (operator's PATH must include it).
    """
    from pathlib import Path  # noqa: PLC0415

    here = Path(__file__).resolve()
    repo_root = here.parents[4]
    candidate = repo_root / "scripts" / _BRIDGE_SCRIPT
    if candidate.is_file():
        return str(candidate)
    return _BRIDGE_SCRIPT


def _probe_local_sbs1(host: str, port: int, timeout_s: float) -> bool:
    """Best-effort TCP probe: returns True if a server is accepting
    connections at (host, port) within ``timeout_s`` seconds. Never
    raises — any error (refused, timeout, DNS, etc.) returns False.
    """
    import socket  # noqa: PLC0415

    try:
        with socket.create_connection((host, port), timeout=timeout_s) as _:
            return True
    except OSError:
        return False


def _default_spec() -> SubprocessSpec:
    # Auto-discovery (slice-31): if the operator hasn't set
    # OPENWEBRX_PLUS_DUMP1090_BIN, probe 127.0.0.1:30003 (the standard
    # SBS1 port for dump1090-fa/mutability/readsb). If a server is
    # reachable there, default to the SBS1 bridge script in --no-spawn
    # mode against that endpoint — the operator's already-running
    # dump1090 (started via systemctl or whatever) just works without
    # any OpenWebRX+ config. If 30003 is not reachable, fall through to
    # the legacy default (spawn `dump1090` from PATH).
    bin_unset = not os.environ.get(_BIN_ENV)
    sbs1_reachable = bin_unset and _probe_local_sbs1(
        _SBS1_DEFAULT_HOST, _SBS1_DEFAULT_PORT, _SBS1_PROBE_TIMEOUT_S
    )
    if sbs1_reachable:
        bridge = _bridge_script_path()
        binary = "python3"
        extra = [
            bridge,
            "--no-spawn",
            "--connect-host",
            _SBS1_DEFAULT_HOST,
            "--connect-port",
            str(_SBS1_DEFAULT_PORT),
        ]
        return SubprocessSpec(
            argv=(binary, *extra),
            # The bridge accepts cf32 (it converts internally); the
            # runner hands cf32 to children that don't override.
            # No explicit iq_format here — let the runner use its
            # default (cs16) and the bridge's _stdin_to_cs16 handles
            # the conversion via OWRX_IQ_FORMAT.
            iq_format="cs16",
            ready_timeout=5.0,
            restart_backoff=(0.5, 2.0, 8.0),
        )

    binary = os.environ.get(_BIN_ENV, _DEFAULT_BIN)
    extra = shlex.split(os.environ.get(_ARGS_ENV, _DEFAULT_ARGS))
    return SubprocessSpec(
        argv=(binary, *extra),
        # dump1090's SC16 = interleaved int16 — the runner converts cf32.
        iq_format="cs16",
        # Children that handshake get a precise attach failure instead of
        # silently dropped IQ; stock binaries that never say ready still
        # work because the timeout only applies when ready_timeout is set.
        ready_timeout=None,
        restart_backoff=(0.5, 2.0, 8.0),
    )


@DecoderRegistry.register
class Dump1090Plugin(SubprocessDecoderPlugin):
    """ADS-B via an external dump1090-class binary (subprocess family)."""

    manifest: ClassVar[DecoderManifest] = DecoderManifest(
        name="dump1090",
        version="0.2.0",
        label="ADS-B dump1090 (subprocess)",
        tap_point="rf_band",
        description=(
            "Production-grade ADS-B / Mode S via an external dump1090-class "
            "binary: IQ on stdin (cs16, 2 MSPS), NDJSON events on stdout "
            "(frame + aircraft snapshots with CPR positions, velocity, "
            "Gilliam altitude when the binary decodes them). Configure the "
            "binary with OPENWEBRX_PLUS_DUMP1090_BIN / _ARGS env vars. "
            "Same wire events as the in-process 'adsb' plugin — the "
            "aircraft table viz works with either. "
            "Slice-31 auto-discovery: if OPENWEBRX_PLUS_DUMP1090_BIN is "
            "unset, the plugin probes 127.0.0.1:30003 (the standard SBS1 "
            "port for dump1090-fa/mutability/readsb). If a server is "
            "reachable there, the plugin uses scripts/sbs1_to_ndjson.py "
            "--no-spawn against it — operators with a running dump1090 "
            "service need no extra config. The bridge also probes the "
            "real binary's fork identity via --version and reports it in "
            "the ready event for diagnostics."
        ),
        required_sample_rate=MODE_S_SAMPLE_RATE,
        events=("frame", "aircraft", "decoder_state"),
    )

    spec: ClassVar[SubprocessSpec] = _default_spec()
