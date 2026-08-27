"""SpyServer remote-source tests — against a fake SpyServer TCP server.

The fake server (real ``asyncio.start_server`` on an ephemeral port)
codifies the protocol behavior our client expects — the same
both-ends-tested strategy as the fake Kiwi websocket server and the fake
rtl_tcp server (ADR-006 test rule: CI never talks to live receivers).
On connect it runs the HELLO → SERVER_INFO handshake, interleaves the
PING/SYNC chatter a real server emits, and once the client configures
the stream it pumps float32 IQ frames behind the 20-byte message header.

These tests double as the executable spec for the BRING-UP items in
sources/spyserver.py: if a real SpyServer behaves like FakeSpyServer,
the client works; if not, adjust the protocol constants in one place.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
from typing import Any

import numpy as np
import pytest

from openwebrx_plus.sources import (
    SourceRegistry,
    SpyServerSource,
    parse_server_info,
)

# Wire constants mirrored from the client under test (kept local so a
# constant change forces a conscious test update).
HDR = "<IIQI"
HDR_SIZE = struct.calcsize(HDR)
CMD_HELLO = 1
CMD_GET_INFO = 2
CMD_SET_STREAMING_MODE = 3
CMD_SET_IQ_FORMAT = 4
CMD_SET_IQ_FREQUENCY = 5
CMD_SET_IQ_DECIMATION = 6
CMD_SET_IQ_GAIN = 7
MSG_SERVER_HELLO = 0x100
MSG_SERVER_INFO = 0x101
MSG_SERVER_PING = 0x102
MSG_SERVER_SYNC = 0x103
MSG_SERVER_BYE = 0x107
STREAM_IQ = 2


def _msg(msg_type: int, stream_type: int, user_data: int, body: bytes) -> bytes:
    return struct.pack(HDR, msg_type, stream_type, user_data, len(body)) + body


def _server_info_body(
    device_type: int = 2,  # Airspy HF+
    serial: int = 0x48463332,
    max_rate: int = 768_000,
    max_bw: int = 768_000,
    stages: int = 6,
    gain_stages: tuple[str, ...] = ("RF",),
    min_freq: int = 9_000,
    max_freq: int = 31_000_000,
    if_freq: int = 0,
) -> bytes:
    body = struct.pack(
        "<IQIIIIiii",
        device_type, serial, max_rate, max_bw, stages, len(gain_stages),
        min_freq, max_freq, if_freq,
    )
    for name in gain_stages:
        body += struct.pack("<H", len(name)) + name.encode()
    return body


class FakeSpyServer:
    """Minimal SpyServer TCP server (protocol shape per ADR-006 / spyserver.py)."""

    def __init__(
        self,
        info_body: bytes | None = None,
        refuse_reason: str | None = None,
        frame_samples: int = 512,
        i_val: float = 0.25,
        q_val: float = -0.5,
    ) -> None:
        self.commands: list[tuple[int, bytes]] = []
        self.configured = asyncio.Event()
        self.info_body = info_body if info_body is not None else _server_info_body()
        self.refuse_reason = refuse_reason
        self.frame_samples = frame_samples
        self.i_val = i_val
        self.q_val = q_val
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        # Live client connections — asyncio.Server.close() does NOT kick
        # established connections (unlike websockets' Server.close()), so
        # stop() must terminate them explicitly or wait_closed() blocks.
        self._writers: list[asyncio.StreamWriter] = []
        self._pumps: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handler, "127.0.0.1", 0)
        assert self.server.sockets is not None
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        # Kick every live client first (see _writers note), then close the
        # listener; wait_closed then sees the handlers unwind promptly.
        for writer in self._writers:
            writer.close()
        for pump in self._pumps:
            pump.cancel()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _send(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        writer.write(data)
        await writer.drain()

    async def _handler(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        pump: asyncio.Task[None] | None = None
        self._writers.append(writer)
        try:
            while True:
                try:
                    header = await reader.readexactly(HDR_SIZE)
                except asyncio.IncompleteReadError:
                    return  # client went away
                msg_type, _stream, _user, body_size = struct.unpack(HDR, header)
                body = (
                    await reader.readexactly(body_size) if body_size else b""
                )
                self.commands.append((msg_type, body))

                if msg_type == CMD_HELLO:
                    if self.refuse_reason is not None:
                        await self._send(
                            writer,
                            _msg(
                                MSG_SERVER_BYE, 0, 0,
                                self.refuse_reason.encode(),
                            ),
                        )
                        return
                    await self._send(
                        writer, _msg(MSG_SERVER_HELLO, 0, 0, struct.pack("<I", 2))
                    )
                    # A chatty server keeps the link warm mid-handshake.
                    await self._send(writer, _msg(MSG_SERVER_PING, 0, 0, b""))
                elif msg_type == CMD_GET_INFO:
                    await self._send(
                        writer, _msg(MSG_SERVER_INFO, 0, 0, self.info_body)
                    )
                    await self._send(writer, _msg(MSG_SERVER_SYNC, 0, 7, b""))
                elif msg_type == CMD_SET_IQ_FREQUENCY:
                    # Real servers acknowledge reconfigures with SYNC.
                    await self._send(
                        writer, _msg(MSG_SERVER_SYNC, 0, 42, b"")
                    )
                    if pump is None:
                        pump = asyncio.create_task(self._pump(writer))
                        self._pumps.append(pump)
                        self.configured.set()
                # Streaming-mode/format/decimation/gain commands are just
                # recorded (the fake accepts everything).
        finally:
            self._writers.remove(writer)
            if pump is not None:
                pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def _pump(self, writer: asyncio.StreamWriter) -> None:
        samples = np.empty(self.frame_samples * 2, dtype=np.float32)
        samples[0::2] = self.i_val
        samples[1::2] = self.q_val
        payload = samples.tobytes()
        seq = 0
        try:
            while True:
                # Data frames: stream_type carries the meaning; the
                # message_type value on stream frames varies by server
                # version (0 here) — the client must not depend on it.
                await self._send(
                    writer, _msg(0, STREAM_IQ, seq, payload)
                )
                seq += 1
                await asyncio.sleep(0.002)
        except (ConnectionError, OSError):
            return

    def command_bodies(self, msg_type: int) -> list[bytes]:
        return [body for t, body in self.commands if t == msg_type]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class TestSpyServerStream:
    async def test_streams_iq_and_configures(self) -> None:
        server = FakeSpyServer(i_val=0.25, q_val=-0.5, frame_samples=512)
        await server.start()
        gen: Any = None
        try:
            src = SpyServerSource(host="127.0.0.1", port=server.port, chunk_size=512)
            assert src.fixed_sample_rate == 768_000
            assert src.info.type == "spyserver"

            gen = src.spawn(center_freq=14_150_000, sample_rate=768_000)
            chunk = await asyncio.wait_for(gen.__anext__(), timeout=5.0)

            assert chunk.dtype == np.complex64
            assert chunk.shape == (512,)
            expected = np.full(512, 0.25 - 0.5j, dtype=np.complex64)
            np.testing.assert_allclose(chunk, expected, rtol=1e-6)

            # Runtime info + device discovery surfaced for the UI/tests.
            assert src.info.endpoint == f"127.0.0.1:{server.port}"
            assert src.device_info is not None
            assert src.device_info.device_name == "Airspy HF+ / Discovery"
            assert src.device_info.maximum_sample_rate == 768_000
            assert src.device_info.gain_stages == ("RF",)

            await asyncio.sleep(0.25)  # recorder catch-up (flake-safe)
            hello = server.command_bodies(CMD_HELLO)
            assert hello, server.commands
            assert hello[0] == (
                struct.pack("<II", 2, 1_600)
                + b"openwebrx_plus (federation client)"
            )
            assert server.command_bodies(CMD_GET_INFO) == [b""]
            assert server.command_bodies(CMD_SET_STREAMING_MODE) == [
                struct.pack("<I", 0x05)
            ]  # COMMANDS | IQ
            assert server.command_bodies(CMD_SET_IQ_FORMAT) == [
                struct.pack("<I", 1)
            ]  # float32
            assert server.command_bodies(CMD_SET_IQ_DECIMATION) == [
                struct.pack("<I", 0)
            ]  # full rate: 768000 == 768000 >> 0
            assert server.command_bodies(CMD_SET_IQ_FREQUENCY) == [
                struct.pack("<q", 14_150_000)
            ]
        finally:
            if gen is not None:
                await gen.aclose()
            await server.stop()

    async def test_decimation_picked_for_narrow_rate(self) -> None:
        # An Airspy R2-class server: 10 Msps max, 8 decimation stages.
        server = FakeSpyServer(
            info_body=_server_info_body(
                device_type=1, max_rate=10_000_000, stages=8, max_bw=10_000_000,
                min_freq=24, max_freq=1_800_000_000,
            ),
            frame_samples=256,
        )
        await server.start()
        gen: Any = None
        try:
            # 625 kHz = 10 MHz >> 4 — an exact power-of-two division.
            src = SpyServerSource(
                host="127.0.0.1", port=server.port,
                sample_rate=625_000, chunk_size=256,
            )
            gen = src.spawn(center_freq=100_000_000, sample_rate=625_000)
            chunk = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
            assert chunk.shape == (256,)
            await asyncio.sleep(0.25)
            assert server.command_bodies(CMD_SET_IQ_DECIMATION) == [
                struct.pack("<I", 4)
            ]
            assert src.device_info is not None
            assert src.device_info.device_name == "Airspy R2/Mini"
        finally:
            if gen is not None:
                await gen.aclose()
            await server.stop()

    async def test_rate_mismatch_is_actionable(self) -> None:
        # RTL-SDR-class server (2.4 Msps max) can't produce 768 kHz
        # (achievable: 2.4M, 1.2M, 600k, 300k, 150k).
        server = FakeSpyServer(
            info_body=_server_info_body(
                device_type=3, max_rate=2_400_000, stages=4, max_bw=2_400_000,
            ),
        )
        await server.start()
        try:
            src = SpyServerSource(
                host="127.0.0.1", port=server.port, sample_rate=768_000
            )
            gen = src.spawn(center_freq=1090_000_000, sample_rate=768_000)
            with pytest.raises(RuntimeError, match="achievable rates"):
                await asyncio.wait_for(gen.__anext__(), timeout=5.0)
        finally:
            await server.stop()

    async def test_initial_gain_sent_at_connect(self) -> None:
        server = FakeSpyServer(frame_samples=128)
        await server.start()
        gen: Any = None
        try:
            src = SpyServerSource(
                host="127.0.0.1", port=server.port, chunk_size=128, gain=10.0
            )
            gen = src.spawn(center_freq=7_100_000, sample_rate=768_000, gain=10.0)
            await asyncio.wait_for(gen.__anext__(), timeout=5.0)
            await asyncio.sleep(0.25)
            assert server.command_bodies(CMD_SET_IQ_GAIN) == [
                struct.pack("<ii", 10, 1)
            ]  # 10 dB, manual gain type
        finally:
            if gen is not None:
                await gen.aclose()
            await server.stop()

    async def test_runtime_gain_latest_wins(self) -> None:
        server = FakeSpyServer(frame_samples=128)
        await server.start()
        gen: Any = None
        try:
            src = SpyServerSource(host="127.0.0.1", port=server.port, chunk_size=128)
            gen = src.spawn(center_freq=14_150_000, sample_rate=768_000)
            await asyncio.wait_for(gen.__anext__(), timeout=5.0)

            assert src.set_runtime_gain(6.0) is True
            assert src.set_runtime_gain(9.0) is True  # latest wins
            await asyncio.wait_for(gen.__anext__(), timeout=5.0)
            await asyncio.sleep(0.25)
            gains = server.command_bodies(CMD_SET_IQ_GAIN)
            assert struct.pack("<ii", 9, 1) in gains
            assert struct.pack("<ii", 6, 1) not in gains

            # None → auto gain type.
            assert src.set_runtime_gain(None) is True
            await asyncio.wait_for(gen.__anext__(), timeout=5.0)
            await asyncio.sleep(0.25)
            assert struct.pack("<ii", 0, 0) in server.command_bodies(CMD_SET_IQ_GAIN)
        finally:
            if gen is not None:
                await gen.aclose()
            await server.stop()

    async def test_server_bye_refuses_session(self) -> None:
        server = FakeSpyServer(refuse_reason="protocol version too old")
        await server.start()
        try:
            src = SpyServerSource(host="127.0.0.1", port=server.port)
            gen = src.spawn(center_freq=14_150_000, sample_rate=768_000)
            with pytest.raises(RuntimeError, match="refused.*protocol version too old"):
                await asyncio.wait_for(gen.__anext__(), timeout=5.0)
        finally:
            await server.stop()

    async def test_server_close_ends_stream_cleanly(self) -> None:
        server = FakeSpyServer(frame_samples=256)
        await server.start()
        try:
            src = SpyServerSource(host="127.0.0.1", port=server.port, chunk_size=256)
            gen = src.spawn(center_freq=7_100_000, sample_rate=768_000)
            try:
                chunk = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
                assert chunk.shape == (256,)
                await server.stop()  # kicks the connection
                with pytest.raises(StopAsyncIteration):
                    await asyncio.wait_for(gen.__anext__(), timeout=5.0)
            finally:
                await gen.aclose()
        finally:
            await server.stop()

    async def test_unreachable_server_raises(self) -> None:
        port = _free_port()  # nothing listening — connection refused
        src = SpyServerSource(host="127.0.0.1", port=port, connect_timeout=2.0)
        gen = src.spawn(14_150_000, 768_000)
        with pytest.raises(RuntimeError, match="cannot reach SpyServer"):
            await asyncio.wait_for(gen.__anext__(), timeout=6.0)


class TestSpyServerConfigValidation:
    def test_host_required(self) -> None:
        with pytest.raises(ValueError, match="host is required"):
            SpyServerSource(host="")

    def test_bad_port_rejected(self) -> None:
        with pytest.raises(ValueError, match="port"):
            SpyServerSource(host="sdr.example.com", port=70_000)

    def test_tiny_rate_rejected(self) -> None:
        with pytest.raises(ValueError, match="sample_rate"):
            SpyServerSource(host="sdr.example.com", sample_rate=100)

    def test_negative_gain_rejected(self) -> None:
        with pytest.raises(ValueError, match="gain"):
            SpyServerSource(host="sdr.example.com", gain=-3.0)


class TestSpyServerWire:
    def test_parse_server_info_full(self) -> None:
        body = _server_info_body(
            device_type=2, serial=42, max_rate=768_000, max_bw=768_000,
            stages=6, gain_stages=("RF", "IF"), min_freq=9_000,
            max_freq=31_000_000, if_freq=0,
        )
        info = parse_server_info(body)
        assert info.device_type == 2
        assert info.device_serial == 42
        assert info.maximum_sample_rate == 768_000
        assert info.decimation_stage_count == 6
        assert info.gain_stages == ("RF", "IF")
        assert info.minimum_frequency == 9_000
        assert info.maximum_frequency == 31_000_000
        assert info.if_frequency == 0

    def test_parse_server_info_truncated(self) -> None:
        with pytest.raises(ValueError, match="too short"):
            parse_server_info(b"\x02\x00\x00\x00\x2a")

    def test_parse_server_info_bad_stage_names(self) -> None:
        body = _server_info_body(gain_stages=("RF", "IF"))
        # Corrupt the second name's length prefix to point past the end.
        corrupt = bytearray(body)
        corrupt[-3] = 0xFF  # second u16 length high byte
        corrupt[-2] = 0xFF
        with pytest.raises(ValueError, match="truncated"):
            parse_server_info(bytes(corrupt))

    def test_pick_decimation_exact(self) -> None:
        from openwebrx_plus.sources.spyserver import pick_decimation

        assert pick_decimation(768_000, 768_000, 6) == (0, 768_000)
        assert pick_decimation(625_000, 10_000_000, 8) == (4, 625_000)
        assert pick_decimation(75_000, 2_400_000, 5) == (5, 75_000)

    def test_pick_decimation_closest_when_impossible(self) -> None:
        from openwebrx_plus.sources.spyserver import pick_decimation

        # 768k on a 2.4M device: 600k (k=2) is closest.
        assert pick_decimation(768_000, 2_400_000, 4) == (2, 600_000)
        # Stage cap respected: only k=0 is available.
        assert pick_decimation(100_000, 2_400_000, 0) == (0, 2_400_000)


class TestSpyServerRegistry:
    def test_manifest_registered_as_remote(self) -> None:
        m = SourceRegistry.get_manifest("spyserver")
        assert m is not None
        assert m.hardware_required is False  # remote receiver, no local hardware
        assert m.default_sample_rate == 768_000
        assert m.gain_range == (0.0, 48.0)

    def test_registry_creates_spyserver_source(self) -> None:
        src = SourceRegistry.create(
            "spyserver", host="sdr.example.com", port=5555, sample_rate=768_000
        )
        assert isinstance(src, SpyServerSource)
        assert src.info.type == "spyserver"
        assert src.fixed_sample_rate == 768_000
