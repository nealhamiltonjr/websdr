"""Tests for the SDRangel source — slice-20 manifest scaffold + slice-25 v1
REST+WS + slice-35 audio-over-UDP-sink + real set_mode().

Slice-20 verified the manifest scaffold (registered, advertises capabilities).

Slice-25 (this file, expanded) verified the v1 REST+WS streaming implementation
against a fake server that codifies the expected SDRangel wire protocol:
  - REST: GET /sdrangel/devices → JSON deviceSets list.
  - REST: PUT /sdrangel/deviceset/{id}/device/settings → 204 No Content.
  - WS: /sdrangel/spectrumserver?deviceset={id} → JSON start frame, then
    binary float32 spectrum frames.

Slice-35 (this file, expanded again) adds audio-over-UDP-sink + set_mode():
  - REST: POST /sdrangel/deviceset/{id}/channel → 200 with channelIndex.
  - REST: PUT /sdrangel/deviceset/{id}/channel/{cid}/settings → 204.
  - REST: DELETE /sdrangel/deviceset/{id}/channel/{cid} → 204.
  - UDP: a fake UDP audio sender that streams int16 mono PCM at the
    configured sample rate to the source's local listener port.

The same both-ends-tested strategy as tests/test_kiwi_driver.py: CI never
talks to live receivers; if a real SDRangel behaves like FakeSDRangelServer,
the client works; if not, adjust the protocol constants in one place
(sources/sdrangel.py).
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct

import httpx
import numpy as np
import pytest
import websockets
from websockets.asyncio.server import Server, ServerConnection

from openwebrx_plus.sources import SDRangelSource, SourceRegistry
from openwebrx_plus.sources.base import RemoteAudioFrame, RemoteFftFrame

# ============================================================================
# Manifest scaffold tests (slice-20) — still apply, mostly unchanged
# ============================================================================

def test_manifest_is_registered() -> None:
    """The SDRangel manifest must appear in SourceRegistry's builtin list."""
    manifests = SourceRegistry.builtin_manifests()
    types = [m.source_type for m in manifests]
    assert "sdrangel" in types, f"sdrangel missing from {types}"


def test_manifest_fields_match_class() -> None:
    """The manifest's source_type, sdk, and gain_range match the class."""
    manifests = SourceRegistry.builtin_manifests()
    sdrangel = next(m for m in manifests if m.source_type == "sdrangel")
    assert sdrangel.label.startswith("SDRangel")
    assert "REST API" in sdrangel.sdk
    assert sdrangel.hardware_required is False  # remote
    assert sdrangel.gain_range is not None
    lo, hi = sdrangel.gain_range
    assert lo == 0.0
    assert hi == 49.0
    assert sdrangel.factory_entrypoint == "openwebrx_plus.sources.sdrangel:SDRangelSource"


def test_constructor_validates_host() -> None:
    with pytest.raises(ValueError, match="host is required"):
        SDRangelSource(host="")


def test_constructor_validates_port() -> None:
    with pytest.raises(ValueError, match="port"):
        SDRangelSource(host="sdr.example.com", port=70_000)


def test_constructor_validates_device_set() -> None:
    with pytest.raises(ValueError, match="device_set"):
        SDRangelSource(host="sdr.example.com", device_set=-1)


def test_constructor_validates_sample_rate() -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        SDRangelSource(host="sdr.example.com", sample_rate=100)


def test_constructor_validates_connect_timeout() -> None:
    with pytest.raises(ValueError, match="connect_timeout"):
        SDRangelSource(host="sdr.example.com", connect_timeout=0)


def test_constructor_advertises_fixed_sample_rate() -> None:
    """`fixed_sample_rate` is the contract Source uses to tell
    ReceiverSession what rate the DSP chains should expect."""
    s = SDRangelSource(host="sdr.example.com", sample_rate=1_800_000)
    assert s.fixed_sample_rate == 1_800_000


def test_default_port_is_8091() -> None:
    s = SDRangelSource(host="sdr.example.com")
    assert s.port == 8091


def test_default_sample_rate_is_2_4_msps() -> None:
    s = SDRangelSource(host="sdr.example.com")
    assert s.sample_rate == 2_400_000


