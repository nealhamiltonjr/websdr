"""OpenWebRX(+) federation client — RemoteDisplaySource (ADR-006).

Connects to any public OpenWebRX / OpenWebRX+ receiver over the internet and
streams its waterfall + demodulated audio into a ReceiverSession. This is the
receive side of the federation pillar: the URL a user pastes is the same one
they would open in a browser — e.g. the deep link ::

    http://boomerthedog.com:8073/#freq=3570000,mod=lsb,sql=-150

— parsed by :func:`parse_openwebrx_url` into host/port + initial tune.

Unlike the other network sources (rtl_tcp, Kiwi), the OpenWebRX protocol does
NOT carry raw IQ: the remote server computes the FFT and demodulates the
tuned channel itself. So this source is a *display-stream* source: it yields
:class:`~openwebrx_plus.sources.base.RemoteFftFrame` /
:class:`~openwebrx_plus.sources.base.RemoteAudioFrame` instead of IQ, and the
ReceiverSession bypasses its pycsdr chains (see receiver_session.py). Tuning
is forwarded to the remote ``dspcontrol`` — our UI drives their demodulator.

Protocol (extracted from the vendored upstream source, owrx/connection.py +
htdocs/openwebrx.js; literals isolated below for first-live-connection fixes):

  1. connect ``ws://host:port/ws/`` (``wss://`` for https receivers)
  2. client sends the handshake text::

         SERVER DE CLIENT client=<id> type=receiver

     the server replies ``CLIENT DE SERVER server=openwebrx version=...``
     and immediately pushes a burst of JSON messages: ``receiver_details``,
     ``config`` (global, then sdr-layer), ``features``, ``modes``,
     ``profiles``, ``dial_frequencies``, ``bookmarks``, ``bands``.
  3. client sends ``{"type": "connectionproperties", "params":
     {"output_rate": ..., "hd_output_rate": ...}}`` (audio rate request)
  4. client sends ``{"type": "dspcontrol", "action": "start"}`` then tuning
     params ``{"type": "dspcontrol", "params": {...}}`` with the keys
     ``offset_freq`` (VFO offset from the remote center), ``mod``,
     ``squelch_level``, ``low_cut``/``high_cut`` (bandpass, from the remote's
     own mode table when available).
  5. binary frames arrive with a 1-byte tag: ``0x01`` FFT, ``0x02`` audio,
     ``0x03`` secondary FFT, ``0x04`` HD audio. FFT uses per-frame-reset
     ADPCM (or float32); audio uses a continuous sync-framed ADPCM stream
     (or int16). See ``sources/_adpcm.py`` for the exact byte formats.
  6. JSON telemetry continues: ``smeter``, ``cpuusage``, ``metadata`` …;
     ``{"type": "backoff", "reason": ...}`` means the receiver refused us.

Etiquette (ADR-006, non-negotiable):

  * identify honestly — the handshake ``client=`` id and a distinct
    User-Agent say who we are; never impersonate a browser;
  * one connection per receiver, closed promptly on stop;
  * no reconnect storms — a dead or refused endpoint raises a clear error
    and ends the stream; reconnection is user-initiated.

BRING-UP NOTE (same policy as the Kiwi literals): this dev box has no route
to public receivers, so the protocol above is codified by the in-repo fake
server (tests/test_openwebrx_remote_driver.py), built from the vendored
upstream implementation. On the first live connection verify: (a) the
handshake acceptance, (b) config keys, (c) ADPCM interop, (d) dspcontrol
param names — adjust the ``_*`` constants below if needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import numpy as np
import structlog
import websockets

from ._adpcm import ImaAdpcmCodec, decode_fft_adpcm
from .base import RemoteAudioFrame, RemoteFftFrame, SourceInfo

log = structlog.get_logger(__name__)

# --- Protocol literals (single place to fix after first live bring-up) -----
_WS_PATH = "/ws/"
_HANDSHAKE_FMT = "SERVER DE CLIENT client={client} type=receiver"
_SERVER_HANDSHAKE_PREFIX = "CLIENT DE SERVER"
_TYPE_FFT = 0x01
_TYPE_AUDIO = 0x02
_TYPE_SECONDARY_FFT = 0x03
_TYPE_HD_AUDIO = 0x04
_DEFAULT_FFT_COMPRESSION = "adpcm"  # server-side defaults; config overrides
_DEFAULT_AUDIO_COMPRESSION = "adpcm"
_DEFAULT_OUTPUT_RATE = 12_000  # OpenWebRX+ server default
_DEFAULT_HD_OUTPUT_RATE = 48_000
_HANDSHAKE_TIMEOUT = 8.0
_USER_AGENT = "openwebrx-plus-federation/0.1 (modernized OpenWebRX+)"

# Deep-link keys (upstream htdocs/lib/DemodulatorPanel.js parseHash):
# freq=<Hz>, mod=<mode>, secondary_mod=<mode>, sql=<dB>, key=<magic>.
# Mode-name normalization for our UI's mode strings → OpenWebRX modulations.
_MODE_ALIASES = {
    "fm": "nfm",
    "nfm": "nfm",
    "wfm": "wfm",
    "am": "am",
    "sam": "sam",
    "lsb": "lsb",
    "usb": "usb",
    "cw": "cw",
    "usbd": "usbd",
    "lsbd": "lsbd",
}

# Bandpass fallback (Hz, OpenWebRX conventions from owrx/modes.py) used until
# the remote's own "modes" message arrives (it usually arrives immediately).
_FALLBACK_BANDPASS: dict[str, tuple[int, int]] = {
    "nfm": (-4000, 4000),
    "wfm": (-75000, 75000),
    "am": (-4000, 4000),
    "sam": (-4000, 4000),
    "lsb": (-2750, -150),
    "usb": (150, 2750),
    "cw": (700, 900),
    "usbd": (150, 2750),
    "lsbd": (-2750, -150),
}


@dataclass(frozen=True)
class RemoteTarget:
    """A parsed receiver deep link / endpoint."""

    host: str
    port: int
    use_tls: bool
    freq: int | None = None  # Hz
    mod: str | None = None
    secondary_mod: str | None = None
    squelch: float | None = None  # dB
    magic_key: str | None = None


def parse_openwebrx_url(url: str) -> RemoteTarget:
    """Parse an OpenWebRX receiver URL (deep link) into a :class:`RemoteTarget`.

    Accepts the forms found in the wild::

        http://boomerthedog.com:8073/#freq=3570000,mod=lsb,sql=-150
        https://rx.example.com/
        ws://host:8073           (scheme already websocket)
        boomerthedog.com:8073    (bare host[:port], http assumed)

    The ``#`` fragment carries comma-separated ``key=value`` pairs
    (``freq``/``mod``/``secondary_mod``/``sql``/``key``); unknown keys are
    tolerated and skipped, exactly like the upstream client's parseHash().
    """
    url = url.strip()
    if not url:
        raise ValueError("empty receiver URL")

    # Bare host[:port] → assume http.
    if "://" not in url:
        url = "http://" + url

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ValueError(f"unparseable receiver URL: {url!r}") from exc

    scheme = (parts.scheme or "http").lower()
    if scheme in ("https", "wss"):
        use_tls = True
    elif scheme in ("http", "ws"):
        use_tls = False
    else:
        raise ValueError(f"unsupported scheme {scheme!r} in receiver URL {url!r}")

    host = (parts.hostname or "").strip()
    if not host:
        raise ValueError(f"receiver URL has no host: {url!r}")
    port = parts.port or (443 if use_tls else 8073)

    # Deep-link fragment: "#freq=3570000,mod=lsb,sql=-150"
    fragment = (parts.fragment or "").strip()
    kv: dict[str, str] = {}
    if fragment:
        for pair in fragment.split(","):
            key, sep, value = pair.partition("=")
            if sep and key.strip():
                kv[key.strip()] = value.strip()

    freq: int | None = None
    if "freq" in kv:
        try:
            freq = int(float(kv["freq"]))
        except ValueError:
            log.debug("ignoring unparseable deep-link freq", value=kv["freq"])
        else:
            if freq <= 0:
                freq = None
    mod = kv.get("mod") or None
    secondary_mod = kv.get("secondary_mod") or None
    squelch: float | None = None
    if "sql" in kv:
        with contextlib.suppress(ValueError):
            squelch = float(kv["sql"])
    magic_key = kv.get("key") or None

    return RemoteTarget(
        host=host,
        port=port,
        use_tls=use_tls,
        freq=freq,
        mod=mod,
        secondary_mod=secondary_mod,
        squelch=squelch,
        magic_key=magic_key,
    )


@dataclass
class RemoteDisplaySource:
    """A remote OpenWebRX(+) receiver as a display-stream source (ADR-006).

    Yields already-computed FFT frames (dB bins) and demodulated audio
    (int16 PCM) — NOT raw IQ. Instantiate from a deep-link URL::

        RemoteDisplaySource(url="http://boomerthedog.com:8073/"
                                 "#freq=3570000,mod=lsb,sql=-150")

    or from host/port plus explicit tuning. The ReceiverSession detects the
    ``display_stream`` attribute and bypasses its pycsdr chains, repacking
    the frames into the standard WRFO/AUDI wire formats for the frontend.
    """

    url: str | None = None  # deep-link passthrough, parsed in __post_init__
    host: str = ""
    port: int = 8073
    use_tls: bool = False
    freq: int | None = None  # initial tune (Hz), deep link or explicit
    mod: str | None = None  # initial modulation
    squelch: float | None = None  # initial squelch (dB)
    output_rate: int = _DEFAULT_OUTPUT_RATE
    hd_output_rate: int = _DEFAULT_HD_OUTPUT_RATE
    client_id: str = "openwebrx_plus_federation"
    connect_timeout: float = 10.0
    info: SourceInfo = field(
        default_factory=lambda: SourceInfo(
            type="openwebrx_remote",
            label="OpenWebRX remote",
            sample_rate=0,
        )
    )

    # -- runtime state (populated while streaming) ---------------------------
    remote_config: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    receiver_details: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    features: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    modes: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    profiles: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    server_version: str | None = field(default=None, init=False)
    smeter: float | None = field(default=None, init=False)
    _connection: Any = field(default=None, init=False, repr=False)
    _audio_codec: ImaAdpcmCodec = field(default_factory=ImaAdpcmCodec, init=False, repr=False)
    _fft_compression: str = field(default=_DEFAULT_FFT_COMPRESSION, init=False)
    _audio_compression: str = field(default=_DEFAULT_AUDIO_COMPRESSION, init=False)
    _current_offset: int = field(default=0, init=False)
    _current_mod: str | None = field(default=None, init=False)
    _initial_params_sent: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.url:
            target = parse_openwebrx_url(self.url)
            self.host = target.host
            self.port = target.port
            self.use_tls = target.use_tls
            if self.freq is None:
                self.freq = target.freq
            if self.mod is None:
                self.mod = target.mod
            if self.squelch is None:
                self.squelch = target.squelch
        if not self.host:
            raise ValueError(
                "host is required (or pass url= with a receiver deep link, "
                "e.g. http://boomerthedog.com:8073/#freq=3570000,mod=lsb)"
            )
        if not (1 <= self.port <= 65535):
            raise ValueError(f"port must be 1-65535, got {self.port}")
        if self.output_rate < 1000:
            raise ValueError("output_rate must be >= 1000")
        self.info = SourceInfo(
            type="openwebrx_remote",
            label=f"OpenWebRX {self.host}:{self.port}",
            endpoint=f"{self.host}:{self.port}",
            sample_rate=0,
        )

    # ------------------------------------------------------------------
    # Introspection for the session / metadata pump
    # ------------------------------------------------------------------

    @property
    def tuned_freq(self) -> int | None:
        """Absolute VFO frequency = remote center + current offset."""
        center = self.remote_config.get("center_freq")
        if center is None:
            return self.freq
        return int(center) + self._current_offset

    @property
    def center_freq(self) -> int | None:
        center = self.remote_config.get("center_freq")
        return int(center) if center is not None else None

    # ------------------------------------------------------------------
    # The display stream
    # ------------------------------------------------------------------

    async def display_stream(
        self,
    ) -> AsyncGenerator[RemoteFftFrame | RemoteAudioFrame, None]:
        """Connect to the receiver and yield FFT/audio frames forever.

        Raises RuntimeError on connect failure or if the receiver refuses us
        (``backoff``) — no retry loop (ADR-006 etiquette). Ends cleanly when
        the server closes the connection.
        """
        uri = f"{'wss' if self.use_tls else 'ws'}://{self.host}:{self.port}{_WS_PATH}"
        try:
            connection: Any = await asyncio.wait_for(
                websockets.connect(
                    uri,
                    open_timeout=self.connect_timeout,
                    additional_headers={"User-Agent": _USER_AGENT},
                ),
                timeout=self.connect_timeout + 2.0,
            )
        except TimeoutError:
            raise RuntimeError(
                f"OpenWebRX receiver {self.host}:{self.port} did not open a "
                f"websocket within {self.connect_timeout:.0f}s"
            ) from None
        except OSError as exc:
            raise RuntimeError(
                f"cannot reach OpenWebRX receiver {self.host}:{self.port}: {exc}"
            ) from exc

        self._connection = connection
        self._audio_codec.reset()
        self._initial_params_sent = False
        try:
            await connection.send(_HANDSHAKE_FMT.format(client=self.client_id))
            log.info(
                "openwebrx federation client connected",
                endpoint=f"{self.host}:{self.port}",
                uri=uri,
            )

            async for frame in self._pump(connection):
                yield frame
        finally:
            self._connection = None
            with contextlib.suppress(Exception):
                await connection.close()
            log.info(
                "openwebrx federation client disconnected",
                endpoint=f"{self.host}:{self.port}",
            )

    async def _pump(
        self, connection: Any
    ) -> AsyncGenerator[RemoteFftFrame | RemoteAudioFrame, None]:
        """Message loop: handshake reply → rates → dsp start → frames.

        Any binary frames that arrive during the handshake/initial-config
        window are still decoded and yielded — nothing is dropped.
        """
        # 1. server handshake reply ("CLIENT DE SERVER server=... version=...")
        deadline = asyncio.get_running_loop().time() + _HANDSHAKE_TIMEOUT
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise RuntimeError(
                    f"receiver {self.host}:{self.port} never completed the "
                    "handshake (expected 'CLIENT DE SERVER ...')"
                )
            message = await asyncio.wait_for(connection.recv(), timeout=remaining)
            if isinstance(message, str):
                if message.startswith(_SERVER_HANDSHAKE_PREFIX):
                    for token in message[len(_SERVER_HANDSHAKE_PREFIX) :].split():
                        key, sep, value = token.partition("=")
                        if sep and key == "version":
                            self.server_version = value
                    log.info(
                        "openwebrx handshake complete",
                        server=self.host,
                        version=self.server_version,
                    )
                    break
                # JSON pushed before the handshake reply (allowed) — handle it.
                self._handle_text(connection, message)
            else:
                frame = self._handle_binary(message)
                if frame is not None:
                    yield frame

        # 2. audio rate request + start the demodulator
        await self._send(
            connection,
            {
                "type": "connectionproperties",
                "params": {
                    "output_rate": self.output_rate,
                    "hd_output_rate": self.hd_output_rate,
                },
            },
        )
        await self._send(connection, {"type": "dspcontrol", "action": "start"})

        # 3. main loop; initial tuning fires as soon as center_freq is known
        async for message in connection:
            if isinstance(message, str):
                self._handle_text(connection, message)
            else:
                frame = self._handle_binary(message)
                if frame is not None:
                    yield frame

    # ------------------------------------------------------------------
    # Control (tuning) — called by the ReceiverSession
    # ------------------------------------------------------------------

    async def tune(self, freq_hz: int) -> None:
        """Tune the remote demodulator to an absolute frequency."""
        center = self.remote_config.get("center_freq")
        if center is None:
            # Not connected yet — remember it; applied after handshake.
            self.freq = freq_hz
            log.debug("tune before connect — deferred", freq=freq_hz)
            return
        offset = int(freq_hz - int(center))
        half_rate = int(self.remote_config.get("samp_rate", 0)) // 2
        if half_rate and abs(offset) > half_rate:
            log.warning(
                "tune outside remote passband — clamping",
                requested=freq_hz,
                center=int(center),
                span=2 * half_rate,
            )
            offset = max(-half_rate, min(half_rate, offset))
        self._current_offset = offset
        await self._send_params({"offset_freq": offset})

    async def set_mode(self, mode: str) -> None:
        """Switch the remote demodulator (mode name normalized)."""
        mod = _MODE_ALIASES.get(mode.strip().lower(), mode.strip().lower())
        params: dict[str, Any] = {"mod": mod}
        bandpass = self._bandpass_for(mod)
        if bandpass is not None:
            params["low_cut"], params["high_cut"] = bandpass
        self._current_mod = mod
        await self._send_params(params)

    async def set_squelch(self, level: float) -> None:
        """Set the remote squelch (dB)."""
        await self._send_params({"squelch_level": level})

    async def close(self) -> None:
        """Close the connection politely (frees a user slot on the remote)."""
        connection = self._connection
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _send(self, connection: Any, message: dict[str, Any]) -> None:
        """Send one JSON control message; tolerate a just-closed receiver.

        A receiver that refused us (``backoff``) closes immediately after
        sending the refusal — our rate request / dsp start can hit the closed
        socket BEFORE the buffered refusal message is processed. Swallowing
        the send error lets the message loop surface the real reason.
        """
        try:
            await connection.send(json.dumps(message))
        except websockets.exceptions.ConnectionClosed:
            log.warning(
                "control send failed — receiver closed the connection",
                message_type=message.get("type"),
            )

    async def _send_params(self, params: dict[str, Any]) -> None:
        connection = self._connection
        if connection is None:
            log.warning("dspcontrol dropped — not connected", params=params)
            return
        await self._send(connection, {"type": "dspcontrol", "params": params})
        log.debug("dspcontrol sent", params=params)

    def _bandpass_for(self, mod: str) -> tuple[int, int] | None:
        """Bandpass (low_cut, high_cut) for a modulation.

        Prefers the remote's own "modes" message (exact server-side filter
        definitions); falls back to the OpenWebRX mode table.
        """
        for mode in self.modes:
            if isinstance(mode, dict) and mode.get("modulation") == mod:
                bandpass = mode.get("bandpass")
                if isinstance(bandpass, dict):
                    low = bandpass.get("low_cut")
                    high = bandpass.get("high_cut")
                    if isinstance(low, (int, float)) and isinstance(high, (int, float)):
                        return int(low), int(high)
                return None
        return _FALLBACK_BANDPASS.get(mod)

    def _maybe_send_initial_tuning(self, connection: Any) -> None:
        """Fire the initial tune once center_freq is known (post-config)."""
        if self._initial_params_sent:
            return
        center = self.remote_config.get("center_freq")
        if center is None:
            return
        self._initial_params_sent = True

        center = int(center)
        start_freq = self.remote_config.get("start_freq")
        target = self.freq if self.freq is not None else (
            int(start_freq) if start_freq is not None else center
        )
        mod = self.mod or self.remote_config.get("start_mod") or "nfm"
        mod = _MODE_ALIASES.get(str(mod).lower(), str(mod).lower())
        squelch = (
            self.squelch
            if self.squelch is not None
            else self.remote_config.get("initial_squelch_level", -150)
        )

        params: dict[str, Any] = {
            "offset_freq": int(target - center),
            "mod": mod,
            "squelch_level": squelch,
        }
        bandpass = self._bandpass_for(mod)
        if bandpass is not None:
            params["low_cut"], params["high_cut"] = bandpass

        self._current_offset = int(target - center)
        self._current_mod = mod
        # Best-effort fire-and-forget from the message handler; failures are
        # loud but must not kill the stream.
        task = asyncio.ensure_future(self._send_params(params))
        task.add_done_callback(_log_send_failure)

    def _handle_text(self, connection: Any, message: str) -> None:
        """Dispatch one JSON message from the receiver."""
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            log.debug("non-JSON text message ignored", preview=message[:80])
            return
        if not isinstance(msg, dict):
            return
        mtype = msg.get("type")
        value = msg.get("value")

        if mtype == "config":
            if isinstance(value, dict):
                self.remote_config.update(value)
                if "fft_compression" in value:
                    self._fft_compression = str(value["fft_compression"])
                if "audio_compression" in value:
                    self._audio_compression = str(value["audio_compression"])
                if "samp_rate" in value:
                    self.info = SourceInfo(
                        type="openwebrx_remote",
                        label=f"OpenWebRX {self.host}:{self.port}",
                        endpoint=f"{self.host}:{self.port}",
                        sample_rate=int(value["samp_rate"]),
                    )
                self._maybe_send_initial_tuning(connection)
        elif mtype == "receiver_details":
            if isinstance(value, dict):
                self.receiver_details = value
        elif mtype == "features":
            if isinstance(value, dict):
                self.features = value
        elif mtype == "modes":
            if isinstance(value, list):
                self.modes = [m for m in value if isinstance(m, dict)]
                self._maybe_send_initial_tuning(connection)
        elif mtype == "profiles":
            if isinstance(value, list):
                self.profiles = value
        elif mtype == "smeter":
            self.smeter = value if isinstance(value, (int, float)) else None
        elif mtype == "backoff":
            raise RuntimeError(
                f"receiver {self.host}:{self.port} refused the connection: "
                f"{msg.get('reason', 'backoff requested')}"
            )
        elif mtype in ("sdr_error", "demodulator_error"):
            log.warning("receiver reported error", type=mtype, message=value)
        elif mtype == "log_message":
            log.info("receiver log", message=value)
        elif mtype in (
            "cpuusage",
            "clients",
            "dial_frequencies",
            "bookmarks",
            "bands",
            "metadata",
            "secondary_config",
            "secondary_demod",
            "chat_message",
            "temperature",
            "battery",
        ):
            log.debug("receiver message ignored", type=mtype)
        else:
            log.debug("unknown receiver message", type=mtype)

    def _handle_binary(self, data: bytes) -> RemoteFftFrame | RemoteAudioFrame | None:
        """Demux + decode one binary frame (tag byte + payload)."""
        if not data:
            return None
        tag = data[0]
        payload = bytes(data[1:])

        if tag == _TYPE_FFT:
            if self._fft_compression == "none":
                bins = np.frombuffer(payload, dtype="<f4").astype(np.float32)
            else:
                fft_size = self.remote_config.get("fft_size")
                bins = decode_fft_adpcm(
                    payload, int(fft_size) if isinstance(fft_size, int) else None
                )
            center = self.remote_config.get("center_freq")
            samp_rate = self.remote_config.get("samp_rate")
            levels = self.remote_config.get("waterfall_levels")
            min_db = float(levels[0]) if isinstance(levels, (list, tuple)) and len(levels) >= 1 else None
            max_db = float(levels[1]) if isinstance(levels, (list, tuple)) and len(levels) >= 2 else None
            return RemoteFftFrame(
                bins=bins,
                center_freq=int(center) if isinstance(center, (int, float)) else 0,
                sample_rate=int(samp_rate) if isinstance(samp_rate, (int, float)) else 0,
                min_db=min_db,
                max_db=max_db,
            )

        if tag == _TYPE_AUDIO:
            if self._audio_compression == "none":
                pcm = np.frombuffer(payload, dtype="<i2").astype(np.int16)
            else:
                pcm = self._audio_codec.decode_with_sync(payload)
            if len(pcm) == 0:
                return None
            return RemoteAudioFrame(pcm=pcm, sample_rate=self.output_rate)

        if tag == _TYPE_SECONDARY_FFT:
            log.debug("secondary FFT frame skipped (digimodes not wired yet)")
            return None
        if tag == _TYPE_HD_AUDIO:
            # Slice-14: HD audio (WFM music quality) — same ADPCM codec as the
            # standard audio stream, but at hd_output_rate (default 48 kHz).
            # The wire frame includes the standard ADPCM sync prefix; we decode
            # it via the same _audio_codec instance (the codec is stateless
            # w.r.t. the sample rate — the rate lives in the framing header,
            # which _pack_audio_frame echoes so the client AudioPlayer
            # resamples appropriately).
            if self._audio_compression == "none":
                pcm = np.frombuffer(payload, dtype="<i2").astype(np.int16)
            else:
                pcm = self._audio_codec.decode_with_sync(payload)
            if len(pcm) == 0:
                return None
            return RemoteAudioFrame(pcm=pcm, sample_rate=self.hd_output_rate)

        log.debug("unknown binary frame tag", tag=tag)
        return None


def _log_send_failure(task: asyncio.Future[None]) -> None:
    """Done-callback for fire-and-forget dspcontrol sends."""
    exc = task.exception()
    if exc is not None:
        log.warning("dspcontrol send failed", error=str(exc))
