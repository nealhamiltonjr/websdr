"""KiwiSDR remote source — WebSocket IQ client for the public HF network.

The KiwiSDR (kiwisdr.com) is a self-contained web SDR: a BeagleBone + 14-bit
ADC covering 0–30 MHz that up to 8 users listen to simultaneously over the
network. 1000+ receivers world-wide are public — see the directory at
rx.kiwisdr.com (proxied by ``GET /api/directory/kiwi``), or just point this
source at any ``ws://host:8073`` endpoint. This is the "test live signals in
dev without owning hardware" answer for HF: real ionospheric propagation,
real QRN, real stations, full pycsdr chain locally.

How it maps onto the Source contract:

  * The Kiwi demodulates *channels*, but its ``mod=IQ`` mode streams raw
    int16 IQ for a passband around the tuned frequency — we take that, so
    OUR pycsdr chains (waterfall + demodulators) do all the DSP.
  * The IQ stream rate equals the Kiwi sound rate (default 12 kHz here);
    ``fixed_sample_rate`` advertises it so the ReceiverSession adopts the
    right rate before building its chains.
  * Tuning is remote: ``spawn(center_freq=...)`` retunes the Kiwi via the
    ``SET mod=IQ freq=...`` message.

Protocol shape (jks KiwiSDR websocket protocol, as used by kiwiclient and
OpenWebRX's historic KiwiSDR source):

  * Text messages, space-separated ``key=value`` pairs, both directions.
  * Client connects to ``ws://host:port/``, identifies with ``WHO am_I=...``
    and ``SET auth t=<unix-time>`` (newer firmware), then tunes with
    ``SET mod=IQ freq=<kHz> low_cut=<Hz> high_cut=<Hz>``.
  * Audio-rate negotiation: the server may propose ``SET AR in=... out=...``;
    the client confirms with ``SET AR OK in=... out=...``.
  * IQ arrives as binary messages: a 4-byte header (u16 LE sequence number,
    flags, rx channel) followed by interleaved int16 LE I/Q pairs.

BRING-UP NOTE (same policy as the SDRplay cdef in ADR-004): this dev box
has no network route to a live Kiwi, so the handshake literals above are
verified against the in-repo fake server (tests/test_kiwi_driver.py), which
codifies the expected behavior. On the FIRST connection to a real receiver,
verify: (a) handshake acceptance, (b) ``SET AR`` rate semantics, (c) the
binary header layout, and (d) whether ``iq_sample_rate`` must be one of a
fixed set. Adjust the ``_*`` constants below if needed — they are the only
protocol literals in the file.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import structlog
import websockets
from websockets.exceptions import ConnectionClosed

from ._hw_common import cs16_to_cf32
from .base import SourceInfo

log = structlog.get_logger(__name__)

# --- Protocol literals (single place to fix after first live bring-up) -----
_WHO_FMT = "WHO am_I={user}"
_AUTH_FMT = "SET auth t={ts}"
_SET_MOD_IQ_FMT = "SET mod=IQ low_cut={low} high_cut={high} freq={freq_khz}"
_SET_AR_REQUEST_FMT = "SET AR out={rate}"  # our rate request
_SET_AR_CONFIRM_FMT = "SET AR OK in={in_rate} out={out_rate}"  # reply to proposal
_AUDIO_HEADER_BYTES = 4  # u16 LE seq + flags + rx-channel


@dataclass
class KiwiSdrSource:
    """A remote KiwiSDR receiver as a Source (ADR-006).

    Args:
        host: KiwiSDR hostname or IP (no scheme).
        port: KiwiSDR websocket port (default 8073).
        use_tls: connect via ``wss://`` (Kiwis behind TLS proxies).
        user: client identification sent to the receiver — public Kiis are
            volunteer-run; identify honestly (ADR-006 etiquette).
        password: optional password for restricted receivers (sent as
            ``SET auth p=...`` — verify support on live bring-up).
        iq_sample_rate: the Kiwi sound rate to request; the IQ stream runs
            at this rate and ``fixed_sample_rate`` advertises it.
        iq_bandwidth: passband in Hz around the tuned frequency; 0 = full
            (``-rate/2 ... +rate/2``).
        chunk_size: complex samples per yielded chunk.
        connect_timeout: seconds to wait for the websocket to open.
    """

    host: str
    port: int = 8073
    use_tls: bool = False
    user: str = "openwebrx_plus"
    password: str | None = None
    iq_sample_rate: int = 12_000
    iq_bandwidth: int = 0
    chunk_size: int = 4096
    connect_timeout: float = 10.0
    info: SourceInfo = field(default_factory=lambda: SourceInfo(
        type="kiwi",
        label="KiwiSDR (remote)",
        sample_rate=12_000,
    ))

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("host is required (the KiwiSDR address, e.g. rx.example.com)")
        if not (1 <= self.port <= 65535):
            raise ValueError(f"port must be 1-65535, got {self.port}")
        if self.iq_sample_rate < 1000:
            raise ValueError("iq_sample_rate must be >= 1000")
        if self.iq_bandwidth < 0 or self.iq_bandwidth > self.iq_sample_rate:
            raise ValueError(
                f"iq_bandwidth must be 0 (full) or <= iq_sample_rate "
                f"({self.iq_sample_rate}), got {self.iq_bandwidth}"
            )
        # Advertised to ReceiverSession.start() so the DSP chains are built
        # for the rate the Kiwi will actually stream at.
        self.fixed_sample_rate: int = self.iq_sample_rate
        # Slice-6.5: runtime-gain connection storage. Set in spawn() after
        # the websocket opens; cleared in the finally block. The Kiwi
        # protocol uses 'SET AGC=<0|1>' and 'SET GAIN=<dB>' text messages.
        self._connection: Any = None

    @property
    def _uri(self) -> str:
        scheme = "wss" if self.use_tls else "ws"
        return f"{scheme}://{self.host}:{self.port}/"

    async def spawn(
        self,
        center_freq: int,
        sample_rate: int,
        gain: float | None = None,
    ) -> AsyncGenerator[np.ndarray, None]:
        """Stream complex64 IQ from the Kiwi's ``mod=IQ`` channel.

        ``sample_rate``/``gain`` come from the session but the Kiwi dictates
        both (rate via ``fixed_sample_rate``, gain via its server-side AGC):
        a mismatch logs a warning — the session normally adopts the fixed
        rate in ``start()`` before we get here.
        """
        if sample_rate != self.iq_sample_rate:
            log.warning(
                "kiwi sample_rate mismatch — session did not adopt fixed_sample_rate?",
                requested=sample_rate,
                kiwi_rate=self.iq_sample_rate,
            )

        bandwidth = self.iq_bandwidth or self.iq_sample_rate
        low_cut = -(bandwidth // 2)
        high_cut = bandwidth // 2
        freq_khz = center_freq / 1000.0

        object.__setattr__(
            self,
            "info",
            SourceInfo(
                type="kiwi",
                label=f"KiwiSDR {self.host}:{self.port}",
                endpoint=f"{self.host}:{self.port}",
                sample_rate=self.iq_sample_rate,
            ),
        )

        try:
            connection: Any = await asyncio.wait_for(
                websockets.connect(self._uri, open_timeout=self.connect_timeout),
                timeout=self.connect_timeout + 2.0,
            )
            # Slice-6.5: stash the connection so set_runtime_gain() can
            # send 'SET AGC=' / 'SET GAIN=' text messages on the live ws.
            self._connection = connection
        except TimeoutError:
            raise RuntimeError(
                f"KiwiSDR {self.host}:{self.port} did not open a websocket "
                f"within {self.connect_timeout:.0f}s"
            ) from None
        except OSError as exc:
            raise RuntimeError(
                f"cannot reach KiwiSDR {self.host}:{self.port}: {exc}"
            ) from exc

        try:
            await connection.send(_WHO_FMT.format(user=self.user))
            auth = _AUTH_FMT.format(ts=int(time.time()))
            if self.password:
                auth += f" p={self.password}"
            await connection.send(auth)
            await connection.send(
                _SET_MOD_IQ_FMT.format(low=low_cut, high=high_cut, freq_khz=f"{freq_khz:.6f}")
            )
            await connection.send(_SET_AR_REQUEST_FMT.format(rate=self.iq_sample_rate))
            log.info(
                "kiwisdr streaming",
                endpoint=f"{self.host}:{self.port}",
                center_freq=center_freq,
                iq_rate=self.iq_sample_rate,
                bandwidth=bandwidth,
            )

            buf = bytearray()
            need = self.chunk_size * 4  # int16 stereo pair = 4 bytes/sample
            async for message in connection:
                if isinstance(message, str):
                    await self._handle_text(connection, message)
                    continue
                if len(message) <= _AUDIO_HEADER_BYTES:
                    continue  # header-only keepalive
                buf += message[_AUDIO_HEADER_BYTES:]
                while len(buf) >= need:
                    chunk = cs16_to_cf32(
                        np.frombuffer(bytes(buf[:need]), dtype=np.int16)
                    )
                    del buf[:need]
                    yield chunk
        except ConnectionClosed:
            log.info(
                "kiwisdr closed the connection", endpoint=f"{self.host}:{self.port}"
            )
            return
        finally:
            await connection.close()
            # Slice-6.5: clear the runtime-gain connection handle.
            self._connection = None

    def set_runtime_gain(self, gain_db: float | None) -> bool:
        """Apply a gain change while streaming (slice-6.5 RuntimeGainSource).

        - ``gain_db`` numeric: send 'SET AGC=0' then 'SET GAIN=<dB>' on
          the live websocket (the Kiwi protocol's RF-gain commands).
        - ``None``: send 'SET AGC=1' to enable the Kiwi's server-side AGC.

        Returns True when the connection is live and the request was
        dispatched (the ws send is fire-and-forget — the Kiwi echoes a
        status line back via _handle_text on success or stays silent
        on failure; we don't wait for the echo).

        This schedules the ws send onto the running event loop via
        ``asyncio.run_coroutine_threadsafe`` — safe to call from the WS
        listener task while spawn()'s ``async for message in connection``
        is being consumed.
        """
        connection = self._connection
        if connection is None:
            return False
        try:
            loop = asyncio.get_event_loop()
            if gain_db is None:
                loop.create_task(connection.send("SET AGC=1"))
            else:
                gain_int = int(round(gain_db))
                async def _send_both() -> None:
                    await connection.send("SET AGC=0")
                    await connection.send(f"SET GAIN={gain_int}")
                loop.create_task(_send_both())
            return True
        except Exception:  # noqa: BLE001
            log.debug("kiwi runtime gain failed", exc_info=True)
            return False

    async def _handle_text(self, connection: Any, message: str) -> None:
        """React to server text messages (rate proposals in particular)."""
        parts = message.split()
        if not parts:
            return
        head = parts[0].lower()
        if head == "set" and len(parts) >= 3 and parts[1].lower() == "ar":
            # "SET AR in=... out=..." — the server PROPOSES a sound rate;
            # "SET AR OK ..." — the server CONFIRMS ours (don't echo back,
            # that would ping-pong forever).
            if parts[2].lower() == "ok":
                return
            kv = {}
            for part in parts[2:]:
                key, sep, value = part.partition("=")
                if sep:
                    kv[key] = value
            out_rate = kv.get("out")
            if out_rate is not None and out_rate.isdigit() and int(out_rate) != self.iq_sample_rate:
                log.warning(
                    "kiwisdr negotiated a different sound rate than requested",
                    requested=self.iq_sample_rate,
                    negotiated=out_rate,
                    hint="set iq_sample_rate to match before spawning the session",
                )
            reply = _SET_AR_CONFIRM_FMT.format(
                in_rate=kv.get("in", 0), out_rate=kv.get("out", 0)
            )
            await connection.send(reply)
        # Everything else (WHO/ADM/LOAD/status chatter) is informational.

    async def close(self) -> None:
        return None
