"""OpenWebRX federation-client tests — against a fake OpenWebRX(+) server.

The fake server (real ``websockets.serve`` on an ephemeral port) codifies the
protocol behavior our client expects, extracted from the vendored upstream
implementation (owrx/connection.py, owrx/websocket.py, htdocs/openwebrx.js,
htdocs/lib/AudioEngine.js) — the same both-ends-tested strategy as the fake
Kiwi server (ADR-006 test rule: CI never talks to live receivers).

On connect it performs the real handshake dance: replies ``CLIENT DE SERVER
server=openwebrx version=...``, pushes the config burst (receiver_details,
config ×2, features, modes, profiles), then — once the client sends
``dspcontrol start`` — streams ADPCM-compressed FFT frames (with a peak that
follows the client's ``offset_freq``) and sync-framed ADPCM audio at the
client's requested output rate.

The ADPCM codecs on BOTH ends are the exact libcsdr/JS ports in
``sources/_adpcm.py``, so the round-trip tests below pin the encoder and
decoder against each other: a structural mistake in either shows up as a
massive decode error, not a subtle shift.

These tests double as the executable spec for the BRING-UP items in
sources/openwebrx_remote.py: if a real receiver behaves like
FakeOpenWebRxServer, the client works; if not, adjust the protocol constants
in one place.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct

import numpy as np
import pytest
import websockets
from websockets.asyncio.server import Server, ServerConnection

from openwebrx_plus.sessions.receiver_session import (
    AUDIO_HEADER_MAGIC,
    FFT_HEADER_MAGIC,
    ReceiverSession,
)
from openwebrx_plus.sources import (
    RemoteDisplaySource,
    SourceRegistry,
    parse_openwebrx_url,
)
from openwebrx_plus.sources._adpcm import (
    AudioAdpcmSyncEncoder,
    FftAdpcmEncoder,
    ImaAdpcmCodec,
    decode_fft_adpcm,
)
from openwebrx_plus.sources.base import RemoteAudioFrame, RemoteFftFrame

# ---------------------------------------------------------------------------
# The fake OpenWebRX(+) server
# ---------------------------------------------------------------------------


class FakeOpenWebRxServer:
    """Minimal but protocol-faithful OpenWebRX+ receiver.

    Spectrum model: noise floor at ``floor_db`` with a ``peak_db`` bump at
    the bin matching the client's current ``offset_freq`` — tune and the
    peak moves, which is exactly what the integration tests assert.
    """

    def __init__(
        self,
        fft_size: int = 256,
        samp_rate: int = 240_000,
        center_freq: int = 3_568_000,
        start_freq: int = 3_570_000,
        start_mod: str = "lsb",
        floor_db: float = -100.0,
        peak_db: float = -55.0,
        send_backoff: bool = False,
        frame_interval: float = 0.05,
    ) -> None:
        self.fft_size = fft_size
        self.samp_rate = samp_rate
        self.center_freq = center_freq
        self.start_freq = start_freq
        self.start_mod = start_mod
        self.floor_db = floor_db
        self.peak_db = peak_db
        self.send_backoff = send_backoff
        self.frame_interval = frame_interval

        self.handshakes: list[str] = []
        self.client_messages: list[dict] = []
        self.params_history: list[dict] = []
        self.current_offset = 0
        self.requested_output_rate: int | None = None
        self.dsp_started = asyncio.Event()
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

    # -- helpers -------------------------------------------------------------

    def _peak_bin(self) -> int:
        """Bin index (DC-centered, swapped order) for the current offset."""
        fraction = (self.current_offset + self.samp_rate / 2) / self.samp_rate
        return int(round(fraction * self.fft_size)) % self.fft_size

    def _make_spectrum(self, frame_idx: int) -> np.ndarray:
        rng = np.random.default_rng(1234 + frame_idx)
        bins = self.floor_db + 1.5 * rng.standard_normal(self.fft_size)
        peak = self._peak_bin()
        for d in range(-3, 4):
            bins[(peak + d) % self.fft_size] += (
                self.peak_db - self.floor_db
            ) * np.exp(-(d**2) / 4.0)
        return bins.astype(np.float32)

    # -- protocol ------------------------------------------------------------

    async def _handler(self, ws: ServerConnection) -> None:
        # 1. handshake — the client must identify itself as a receiver
        first = await ws.recv()
        if not isinstance(first, str):
            await ws.close(code=1002)
            return
        self.handshakes.append(first)
        if not first.startswith("SERVER DE CLIENT") or "type=receiver" not in first:
            await ws.close(code=1002)
            return
        await ws.send("CLIENT DE SERVER server=openwebrx version=fake-1.2.3")

        # 2. the config burst (order mirrors owrx/connection.py)
        await ws.send(
            json.dumps(
                {
                    "type": "receiver_details",
                    "value": {
                        "receiver": {
                            "name": "Fake RX",
                            "location": {"latitude": 40.1, "longitude": -79.1},
                        }
                    },
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "type": "config",
                    "value": {
                        "fft_size": self.fft_size,
                        "fft_compression": "adpcm",
                        "audio_compression": "adpcm",
                        "max_clients": 4,
                        "waterfall_levels": [-90, -20],
                        "tuning_precision": 10,
                    },
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "type": "config",
                    "value": {
                        "samp_rate": self.samp_rate,
                        "center_freq": self.center_freq,
                        "start_freq": self.start_freq,
                        "start_mod": self.start_mod,
                        "initial_squelch_level": -150,
                        "sdr_id": "fake-sdr",
                        "profile_id": "p1",
                    },
                }
            )
        )
        await ws.send(json.dumps({"type": "features", "value": {"wsjt": False}}))
        await ws.send(
            json.dumps(
                {
                    "type": "modes",
                    "value": [
                        {
                            "modulation": "lsb",
                            "name": "LSB",
                            "type": "analog",
                            "squelch": True,
                            "bandpass": {"low_cut": -2750, "high_cut": -150},
                        },
                        {
                            "modulation": "usb",
                            "name": "USB",
                            "type": "analog",
                            "squelch": True,
                            "bandpass": {"low_cut": 150, "high_cut": 2750},
                        },
                        {
                            "modulation": "am",
                            "name": "AM",
                            "type": "analog",
                            "squelch": True,
                            "bandpass": {"low_cut": -4000, "high_cut": 4000},
                        },
                    ],
                }
            )
        )
        await ws.send(
            json.dumps({"type": "profiles", "value": [{"id": "p1", "name": "80 m"}]})
        )

        if self.send_backoff:
            await ws.send(json.dumps({"type": "backoff", "reason": "Too many clients"}))
            await ws.close()
            return

        pump = asyncio.create_task(self._pump(ws))
        try:
            async for message in ws:
                if not isinstance(message, str):
                    continue
                try:
                    msg = json.loads(message)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                self.client_messages.append(msg)
                if msg.get("type") == "dspcontrol":
                    if msg.get("action") == "start":
                        self.dsp_started.set()
                    params = msg.get("params")
                    if isinstance(params, dict):
                        self.params_history.append(dict(params))
                        if "offset_freq" in params:
                            self.current_offset = int(params["offset_freq"])
                elif msg.get("type") == "connectionproperties":
                    params = msg.get("params") or {}
                    rate = params.get("output_rate")
                    if isinstance(rate, int):
                        self.requested_output_rate = rate
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump

    async def _pump(self, ws: ServerConnection) -> None:
        await self.dsp_started.wait()
        fft_encoder = FftAdpcmEncoder(self.fft_size)
        audio_encoder = AudioAdpcmSyncEncoder()
        audio_rate = self.requested_output_rate or 12_000
        t = 0.0
        frame_idx = 0
        try:
            while True:
                if self.requested_output_rate:
                    audio_rate = self.requested_output_rate
                # FFT frame (tag 0x01)
                bins = self._make_spectrum(frame_idx)
                await ws.send(b"\x01" + fft_encoder.encode(bins))
                # audio frame (tag 0x02): 480-sample 700 Hz sine chunks
                n = 480
                times = t + np.arange(n) / audio_rate
                pcm = (10_000 * np.sin(2 * np.pi * 700.0 * times)).astype(np.int16)
                await ws.send(b"\x02" + audio_encoder.encode(pcm))
                t += n / audio_rate
                frame_idx += 1
                await asyncio.sleep(self.frame_interval)
        except websockets.exceptions.ConnectionClosed:
            return


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _collect(
    source: RemoteDisplaySource,
    *,
    want_fft: int = 1,
    want_audio: int = 1,
    budget_s: float = 6.0,
) -> tuple[list, list]:
    """Consume display_stream() until enough frames arrive (or budget spent)."""
    fft_frames: list = []
    audio_frames: list = []
    gen = source.display_stream()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_s
    try:
        while len(fft_frames) < want_fft or len(audio_frames) < want_audio:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                frame = await asyncio.wait_for(gen.__anext__(), timeout=remaining)
            except StopAsyncIteration:
                break
            if isinstance(frame, RemoteAudioFrame):
                audio_frames.append(frame)
            elif isinstance(frame, RemoteFftFrame):
                fft_frames.append(frame)
    finally:
        await gen.aclose()
    return fft_frames, audio_frames


async def _wait_for(predicate, budget_s: float = 3.0) -> None:
    """Poll until predicate() is true (server-side state visibility).

    The predicate reads the fake server's task-local state — there is no
    asyncio.Event to await, so polling is the honest primitive here.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    pytest.fail("condition not met within budget")


