"""SDRangel remote source — ADR-006 federation client.

Slice-25 shipped the v1 REST+WS streaming implementation (spectrum only).
Slice-35 (this file) adds **audio-over-UDP-sink** + a real ``set_mode()``:

  1. Probes the REST API to confirm the device set exists.
  2. PUTs device settings (center frequency + sample rate) on spawn
     and on every ``tune()`` call.
  3. If ``audio_enabled=True``: opens a local UDP socket, POSTs a channel
     add (NFM/WFM/AM/LSB/USB/CW per ``mode``), PUTs channel settings to
     configure the UDP audio sink to point at our local port, and reads
     int16 mono PCM from the socket in a background task.
  4. Opens the spectrum-server WebSocket.
  5. Reads JSON start metadata + binary spectrum frames; yields one
     ``RemoteFftFrame`` per binary frame (ReceiverSession repacks into
     the WRFO wire format).
  6. Interleaves ``RemoteAudioFrame`` chunks from the UDP listener.
  7. ``tune()`` re-PUTs device settings; the WS picks up the change live.
  8. ``set_mode(mode)`` DELETEs the current channel + POSTs a new one +
     PUTs its settings to re-point the UDP sink at our port.

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
    - ``POST /deviceset/{id}/channel`` — add a channel. Body:
      ``{"channelType": "NFM", "direction": 0}`` (direction 0 = RX).
      Response: ``{"channelIndex": N}``.
    - ``PUT /deviceset/{id}/channel/{cid}/settings`` — set the demod
      settings (RFBW, AFBW, squelch, volume, UDP audio sink).
      The settings body shape is
      ``{"NFMSettings": {"audioSampleRate": 8000, "udpAddress": "127.0.0.1",
                          "udpPort": 9999, "udpEnabled": true, ...}}``
      where the top-level key is ``<channelType>Settings``.
    - ``DELETE /deviceset/{id}/channel/{cid}`` — remove a channel.
  * Spectrum server:
    - ``GET /spectrumserver?deviceset={id}`` — WebSocket upgrade
    - On connect, the server may emit a JSON ``{"type": "start",
      "size": N, ...}`` control frame; then binary spectrum frames at
      the device's FFT rate. Each binary frame is N float32 dB values
      (no header) OR a 4-byte LE header (uint16 size + uint16 history)
      followed by size * float32 bins — both formats are accepted by
      :meth:`_parse_spectrum_frame` (auto-detect by length math).
  * Audio: SDRangel has no built-in audio-over-WS. Demod channels
    (NFM/WFM/AM/SSB/CW) support a **UDP audio sink** — when enabled,
    the channel streams int16 mono PCM at the configured sample rate
    to a UDP destination. Slice-35 wires this up: we open a local UDP
    socket, configure the channel's UDP sink to point at it, and read
    PCM chunks to yield as ``RemoteAudioFrame``.

Mode → channelType mapping (SDRangel's channel Ids):

  * ``USB`` / ``LSB`` → ``SSB`` (with sideband setting)
  * ``AM`` → ``AM``
  * ``NFM`` / ``FM`` → ``NFM``
  * ``WFM`` / ``WBFM`` → ``WFM``
  * ``CW`` → ``CW``

BRING-UP NOTE (same policy as KiwiSDR in sources/kiwi.py): this dev
box has no network route to a live SDRangel instance, so the wire
literals above are verified against the in-repo fake server
(``tests/test_sdrangel_driver.py`` — FakeSDRangelREST + WS + UDP)
which codifies the expected behavior. On the FIRST connection to a real
receiver, verify: (a) REST endpoint paths, (b) JSON metadata field
names (``size`` / ``fftSize``, ``sampleRate``, ``centerFrequency``),
(c) the binary frame layout (header or no header), (d) the channel-add
response shape (``channelIndex`` vs ``index``), (e) the channel
settings body shape (``<channelType>Settings`` top-level key).
Adjust the ``_*`` constants below if needed — they are the only
protocol literals in the file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import struct
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import httpx
import numpy as np
import structlog
import websockets
from websockets.exceptions import ConnectionClosed

from .base import RemoteAudioFrame, RemoteFftFrame, SourceInfo

log = structlog.get_logger(__name__)

# --- Protocol literals (single place to fix after first live bring-up) -----
_DEFAULT_PORT = 8091
_REST_BASE = "/sdrangel"
_WS_PATH = "/sdrangel/spectrumserver"  # deviceset query param appended
# Default min/max dB if the server's JSON start metadata doesn't include
# waterfall_levels (some SDRangel versions omit it).
_DEFAULT_MIN_DB = -100.0
_DEFAULT_MAX_DB = 0.0

# SDRangel channel-type Ids (POST /deviceset/{id}/channel body's "channelType").
# The WFM/NFM/AM/CW channels are first-class; SSB covers USB+LSB (sideband
# selected via the SSBSettings.sidebands field — 0=LSB, 1=USB, 2=DSB).
_SDR_CHANNEL_TYPES: dict[str, str] = {
    "USB": "SSB",
    "LSB": "SSB",
    "AM": "AM",
    "NFM": "NFM",
    "FM": "NFM",  # alias
    "WFM": "WFM",
    "WBFM": "WFM",  # alias
    "CW": "CW",
}
# SSB sideband codes (SSBSettings.sidebands field).
_SSB_LSB = 0
_SSB_USB = 1
_SSB_LSB_STR = "lsb"
_SSB_USB_STR = "usb"

# UDP audio chunk: read up to this many bytes per recv. int16 mono @ 8 kS/s
# = 16 kB/s; 4096 samples = 8 kB ≈ 0.5 s of audio — reasonable chunk size.
_AUDIO_UDP_RECV_BYTES = 8192
# Queue depth for audio chunks before the UDP listener drops (drop-oldest,
# matching the IqHub pattern — slow consumers don't grow buffers unbounded).
_AUDIO_QUEUE_MAX = 32


@dataclass
class SDRangelSource:
    """A remote SDRangel receiver as a DisplayStreamSource (ADR-006 Tier C).

    Slice-25 shipped v1 (spectrum only). Slice-35 adds audio-over-UDP-sink
    + a real ``set_mode()``.

    The class implements the :class:`~openwebrx_plus.sources.base.DisplayStreamSource`
    protocol — :meth:`display_stream` yields :class:`RemoteFftFrame`
    per binary spectrum frame (and :class:`RemoteAudioFrame` chunks
    when audio is enabled). The ReceiverSession repacks them into the
    WRFO / AUDI wire formats. ``spawn()`` is not provided (the
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
        audio_enabled: if True, open a local UDP socket, add a demod
            channel on the remote, and configure its UDP audio sink to
            stream int16 mono PCM to us. display_stream() then yields
            interleaved RemoteAudioFrame chunks alongside the FFT frames.
            Default False (spectrum-only, matching slice-25 behavior).
        audio_output_rate: audio sample rate in Hz. Must match one of
            SDRangel's supported rates (8000 / 12000 / 24000 / 48000).
            Default 8000 — the legacy OpenWebRX+ ws audio wire format.
        audio_mode: initial demod mode when audio_enabled (NFM/WFM/AM/
            USB/LSB/CW). Default "NFM". Use set_mode() to switch later.
        audio_udp_port: local UDP port to listen on. 0 = auto-assign
            an ephemeral port (recommended — avoids conflicts).
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
    # Slice-35 audio params:
    audio_enabled: bool = False
    audio_output_rate: int = 8000
    audio_mode: str = "NFM"
    audio_udp_port: int = 0
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
        if self.audio_output_rate not in (8000, 12000, 24000, 48000):
            raise ValueError(
                f"audio_output_rate must be 8000/12000/24000/48000, "
                f"got {self.audio_output_rate}"
            )
        mode_norm = self.audio_mode.upper()
        if mode_norm not in _SDR_CHANNEL_TYPES:
            raise ValueError(
                f"audio_mode must be one of {sorted(_SDR_CHANNEL_TYPES)}, "
                f"got {self.audio_mode!r}"
            )
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
        # Slice-35 audio state:
        self._audio_socket: socket.socket | None = None
        self._audio_listen_task: asyncio.Task[None] | None = None
        self._audio_queue: asyncio.Queue[bytes] | None = None
        self._audio_channel_index: int | None = None
        self._audio_current_mode: str = self.audio_mode.upper()
        # _audio_local_port is set when _setup_audio() binds the UDP socket.
        # Initialized to None here so attribute access before bind returns
        # None (not AttributeError) — tests poll for it.
        self._audio_local_port: int | None = None
        # Event set when _setup_audio() has finished binding the UDP socket
        # + adding the remote channel. Tests await this instead of polling.
        self._audio_ready: asyncio.Event = asyncio.Event()

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
    ) -> AsyncGenerator[RemoteFftFrame | RemoteAudioFrame, None]:
        """Stream spectrum frames (and optionally audio) from SDRangel.

        Slice-35: if ``audio_enabled=True``, opens a local UDP socket,
        adds a demod channel via REST, configures its UDP sink, and
        yields interleaved RemoteAudioFrame chunks alongside the FFT
        frames. The audio listener runs in a background task; this
        generator drains its queue between WS reads.

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
            await self._put_device_settings(http, self._remote_center_freq)
            # 3. (slice-35) If audio enabled: open UDP listener + add channel.
            if self.audio_enabled:
                await self._setup_audio(http)
            # 4. Open the spectrum WS.
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
            await self._teardown_audio()
            if owns_http:
                await http.aclose()
                self._http_client = None
            raise RuntimeError(
                f"SDRangel {self.host}:{self.port} did not open the spectrum "
                f"WebSocket within {self.connect_timeout:.0f}s"
            ) from None
        except (httpx.HTTPError, OSError, ConnectionRefusedError) as exc:
            await self._teardown_audio()
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
            audio_enabled=self.audio_enabled,
            audio_mode=self._audio_current_mode if self.audio_enabled else None,
            audio_output_rate=self.audio_output_rate if self.audio_enabled else None,
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
                # (slice-35) Drain any pending audio chunks between WS
                # frames — non-blocking, keeps audio flowing at near-realtime
                # even when the WS fps is low.
                async for audio_frame in self._drain_audio():
                    yield audio_frame
        except ConnectionClosed:
            log.info(
                "sdrangel closed the connection",
                endpoint=f"{self.host}:{self.port}",
            )
            return
        finally:
            with contextlib.suppress(Exception):
                await ws.close()
            await self._teardown_audio()
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
    # Channel management (slice-35)
    # ------------------------------------------------------------------

    async def _add_channel(
        self,
        http: httpx.AsyncClient,
        mode: str,
    ) -> int:
        """POST /deviceset/{id}/channel — add a demod channel.

        Returns the new channel's index (from the response's
        ``channelIndex`` field — SDRangel REST v7+).
        """
        channel_type = _SDR_CHANNEL_TYPES.get(mode.upper())
        if channel_type is None:
            raise ValueError(
                f"unsupported mode {mode!r}; supported: {sorted(_SDR_CHANNEL_TYPES)}"
            )
        body = {
            "channelType": channel_type,
            "direction": 0,  # 0 = RX
        }
        try:
            resp = await http.post(
                f"/deviceset/{self.device_set}/channel",
                json=body,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"SDRangel REST channel POST failed (mode={mode}): {exc}"
            ) from exc
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"SDRangel REST channel POST returned non-JSON: {exc}"
            ) from exc
        # SDRangel v7+ returns {"channelIndex": N}; some forks return
        # {"index": N}. Accept both.
        idx = data.get("channelIndex")
        if idx is None:
            idx = data.get("index")
        if not isinstance(idx, int) or idx < 0:
            raise RuntimeError(
                f"SDRangel REST channel POST returned no channelIndex: {data}"
            )
        return int(idx)

    async def _delete_channel(
        self,
        http: httpx.AsyncClient,
        channel_index: int,
    ) -> None:
        """DELETE /deviceset/{id}/channel/{cid} — remove a channel."""
        try:
            resp = await http.delete(
                f"/deviceset/{self.device_set}/channel/{channel_index}"
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"SDRangel REST channel DELETE failed (cid={channel_index}): {exc}"
            ) from exc

    async def _put_channel_settings(
        self,
        http: httpx.AsyncClient,
        channel_index: int,
        mode: str,
    ) -> None:
        """PUT /deviceset/{id}/channel/{cid}/settings — configure the demod.

        The body shape is ``{"<channelType>Settings": {...}}``. For SSB,
        the sidebands field selects USB (1) / LSB (0). All channels with
        audio output support the UDP sink fields:
        ``udpAddress``, ``udpPort``, ``udpEnabled``, ``audioSampleRate``.
        """
        channel_type = _SDR_CHANNEL_TYPES[mode.upper()]
        settings_key = f"{channel_type}Settings"
        # _audio_local_port is set by _setup_audio() before any _put_channel_settings
        # call; assert to satisfy mypy that it's not None here.
        assert self._audio_local_port is not None, (
            "_put_channel_settings called before _setup_audio bound the UDP socket"
        )
        settings: dict[str, Any] = {
            "audioSampleRate": int(self.audio_output_rate),
            "udpAddress": "127.0.0.1",
            "udpPort": int(self._audio_local_port),
            "udpEnabled": True,
        }
        # SSB needs the sideband field.
        if channel_type == "SSB":
            settings["sidebands"] = _SSB_USB if mode.upper() == "USB" else _SSB_LSB
        body = {settings_key: settings}
        try:
            resp = await http.put(
                f"/deviceset/{self.device_set}/channel/{channel_index}/settings",
                json=body,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"SDRangel REST channel PUT failed (cid={channel_index}, mode={mode}): {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Audio UDP listener (slice-35)
    # ------------------------------------------------------------------

    async def _setup_audio(self, http: httpx.AsyncClient) -> None:
        """Open the UDP socket, spawn the listener task, add + configure
        the remote demod channel."""
        # Bind the UDP socket first so we know the local port before
        # configuring the remote sink.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        # Bind to localhost on the requested port (0 = ephemeral).
        sock.bind(("127.0.0.1", int(self.audio_udp_port)))
        actual_port = sock.getsockname()[1]
        self._audio_socket = sock
        self._audio_local_port = actual_port
        # Bounded queue — drop-oldest on overflow (matching the IqHub pattern).
        self._audio_queue = asyncio.Queue(maxsize=_AUDIO_QUEUE_MAX)
        # Start the background listener.
        self._audio_listen_task = asyncio.create_task(self._audio_listen_loop())
        # Add the channel + configure its UDP sink.
        idx = await self._add_channel(http, self._audio_current_mode)
        self._audio_channel_index = idx
        await self._put_channel_settings(http, idx, self._audio_current_mode)
        # Signal that audio is ready (tests await this).
        self._audio_ready.set()
        log.info(
            "sdrangel audio configured",
            endpoint=f"{self.host}:{self.port}",
            mode=self._audio_current_mode,
            sample_rate=self.audio_output_rate,
            udp_port=actual_port,
            channel_index=idx,
        )

    async def _teardown_audio(self) -> None:
        """Cancel the listener task, close the UDP socket, remove the channel."""
        # Cancel the listener task.
        if self._audio_listen_task is not None:
            self._audio_listen_task.cancel()
            with contextlib.suppress(BaseException):
                await self._audio_listen_task
            self._audio_listen_task = None
        # Close the UDP socket.
        if self._audio_socket is not None:
            with contextlib.suppress(Exception):
                self._audio_socket.close()
            self._audio_socket = None
        # DELETE the remote channel (best-effort — if the HTTP client is
        # already closed, there's nothing we can do).
        if self._audio_channel_index is not None and self._http_client is not None:
            http = self._http_client
            with contextlib.suppress(Exception):
                await self._delete_channel(http, self._audio_channel_index)
        self._audio_channel_index = None
        self._audio_queue = None
        self._audio_local_port = None
        self._audio_ready.clear()

    async def _audio_listen_loop(self) -> None:
        """Background task: read int16 PCM chunks from the UDP socket and
        push them onto the queue. Drop-oldest on overflow."""
        sock = self._audio_socket
        q = self._audio_queue
        if sock is None or q is None:
            return
        loop = asyncio.get_running_loop()
        while True:
            try:
                data = await loop.sock_recv(sock, _AUDIO_UDP_RECV_BYTES)
            except (OSError, ConnectionError):
                # Socket closed — exit cleanly.
                return
            except asyncio.CancelledError:
                return
            if not data:
                return
            # Drop-oldest on overflow — drop the OLDEST chunk to make room
            # for the new one (matching IqHub's policy: stale audio is
            # worthless; fresh audio is mandatory).
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(data)

    async def _drain_audio(self) -> AsyncGenerator[RemoteAudioFrame, None]:
        """Yield any pending audio chunks as RemoteAudioFrame objects.

        Non-blocking — yields nothing if the queue is empty. Called
        between WS reads to keep audio flowing.
        """
        if self._audio_queue is None:
            return
        while True:
            try:
                data = self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            # The UDP payload is raw int16 LE PCM (mono). SDRangel's UDP
            # sink doesn't add a header — just the samples.
            if len(data) < 2:
                continue
            # numpy frombuffer is zero-copy; .copy() to detach from the
            # UDP buffer (which the socket reuses).
            pcm = np.frombuffer(data, dtype=np.int16).copy()
            yield RemoteAudioFrame(
                pcm=pcm,
                sample_rate=int(self.audio_output_rate),
            )

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
        """Switch the remote demodulator (slice-35).

        DELETEs the current channel, POSTs a new one with the requested
        mode, PUTs its settings to re-point the UDP sink at our local
        port. If audio is not enabled, this is a no-op (we have no
        channel to swap — the spectrum-only path doesn't depend on a
        demod channel).
        """
        mode_norm = mode.upper()
        if mode_norm not in _SDR_CHANNEL_TYPES:
            raise ValueError(
                f"unsupported mode {mode!r}; supported: {sorted(_SDR_CHANNEL_TYPES)}"
            )
        if not self.audio_enabled:
            # Spectrum-only mode — no demod channel to swap. Track the
            # requested mode so a future enable-audio call starts with it.
            self._audio_current_mode = mode_norm
            return
        http = self._http_client
        if http is None:
            # Not streaming yet — just remember the mode for the upcoming
            # display_stream() call's channel add.
            self._audio_current_mode = mode_norm
            return
        # Swap the channel: DELETE old, POST new, PUT settings.
        if self._audio_channel_index is not None:
            await self._delete_channel(http, self._audio_channel_index)
            self._audio_channel_index = None
        idx = await self._add_channel(http, mode_norm)
        self._audio_channel_index = idx
        await self._put_channel_settings(http, idx, mode_norm)
        self._audio_current_mode = mode_norm
        log.info(
            "sdrangel set_mode",
            endpoint=f"{self.host}:{self.port}",
            mode=mode_norm,
            channel_index=idx,
        )

    async def close(self) -> None:
        """Nothing to clean up beyond what display_stream()'s finally block did.

        If display_stream() is mid-flight, its finally block closes the
        WS + HTTP client + UDP listener + remote channel. If close() is
        called outside the streaming loop (e.g., on a never-spawned
        source), it's a no-op.
        """
        return None
