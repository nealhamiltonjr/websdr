"""SDRangel remote source — ADR-006 federation client (slice-20).

SDRangel (`sdrangel.org` <https://www.sdrangel.org/>) is a Qt-based
desktop SDR application with a built-in REST + WebSocket server for
remote control. It is THE dominant Linux desktop SDR app outside the
browser-native space — and a number of public SDRangel instances
expose their REST API for federation.

Slice-20 status: **manifest scaffolding + manifest registration**.
The Source class exists, is registered, advertises its capabilities
in the SourceRegistry, and refuses spawn with an actionable error
message. The actual REST+WS implementation lands in a future slice
(see "Implementation plan" below); this scaffold lets the UI advertise
SDRangel support so operators can wire it in manually until the
production impl ships.

Wire facts (SDRangel REST API v7+ — see
https://github.com/f4exb/sdrangel/wiki/REST-API):

  * Base URL: ``http://host:port/sdrangel`` (default port 8091).
  * Authentication: none (no auth in the public deployments); basic
    auth on instances behind a reverse proxy.
  * Device control:
    - ``GET /devices`` — list configured device sets
    - ``PUT /deviceset/{id}/device/settings`` — set center freq,
      sample rate, decimation, gain (HardwareDe-tunable fields)
    - ``GET /deviceset/{id}/device/report`` — current state
  * Channel control (the demodulator):
    - ``POST /deviceset/{id}/channel`` — add a channel (e.g., NFM,
      WFM, AM, LSB, USB, CW)
    - ``PUT /deviceset/{id}/channel/{cid}/settings`` — set the demod
      settings (RFBW, AFBW, squelch, volume)
  * Spectrum server:
    - ``GET /spectrumserver?deviceId={id}`` — WebSocket upgrade
    - On connect, the server emits a one-shot ``{type: "start"}``
      control frame, then binary spectrum frames at the device's
      FFT rate.
  * Audio:
    - SDRangel has no built-in audio-over-WS — the demod output goes
      to the local sound card. We'd need to wire SDRangel's
      ``UDP sink`` channel to a local UDP port that we read from.

Implementation plan (future slice, not slice-20):

  1. ``SDRangelSource.__post_init__`` validates host/port and probes
     the REST API (``GET /devices``) to discover what's available.
  2. ``spawn()`` opens the spectrum WebSocket, emits ``RemoteFftFrame``
     per binary frame (replicating the KiwiSDR pattern in
     ``sources/kiwi.py``). No raw IQ is currently exposed; the
     ReceiverSession builds its FFT chain from the remote spectrum
     frames instead (DisplayStreamSource path, ADR-006).
  3. ``tune(freq)`` PUTs a device settings update with the new center
     frequency. The spectrum WebSocket picks up the change live.
  4. ``set_mode(mode)`` POSTs a channel add or PUTs channel settings
     to swap the demodulator.
  5. Audio: deferred — the federation client shows the FFT/spectrum
     only (no audio for SDRangel until the UDP-sink path is built).

The class below exists so the manifest registration succeeds and the
UI can advertise SDRangel support. Spawning raises
``NotImplementedError`` with an actionable message — operators who
want SDRangel today can run a local SDRangel instance and connect
via its web UI directly.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

import structlog

from .base import RemoteAudioFrame, RemoteFftFrame, SourceInfo

log = structlog.get_logger(__name__)

# Default SDRangel REST API port.
_DEFAULT_PORT = 8091
# The REST API base path. All device/channel/spectrum endpoints live
# under this prefix.
_REST_BASE = "/sdrangel"


@dataclass
class SDRangelSource:
    """A remote SDRangel receiver as a Source (ADR-006 Tier C — REST+WS).

    Slice-20 status: **manifest scaffolding only**. The class is
    registered in SourceRegistry (UI advertises it), but ``spawn()``
    raises NotImplementedError with a pointer to the implementation
    plan above. This matches the slice-14 STATUS.md roadmap:

        'SDRangel client — a substantial REST+WS API surface; the
        manifest scaffolding + manifest registration can land
        without the implementation if the UI needs to advertise it.'

    Args:
        host: SDRangel hostname or IP (no scheme).
        port: SDRangel REST API port (default 8091).
        device_set: 0-indexed device set to control (one SDRangel
            instance can host multiple devices; pick the one you want).
        sample_rate: requested IQ rate in Hz. Used only in the manifest
            advertisement; the REST impl will negotiate with the
            device's actual capabilities.
        user: client identification (for the User-Agent header — SDRangel
            public instances are volunteer-run).
        connect_timeout: seconds to wait for the REST probe + WS upgrade.
    """

    host: str
    port: int = _DEFAULT_PORT
    device_set: int = 0
    sample_rate: int = 2_400_000
    user: str = "openwebrx_plus (federation client)"
    connect_timeout: float = 10.0
    info: SourceInfo = field(default_factory=lambda: SourceInfo(
        type="sdrangel",
        label="SDRangel (remote)",
        sample_rate=2_400_000,
    ))

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("host is required (the SDRangel address)")
        if not (1 <= self.port <= 65535):
            raise ValueError(f"port must be 1-65535, got {self.port}")
        if self.device_set < 0:
            raise ValueError(f"device_set must be >= 0, got {self.device_set}")
        if self.sample_rate < 1000:
            raise ValueError(f"sample_rate must be >= 1000, got {self.sample_rate}")
        if self.connect_timeout <= 0:
            raise ValueError("connect_timeout must be > 0")
        # Advertised to ReceiverSession.start() so the DSP chains are
        # built for the rate we'll negotiate.
        self.fixed_sample_rate: int = self.sample_rate

    # -- Source protocol (NOT YET IMPLEMENTED — slice-20 scaffold) ---------

    async def spawn(
        self,
        center_freq: int,
        sample_rate: int,
        gain: float | None = None,
    ) -> AsyncGenerator[RemoteFftFrame | RemoteAudioFrame, None]:
        """Stream spectrum/audio frames from the SDRangel spectrum server.

        Slice-20 status: NOT IMPLEMENTED. The manifest registration
        lets the UI advertise SDRangel support; calling spawn() raises
        NotImplementedError so the operator gets an actionable error
        rather than silent no-op behavior.

        The future implementation (per the module docstring):

        1. ``httpx.GetClient`` probe ``GET {rest_base}/devices`` to
           confirm the device set exists.
        2. ``PUT {rest_base}/deviceset/{device_set}/device/settings``
           with the new center frequency + sample rate + decimation.
        3. Open a WebSocket to
           ``{rest_base}/spectrumserver?deviceId={device_set}``.
        4. Read binary spectrum frames, build :class:`RemoteFftFrame`
           per frame, yield to the ReceiverSession.
        5. Teardown: close WS, leave the SDRangel device running (don't
           stop the device — it's a shared resource).

        Raises:
            NotImplementedError: always, in slice-20. The error message
                points operators to the implementation plan in this
                module's docstring.
        """
        raise NotImplementedError(
            "SDRangelSource.spawn() is not implemented (slice-20 manifest "
            "scaffold only). The manifest registration lets the UI "
            "advertise SDRangel support; the REST+WS streaming path "
            "lands in a future slice. See the module docstring at "
            "openwebrx_plus/sources/sdrangel.py for the implementation "
            "plan, or run a local SDRangel instance and connect via "
            "its own web UI today."
        )
        # Unreachable — the yield below is for typing only.
        if False:  # pragma: no cover
            yield

    async def tune(self, freq_hz: int) -> None:
        """Tune the remote device to an absolute frequency (Hz).

        Slice-20: not implemented (raises NotImplementedError). The
        future implementation will PUT to
        ``{rest_base}/deviceset/{device_set}/device/settings`` with
        the new center frequency.
        """
        raise NotImplementedError(
            "SDRangelSource.tune() is not implemented (slice-20 manifest "
            "scaffold only)."
        )

    async def set_mode(self, mode: str) -> None:
        """Switch the remote demodulator.

        Slice-20: not implemented. The future implementation will
        POST a channel add or PUT channel settings to swap the demod.
        """
        raise NotImplementedError(
            "SDRangelSource.set_mode() is not implemented (slice-20 "
            "manifest scaffold only)."
        )

    async def close(self) -> None:
        """Nothing to clean up — the spawn path was never entered."""
        return None