@pytest.fixture
async def fake_rx():
    server = FakeOpenWebRxServer()
    await server.start()
    yield server
    await server.stop()


# ---------------------------------------------------------------------------
# Deep-link parsing
# ---------------------------------------------------------------------------


def test_deeplink_boomerthedog():
    """The user's example URL parses end-to-end."""
    target = parse_openwebrx_url(
        "http://boomerthedog.com:8073/#freq=3570000,mod=lsb,sql=-150"
    )
    assert target.host == "boomerthedog.com"
    assert target.port == 8073
    assert target.use_tls is False
    assert target.freq == 3_570_000
    assert target.mod == "lsb"
    assert target.squelch == -150.0
    assert target.secondary_mod is None
    assert target.magic_key is None


def test_deeplink_variants():
    # https → wss, default https port, deep link with secondary mode + key
    t = parse_openwebrx_url("https://rx.example.com/#freq=14200000,mod=usb,"
                            "secondary_mod=ft8,sql=-120,key=hunter2")
    assert t.use_tls is True
    assert t.port == 443
    assert t.freq == 14_200_000
    assert t.mod == "usb"
    assert t.secondary_mod == "ft8"
    assert t.squelch == -120.0
    assert t.magic_key == "hunter2"

    # no fragment, no port → http default
    t = parse_openwebrx_url("http://rx.example.com/")
    assert t.port == 8073
    assert t.freq is None
    assert t.mod is None

    # bare host and ws:// scheme
    assert parse_openwebrx_url("boomerthedog.com:8073").port == 8073
    t = parse_openwebrx_url("ws://demo.example.org")
    assert t.use_tls is False
    assert t.port == 8073

    # unknown deep-link keys are tolerated
    t = parse_openwebrx_url("http://h/#freq=7100000,mod=am,step=2500")
    assert t.freq == 7_100_000
    assert t.mod == "am"

    # garbage freq / sql values are dropped, not fatal
    t = parse_openwebrx_url("http://h/#freq=abc,sql=xyz")
    assert t.freq is None
    assert t.squelch is None