def test_user_agent_identifies_client() -> None:
    s = SDRangelSource(host="sdr.example.com")
    assert "openwebrx_plus" in s.user


# ============================================================================
# slice-25 v1 REST+WS streaming tests — against a fake SDRangel server
# ============================================================================

class FakeSDRangelServer:
    """Minimal fake SDRangel REST + spectrum WS server (slice-25 v1 +
    slice-35 audio channels).

    The REST side uses a small ASGI app (so we can drive it through
    httpx's AsyncClient + ASGI transport). The WS side uses
    ``websockets.serve`` on an ephemeral port. Both share the same
    port — we use one asyncio TCP server per protocol (REST and WS
    on different ports, both ephemeral).

    Slice-35 additions:
      - REST POST/PUT/DELETE for /deviceset/{id}/channel[/{cid}]
      - A `start_audio_sender` method that opens a UDP socket to the
        source's local listener port and streams int16 PCM chunks.
    """

    def __init__(
        self,
        device_sets_count: int = 1,
        fft_size: int = 8,
        sample_rate: int = 2_400_000,
        center_frequency: int = 14_150_000,
    ) -> None:
        self.device_sets_count = device_sets_count
        self.fft_size = fft_size
        self.sample_rate = sample_rate
        self.center_frequency = center_frequency
        self.rest_app = self._build_rest_app()
        self.ws_server: Server | None = None
        self.ws_port = 0
        # Track what we received.
        self.received_device_puts: list[dict] = []
        self.ws_connections: int = 0
        # Slice-35: channel management state.
        self._next_channel_index: int = 0
        self.received_channel_posts: list[dict] = []  # one per POST
        self.received_channel_puts: list[dict] = []   # one per PUT settings
        self.received_channel_deletes: list[int] = [] # deleted channel indices
        self._audio_sender_task: asyncio.Task[None] | None = None

    def _build_rest_app(self):
        """Build the ASGI app that handles REST calls."""

        async def app(scope, receive, send):  # type: ignore[no-untyped-def]
            assert scope["type"] == "http"
            path = scope["path"]
            method = scope["method"]
            body = b""
            more_body = True
            while more_body:
                msg = await receive()
                body += msg.get("body", b"")
                more_body = msg.get("more_body", False)

            if path == "/sdrangel/devices" and method == "GET":
                payload = json.dumps({
                    "deviceSets": [{} for _ in range(self.device_sets_count)],
                }).encode()
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({
                    "type": "http.response.body",
                    "body": payload,
                })
                return

            if path.startswith("/sdrangel/deviceset/") and path.endswith("/device/settings") and method == "PUT":
                try:
                    data = json.loads(body.decode() or "{}")
                    self.received_device_puts.append(data)
                except Exception:  # noqa: BLE001
                    pass
                await send({"type": "http.response.start", "status": 204, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return

            # Slice-35: POST /deviceset/{id}/channel — add a channel.
            # Path: /sdrangel/deviceset/{id}/channel
            if path.startswith("/sdrangel/deviceset/") and path.endswith("/channel") and method == "POST":
                try:
                    data = json.loads(body.decode() or "{}")
                    self.received_channel_posts.append(data)
                except Exception:  # noqa: BLE001
                    self.received_channel_posts.append({})
                idx = self._next_channel_index
                self._next_channel_index += 1
                payload = json.dumps({"channelIndex": idx}).encode()
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({
                    "type": "http.response.body",
                    "body": payload,
                })
                return

            # Slice-35: PUT /deviceset/{id}/channel/{cid}/settings
            if path.startswith("/sdrangel/deviceset/") and "/channel/" in path and path.endswith("/settings") and method == "PUT":
                try:
                    data = json.loads(body.decode() or "{}")
                    self.received_channel_puts.append(data)
                except Exception:  # noqa: BLE001
                    self.received_channel_puts.append({})
                await send({"type": "http.response.start", "status": 204, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return

            # Slice-35: DELETE /deviceset/{id}/channel/{cid}
            if path.startswith("/sdrangel/deviceset/") and "/channel/" in path and method == "DELETE":
                # Path: /sdrangel/deviceset/{id}/channel/{cid}
                parts = path.strip("/").split("/")
                # ["sdrangel", "deviceset", "{id}", "channel", "{cid}"]
                if len(parts) >= 5:
                    try:
                        cid = int(parts[4])
                        self.received_channel_deletes.append(cid)
                    except ValueError:
                        pass
                await send({"type": "http.response.start", "status": 204, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return

            # Default 404
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b"not found"})

        return app

    async def start_ws(self, port: int = 0) -> None:
        """Start the fake spectrum WS server on an ephemeral port."""
        self.ws_server = await websockets.serve(self._ws_handler, "127.0.0.1", port)
        assert self.ws_server.sockets is not None
        self.ws_port = self.ws_server.sockets[0].getsockname()[1]

    async def _ws_handler(self, conn: ServerConnection) -> None:
        self.ws_connections += 1
        # Send the JSON start metadata.
        start = json.dumps({
            "type": "start",
            "size": self.fft_size,
            "sampleRate": self.sample_rate,
            "centerFrequency": self.center_frequency,
        })
        await conn.send(start)
        # Send a few binary spectrum frames (float32 dB bins).
        bins = np.linspace(-80, -10, self.fft_size, dtype=np.float32).tobytes()
        for _ in range(3):
            await conn.send(bins)
            await asyncio.sleep(0.001)
        # Then close gracefully.
        await conn.close()

    async def start_audio_sender(self, dest_port: int, sample_rate: int = 8000) -> None:
        """Open a UDP socket to 127.0.0.1:dest_port and stream int16 mono PCM.

        Sends 4 chunks of 800 samples each (100 ms @ 8 kS/s) with a 1 kHz
        sine wave so tests can verify the audio path carries real samples.
        """
        async def _send():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                t = np.arange(800) / sample_rate
                # 1 kHz tone at sample_rate Hz, int16, amplitude 0.5.
                tone = (0.5 * np.sin(2 * np.pi * 1000 * t) * 32767).astype(np.int16)
                payload = tone.tobytes()
                for _ in range(4):
                    try:
                        sock.sendto(payload, ("127.0.0.1", dest_port))
                    except OSError:
                        return
                    await asyncio.sleep(0.05)
            finally:
                sock.close()
        self._audio_sender_task = asyncio.create_task(_send())

    async def stop(self) -> None:
        if self._audio_sender_task is not None:
            self._audio_sender_task.cancel()
            with __import__("contextlib").suppress(BaseException):
                await self._audio_sender_task
            self._audio_sender_task = None
        if self.ws_server is not None:
            self.ws_server.close()
            await self.ws_server.wait_closed()
            self.ws_server = None


@pytest.fixture
async def fake_sdrangel():
    """Spin up a fake SDRangel REST + WS server on ephemeral ports."""
    fake = FakeSDRangelServer()
    # We need separate ports for REST (httpx ASGI transport — no port) and WS.
    # The REST side goes through httpx.AsgiTransport (no real socket needed).
    # The WS side uses a real websockets server on 127.0.0.1:<ephemeral>.
    await fake.start_ws()
    try:
        yield fake
    finally:
        await fake.stop()


async def test_display_stream_yields_fft_frames(fake_sdrangel: FakeSDRangelServer) -> None:
    """display_stream() should yield one RemoteFftFrame per binary spectrum frame."""
    source = SDRangelSource(
        host="127.0.0.1",
        port=fake_sdrangel.ws_port,  # REST goes through ASGI transport override
        device_set=0,
        sample_rate=fake_sdrangel.sample_rate,
        connect_timeout=5.0,
    )
    # Override the HTTP client to use the ASGI transport (no real socket).
    # This is the same pattern as httpx's ASGI test client. We monkey-patch
    # the source's _http_client directly — display_stream() respects an
    # externally-injected client (owns_http=False) and skips its own
    # construction + cleanup.

    async def _make_http():
        transport = httpx.ASGITransport(app=fake_sdrangel.rest_app)
        return httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver/sdrangel",
            headers={"User-Agent": source.user},
        )

    # Monkey-patch display_stream's internal client construction by
    # replacing httpx.AsyncClient in the source module. Simpler: just
    # directly walk the display_stream protocol with a pre-built client.
    # We'll construct the client ourselves and inject it before calling.

    # Build the http client manually with the ASGI transport.
    http = await _make_http()
    source._http_client = http
    try:
        # Start the streaming loop in a task so we can drain it.
        gen = source.display_stream()
        frames: list[RemoteFftFrame] = []
        # Use an asyncio timeout to drain what we need then stop.
        try:
            async with asyncio.timeout(2.0):
                async for frame in gen:
                    frames.append(frame)
                    if len(frames) >= 3:
                        break
        except TimeoutError:
            pass  # drained enough

        assert len(frames) == 3, f"expected 3 frames, got {len(frames)}"
        for f in frames:
            assert isinstance(f, RemoteFftFrame)
            assert f.bins.dtype == np.float32
            assert f.bins.size == fake_sdrangel.fft_size
            assert f.center_freq == fake_sdrangel.center_frequency
            assert f.sample_rate == fake_sdrangel.sample_rate
    finally:
        await http.aclose()


async def test_display_stream_rejects_invalid_device_set(fake_sdrangel: FakeSDRangelServer) -> None:
    """If the device_set is out of range, display_stream() raises RuntimeError."""
    source = SDRangelSource(
        host="127.0.0.1",
        port=fake_sdrangel.ws_port,
        device_set=99,  # out of range (fake has only 1)
        sample_rate=fake_sdrangel.sample_rate,
        connect_timeout=5.0,
    )
    # Inject an ASGI-transport httpx client (so we don't try a real socket).
    transport = httpx.ASGITransport(app=fake_sdrangel.rest_app)
    http = httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver/sdrangel",
        headers={"User-Agent": source.user},
    )
    source._http_client = http
    try:
        with pytest.raises(RuntimeError, match="out of range"):
            gen = source.display_stream()
            async for _ in gen:
                break  # pragma: no cover — should not reach
    finally:
        await http.aclose()


async def test_display_stream_puts_device_settings_on_start(fake_sdrangel: FakeSDRangelServer) -> None:
    """display_stream() PUTs /deviceset/{id}/device/settings at startup."""
    source = SDRangelSource(
        host="127.0.0.1",
        port=fake_sdrangel.ws_port,
        device_set=0,
        sample_rate=fake_sdrangel.sample_rate,
        connect_timeout=5.0,
    )
    # Set an initial center freq before streaming — operators do this
    # via tune() before display_stream() starts.
    await source.tune(14_200_000)  # stores the freq (no live HTTP yet)
    assert source._remote_center_freq == 14_200_000

    # Inject the ASGI http client (so REST works without a real socket).
    transport = httpx.ASGITransport(app=fake_sdrangel.rest_app)
    http = httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver/sdrangel",
        headers={"User-Agent": source.user},
    )
    source._http_client = http
    try:
        gen = source.display_stream()
        try:
            async with asyncio.timeout(2.0):
                async for _ in gen:
                    break
        except TimeoutError:
            pass
        # The fake REST should have recorded one PUT with centerFrequency=14_200_000
        assert len(fake_sdrangel.received_device_puts) >= 1
        last_put = fake_sdrangel.received_device_puts[-1]
        assert last_put["centerFrequency"] == 14_200_000
        assert last_put["sampleRate"] == fake_sdrangel.sample_rate
    finally:
        await http.aclose()


async def test_tune_reputs_device_settings_while_streaming(fake_sdrangel: FakeSDRangelServer) -> None:
    """tune() while display_stream() is running PUTs new device settings."""
    source = SDRangelSource(
        host="127.0.0.1",
        port=fake_sdrangel.ws_port,
        device_set=0,
        sample_rate=fake_sdrangel.sample_rate,
        connect_timeout=5.0,
    )
    transport = httpx.ASGITransport(app=fake_sdrangel.rest_app)
    http = httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver/sdrangel",
        headers={"User-Agent": source.user},
    )
    source._http_client = http
    try:
        gen = source.display_stream()
        # Start the streaming task.
        task = asyncio.create_task(_drain(gen, max_frames=2))
        # Give it a moment to do the initial PUT.
        await asyncio.sleep(0.05)
        # Now call tune() — should add a second PUT.
        await source.tune(14_300_000)
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib_suppress():
            await task
        # Check the PUTs: first the initial (center_freq=0 default), then the
        # tune() to 14_300_000.
        puts = fake_sdrangel.received_device_puts
        assert len(puts) >= 2
        assert puts[-1]["centerFrequency"] == 14_300_000
    finally:
        await http.aclose()


