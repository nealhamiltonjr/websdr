"""KiwiSDR remote-source tests — against a fake Kiwi websocket server.

The fake server (real ``websockets.serve`` on an ephemeral port) codifies
the protocol behavior our client expects — the same both-ends-tested
strategy as the fake rtl_tcp server (ADR-006 test rule: CI never talks to
live receivers). On connect it sends identification + a sound-rate
proposal + status chatter; once the client tunes (``SET mod=IQ``) it
streams constant-valued int16 IQ frames behind the 4-byte header.

These tests double as the executable spec for the BRING-UP items in
sources/kiwi.py: if a real Kiwi behaves like FakeKiwiServer, the client
works; if not, adjust the protocol constants in one place.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct

import numpy as np
import pytest
import websockets
from websockets.asyncio.server import Server, ServerConnection
from websockets.exceptions import ConnectionClosed

from openwebrx_plus.sources import KiwiSdrSource, SourceRegistry


class FakeKiwiServer:
    """Minimal KiwiSDR websocket server (protocol shape per ADR-006)."""

    def __init__(
        self,
        i_val: int = 10000,
        q_val: int = -20000,
        frame_samples: int = 256,
        proposal_rate: int = 12_000,
    ) -> None:
        self.messages: list[str] = []
        self.tuned = asyncio.Event()
        self.i_val = i_val
        self.q_val = q_val
        self.frame_samples = frame_samples
        self.proposal_rate = proposal_rate
        self.server: Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await websockets.serve(self._handler, "127.0.0.1", 0)
        assert self.server.sockets is not None
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handler(self, ws: ServerConnection) -> None:
        # Identification, rate proposal, and chatter the client must tolerate.
        await ws.send("WHO name=fake-kiwi id=TEST version=1.748")
        await ws.send(f"SET AR in={self.proposal_rate} out={self.proposal_rate}")
        await ws.send("LOAD nusers=1/8")
        await ws.send("ADM blah=1")
        pump = asyncio.create_task(self._pump(ws))
        try:
            async for message in ws:
                if isinstance(message, str):
                    self.messages.append(message)
                    if message.startswith("SET mod="):
                        self.tuned.set()
                else:
                    self.messages.append(f"<binary {len(message)}>")
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump

    async def _pump(self, ws: ServerConnection) -> None:
        await self.tuned.wait()
        samples = np.empty(self.frame_samples * 2, dtype=np.int16)
        samples[0::2] = self.i_val
        samples[1::2] = self.q_val
        payload = samples.tobytes()
        seq = 0
        try:
            while True:
                header = struct.pack("<HBB", seq & 0xFFFF, 0, 0)
                await ws.send(header + payload)
                seq += 1
                await asyncio.sleep(0.002)
        except ConnectionClosed:
            return


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class TestKiwiSdrSource:
    async def test_streams_iq_and_negotiates(self) -> None:
        server = FakeKiwiServer(i_val=10000, q_val=-20000, frame_samples=256)
        await server.start()
        gen = None
        try:
            src = KiwiSdrSource(host="127.0.0.1", port=server.port, chunk_size=512)
            assert src.fixed_sample_rate == 12_000
            assert src.info.type == "kiwi"

            gen = src.spawn(center_freq=14_207_000, sample_rate=12_000)
            chunk = await asyncio.wait_for(gen.__anext__(), timeout=5.0)

            assert chunk.dtype == np.complex64
            assert chunk.shape == (512,)
            expected = np.full(512, (10000 - 20000j) / 32768, dtype=np.complex64)
            np.testing.assert_allclose(chunk, expected, rtol=1e-6)
            # Runtime info carries the endpoint for the UI.
            assert src.info.endpoint == f"127.0.0.1:{server.port}"

            await asyncio.sleep(0.25)  # let the recorder flush (flake-safe)
            assert any(m.startswith("WHO am_I=openwebrx_plus") for m in server.messages)
            mod_msgs = [m for m in server.messages if m.startswith("SET mod=")]
            assert mod_msgs, server.messages
            assert "freq=14207.000000" in mod_msgs[0]  # kHz, 6 decimals
            assert "low_cut=-6000" in mod_msgs[0]  # full 12 kHz band
            assert "high_cut=6000" in mod_msgs[0]
            # The client requests its rate and confirms the server's proposal.
            assert "SET AR out=12000" in server.messages
            assert "SET AR OK in=12000 out=12000" in server.messages
        finally:
            if gen is not None:
                await gen.aclose()
            await server.stop()

    async def test_custom_rate_and_bandwidth(self) -> None:
        server = FakeKiwiServer(frame_samples=128, proposal_rate=8000)
        await server.start()
        gen = None
        try:
            src = KiwiSdrSource(
                host="127.0.0.1",
                port=server.port,
                iq_sample_rate=8000,
                iq_bandwidth=4000,
                chunk_size=256,
            )
            assert src.fixed_sample_rate == 8000
            gen = src.spawn(center_freq=3_570_000, sample_rate=8000)
            chunk = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
            assert chunk.shape == (256,)
            await asyncio.sleep(0.25)
            mod_msgs = [m for m in server.messages if m.startswith("SET mod=")]
            assert mod_msgs
            assert "freq=3570.000000" in mod_msgs[0]
            assert "low_cut=-2000" in mod_msgs[0]  # 4 kHz band centered
            assert "high_cut=2000" in mod_msgs[0]
            assert "SET AR out=8000" in server.messages
        finally:
            if gen is not None:
                await gen.aclose()
            await server.stop()

    async def test_server_close_ends_stream_cleanly(self) -> None:
        server = FakeKiwiServer(frame_samples=256)
        await server.start()
        try:
            src = KiwiSdrSource(host="127.0.0.1", port=server.port, chunk_size=256)
            gen = src.spawn(center_freq=7_100_000, sample_rate=12_000)
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
        src = KiwiSdrSource(host="127.0.0.1", port=port, connect_timeout=2.0)
        gen = src.spawn(14_207_000, 12_000)
        with pytest.raises(RuntimeError, match="cannot reach KiwiSDR"):
            await asyncio.wait_for(gen.__anext__(), timeout=6.0)


class TestKiwiConfigValidation:
    def test_host_required(self) -> None:
        with pytest.raises(ValueError, match="host is required"):
            KiwiSdrSource(host="")

    def test_bad_port_rejected(self) -> None:
        with pytest.raises(ValueError, match="port"):
            KiwiSdrSource(host="kiwi.example.com", port=70_000)

    def test_tiny_rate_rejected(self) -> None:
        with pytest.raises(ValueError, match="iq_sample_rate"):
            KiwiSdrSource(host="kiwi.example.com", iq_sample_rate=100)

    def test_bandwidth_wider_than_rate_rejected(self) -> None:
        with pytest.raises(ValueError, match="iq_bandwidth"):
            KiwiSdrSource(host="kiwi.example.com", iq_sample_rate=8000, iq_bandwidth=9000)


class TestKiwiRegistry:
    def test_manifest_registered_as_remote(self) -> None:
        m = SourceRegistry.get_manifest("kiwi")
        assert m is not None
        assert m.hardware_required is False  # remote receiver, no local hardware
        assert m.default_sample_rate == 12_000

    def test_registry_creates_kiwi_source(self) -> None:
        src = SourceRegistry.create(
            "kiwi", host="rx.example.com", port=8073, user="test-client"
        )
        assert isinstance(src, KiwiSdrSource)
        assert src.info.type == "kiwi"
        assert src.user == "test-client"
