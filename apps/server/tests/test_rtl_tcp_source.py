"""Tests for the first-class rtl_tcp remote source (ADR-006).

Reuses the fake rtl_tcp server from the RTL-SDR driver tests (a real
asyncio socket speaking the wire protocol) and verifies:
  - SourceRegistry.create("rtl_tcp", ...) instantiates the remote source
  - spawn() yields complex64 chunks and emits the full command set,
    including the ppm frequency-correction command (0x05)
  - gain=None asks the server for tuner AGC
  - config validation (bad port, bad direct_sampling)
  - REST: the manifest is listed with hardware_required=False, and
    POST /api/receivers can spawn a session backed by the remote source
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from fastapi.testclient import TestClient

from openwebrx_plus.api.rest import create_app
from openwebrx_plus.config import Settings
from openwebrx_plus.sources import RtlTcpSource, SourceRegistry
from openwebrx_plus.sources._hw_common import cu8_to_cf32

from .test_rtl_sdr_driver import _start_fake_rtl_tcp


class TestRtlTcpSource:
    async def test_streams_cf32_and_sends_commands(self) -> None:
        pattern = bytes(range(32))  # 16 complex cu8 samples
        server, commands, port = await _start_fake_rtl_tcp(pattern)
        try:
            src = RtlTcpSource(host="127.0.0.1", port=port, chunk_size=16, ppm=21)
            assert src.info.type == "rtl_tcp"
            assert src.info.label.startswith("rtl_tcp")

            gen = src.spawn(center_freq=7_100_000, sample_rate=1_024_000, gain=32.7)
            chunks: list[np.ndarray] = []
            try:
                for _ in range(3):
                    chunks.append(await gen.__anext__())
            finally:
                await gen.aclose()

            assert len(chunks) == 3
            for c in chunks:
                assert c.dtype == np.complex64
                assert c.shape == (16,)
            expected = cu8_to_cf32(np.frombuffer(pattern, dtype=np.uint8))
            np.testing.assert_allclose(chunks[0], expected, rtol=1e-6)

            await asyncio.sleep(0.25)  # let the command recorder flush (flake-safe)
            assert (0x01, 7_100_000) in commands  # frequency
            assert (0x02, 1_024_000) in commands  # sample rate
            assert (0x03, 0) in commands  # gain mode: manual
            assert (0x04, 327) in commands  # 32.7 dB → 327 tenths
            assert (0x05, 21) in commands  # ppm frequency correction
            assert (0x08, 1) in commands  # rtl agc default on
            # endpoint shows up in the runtime info
            assert src.info.endpoint == f"127.0.0.1:{port}"
        finally:
            server.close()
            await server.wait_closed()

    async def test_gain_none_requests_tuner_agc(self) -> None:
        server, commands, port = await _start_fake_rtl_tcp(bytes(64))
        try:
            src = RtlTcpSource(host="127.0.0.1", port=port, chunk_size=8)
            gen = src.spawn(center_freq=14_070_000, sample_rate=250_000, gain=None)
            try:
                await gen.__anext__()
            finally:
                await gen.aclose()
            await asyncio.sleep(0.25)
            assert (0x03, 1) in commands  # gain mode: auto
        finally:
            server.close()
            await server.wait_closed()

    async def test_direct_sampling_command(self) -> None:
        server, commands, port = await _start_fake_rtl_tcp(bytes(64))
        try:
            src = RtlTcpSource(
                host="127.0.0.1", port=port, chunk_size=8, direct_sampling=2
            )
            gen = src.spawn(center_freq=3_570_000, sample_rate=250_000, gain=None)
            try:
                await gen.__anext__()
            finally:
                await gen.aclose()
            await asyncio.sleep(0.25)
            assert (0x09, 2) in commands  # direct sampling: Q branch
        finally:
            server.close()
            await server.wait_closed()

    async def test_unreachable_host_times_out(self) -> None:
        # 203.0.113.0/24 is TEST-NET-3 — guaranteed unroutable, so the
        # connect timeout path fires deterministically.
        src = RtlTcpSource(
            host="203.0.113.1", port=1234, chunk_size=8, connect_timeout=0.5
        )
        gen = src.spawn(100_000_000, 250_000, None)
        with pytest.raises((TimeoutError, OSError)):
            await asyncio.wait_for(gen.__anext__(), timeout=3.0)

    async def test_bad_magic_rejected(self) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                writer.write(b"NOPE" + b"\x00" * 8)
                await writer.drain()
                await asyncio.sleep(0.2)
            finally:
                writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            src = RtlTcpSource(host="127.0.0.1", port=port, chunk_size=8)
            gen = src.spawn(100_000_000, 250_000, None)
            with pytest.raises(RuntimeError, match="not an rtl_tcp server"):
                await gen.__anext__()
        finally:
            server.close()
            await server.wait_closed()


class TestRtlTcpConfigValidation:
    def test_host_required(self) -> None:
        with pytest.raises(ValueError, match="host is required"):
            RtlTcpSource(host="")

    def test_bad_port_rejected(self) -> None:
        with pytest.raises(ValueError, match="port"):
            RtlTcpSource(host="localhost", port=70_000)

    def test_bad_direct_sampling_rejected(self) -> None:
        with pytest.raises(ValueError, match="direct_sampling"):
            RtlTcpSource(host="localhost", direct_sampling=5)


class TestRtlTcpRegistryAndRest:
    def test_manifest_registered_as_remote(self) -> None:
        m = SourceRegistry.get_manifest("rtl_tcp")
        assert m is not None
        assert m.hardware_required is False  # remote — no local hardware
        assert m.supports_agc is True
        assert m.supports_bias_tee is False  # deliberate foot-gun avoidance
        assert "rtl_tcp" in m.label.lower()

    def test_registry_creates_remote_source(self) -> None:
        src = SourceRegistry.create("rtl_tcp", host="sdr.example.com", port=1235)
        assert isinstance(src, RtlTcpSource)
        assert src.info.type == "rtl_tcp"

    def test_rest_lists_rtl_tcp_manifest(self) -> None:
        app = create_app(Settings(tier="dev"))
        with TestClient(app) as client:
            resp = client.get("/api/sources")
            assert resp.status_code == 200
            entry = next(
                (s for s in resp.json() if s["source_type"] == "rtl_tcp"), None
            )
        assert entry is not None
        assert entry["hardware_required"] is False
        assert entry["default_sample_rate"] == 2_400_000

    async def test_rest_spawns_session_on_remote_source(self) -> None:
        pattern = bytes(256)
        server, _commands, port = await _start_fake_rtl_tcp(pattern)
        try:
            app = create_app(Settings(tier="dev"))
            with TestClient(app) as client:
                resp = client.post(
                    "/api/receivers",
                    json={
                        "receiver_id": "rx-rtltcp-test",
                        "center_freq": 7_100_000,
                        "sample_rate": 250_000,
                        "mode": "USB",
                        "source_type": "rtl_tcp",
                        "source_kwargs": {
                            "host": "127.0.0.1",
                            "port": port,
                            "chunk_size": 128,
                        },
                    },
                )
                assert resp.status_code == 201, resp.text
                assert resp.json()["receiver_id"] == "rx-rtltcp-test"

                listed = client.get("/api/receivers").json()
                match = [r for r in listed if r["receiver_id"] == "rx-rtltcp-test"]
                assert match, listed
                assert match[0]["source"]["type"] == "rtl_tcp"

                deleted = client.delete("/api/receivers/rx-rtltcp-test")
                assert deleted.status_code == 204
        finally:
            server.close()
            await server.wait_closed()