def test_deeplink_rejects_garbage():
    with pytest.raises(ValueError):
        parse_openwebrx_url("")
    with pytest.raises(ValueError):
        parse_openwebrx_url("ftp://host/")
    with pytest.raises(ValueError):
        parse_openwebrx_url("http:///nohost/")


# ---------------------------------------------------------------------------
# ADPCM round-trips (both ports pinned against each other)
# ---------------------------------------------------------------------------


def test_fft_adpcm_roundtrip():
    """encode → decode must reproduce a smooth dB spectrum closely.

    The first ~10 kept bins still carry the pad-region convergence quirk
    (the JS decoder's step=0 reset vs the encoder's step 7 — inherent to the
    wire format, the browser has it too); steady-state accuracy is what
    matters.
    """
    fft_size = 1024
    rng = np.random.default_rng(7)
    original = (-90.0 + 30.0 * np.abs(np.fft.ifft(rng.standard_normal(fft_size)))).astype(
        np.float32
    )  # smooth-ish spectrum
    encoder = FftAdpcmEncoder(fft_size)
    wire = encoder.encode(original)
    assert len(wire) == (fft_size + 10) // 2
    decoded = decode_fft_adpcm(wire, fft_size)
    assert decoded.shape == (fft_size,)
    err = np.abs(decoded - original)
    assert np.mean(err) < 0.5
    assert np.max(err[10:]) < 2.0  # steady state: ADPCM is lossy but tame
    assert np.max(err) < 6.0  # pad transition stays bounded