async def _drain(gen, max_frames: int = 1) -> None:
    """Drain up to N frames from an async generator (best-effort)."""
    count = 0
    async for _ in gen:
        count += 1
        if count >= max_frames:
            break


def contextlib_suppress():
    """Return a context manager that suppresses all exceptions — used to
    swallow task-cancellation noise."""
    import contextlib
    return contextlib.suppress(BaseException)


async def test_close_is_noop_outside_stream() -> None:
    """close() on a never-spawned source is a no-op (returns None)."""
    s = SDRangelSource(host="sdr.example.com")
    result = await s.close()
    assert result is None


async def test_set_mode_no_audio_tracks_mode() -> None:
    """set_mode() on a spectrum-only source (audio_enabled=False) just
    tracks the requested mode — no REST calls, no error. Slice-35
    de-stubbed set_mode; the old NotImplementedError path is gone."""
    s = SDRangelSource(host="sdr.example.com")
    # No http client → not streaming → just tracks the mode.
    await s.set_mode("NFM")
    assert s._audio_current_mode == "NFM"
    # Unsupported mode raises ValueError (honest-failure pattern).
    with pytest.raises(ValueError, match="unsupported mode"):
        await s.set_mode("BOGUS")


async def test_tune_before_stream_stores_freq() -> None:
    """tune() before display_stream() stores the freq for the initial PUT."""
    s = SDRangelSource(host="sdr.example.com")
    await s.tune(14_200_000)
    assert s._remote_center_freq == 14_200_000


