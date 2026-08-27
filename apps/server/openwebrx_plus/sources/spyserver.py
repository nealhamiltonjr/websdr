"""SpyServer remote source — Airspy's network SDR protocol client (ADR-006).

SpyServer (`spyserver.com` <https://airspy.com/spy-server/>) is Airspy's
free remote-SDR server: one machine runs ``spyserver`` attached to an
Airspy HF+ / Discovery / R2 or an RTL-SDR, and any number of clients
listen over TCP. It is THE dominant protocol for public HF receivers
outside the KiwiSDR world — completing ADR-006's Tier-A raw-IQ remotes
(rtl_tcp ✅, SpyServer ✅, SoapyRemote via ``soapy`` ✅).

How it maps onto the Source contract:

  * We request the IQ stream in float32 format at a decimation the user
    picks via ``sample_rate`` — all OUR pycsdr chains (waterfall +
    demodulators) then run locally, exactly like a local SDR.
  * SpyServer rates are always ``device_max_rate / 2**decimation``. The
    client picks the decimation that hits the requested rate EXACTLY and
    refuses (with an actionable message listing the achievable rates)
    when it can't — a silent rate mismatch would skew every frequency
    axis. Default 768 kHz matches an HF+ / Discovery server at full
    rate; use 2 400 000 for RTL-SDR servers, 10 000 000 / 2**k for
    Airspy R2 servers.
  * ``fixed_sample_rate`` advertises the requested rate so the
    ReceiverSession builds its chains for it before spawn.
  * Runtime gain (slice-4.7 ``RuntimeGainSource``): queued and applied
    between chunks via ``COMMAND_SET_IQ_GAIN`` — latest-wins, same
    pattern as rtl_tcp.

Protocol shape (SpyServer protocol version 2, as documented by the
protocol headers shipped with SDRSharp and reimplemented by the open
source clients):

  * Every message = 20-byte header + body:
    ``<IIQI`` → message_type u32, stream_type u32, user_data u64,
    body_size u32.
  * Client commands 0x01..: HELLO (protocol version + software version +
    client name), GET_INFO, SET_STREAMING_MODE (bitmask), SET_IQ_FORMAT,
    SET_IQ_FREQUENCY (i64 Hz), SET_IQ_DECIMATION (u32, 0 = full rate),
    SET_IQ_GAIN (i32 gain dB + i32 gain type).
  * Server messages 0x100..: SERVER_HELLO, SERVER_INFO (device info
    struct), SERVER_PING / SYNC / STAT / PROGRESS / REPLY / BYE / AUX.
  * Control messages carry ``stream_type=0``; stream bodies (STATUS /
    IQ / AF / FFT) are distinguished by their non-zero ``stream_type`` —
    the client dispatches on ``stream_type`` first precisely so it stays
    correct regardless of which ``message_type`` value a given server
    version stamps on data frames.
  * SERVER_INFO body: device type u32, serial u64, maximum sample rate
    u32, maximum bandwidth u32, decimation stage count u32, gain stage
    count u32, minimum/maximum/if frequency i32 × 3, then that many
    u16-length-prefixed gain-stage names.

BRING-UP NOTE (same policy as the KiwiSDR/SDRplay literals, ADR-006):
this dev box has no network route to a live SpyServer, so the literals
below are verified against the in-repo fake server
(tests/test_spyserver_driver.py) which codifies the expected behavior.
On the FIRST connection to a real server, verify: (a) HELLO acceptance
(protocol version + software version), (b) the SERVER_INFO body layout,
(c) which message_type value arrives on stream frames, (d) SET_IQ_GAIN
``gain_type`` semantics (auto vs manual numbering differs between
devices), and (e) whether float32 IQ is honored for the device class.
Adjust the ``_*`` constants — they are the only protocol literals.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

import numpy as np
import structlog

from .base import SourceInfo

log = structlog.get_logger(__name__)

# --- Protocol literals (single place to fix after first live bring-up) ------
_PROTOCOL_VERSION = 2
_SOFTWARE_VERSION = 1_600  # client software version, like SDRSharp 1600+
_DEFAULT_CLIENT_NAME = "openwebrx_plus (federation client)"
_HEADER_FMT = "<IIQI"  # message_type, stream_type, user_data, body_size
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)

# Client → server commands
_CMD_HELLO = 0x00000001
_CMD_GET_INFO = 0x00000002
_CMD_SET_STREAMING_MODE = 0x00000003
_CMD_SET_IQ_FORMAT = 0x00000004
_CMD_SET_IQ_FREQUENCY = 0x00000005
_CMD_SET_IQ_DECIMATION = 0x00000006
_CMD_SET_IQ_GAIN = 0x00000007

# Server → client messages
_MSG_SERVER_HELLO = 0x00000100
_MSG_SERVER_INFO = 0x00000101
_MSG_SERVER_PING = 0x00000102
_MSG_SERVER_SYNC = 0x00000103
_MSG_SERVER_STAT = 0x00000104
_MSG_SERVER_PROGRESS = 0x00000105
_MSG_SERVER_REPLY = 0x00000106
_MSG_SERVER_BYE = 0x00000107

# Stream types (non-zero on data frames; control frames carry 0)
_STREAM_STATUS = 1
_STREAM_IQ = 2
_STREAM_AF = 3
_STREAM_FFT = 4

# Streaming-mode bits
_STREAM_MODE_COMMANDS = 1 << 0
_STREAM_MODE_IQ = 1 << 2

# IQ formats — we ask for interleaved float32
_IQ_FORMAT_FLOAT32 = 1

# Gain types (VERIFY semantics on first live connection — the numbering
# differs between device families; 0/1 is auto/manual on most)
_GAIN_TYPE_AUTO = 0
_GAIN_TYPE_MANUAL = 1

# Device types reported by SERVER_INFO
_DEVICE_AIRSPY_ONE = 1
_DEVICE_AIRSPY_HF = 2
_DEVICE_RTLSDR = 3
_DEVICE_NAMES = {
    _DEVICE_AIRSPY_ONE: "Airspy R2/Mini",
    _DEVICE_AIRSPY_HF: "Airspy HF+ / Discovery",
    _DEVICE_RTLSDR: "RTL-SDR",
}


@dataclass(frozen=True)
class SpyServerDeviceInfo:
    """Parsed SERVER_INFO — what the remote server is actually running."""

    device_type: int
    device_serial: int
    maximum_sample_rate: int
    maximum_bandwidth: int
    decimation_stage_count: int
    gain_stage_count: int
    minimum_frequency: int
    maximum_frequency: int
    if_frequency: int
    gain_stages: tuple[str, ...]

    @property
    def device_name(self) -> str:
        return _DEVICE_NAMES.get(self.device_type, f"device #{self.device_type}")


_INFO_FIXED_FMT = "<IQIIIIiii"  # everything before the gain-stage names
_INFO_FIXED_SIZE = struct.calcsize(_INFO_FIXED_FMT)


def parse_server_info(body: bytes) -> SpyServerDeviceInfo:
    """Decode a SERVER_INFO body (layout per the protocol header).

    Raises ``ValueError`` on truncation or malformed gain-stage names —
    the bring-up tripwire: if a real server's layout differs, this is
    where it fails, loudly.
    """
    if len(body) < _INFO_FIXED_SIZE:
        raise ValueError(
            f"SERVER_INFO body too short: {len(body)} bytes "
            f"(expected >= {_INFO_FIXED_SIZE})"
        )
    (
        device_type,
        device_serial,
        maximum_sample_rate,
        maximum_bandwidth,
        decimation_stage_count,
        gain_stage_count,
        minimum_frequency,
        maximum_frequency,
        if_frequency,
    ) = struct.unpack(_INFO_FIXED_FMT, body[:_INFO_FIXED_SIZE])
    names: list[str] = []
    off = _INFO_FIXED_SIZE
    for _ in range(gain_stage_count):
        if off + 2 > len(body):
            raise ValueError("SERVER_INFO gain-stage names truncated")
        (n,) = struct.unpack_from("<H", body, off)
        off += 2
        if off + n > len(body):
            raise ValueError("SERVER_INFO gain-stage name truncated")
        names.append(body[off:off + n].decode("utf-8", errors="replace"))
        off += n
    return SpyServerDeviceInfo(
        device_type=device_type,
        device_serial=device_serial,
        maximum_sample_rate=maximum_sample_rate,
        maximum_bandwidth=maximum_bandwidth,
        decimation_stage_count=decimation_stage_count,
        gain_stage_count=gain_stage_count,
        minimum_frequency=minimum_frequency,
        maximum_frequency=maximum_frequency,
        if_frequency=if_frequency,
        gain_stages=tuple(names),
    )


def pick_decimation(
    target_rate: int, max_rate: int, stage_count: int
) -> tuple[int, int]:
    """Choose the decimation whose rate hits ``target_rate`` exactly.

    SpyServer rates are ``max_rate >> k`` for k in 0..stage_count. Returns
    ``(k, actual_rate)``; when no k matches, returns the CLOSEST k and its
    rate — the caller decides whether close is good enough (we don't).
    """
    best_k, best_rate, best_err = 0, max_rate, abs(max_rate - target_rate)
    for k in range(0, stage_count + 1):
        rate = max_rate >> k
        err = abs(rate - target_rate)
        if err < best_err:
            best_k, best_rate, best_err = k, rate, err
    return best_k, best_rate


@dataclass
class SpyServerSource:
    """A remote SpyServer receiver as a Source (ADR-006 Tier A).

    Args:
        host: SpyServer hostname or IP (no scheme).
        port: SpyServer TCP port (default 5555).
        sample_rate: requested IQ rate in Hz. Must be exactly
            ``device_max_rate / 2**k`` for some decimation k the server
            supports — 768 000 (default) matches an HF+ / Discovery
            server at full rate; 2 400 000 for RTL-SDR servers. A rate
            that can't be produced fails the stream with the list of
            achievable rates in the message.
        gain: initial gain in dB sent at connect time (None = auto).
        user: client identification sent in the HELLO — public servers
            are volunteer-run; identify honestly (ADR-006 etiquette).
        chunk_size: complex samples per yielded chunk.
        connect_timeout: seconds to wait for TCP connect + handshake.
    """

    host: str
    port: int = 5555
    sample_rate: int = 768_000
    gain: float | None = None
    user: str = _DEFAULT_CLIENT_NAME
    chunk_size: int = 65536
    connect_timeout: float = 10.0
    info: SourceInfo = field(default_factory=lambda: SourceInfo(
        type="spyserver",
        label="SpyServer (remote)",
        sample_rate=768_000,
    ))
    # Populated by spawn() after SERVER_INFO arrives (tests read it).
    device_info: SpyServerDeviceInfo | None = field(
        default=None, init=False, repr=False
    )
    # Runtime gain channel (slice-4.7) — consumed between chunks by the
    # stream loop; latest-wins. Created via default_factory so spawn()
    # can drain it from the very first iteration.
    _gain_q: asyncio.Queue[float | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=1), init=False, repr=False
    )

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("host is required (the SpyServer address)")
        if not (1 <= self.port <= 65535):
            raise ValueError(f"port must be 1-65535, got {self.port}")
        if self.sample_rate < 1000:
            raise ValueError(f"sample_rate must be >= 1000, got {self.sample_rate}")
        if self.chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        if self.connect_timeout <= 0:
            raise ValueError("connect_timeout must be > 0")
        if self.gain is not None and self.gain < 0:
            raise ValueError("gain must be >= 0 dB or None (auto)")
        # Advertised to ReceiverSession.start() so the DSP chains are
        # built for the rate the server will actually stream at (the
        # rate contract is enforced in spawn() — see pick_decimation).
        self.fixed_sample_rate: int = self.sample_rate

    # -- wire helpers --------------------------------------------------------

    def _command(
        self, writer: asyncio.StreamWriter, msg_type: int, body: bytes = b""
    ) -> None:
        """Queue one client command (caller drains)."""
        writer.write(
            struct.pack(_HEADER_FMT, msg_type, 0, 0, len(body)) + body
        )

    async def _read_message(
        self, reader: asyncio.StreamReader
    ) -> tuple[int, int, int, bytes]:
        """Read one framed message → (msg_type, stream_type, user_data, body)."""
        header = await reader.readexactly(_HEADER_SIZE)
        msg_type, stream_type, user_data, body_size = struct.unpack(
            _HEADER_FMT, header
        )
        body = await reader.readexactly(body_size) if body_size else b""
        return msg_type, stream_type, user_data, body

    async def _await_server_message(
        self,
        reader: asyncio.StreamReader,
        wanted_type: int,
        wait_secs: float,
        what: str,
    ) -> bytes:
        """Read messages until one of ``wanted_type`` arrives.

        Tolerates (and logs at debug) the pings/syncs/stats a real
        server interleaves. SERVER_BYE aborts with its reason; EOF or
        timeout raise RuntimeError.
        """
        while True:
            try:
                msg_type, _stream, _user, body = await asyncio.wait_for(
                    self._read_message(reader), timeout=wait_secs
                )
            except asyncio.IncompleteReadError:
                raise RuntimeError(
                    f"SpyServer {self.host}:{self.port} closed the connection "
                    f"while waiting for {what}"
                ) from None
            except TimeoutError:
                raise RuntimeError(
                    f"SpyServer {self.host}:{self.port} did not send {what} "
                    f"within {wait_secs:.0f}s"
                ) from None
            if msg_type == wanted_type:
                return body
            if msg_type == _MSG_SERVER_BYE:
                reason = body.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"SpyServer {self.host}:{self.port} refused the session"
                    + (f": {reason}" if reason else "")
                )
            if msg_type == _MSG_SERVER_PING:
                # Keepalives arrive at any point — informational here.
                log.debug("spyserver ping", endpoint=f"{self.host}:{self.port}")
            # SYNC/STAT/PROGRESS/REPLY/HELLO-again chatter: tolerated.

    # -- Source protocol ------------------------------------------------------

    async def spawn(
        self,
        center_freq: int,
        sample_rate: int,
        gain: float | None = None,
    ) -> AsyncGenerator[np.ndarray, None]:
        """Stream complex64 IQ from the SpyServer's float32 IQ stream."""
        if sample_rate != self.sample_rate:
            log.warning(
                "spyserver sample_rate mismatch — session did not adopt "
                "fixed_sample_rate?",
                requested=sample_rate,
                source_rate=self.sample_rate,
            )

        object.__setattr__(
            self,
            "info",
            SourceInfo(
                type="spyserver",
                label=f"SpyServer {self.host}:{self.port}",
                endpoint=f"{self.host}:{self.port}",
                sample_rate=self.sample_rate,
            ),
        )

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.connect_timeout,
            )
        except (TimeoutError, OSError) as exc:
            raise RuntimeError(
                f"cannot reach SpyServer {self.host}:{self.port}: {exc}"
            ) from exc

        try:
            # 1. Handshake: HELLO → SERVER_HELLO (BYE = refused).
            hello_body = (
                struct.pack("<II", _PROTOCOL_VERSION, _SOFTWARE_VERSION)
                + self.user.encode("utf-8")
            )
            self._command(writer, _CMD_HELLO, hello_body)
            await writer.drain()
            hello = await self._await_server_message(
                reader, _MSG_SERVER_HELLO, self.connect_timeout, "SERVER_HELLO"
            )
            protocol = struct.unpack("<I", hello[:4])[0] if len(hello) >= 4 else 0
            if protocol != _PROTOCOL_VERSION:
                log.warning(
                    "spyserver speaks a different protocol version",
                    ours=_PROTOCOL_VERSION,
                    theirs=protocol,
                )

            # 2. Device info (drives the decimation contract).
            self._command(writer, _CMD_GET_INFO)
            await writer.drain()
            dev = parse_server_info(
                await self._await_server_message(
                    reader, _MSG_SERVER_INFO, self.connect_timeout, "SERVER_INFO"
                )
            )
            self.device_info = dev

            # 3. Rate contract: exact match or actionable failure.
            decimation, actual_rate = pick_decimation(
                self.sample_rate, dev.maximum_sample_rate,
                dev.decimation_stage_count,
            )
            if actual_rate != self.sample_rate:
                achievable = [
                    dev.maximum_sample_rate >> k
                    for k in range(0, dev.decimation_stage_count + 1)
                ]
                raise RuntimeError(
                    f"SpyServer {self.host}:{self.port} ({dev.device_name}) "
                    f"cannot produce {self.sample_rate} Hz — achievable rates: "
                    f"{achievable}. Set source_kwargs.sample_rate to one of "
                    f"them (768000 matches an HF+ server at full rate)."
                )

            # 4. Configure the IQ stream: commands + IQ, float32, decimation,
            #    center frequency, optional initial gain.
            self._command(
                writer, _CMD_SET_STREAMING_MODE,
                struct.pack("<I", _STREAM_MODE_COMMANDS | _STREAM_MODE_IQ),
            )
            self._command(
                writer, _CMD_SET_IQ_FORMAT,
                struct.pack("<I", _IQ_FORMAT_FLOAT32),
            )
            self._command(
                writer, _CMD_SET_IQ_DECIMATION,
                struct.pack("<I", decimation),
            )
            self._command(
                writer, _CMD_SET_IQ_FREQUENCY,
                struct.pack("<q", int(center_freq)),
            )
            initial_gain = gain if gain is not None else self.gain
            if initial_gain is not None:
                self._command(
                    writer, _CMD_SET_IQ_GAIN,
                    struct.pack("<ii", int(round(initial_gain)), _GAIN_TYPE_MANUAL),
                )
            await writer.drain()
            log.info(
                "spyserver streaming",
                endpoint=f"{self.host}:{self.port}",
                device=dev.device_name,
                serial=dev.device_serial,
                center_freq=center_freq,
                sample_rate=actual_rate,
                decimation=decimation,
            )

            # 5. Read loop: dispatch on stream_type (data) vs msg_type
            #    (control). IQ bodies are interleaved float32 — a direct
            #    complex64 view (LE, real-first) — chunked to chunk_size.
            buf = bytearray()
            need = self.chunk_size * 8  # float32 stereo pair = 8 bytes/sample
            while True:
                # Runtime gain requests (slice-4.7): latest-wins, applied
                # between chunks so the hot path never blocks.
                while True:
                    try:
                        req = self._gain_q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if req is None:
                        self._command(
                            writer, _CMD_SET_IQ_GAIN,
                            struct.pack("<ii", 0, _GAIN_TYPE_AUTO),
                        )
                    else:
                        self._command(
                            writer, _CMD_SET_IQ_GAIN,
                            struct.pack(
                                "<ii", int(round(req)), _GAIN_TYPE_MANUAL
                            ),
                        )
                    await writer.drain()
                    log.debug(
                        "spyserver runtime gain applied",
                        endpoint=f"{self.host}:{self.port}",
                        gain=req,
                    )
                try:
                    msg_type, stream_type, _user, body = await self._read_message(
                        reader
                    )
                except asyncio.IncompleteReadError:
                    log.info(
                        "spyserver closed the connection",
                        endpoint=f"{self.host}:{self.port}",
                    )
                    return
                if stream_type == _STREAM_IQ:
                    buf += body
                    while len(buf) >= need:
                        # bytes(...) copy → the ndarray owns its memory.
                        chunk = np.frombuffer(
                            bytes(buf[:need]), dtype=np.complex64
                        )
                        del buf[:need]
                        yield chunk
                elif stream_type != 0:
                    # STATUS / AF / FFT frames from other clients' modes —
                    # not ours, skip.
                    continue
                elif msg_type == _MSG_SERVER_BYE:
                    reason = body.decode("utf-8", errors="replace").strip()
                    log.info(
                        "spyserver said goodbye",
                        endpoint=f"{self.host}:{self.port}",
                        reason=reason,
                    )
                    return
                # Remaining control chatter (PING/SYNC/STAT/...): tolerated.
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    def set_runtime_gain(self, gain_db: float | None) -> bool:
        """Queue a runtime gain change (latest-wins; applied between chunks).

        Returns True (always queueable — even before the stream starts,
        the value applies right after connect via the initial-gain path).
        """
        with contextlib.suppress(asyncio.QueueEmpty):
            self._gain_q.get_nowait()
        self._gain_q.put_nowait(None if gain_db is None else float(gain_db))
        return True

    async def close(self) -> None:
        return None