def test_fft_adpcm_exact_frame_length():
    """The wire frame is (fft_size + 10) / 2 bytes — the client's decoder
    infers fft_size from it when the config is missing."""
    encoder = FftAdpcmEncoder(4096)
    wire = encoder.encode(np.zeros(4096, dtype=np.float32))
    assert len(wire) == (4096 + 10) // 2
    decoded = decode_fft_adpcm(wire)  # no fft_size hint
    assert len(decoded) == 4096


def test_audio_adpcm_roundtrip():
    """Sync-framed audio round-trips across chunk boundaries.

    The encoder carries odd tail samples across calls (like libcsdr's ring
    buffer), so awkward chunk sizes never drop or duplicate samples — the
    decoded stream stays sample-aligned with the original.
    """
    rng = np.random.default_rng(3)
    audio_rate = 12_000
    t = np.arange(audio_rate) / audio_rate
    original = (12_000 * np.sin(2 * np.pi * 700.0 * t) + 500 * rng.standard_normal(audio_rate))
    original = original.astype(np.int16)

    encoder = AudioAdpcmSyncEncoder()
    codec = ImaAdpcmCodec()
    decoded_chunks: list[np.ndarray] = []
    # encode in awkward chunk sizes to prove state persistence both ways
    for start in range(0, len(original), 977):
        wire = encoder.encode(original[start : start + 977])
        decoded_chunks.append(codec.decode_with_sync(wire))
    decoded = np.concatenate(decoded_chunks)
    # only the very last odd tail sample may be pending in the encoder
    assert abs(len(decoded) - len(original)) <= 1
    n = min(len(decoded), len(original))
    err = np.abs(decoded[:n].astype(np.int32) - original[:n].astype(np.int32))
    # IMA ADPCM on a ±12k sine ≈ 30 dB SNR → mean |err| ≈ 300; a structural
    # bug (nibble order, sync misalignment, dropped samples) gives > 5000.
    assert np.mean(err) < 450
    # the first ~20 samples carry the stream-start ramp-up (codec state
    # converging from (0,0) onto a full-scale sine — classic ADPCM attack
    # artifact; the periodic SYNC frames carry real state, so it never
    # recurs mid-stream). Steady state stays well below 3k.
    assert np.max(err[20:]) < 3_000


# ---------------------------------------------------------------------------
# Client ↔ fake server protocol
# ---------------------------------------------------------------------------


async def test_handshake_config_and_initial_tuning(fake_rx):
    source = RemoteDisplaySource(
        host="127.0.0.1", port=fake_rx.port, freq=3_570_000, mod="lsb", squelch=-150
    )
    fft, audio = await _collect(source, want_fft=2, want_audio=2)
    assert len(fft) >= 2 and len(audio) >= 2

    # server saw an honest handshake + rate request + dsp start
    assert fake_rx.handshakes[0].startswith("SERVER DE CLIENT")
    assert "type=receiver" in fake_rx.handshakes[0]
    types = [m.get("type") for m in fake_rx.client_messages]
    assert "connectionproperties" in types
    assert "dspcontrol" in types
    assert fake_rx.requested_output_rate == 12_000

    # config was captured (incl. both compression modes)
    assert source.remote_config["fft_size"] == 256
    assert source.remote_config["fft_compression"] == "adpcm"
    assert source.remote_config["audio_compression"] == "adpcm"
    assert source.remote_config["center_freq"] == 3_568_000
    assert source.server_version == "fake-1.2.3"
    assert source.receiver_details["receiver"]["name"] == "Fake RX"
    assert len(source.modes) == 3
    assert source.info.sample_rate == 240_000

    # initial tuning fired from the deep-link-style params:
    #   offset = 3,570,000 - 3,568,000 = +2,000 Hz, lsb, squelch -150
    assert any(
        p.get("offset_freq") == 2_000
        and p.get("mod") == "lsb"
        and p.get("squelch_level") == -150
        for p in fake_rx.params_history
    )
    # bandpass from the remote's own modes table (lsb: -2750..-150)
    assert any(p.get("low_cut") == -2750 and p.get("high_cut") == -150
               for p in fake_rx.params_history)
    assert source.tuned_freq == 3_570_000


async def test_frames_decode_correctly(fake_rx):
    source = RemoteDisplaySource(host="127.0.0.1", port=fake_rx.port)
    fft, audio = await _collect(source, want_fft=3, want_audio=3)

    frame = fft[0]
    assert frame.bins.dtype == np.float32
    assert len(frame.bins) == 256
    assert frame.center_freq == 3_568_000
    assert frame.sample_rate == 240_000
    assert frame.min_db == -90.0 and frame.max_db == -20.0
    # the synthetic peak (+35 dB over the floor) must survive the codec
    assert frame.bins.max() > frame.bins.min() + 20.0

    aframe = audio[0]
    assert aframe.pcm.dtype == np.int16
    assert aframe.sample_rate == 12_000
    assert len(aframe.pcm) == 480
    assert np.max(np.abs(aframe.pcm)) > 3_000  # 700 Hz sine at ±10k amplitude