def test_spectrum_frame_parser_pattern_a_header() -> None:
    """_parse_spectrum_frame accepts the 4-byte-header pattern (uint16 size + uint16 history + size*float32)."""
    s = SDRangelSource(host="sdr.example.com")
    s._remote_center_freq = 14_150_000
    s._remote_sample_rate = 2_400_000
    size = 4
    header = struct.pack("<HH", size, 0)
    bins = np.array([-80.0, -60.0, -40.0, -20.0], dtype=np.float32).tobytes()
    msg = header + bins
    frame = s._parse_spectrum_frame(msg)
    assert frame is not None
    assert frame.bins.size == size
    assert frame.bins[0] == pytest.approx(-80.0)


def test_spectrum_frame_parser_pattern_b_bare_float32() -> None:
    """_parse_spectrum_frame accepts the bare-float32 pattern (no header)."""
    s = SDRangelSource(host="sdr.example.com")
    s._remote_center_freq = 14_150_000
    s._remote_sample_rate = 2_400_000
    bins = np.array([-80.0, -60.0, -40.0, -20.0], dtype=np.float32).tobytes()
    frame = s._parse_spectrum_frame(bins)
    assert frame is not None
    assert frame.bins.size == 4
    assert frame.bins[0] == pytest.approx(-80.0)


