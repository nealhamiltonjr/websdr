"""rtl_tcp remote source — real RTL-SDR (or rsp_tcp) IQ over the network.

rtl_tcp is the classic osmocom network SDR protocol: a 12-byte handshake
("RTL0" + tuner type + gain count), then 5-byte big-endian command packets
(1 byte command + u32 value), then a continuous interleaved uint8 IQ stream.
Because the protocol is tiny and stateless, it is the *universal* remote-SDR
lingua franca:

  * ``rtl_tcp`` itself — run it next to any RTL-SDR (``rtl_tcp -a 0.0.0.0``)
    and the stick becomes reachable from anywhere. On the user's LAN this is
    the cheapest way to get the RTL-SDR V4 off the desk and onto the roof.
  * ``rsp_tcp`` — SDRplay's RSP servers speak the same protocol shape
    (verify the magic/header on first connect to a given rsp_tcp build).
  * A long tail of public rtl_tcp servers run by volunteers (lists come and
    go; treat them as a bonus, not a dependency — see ADR-006 etiquette).

This module owns the wire protocol for the whole package: RtlSdrSource's
``tcp`` transport delegates to :func:`rtl_tcp_stream` below, and the
first-class :class:`RtlTcpSource` exists so a *remote* server can be picked
explicitly in the UI without pretending a local RTL-SDR is attached
(``hardware_required=False`` in the manifest).

Command IDs from osmocom rtl_tcp.c (kept in sync with rtl_sdr.py):
  0x01 frequency   0x02 sample rate   0x03 gain mode    0x04 gain (0.1 dB)
  0x05 ppm         0x08 rtl agc       0x09 direct samp  0x0e bias tee
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import structlog

from ._hw_common import cu8_to_cf32
from .base import SourceInfo

log = structlog.get_logger(__name__)

# rtl_tcp protocol: 1 byte command + 4-byte big-endian parameter.
TCP_SET_FREQUENCY = 0x01
TCP_SET_SAMPLE_RATE = 0x02
TCP_SET_GAIN_MODE = 0x03  # 0 = manual, 1 = auto (tuner gain mode)
TCP_SET_GAIN = 0x04  # tuner gain in tenths of dB
TCP_SET_FREQ_CORRECTION = 0x05  # ppm (IF frequency correction)
TCP_SET_AGC_MODE = 0x08  # RTL2832 digital AGC on/off
TCP_SET_DIRECT_SAMPLING = 0x09  # 0 = off, 1 = I, 2 = Q
TCP_SET_BIAS_TEE = 0x0E  # newer rtl_tcp builds only

TCP_MAGIC = b"RTL0"
TCP_HEADER_SIZE = 12


async def rtl_tcp_stream(
    host: str,
    port: int,
    *,
    center_freq: int,
    sample_rate: int,
    gain: float | None,
    rtl_agc: bool = True,
    direct_sampling: int = 0,
    bias_tee: bool = False,
    ppm: int = 0,
    chunk_size: int = 65536,
    connect_timeout: float = 8.0,
    gain_q: asyncio.Queue[float | None] | None = None,
) -> AsyncIterator[np.ndarray]:
    """Stream complex64 IQ from any rtl_tcp-compatible server.

    Shared implementation behind both :class:`RtlTcpSource` (remote, explicit)
    and RtlSdrSource's ``tcp`` transport (local auto-probe). Yields chunks of
    ``chunk_size`` complex samples; ends cleanly when the server closes the
    connection; raises :class:`RuntimeError` on a non-rtl_tcp endpoint.

    ``gain_q`` (slice-4.7) is an optional runtime-gain channel: the stream
    loop drains it between chunks and sends 0x03/0x04 commands, so a gain
    change lands within one chunk (~30 ms at 2.4 Msps). Latest-wins: the
    producer keeps at most one queued request. If the server stalls, queued
    requests wait until data flows again — acceptable for a knob.
    """
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=connect_timeout
    )
    try:
        header = await reader.readexactly(TCP_HEADER_SIZE)
        if header[:4] != TCP_MAGIC:
            raise RuntimeError(
                f"{host}:{port} is not an rtl_tcp server (magic={header[:4]!r})"
            )
        tuner_type, _gain_count = struct.unpack("<II", header[4:12])

        def cmd(code: int, value: int) -> None:
            writer.write(struct.pack(">BI", code, value))

        cmd(TCP_SET_FREQUENCY, center_freq)
        cmd(TCP_SET_SAMPLE_RATE, sample_rate)
        if ppm:
            cmd(TCP_SET_FREQ_CORRECTION, ppm)
        if gain is None:
            cmd(TCP_SET_GAIN_MODE, 1)
        else:
            cmd(TCP_SET_GAIN_MODE, 0)
            cmd(TCP_SET_GAIN, int(round(gain * 10)))
        cmd(TCP_SET_AGC_MODE, 1 if rtl_agc else 0)
        if direct_sampling:
            cmd(TCP_SET_DIRECT_SAMPLING, direct_sampling)
        if bias_tee:
            cmd(TCP_SET_BIAS_TEE, 1)
        await writer.drain()
        log.info(
            "rtl_tcp streaming",
            endpoint=f"{host}:{port}",
            tuner_type=tuner_type,
            center_freq=center_freq,
            sample_rate=sample_rate,
            ppm=ppm,
        )
        need = chunk_size * 2  # cu8: 2 bytes per complex sample
        try:
            while True:
                # Runtime gain requests (slice-4.7): drain-and-apply between
                # chunks; latest wins. Non-blocking — the IQ read below is the
                # only place this loop suspends.
                while gain_q is not None:
                    try:
                        req = gain_q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if req is None:
                        cmd(TCP_SET_GAIN_MODE, 1)  # tuner AGC
                    else:
                        cmd(TCP_SET_GAIN_MODE, 0)
                        cmd(TCP_SET_GAIN, int(round(req * 10)))
                    await writer.drain()
                    log.debug("rtl_tcp runtime gain applied", endpoint=f"{host}:{port}", gain=req)
                raw = await reader.readexactly(need)
                yield cu8_to_cf32(np.frombuffer(raw, dtype=np.uint8))
        except asyncio.IncompleteReadError:
            log.info("rtl_tcp server closed the connection", endpoint=f"{host}:{port}")
            return
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()


@dataclass
class RtlTcpSource:
    """Remote rtl_tcp server as a first-class Source (ADR-006).

    Use this when the RTL-SDR (or rsp_tcp) lives on another machine — or on
    the public internet. Unlike ``RtlSdrSource`` there is no transport
    probing: the endpoint is explicit and the failure modes are network ones.

    Args:
        host/port: rtl_tcp server endpoint (``rtl_tcp -a 0.0.0.0`` on the
            machine with the stick; default port 1234).
        ppm: frequency correction in ppm for the remote tuner.
        direct_sampling: 0 off, 1 I-branch, 2 Q-branch — the V4 HF path on
            the *server's* stick.
        bias_tee: power the remote bias tee (only if the server allows it —
            think twice before sending this to hardware you don't own).
        rtl_agc: RTL2832 digital AGC (default on).
        chunk_size: complex samples per yielded chunk.
        connect_timeout: seconds to wait for TCP connect + handshake.
    """

    host: str
    port: int = 1234
    ppm: int = 0
    direct_sampling: int = 0
    bias_tee: bool = False
    rtl_agc: bool = True
    chunk_size: int = 65536
    connect_timeout: float = 8.0
    info: SourceInfo = field(default_factory=lambda: SourceInfo(
        type="rtl_tcp",
        label="rtl_tcp (remote)",
        sample_rate=2_048_000,
    ))
    # Runtime gain channel (slice-4.7) — created at instantiation via
    # default_factory (spawn() must be able to pass it into rtl_tcp_stream
    # immediately).
    _gain_q: asyncio.Queue[float | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=1), init=False, repr=False
    )

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("host is required (the rtl_tcp server address)")
        if not (1 <= self.port <= 65535):
            raise ValueError(f"port must be 1-65535, got {self.port}")
        if self.direct_sampling not in (0, 1, 2):
            raise ValueError("direct_sampling must be 0, 1, or 2")
        if self.chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")

    def set_runtime_gain(self, gain_db: float | None) -> bool:
        """Queue a runtime tuner-gain change (latest-wins).

        The request is applied by the stream loop between chunks; if the
        source isn't streaming yet the queued value applies right after the
        next connect. Returns True (always queueable).
        """
        # Latest-wins: drop any stale request, then enqueue ours.
        with contextlib.suppress(asyncio.QueueEmpty):
            self._gain_q.get_nowait()
        self._gain_q.put_nowait(None if gain_db is None else float(gain_db))
        return True

    async def spawn(
        self,
        center_freq: int,
        sample_rate: int,
        gain: float | None = None,
    ) -> AsyncGenerator[np.ndarray, None]:
        object.__setattr__(
            self,
            "info",
            SourceInfo(
                type="rtl_tcp",
                label=f"rtl_tcp {self.host}:{self.port}",
                endpoint=f"{self.host}:{self.port}",
                sample_rate=sample_rate,
            ),
        )
        stream: Any = rtl_tcp_stream(
            self.host,
            self.port,
            center_freq=center_freq,
            sample_rate=sample_rate,
            gain=gain,
            rtl_agc=self.rtl_agc,
            direct_sampling=self.direct_sampling,
            bias_tee=self.bias_tee,
            ppm=self.ppm,
            chunk_size=self.chunk_size,
            connect_timeout=self.connect_timeout,
            gain_q=self._gain_q,
        )
        async for chunk in stream:
            yield chunk

    async def close(self) -> None:
        return None