class _StreamRecorder:
    """Keep a display_stream() running in the background, recording frames.

    Unlike the one-shot _collect helper, the stream stays open — needed for
    tests that drive tuning mid-stream.
    """

    def __init__(self, source: RemoteDisplaySource) -> None:
        self.source = source
        self.fft: list[RemoteFftFrame] = []
        self.audio: list[RemoteAudioFrame] = []
        self._gen = source.display_stream()
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> _StreamRecorder:
        async def run() -> None:
            try:
                async for frame in self._gen:
                    if isinstance(frame, RemoteAudioFrame):
                        self.audio.append(frame)
                    elif isinstance(frame, RemoteFftFrame):
                        self.fft.append(frame)
            except RuntimeError:
                raise

        self._task = asyncio.create_task(run())
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        with contextlib.suppress(Exception):
            await self._gen.aclose()

    async def wait_for_fft(self, count: int, budget_s: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget_s
        # Polling a list appended by the streaming task — there is no Event
        # to await, so a bounded sleep-loop is the pragmatic primitive.
        while len(self.fft) < count and loop.time() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.02)


async def test_tune_moves_the_peak(fake_rx):
    """dspcontrol tuning must move the remote's synthetic peak."""
    source = RemoteDisplaySource(host="127.0.0.1", port=fake_rx.port, freq=3_568_000)
    async with _StreamRecorder(source) as rec:
        await rec.wait_for_fft(2)
        assert rec.fft, "no FFT frames arrived"
        peak_before = int(np.argmax(rec.fft[0].bins))

        # tune +7 kHz (within the ±120 kHz span); the peak must follow
        await source.tune(3_575_000)
        await _wait_for(lambda: any(p.get("offset_freq") == 7_000
                                    for p in fake_rx.params_history))
        marker = len(rec.fft)
        await rec.wait_for_fft(marker + 2)
        peak_after = int(np.argmax(rec.fft[-1].bins))

        bin_hz = fake_rx.samp_rate / fake_rx.fft_size  # 937.5 Hz per bin
        expected_shift = int(round(7_000 / bin_hz))
        actual_shift = (peak_after - peak_before) % fake_rx.fft_size
        actual_shift = min(actual_shift, fake_rx.fft_size - actual_shift)
        assert abs(actual_shift - expected_shift) <= 2  # ±2 bins tolerance

        # mode switch forwards (and normalizes AM → am)
        await source.set_mode("AM")
        await _wait_for(lambda: any(p.get("mod") == "am" for p in fake_rx.params_history))
        await source.set_squelch(-100)
        await _wait_for(lambda: any(p.get("squelch_level") == -100
                                    for p in fake_rx.params_history))
        assert source.tuned_freq == 3_575_000


async def test_tune_clamps_to_passband(fake_rx):
    source = RemoteDisplaySource(host="127.0.0.1", port=fake_rx.port, freq=3_568_000)
    async with _StreamRecorder(source) as rec:
        await rec.wait_for_fft(1)
        await source.tune(30_000_000)  # way outside the ±120 kHz span
        await _wait_for(lambda: any(p.get("offset_freq") == 120_000
                                    for p in fake_rx.params_history))
        # clamped to +samp_rate/2, not the absurd 26 MHz offset
        assert source.tuned_freq == 3_568_000 + 120_000


async def test_backoff_is_a_clean_error():
    server = FakeOpenWebRxServer(send_backoff=True)
    await server.start()
    try:
        source = RemoteDisplaySource(host="127.0.0.1", port=server.port)
        with pytest.raises(RuntimeError, match="refused the connection"):
            await _collect(source, want_fft=1, want_audio=1, budget_s=3.0)
    finally:
        await server.stop()


async def test_connect_failure_is_clean():
    # nothing is listening on this port
    source = RemoteDisplaySource(host="127.0.0.1", port=1, connect_timeout=2.0)
    with pytest.raises(RuntimeError, match="cannot reach"):
        await _collect(source, want_fft=1, want_audio=1, budget_s=5.0)