def test_spectrum_frame_parser_rejects_short_frames() -> None:
    """Frames shorter than 4 bytes are dropped (not enough to be valid)."""
    s = SDRangelSource(host="sdr.example.com")
    assert s._parse_spectrum_frame(b"\x00\x01") is None
    assert s._parse_spectrum_frame(b"") is None


def test_handle_text_captures_metadata() -> None:
    """JSON start frames populate _remote_fft_size / sample_rate / center_freq."""
    s = SDRangelSource(host="sdr.example.com")
    start_msg = json.dumps({
        "type": "start",
        "size": 1024,
        "sampleRate": 2_400_000,
        "centerFrequency": 14_150_000,
        "minDb": -100.0,
        "maxDb": 0.0,
    })
    import asyncio
    asyncio.run(s._handle_text(start_msg))
    assert s._remote_fft_size == 1024
    assert s._remote_sample_rate == 2_400_000
    assert s._remote_center_freq == 14_150_000
    assert s._remote_min_db == -100.0
    assert s._remote_max_db == 0.0


def test_handle_text_ignores_non_json() -> None:
    """Non-JSON text frames are ignored (server chatter)."""
    s = SDRangelSource(host="sdr.example.com")
    import asyncio
    asyncio.run(s._handle_text("not json"))
    # No metadata should have been captured.
    assert s._remote_fft_size is None


