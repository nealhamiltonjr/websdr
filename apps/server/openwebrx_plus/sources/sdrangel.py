"""SDRangel remote source — ADR-006 federation client (slice-25 implementation).

SDRangel (`sdrangel.org` <https://www.sdrangel.org/>) is a Qt-based
desktop SDR application with a built-in REST + WebSocket server for
remote control. It is THE dominant Linux desktop SDR app outside the
browser-native space — and a number of public SDRangel instances
expose their REST API for federation.

Slice-25 status: **v1 REST+WS streaming implementation** — spectrum
only, no audio. Closes the slice-20 STATUS.md open item: "the actual
REST+WS streaming implementation lands in a future slice." The v1
implementation:

  1. Probes the REST API to confirm the device set exists.
  2. PUTs device settings (center frequency + sample rate) on spawn
     and on every tune() call.
  3. Opens the spectrum-server WebSocket.
  4. Reads JSON start metadata + binary spectrum frames; yields one
     RemoteFftFrame per binary frame (ReceiverSession repacks into
     the WRFO wire format).
  5. tune() re-PUTs device settings; the WS picks up the change live.
  6. set_mode() is not yet implemented (v2 — channel add/PUT).
  7. Audio is not yet implemented (v2 — needs UDP sink → RemoteAudioFrame
     translation; SDRangel has no built-in audio-over-WS).

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
    - ``GET /spectrumserver?deviceset={id}`` — WebSocket upgrade
    - On connect, the server may emit a JSON ``{"type": "start",
      "size": N, ...}`` control frame; then binary spectrum frames at
      the device's FFT rate. Each binary frame is N float32 dB values
      (no header) OR a 4-byte LE header (uint16 size + uint16 history)
      followed by size * float32 bins — both formats are accepted by
      :meth:`_parse_spectrum_frame` (auto-detect by length math).
  * Audio: SDRangel has no built-in audio-over-WS — the demod output
    goes to the local sound card. v2 will wire SDRangel's UDP-sink
    channel to a local UDP port we read from.

BRING-UP NOTE (same policy as KiwiSDR in sources/kiwi.py): this dev
box has no network route to a live SDRangel instance, so the wire
literals above are verified against the in-repo fake server
(``tests/test_sdrangel_driver.py`` — FakeSDRangelREST + WS) which
codifies the expected behavior. On the FIRST connection to a real
receiver, verify: (a) REST endpoint paths, (b) JSON metadata field
names (``size`` / ``fftSize``, ``sampleRate``, ``centerFrequency``),
(c) the binary frame layout (header or no header). Adjust the
``_*`` constants below if needed — they are the only protocol
literals in the file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import httpx
import numpy as np
import structlog
import websockets
from websockets.exceptions import ConnectionClosed

from .base import RemoteFftFrame, SourceInfo

log = structlog.get_logger(__name__)

# --- Protocol literals (single place to fix after first live bring-up) -----
_DEFAULT_PORT = 8091
_REST_BASE = "/sdrangel"
_WS_PATH = "/sdrangel/spectrumserver"  # deviceset query param appended
# Default min/max dB if the server's JSON start metadata doesn't include
# waterfall_levels (some SDRangel versions omit it).
_DEFAULT_MIN_DB = -100.0
_DEFAULT_MAX_DB = 0.0


@dataclass
class SDRangelSource:
    """A remote SDRangel receiver as a DisplayStreamSource (ADR-006 Tier C).

    Slice-25 status: **v1 REST+WS streaming implementation** — spectrum
    only, no audio. Closes the slice-20 STATUS.md open item.

    The class implements the :class:`~openwebrx_plus.sources.base.DisplayStreamSource`
    protocol — :meth:`display_stream` yields :class:`RemoteFftFrame`
    per binary spectrum frame, and the ReceiverSession repacks them
    into the WRFO wire format. ``spawn()`` is not provided (the
    DisplayStreamSource contract doesn't include it); ReceiverSession
    detects ``hasattr(source, 'display_stream')`` and bypasses the
    raw-IQ path.

    Args:
        host: SDRangel hostname or IP (no scheme).
        port: SDRangel REST API port (default 8091).
        device_set: 0-indexed device set to control (one SDRangel
            instance can host multiple devices; pick the one you want).
        sample_rate: requested IQ rate in Hz. Used in the manifest
            advertisement AND PUT to the device settings on spawn.
        user: client identification (for the User-Agent header — SDRangel
            public instances are volunteer-run; identify honestly per
            ADR-006 federation etiquette).
        connect_timeout: seconds to wait for the REST probe + WS upgrade.
        use_tls: connect to REST via ``https://`` and WS via ``wss://``
            (for instances behind a TLS-terminating reverse proxy).
        username: optional basic-auth username (for authed instances).
        password: optional basic-auth password (for authed instances).
    """

    host: str
    port: int = _DEFAULT_PORT
    device_set: int = 0
    sample_rate: int = 2_400_000
    user: str = "openwebrx_plus (federation client)"
    connect_timeout: float = 10.0
    use_tls: bool = False
    username: str | None = None
    password: str | None = None
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
        # Runtime state — set when display_stream() opens the WS, cleared
        # in the finally block. tune() uses this to PUT device settings
        # on the live connection.
        self._http_client: httpx.AsyncClient | None = None
        self._ws: Any = None
        # Latest known remote spectrum metadata, captured from JSON start
        # frames + REST probe. Used as the default for RemoteFftFrame
        # when a binary frame doesn't include its own.
        self._remote_center_freq: int = 0
        self._remote_sample_rate: int = self.sample_rate
        self._remote_min_db: float | None = None
        self._remote_max_db: float | None = None
        self._remote_fft_size: int | None = None

    # ------------------------------------------------------------------
    # URL builders
    # ------------------------------------------------------------------

    @property
    def _scheme(self) -> str:
        return "https" if self.use_tls else "http"

    @property
    def _ws_scheme(self) -> str:
        return "wss" if self.use_tls else "ws"

    @property
    def _rest_base_url(self) -> str:
        return f"{self._scheme}://{self.host}:{self.port}{_REST_BASE}"

    @property
    def _ws_url(self) -> str:
        return f"{self._ws_scheme}://{self.host}:{self.port}{_WS_PATH}?deviceset={self.device_set}"

    def _auth_headers(self) -> dict[str, str]:
        h = {"User-Agent": self.user}
        if self.username and self.password:
            import base64
            cred = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            h["Authorization"] = f"Basic {cred}"
        return h

    # ------------------------------------------------------------------
    # DisplayStreamSource protocol
    # ------------------------------------------------------------------

    async def display_stream(
        self,
    ) -> AsyncGenerator[RemoteFftFrame, None]:
        """Stream spectrum frames from the SDRangel spectrum server.

        v1 (slice-25): spectrum only — no audio. Audio-over-WS is
        deferred (SDRangel has no built-in audio-over-WS; needs
        UDP-sink → RemoteAudioFrame translation).

        Raises RuntimeError on REST probe failure or WS upgrade failure.

        If ``self._http_client`` is set externally (test injection), it's
        used as-is and NOT closed in the finally block — the caller owns
        it. Otherwise a fresh client is created and closed here.
        """
        owns_http = self._http_client is None
        if owns_http:
            http = httpx.AsyncClient(
                base_url=self._rest_base_url,
                timeout=self.connect_timeout,
                headers=self._auth_headers(),
            )
            self._http_client = http
        else:
            # Use the externally-injected client (test injection). Mypy
            # can't narrow the type from the `is None` check above + the
            # assignment in the if-branch, so assert.
            assert self._http_client is not None
            http = self._http_client
        try:
            # 1. REST probe — confirm the device set exists.
            await self._probe_deviceset(http)
            # 2. PUT device settings (center freq + sample rate).
            # display_stream() doesn't take a center_freq argument (it's
            # a no-arg DisplayStreamSource method), so use the current
            # remote_center_freq (set via __init__'s default of 0 or via
            # a prior tune() call). Operators call tune() before display_stream()
            # to set the initial frequency.
            await self._put_device_settings(http, self._remote_center_freq)
            # 3. Open the spectrum WS.
            ws: Any = await asyncio.wait_for(
                websockets.connect(
                    self._ws_url,
                    open_timeout=self.connect_timeout,
                    additional_headers={"User-Agent": self.user},
                ),
                timeout=self.connect_timeout + 2.0,
            )
            self._ws = ws
        except TimeoutError:
            if owns_http:
                await http.aclose()
                self._http_client = None
            raise RuntimeError(
                f"SDRangel {self.host}:{self.port} did not open the spectrum "
                f"WebSocket within {self.connect_timeout:.0f}s"
            ) from None
        except (httpx.HTTPError, OSError, ConnectionRefusedError) as exc:
            if owns_http:
                await http.aclose()
                self._http_client = None
            raise RuntimeError(
                f"cannot reach SDRangel {self.host}:{self.port}: {exc}"
            ) from exc

        log.info(
            "sdrangel streaming",
            endpoint=f"{self.host}:{self.port}",
            device_set=self.device_set,
            sample_rate=self.sample_rate,
        )

        try:
            async for message in ws:
                if isinstance(message, str):
                    await self._handle_text(message)
                    continue
                # Binary spectrum frame.
                frame = self._parse_spectrum_frame(message)
                if frame is not None:
                    yield frame
        except ConnectionClosed:
            log.info(
                "sdrangel closed the connection",
                endpoint=f"{self.host}:{self.port}",
            )
            return
        finally:
            with contextlib.suppress(Exception):
                await ws.close()
            if owns_http:
                await http.aclose()
                self._http_client = None
            self._ws = None
            log.info(
                "sdrangel disconnected",
                endpoint=f"{self.host}:{self.port}",
            )

    # ------------------------------------------------------------------
    # REST helpers
    # ------------------------------------------------------------------

    async def _probe_deviceset(self, http: httpx.AsyncClient) -> None:
        """GET /devices — confirm the device_set index is in range."""
        try:
            resp = await http.get("/devices")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"SDRangel REST /devices probe failed: {exc}"
            ) from exc
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"SDRangel REST /devices returned non-JSON body: {exc}"
            ) from exc
        # The body shape is {"deviceSets": [{"samplingDevice": {...}}, ...]}
        device_sets = body.get("deviceSets", []) if isinstance(body, dict) else []
        if self.device_set >= len(device_sets):
            raise RuntimeError(
                f"SDRangel device_set {self.device_set} out of range "
                f"(only {len(device_sets)} device sets advertised)"
            )

    async def _put_device_settings(
        self,
        http: httpx.AsyncClient,
        center_freq: int,
    ) -> None:
        """PUT /deviceset/{id}/device/settings — set center freq + sample rate."""
        body = {
            "deviceHwType": "Unknown",  # SDRangel accepts Unknown for retune
            "centerFrequency": int(center_freq),
            "sampleRate": int(self.sample_rate),
        }
        try:
            resp = await http.put(
                f"/deviceset/{self.device_set}/device/settings",
                json=body,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"SDRangel REST device PUT failed: {exc}"
            ) from exc
        # Track the value we last asked for — used as the default for
        # RemoteFftFrame.center_freq when the WS metadata doesn't carry it.
        self._remote_center_freq = int(center_freq)

    # ------------------------------------------------------------------
    # WS message handlers
    # ------------------------------------------------------------------

    async def _handle_text(self, message: str) -> None:
        """Parse JSON metadata frames from the spectrum server.

        SDRangel sends a JSON ``{"type": "start", ...}`` frame on
        connect with FFT size + sample rate + center frequency. Newer
        versions also send periodic metadata frames when the operator
        changes settings. We capture the fields for use as defaults
        when parsing binary frames.
        """
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            # Non-JSON text — ignore (server chatter).
            return
        if not isinstance(data, dict):
            return
        # Field names: try the common variants.
        size = data.get("size") or data.get("fftSize") or data.get("fft_size")
        if isinstance(size, int) and size > 0:
            self._remote_fft_size = int(size)
        sr = data.get("sampleRate") or data.get("sample_rate")
        if isinstance(sr, int) and sr > 0:
            self._remote_sample_rate = int(sr)
        cf = data.get("centerFrequency") or data.get("center_freq")
        if isinstance(cf, int) and cf > 0:
            self._remote_center_freq = int(cf)
        # Use explicit None check for min/max dB — `0.0` is falsy in
        # Python, so `data.get("maxDb") or ...` would skip a legitimately
        # zero max_db value.
        mindb = data.get("minDb")
        if mindb is None:
            mindb = data.get("min_db")
        if isinstance(mindb, (int, float)):
            self._remote_min_db = float(mindb)
        maxdb = data.get("maxDb")
        if maxdb is None:
            maxdb = data.get("max_db")
        if isinstance(maxdb, (int, float)):
            self._remote_max_db = float(maxdb)

    def _parse_spectrum_frame(self, message: bytes) -> RemoteFftFrame | None:
        """Build a RemoteFftFrame from a binary spectrum frame.

        Two formats are accepted (auto-detected by length math):
          A. 4-byte LE header (uint16 size + uint16 history) + size*float32
             bins. Detected when len(message) == 4 + 4*size.
          B. Bare float32 bins (no header). Detected when len(message)
             is divisible by 4.

        If neither pattern matches, the frame is dropped with a debug log
        (we don't raise — a single bad frame shouldn't kill the stream).
        """
        n = len(message)
        if n < 4:
            return None
        # Try pattern A: 4-byte header.
        if n >= 4:
            import struct
            size_a = struct.unpack_from("<HH", message, 0)[0]
            if 4 + 4 * size_a == n and size_a > 0:
                bins = np.frombuffer(message, dtype=np.float32, count=size_a, offset=4)
                return self._build_frame(bins)
        # Pattern B: bare float32 bins.
        if n % 4 == 0:
            bins = np.frombuffer(message, dtype=np.float32)
            if bins.size > 0:
                return self._build_frame(bins)
        log.debug("sdrangel spectrum frame has unexpected length", length=n)
        return None

    def _build_frame(self, bins: np.ndarray) -> RemoteFftFrame:
        """Construct a RemoteFftFrame from parsed bins using the latest
        captured remote metadata (or sensible defaults)."""
        return RemoteFftFrame(
            bins=bins,
            center_freq=self._remote_center_freq,
            sample_rate=self._remote_sample_rate,
            min_db=self._remote_min_db if self._remote_min_db is not None else _DEFAULT_MIN_DB,
            max_db=self._remote_max_db if self._remote_max_db is not None else _DEFAULT_MAX_DB,
        )

    # ------------------------------------------------------------------
    # Control methods
    # ------------------------------------------------------------------

    async def tune(self, freq_hz: int) -> None:
        """Tune the remote device to an absolute frequency (Hz).

        PUTs to /deviceset/{device_set}/device/settings with the new
        center frequency. The spectrum WS picks up the change live.
        """
        http = self._http_client
        if http is None:
            # Not streaming yet — store the value for the upcoming
            # display_stream() call's initial PUT.
            self._remote_center_freq = int(freq_hz)
            return
        await self._put_device_settings(http, int(freq_hz))

    async def set_mode(self, mode: str) -> None:
        """Switch the remote demodulator.

        v1 (slice-25): NOT IMPLEMENTED. The future implementation will
        POST /deviceset/{id}/channel to add a channel (e.g., NFM, WFM,
        AM, LSB, USB, CW), then PUT /deviceset/{id}/channel/{cid}/settings
        to configure it. For now, raises NotImplementedError so the
        operator gets an actionable error rather than silent no-op.
        """
        raise NotImplementedError(
            "SDRangelSource.set_mode() is not implemented in slice-25 "
            "(spectrum-only v1). The future implementation will POST a "
            "channel add + PUT channel settings to swap the demodulator."
        )

    async def close(self) -> None:
        """Nothing to clean up beyond what display_stream()'s finally block did.

        If display_stream() is mid-flight, its finally block closes the
        WS + HTTP client. If close() is called outside the streaming
        loop (e.g., on a never-spawned source), it's a no-op.
        """
        return None