# ---------------------------------------------------------------------------
# ReceiverSession integration (WRFO/AUDI wire formats, tuning plumbing)
# ---------------------------------------------------------------------------


async def test_session_display_path_end_to_end(fake_rx):
    """A remote receiver flows through a normal ReceiverSession: the frontend
    would see standard WRFO/AUDI frames with remote header values."""
    source = RemoteDisplaySource(
        url=f"http://127.0.0.1:{fake_rx.port}/#freq=3570000,mod=lsb,sql=-150"
    )
    session = ReceiverSession(
        receiver_id="rx-remote-test",
        source=source,  # type: ignore[arg-type]
        center_freq=3_568_000,
        sample_rate=240_000,
        mode="LSB",
    )
    await session.start()
    q = session.subscribe()

    got_fft = got_audio = False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 6.0
    while loop.time() < deadline and not (got_fft and got_audio):
        frame = await asyncio.wait_for(q.get(), timeout=deadline - loop.time())
        if frame[:4] == struct.pack("<I", FFT_HEADER_MAGIC):
            (magic, version, rx_hash, center, rate, min_db, max_db, bins) = struct.unpack(
                "<IIIffffI", frame[:32]
            )
            assert magic == FFT_HEADER_MAGIC and version == 1
            assert center == 3_568_000.0
            assert rate == 240_000.0
            assert bins == 256
            assert (len(frame) - 32) == 256 * 4
            got_fft = True
            # remote waterfall_levels win over session defaults
            assert min_db == -90.0 and max_db == -20.0
        elif frame[:4] == struct.pack("<I", AUDIO_HEADER_MAGIC):
            (magic, version, rate, count) = struct.unpack("<IIII", frame[:16])
            assert magic == AUDIO_HEADER_MAGIC and version == 1
            assert rate == 12_000  # remote output rate carried in the header
            assert count == 480
            assert len(frame) == 16 + 480 * 2
            got_audio = True

    assert got_fft and got_audio
    # session adopted the remote's parameters
    assert session.fft_size == 256
    assert session.min_db == -90.0 and session.max_db == -20.0
    assert session.display_frequency == 3_570_000  # deep-link tune applied

    # control plumbing: tune + mode forward to the remote
    await session.set_frequency(3_571_000)
    await _wait_for(lambda: any(p.get("offset_freq") == 3_000
                                for p in fake_rx.params_history))
    await session.set_mode("AM")
    await _wait_for(lambda: any(p.get("mod") == "am" for p in fake_rx.params_history))
    assert session.display_frequency == 3_571_000
    assert session.mode == "AM"

    await session.stop()
    # stopping released the receiver (server saw the close)
    await _wait_for(lambda: fake_rx.server is not None)
    # source no longer holds a connection
    assert source._connection is None


# ---------------------------------------------------------------------------
# Registry + directory wiring
# ---------------------------------------------------------------------------


def test_manifest_registered():
    manifest = SourceRegistry.get_manifest("openwebrx_remote")
    assert manifest is not None
    assert manifest.hardware_required is False
    assert manifest.factory_entrypoint == (
        "openwebrx_plus.sources.openwebrx_remote:RemoteDisplaySource"
    )
    types = {m.source_type for m in SourceRegistry.all_manifests()}
    assert "openwebrx_remote" in types


def test_registry_creates_from_url():
    source = SourceRegistry.create(
        "openwebrx_remote",
        url="http://boomerthedog.com:8073/#freq=3570000,mod=lsb,sql=-150",
    )
    assert isinstance(source, RemoteDisplaySource)
    assert source.host == "boomerthedog.com"
    assert source.port == 8073
    assert source.freq == 3_570_000
    assert source.mod == "lsb"
    assert source.squelch == -150.0
    assert source.info.type == "openwebrx_remote"


def test_receiverbook_entries_are_spawnable():
    """Directory entries → POST /api/receivers kwargs (ADR-006 wiring)."""
    from openwebrx_plus.sources.directory import _parse_receiverbook

    doc = {
        "receivers": [
            {"name": "Boomer's RX", "url": "http://boomerthedog.com:8073/",
             "lat": 40.5, "lon": -79.9},
        ]
    }
    entries = _parse_receiverbook(doc)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.source_type == "openwebrx_remote"
    # the URL a user would paste is exactly what spawns a receiver
    source = SourceRegistry.create("openwebrx_remote", url=entry.url)
    assert source.host == "boomerthedog.com"
    assert source.port == 8073