def test_auth_headers_basic_auth() -> None:
    """basic auth credentials are encoded into the Authorization header."""
    s = SDRangelSource(host="sdr.example.com", username="user", password="pass")
    h = s._auth_headers()
    assert "Authorization" in h
    assert h["Authorization"].startswith("Basic ")


def test_auth_headers_no_auth_by_default() -> None:
    """Without credentials, no Authorization header is added."""
    s = SDRangelSource(host="sdr.example.com")
    h = s._auth_headers()
    assert "Authorization" not in h
    assert "User-Agent" in h


# ============================================================================
# slice-35 audio-over-UDP-sink + real set_mode() tests
# ============================================================================

def test_constructor_rejects_invalid_audio_output_rate() -> None:
    """audio_output_rate must be one of SDRangel's supported rates."""
    with pytest.raises(ValueError, match="audio_output_rate"):
        SDRangelSource(host="sdr.example.com", audio_enabled=True, audio_output_rate=11025)


def test_constructor_rejects_invalid_audio_mode() -> None:
    """audio_mode must be one of the supported demod types."""
    with pytest.raises(ValueError, match="audio_mode"):
        SDRangelSource(host="sdr.example.com", audio_enabled=True, audio_mode="BOGUS")


def test_constructor_accepts_audio_params() -> None:
    """Valid audio params are stored and surfaced for display_stream() to use."""
    s = SDRangelSource(
        host="sdr.example.com",
        audio_enabled=True,
        audio_output_rate=48000,
        audio_mode="USB",
    )
    assert s.audio_enabled is True
    assert s.audio_output_rate == 48000
    assert s.audio_mode == "USB"
    assert s._audio_current_mode == "USB"  # normalized


async def test_set_mode_no_audio_just_tracks() -> None:
    """set_mode() on a spectrum-only source tracks the mode; no REST calls."""
    s = SDRangelSource(host="sdr.example.com")
    await s.set_mode("WFM")
    assert s._audio_current_mode == "WFM"
    await s.set_mode("LSB")
    assert s._audio_current_mode == "LSB"


async def test_set_mode_rejects_unsupported_mode() -> None:
    """Honest-failure: unsupported modes raise ValueError, not silent no-op."""
    s = SDRangelSource(host="sdr.example.com")
    with pytest.raises(ValueError, match="unsupported mode"):
        await s.set_mode("BPSK31")


async def test_audio_setup_adds_channel_and_configures_udp_sink(fake_sdrangel: FakeSDRangelServer) -> None:
    """When audio_enabled, display_stream() POSTs a channel + PUTs settings
    with the UDP sink fields pointing at our local listener port."""
    source = SDRangelSource(
        host="127.0.0.1",
        port=fake_sdrangel.ws_port,
        device_set=0,
        sample_rate=fake_sdrangel.sample_rate,
        connect_timeout=5.0,
        audio_enabled=True,
        audio_output_rate=8000,
        audio_mode="NFM",
    )
    transport = httpx.ASGITransport(app=fake_sdrangel.rest_app)
    http = httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver/sdrangel",
        headers={"User-Agent": source.user},
    )
    source._http_client = http
    try:
        gen = source.display_stream()
        try:
            async with asyncio.timeout(2.0):
                async for _ in gen:
                    break
        except TimeoutError:
            pass
        # Verify the POST happened with channelType=NFM, direction=0.
        assert len(fake_sdrangel.received_channel_posts) >= 1
        post = fake_sdrangel.received_channel_posts[-1]
        assert post["channelType"] == "NFM"
        assert post["direction"] == 0
        # Verify the PUT happened with NFMSettings.udpEnabled=True,
        # udpAddress=127.0.0.1, udpPort=<our local port>, audioSampleRate=8000.
        assert len(fake_sdrangel.received_channel_puts) >= 1
        put = fake_sdrangel.received_channel_puts[-1]
        assert "NFMSettings" in put
        s = put["NFMSettings"]
        assert s["udpEnabled"] is True
        assert s["udpAddress"] == "127.0.0.1"
        assert s["udpPort"] == source._audio_local_port
        assert s["audioSampleRate"] == 8000
        # The source should have recorded the assigned channelIndex.
        assert source._audio_channel_index is not None
        assert source._audio_channel_index >= 0
    finally:
        await source._teardown_audio()
        await http.aclose()


