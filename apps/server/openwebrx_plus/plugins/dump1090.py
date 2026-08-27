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
NDJSON; point ``OPENWEBRX_PLUS_DUMP1090_BIN`` at a convention-speaking
build or a thin wrapper that translates. The test suite drives a
protocol-faithful fake binary (``tests/fakes/fake_dump1090.py``) which
implements this contract exactly — including synthetic position fields —
so the whole path is verifiable hardware-free.
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


def _default_spec() -> SubprocessSpec:
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
        version="0.1.0",
        label="ADS-B dump1090 (subprocess)",
        tap_point="rf_band",
        description=(
            "Production-grade ADS-B / Mode S via an external dump1090-class "
            "binary: IQ on stdin (cs16, 2 MSPS), NDJSON events on stdout "
            "(frame + aircraft snapshots with CPR positions, velocity, "
            "Gilliam altitude when the binary decodes them). Configure the "
            "binary with OPENWEBRX_PLUS_DUMP1090_BIN / _ARGS env vars. "
            "Same wire events as the in-process 'adsb' plugin — the "
            "aircraft table viz works with either."
        ),
        required_sample_rate=MODE_S_SAMPLE_RATE,
        events=("frame", "aircraft", "decoder_state"),
    )

    spec: ClassVar[SubprocessSpec] = _default_spec()
