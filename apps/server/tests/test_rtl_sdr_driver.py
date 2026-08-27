"""RTL-SDR real-driver tests — all hardware-free.

  * tcp transport: spins up a *fake rtl_tcp server* (real asyncio socket)
    that speaks the wire protocol, records commands, and streams cu8 data.
    Verifies command emission (freq/rate/gain/agc/direct-sampling) and the
    cu8 → complex64 conversion end-to-end.
  * subprocess transport: a fake ``rtl_sdr`` executable records its argv
    and streams a byte pattern on stdout.
  * transport resolution: auto-mode failure surfaces a helpful error.
  * config validation.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from pathlib import Path

import numpy as np
import pytest

from openwebrx_plus.sources._hw_common import cu8_to_cf32
from openwebrx_plus.sources.rtl_sdr import RtlSdrSource


async def _start_fake_rtl_tcp(pattern: bytes) -> tuple[asyncio.Server, list[tuple[int, int]], int]:
    """Fake rtl_tcp: handshake header, command recorder, cu8 pump."""
    commands: list[tuple[int, int]] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # NOTE: handlers MUST close their writer — on Python 3.12
        # Server.wait_closed() hangs otherwise (transport never closes).
        try:
            writer.write(b"RTL0" + struct.pack("<II", 1, 29))  # tuner=1, 29 gains
            await writer.drain()
            await asyncio.gather(pump(reader, writer), cmd_loop(reader))
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def pump(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                writer.write(pattern)
                await writer.drain()
                await asyncio.sleep(0.001)
        except (ConnectionError, OSError):
            pass

    async def cmd_loop(reader: asyncio.StreamReader) -> None:
        try:
            while True:
                head = await reader.readexactly(5)
                cmd, value = struct.unpack(">BI", head)
                commands.append((cmd, value))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, commands, port


class TestRtlTcpTransport:
    async def test_streams_cf32_and_sends_commands(self) -> None:
        pattern = bytes(range(16))  # 8 complex cu8 samples
        server, commands, port = await _start_fake_rtl_tcp(pattern)
        try:
            src = RtlSdrSource(transport="tcp", port=port, chunk_size=8)
            assert src.info.type == "rtl_sdr"

            gen = src.spawn(center_freq=145_000_000, sample_rate=250_000, gain=30.0)
            chunks: list[np.ndarray] = []
            try:
                for _ in range(3):
                    chunks.append(await gen.__anext__())
            finally:
                await gen.aclose()

            assert len(chunks) == 3
            for c in chunks:
                assert c.dtype == np.complex64
                assert c.shape == (8,)

            # Conversion correctness: chunk 0 == converted pattern.
            expected = cu8_to_cf32(np.frombuffer(pattern, dtype=np.uint8))
            np.testing.assert_allclose(chunks[0], expected, rtol=1e-6)

            # Command emission (allow a moment for the recorder).
            await asyncio.sleep(0.05)
            freq_cmds = [v for c, v in commands if c == 0x01]
            assert 145_000_000 in freq_cmds
            rate_cmds = [v for c, v in commands if c == 0x02]
            assert 250_000 in rate_cmds
            assert (0x03, 0) in commands  # gain mode: manual
            assert (0x04, 300) in commands  # 30.0 dB → 300 tenths
            assert (0x08, 1) in commands  # rtl agc default on
        finally:
            server.close()
            await server.wait_closed()

    async def test_gain_none_requests_tuner_agc(self) -> None:
        pattern = bytes(32)
        server, commands, port = await _start_fake_rtl_tcp(pattern)
        try:
            src = RtlSdrSource(transport="tcp", port=port, chunk_size=8)
            gen = src.spawn(center_freq=1090_000_000, sample_rate=2_000_000, gain=None)
            try:
                await gen.__anext__()
            finally:
                await gen.aclose()
            await asyncio.sleep(0.05)
            assert (0x03, 1) in commands  # gain mode: auto
        finally:
            server.close()
            await server.wait_closed()

    async def test_direct_sampling_and_bias_tee_commands(self) -> None:
        pattern = bytes(32)
        server, commands, port = await _start_fake_rtl_tcp(pattern)
        try:
            src = RtlSdrSource(
                transport="tcp", port=port, chunk_size=8,
                direct_sampling=2, bias_tee=True,
            )
            gen = src.spawn(center_freq=7_100_000, sample_rate=250_000, gain=None)
            try:
                await gen.__anext__()
            finally:
                await gen.aclose()
            await asyncio.sleep(0.05)
            assert (0x09, 2) in commands  # direct sampling: Q branch (V4 HF)
            assert (0x0E, 1) in commands  # bias tee on
        finally:
            server.close()
            await server.wait_closed()

    async def test_bad_magic_rejected(self) -> None:
        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                writer.write(b"NOPE" + b"\x00" * 8)
                await writer.drain()
                await asyncio.sleep(0.2)
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError, OSError):
                    await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            src = RtlSdrSource(transport="tcp", port=port, chunk_size=8)
            gen = src.spawn(100_000_000, 250_000, None)
            with pytest.raises(RuntimeError, match="not an rtl_tcp server"):
                await gen.__anext__()
        finally:
            server.close()
            await server.wait_closed()


class TestRtlSubprocessTransport:
    async def test_streams_and_invokes_cli(self, tmp_path: Path) -> None:
        script = tmp_path / "fake_rtl_sdr"
        args_file = tmp_path / "args.txt"
        script.write_text(
            "#!/bin/sh\n"
            f"echo \"$@\" > {args_file}\n"
            "exec python3 -c 'import sys; sys.stdout.buffer.write(bytes(range(64)))'\n"
        )
        script.chmod(0o755)

        src = RtlSdrSource(
            transport="subprocess",
            rtl_sdr_binary=str(script),
            chunk_size=8,
        )
        gen = src.spawn(center_freq=145_000_000, sample_rate=250_000, gain=20.0)
        try:
            chunk = await gen.__anext__()
        finally:
            await gen.aclose()

        assert chunk.dtype == np.complex64
        assert chunk.shape == (8,)
        expected = cu8_to_cf32(np.frombuffer(bytes(range(16)), dtype=np.uint8))
        np.testing.assert_allclose(chunk, expected, rtol=1e-6)

        argv = args_file.read_text()
        assert "-f 145000000" in argv
        assert "-s 250000" in argv
        assert "-g 200" in argv  # 20.0 dB → 200 tenths
        assert argv.strip().endswith("-")


class TestTransportResolution:
    async def test_auto_with_nothing_available_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def no_tcp(self: RtlSdrSource, timeout_s: float = 0.3) -> bool:
            return False

        monkeypatch.setattr(RtlSdrSource, "_usb_available", staticmethod(lambda: False))
        monkeypatch.setattr(RtlSdrSource, "_tcp_available", no_tcp)
        monkeypatch.setattr(RtlSdrSource, "_subprocess_available", lambda self: False)

        src = RtlSdrSource()
        gen = src.spawn(100_000_000, 250_000, None)
        with pytest.raises(RuntimeError, match="no RTL-SDR transport available"):
            await gen.__anext__()

    async def test_auto_prefers_tcp_when_no_usb(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Auto mode with a reachable rtl_tcp falls through to the tcp transport."""
        pattern = bytes(64)
        server, _commands, port = await _start_fake_rtl_tcp(pattern)
        try:
            monkeypatch.setattr(RtlSdrSource, "_usb_available", staticmethod(lambda: False))
            monkeypatch.setattr(
                RtlSdrSource, "_subprocess_available", lambda self: False
            )
            src = RtlSdrSource(port=port, chunk_size=8)
            gen = src.spawn(100_000_000, 250_000, None)
            try:
                chunk = await gen.__anext__()
                assert chunk.shape == (8,)
            finally:
                await gen.aclose()
            assert "tcp" in src.info.label
        finally:
            server.close()
            await server.wait_closed()


class TestConfigValidation:
    def test_invalid_transport_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid transport"):
            RtlSdrSource(transport="spi")

    def test_invalid_direct_sampling_rejected(self) -> None:
        with pytest.raises(ValueError, match="direct_sampling"):
            RtlSdrSource(direct_sampling=3)