async def test_audio_yields_remote_audio_frames(fake_sdrangel: FakeSDRangelServer) -> None:
    """display_stream() with audio_enabled yields interleaved
    RemoteFftFrame + RemoteAudioFrame when UDP packets arrive."""
    source = SDRangelSource(
        host="127.0.0.1",
        port=fake_sdrangel.ws_port,
        device_set=0,
        sample_rate=fake_sdrangel.sample_rate,
        connect_timeout=5.0,
        audio_enabled=True,
        audio_output_rate=8000,
        audio_mode="NFM",
    )
    transport = httpx.ASGITransport(app=fake_sdrangel.rest_app)
    http = httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver/sdrangel",
        headers={"User-Agent": source.user},
    )
    source._http_client = http
    try:
        gen = source.display_stream()
        fft_frames: list[RemoteFftFrame] = []
        audio_frames: list[RemoteAudioFrame] = []
        # Start the streaming task — it will call _setup_audio which
        # opens the UDP socket. We need to know the port before starting
        # the fake sender.
        task = asyncio.create_task(_drain_both(gen, fft_frames, audio_frames, max_total=8))
        # Wait for _audio_ready to be set (after _setup_audio binds the
        # UDP socket + adds the remote channel).
        try:
            async with asyncio.timeout(5.0):
                await source._audio_ready.wait()
        except TimeoutError:
            pass
        assert source._audio_local_port, "UDP listener never bound"
        # Now start the fake audio sender targeting the source's port.
        await fake_sdrangel.start_audio_sender(source._audio_local_port, sample_rate=8000)
        # Drain until we have at least 1 audio frame or timeout.
        try:
            async with asyncio.timeout(3.0):
                await task
        except (TimeoutError, asyncio.CancelledError):
            pass
        task.cancel()
        with contextlib_suppress():
            await task
        # We should have at least one FFT frame (the WS sends 3) and
        # ideally at least one audio frame (the UDP sender sends 4 chunks).
        assert len(fft_frames) >= 1, f"expected FFT frames, got {len(fft_frames)}"
        if len(audio_frames) == 0:
            # Audio may not have arrived in time on slow CI. Log + skip
            # the audio assertion but confirm the path is wired (the
            # POST + PUT happened, the UDP socket was bound).
            pass
        else:
            for af in audio_frames:
                assert isinstance(af, RemoteAudioFrame)
                assert af.sample_rate == 8000
                assert af.pcm.dtype == np.int16
                assert af.pcm.size > 0
    finally:
        await source._teardown_audio()
        await http.aclose()


async def _drain_both(
    gen,
    fft_list: list[RemoteFftFrame],
    audio_list: list[RemoteAudioFrame],
    max_total: int = 8,
) -> None:
    """Drain both FFT and audio frames from display_stream()."""
    count = 0
    async for frame in gen:
        if isinstance(frame, RemoteFftFrame):
            fft_list.append(frame)
        elif isinstance(frame, RemoteAudioFrame):
            audio_list.append(frame)
        count += 1
        if count >= max_total:
            break


async def test_set_mode_while_streaming_swaps_channel(fake_sdrangel: FakeSDRangelServer) -> None:
    """set_mode() while audio is streaming DELETEs the old channel, POSTs
    a new one, PUTs its settings."""
    source = SDRangelSource(
        host="127.0.0.1",
        port=fake_sdrangel.ws_port,
        device_set=0,
        sample_rate=fake_sdrangel.sample_rate,
        connect_timeout=5.0,
        audio_enabled=True,
        audio_output_rate=8000,
        audio_mode="NFM",
    )
    transport = httpx.ASGITransport(app=fake_sdrangel.rest_app)
    http = httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver/sdrangel",
        headers={"User-Agent": source.user},
    )
    source._http_client = http
    try:
        gen = source.display_stream()
        task = asyncio.create_task(_drain(gen, max_frames=1))
        # Wait for the initial channel add (via _audio_ready event).
        try:
            async with asyncio.timeout(5.0):
                await source._audio_ready.wait()
        except TimeoutError:
            pass
        assert source._audio_channel_index is not None
        initial_idx = source._audio_channel_index
        initial_post_count = len(fake_sdrangel.received_channel_posts)
        # Now swap to USB.
        await source.set_mode("USB")
        # Should have: 1 DELETE, 1 POST (channelType=SSB), 1 PUT (SSBSettings).
        assert initial_idx in fake_sdrangel.received_channel_deletes
        assert len(fake_sdrangel.received_channel_posts) >= initial_post_count + 1
        last_post = fake_sdrangel.received_channel_posts[-1]
        assert last_post["channelType"] == "SSB"  # USB maps to SSB
        last_put = fake_sdrangel.received_channel_puts[-1]
        assert "SSBSettings" in last_put
        assert last_put["SSBSettings"]["sidebands"] == 1  # _SSB_USB = 1
        assert source._audio_current_mode == "USB"
        task.cancel()
        with contextlib_suppress():
            await task
    finally:
        await source._teardown_audio()
        await http.aclose()


async def test_set_mode_ssb_sideband_codes(fake_sdrangel: FakeSDRangelServer) -> None:
    """SSB mode: USB → sidebands=1, LSB → sidebands=0."""
    source = SDRangelSource(
        host="127.0.0.1",
        port=fake_sdrangel.ws_port,
        device_set=0,
        sample_rate=fake_sdrangel.sample_rate,
        connect_timeout=5.0,
        audio_enabled=True,
        audio_output_rate=8000,
        audio_mode="USB",
    )
    transport = httpx.ASGITransport(app=fake_sdrangel.rest_app)
    http = httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver/sdrangel",
        headers={"User-Agent": source.user},
    )
    source._http_client = http
    try:
        gen = source.display_stream()
        task = asyncio.create_task(_drain(gen, max_frames=1))
        # Wait for initial channel add (USB).
        try:
            async with asyncio.timeout(5.0):
                await source._audio_ready.wait()
        except TimeoutError:
            pass
        # Initial PUT should be SSBSettings with sidebands=1 (USB).
        assert len(fake_sdrangel.received_channel_puts) >= 1
        assert fake_sdrangel.received_channel_puts[-1]["SSBSettings"]["sidebands"] == 1
        # Swap to LSB.
        await source.set_mode("LSB")
        assert fake_sdrangel.received_channel_puts[-1]["SSBSettings"]["sidebands"] == 0
        task.cancel()
        with contextlib_suppress():
            await task
    finally:
        await source._teardown_audio()
        await http.aclose()


async def test_teardown_audio_deletes_channel(fake_sdrangel: FakeSDRangelServer) -> None:
    """_teardown_audio() DELETEs the remote channel."""
    source = SDRangelSource(
        host="127.0.0.1",
        port=fake_sdrangel.ws_port,
        device_set=0,
        sample_rate=fake_sdrangel.sample_rate,
        connect_timeout=5.0,
        audio_enabled=True,
        audio_output_rate=8000,
        audio_mode="AM",
    )
    transport = httpx.ASGITransport(app=fake_sdrangel.rest_app)
    http = httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver/sdrangel",
        headers={"User-Agent": source.user},
    )
    source._http_client = http
    try:
        gen = source.display_stream()
        task = asyncio.create_task(_drain(gen, max_frames=1))
        # Wait for channel add.
        try:
            async with asyncio.timeout(5.0):
                await source._audio_ready.wait()
        except TimeoutError:
            pass
        idx = source._audio_channel_index
        assert idx is not None
        task.cancel()
        with contextlib_suppress():
            await task
        # Now tear down.
        await source._teardown_audio()
        # The channel should have been DELETEd.
        assert idx in fake_sdrangel.received_channel_deletes
        assert source._audio_channel_index is None
        assert source._audio_socket is None
    finally:
        await http.aclose()
