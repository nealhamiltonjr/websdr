"""Slice-4.7 — per-receiver gain + DSP-mode controls (de-stubbed setGain /
setDSPMode).

Covers:
  - Digital runtime gain on SimulatedSource + FileSource (10^(dB/20) scaling)
  - ReceiverSession.set_gain: validation against the manifest gain range,
    pre-start storage (flows into source.spawn via the IqHub), live
    application, auto (None) reset, display-stream rejection
  - ReceiverSession.set_dsp_mode: raw/classic accepted + chain rebuilt with
    the right conditioning topology; ai/cascade accepted since slice-10
    (in-process AIDenoiser); unknown modes rejected
  - set_mode chain-rebuild regression (the slice-4.7 bug fix: a mode switch
    used to only update the metadata echo while the original demodulator
    kept running)
  - rtl_tcp runtime gain over the wire (fake server asserts the 0x03/0x04
    command bytes) incl. latest-wins queue semantics
  - End-to-end WS: setGain/setDSPMode control frames → metadata echo with
    gain/dspMode/gainRange/supportsAgc → REST /api/receivers reflection,
    plus error frames for out-of-range gain and unavailable AI modes
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from openwebrx_plus.api.rest import create_app
from openwebrx_plus.config import Settings
from openwebrx_plus.sessions import create_session, destroy_session
from openwebrx_plus.sessions.receiver_session import ReceiverSession
from openwebrx_plus.sources.file_source import FileSource
from openwebrx_plus.sources.simulated import SimulatedSource

from .test_rtl_sdr_driver import _start_fake_rtl_tcp

# ---------------------------------------------------------------------------
# Digital runtime gain on the sources
# ---------------------------------------------------------------------------


class TestSimulatedSourceGain:
    async def test_gain_scales_output_amplitude(self) -> None:
        # Two IDENTICAL instances (same seed/params) produce identical
        # deterministic streams — compare chunk k of the gained source
        # against chunk k of the ungained one.
        src = SimulatedSource(realtime=False, chunk_size=4096, seed=7)
        ref = SimulatedSource(realtime=False, chunk_size=4096, seed=7)
        gen = src.spawn(center_freq=14_205_000, sample_rate=2_400_000, gain=None)
        gen_ref = ref.spawn(center_freq=14_205_000, sample_rate=2_400_000, gain=None)
        try:
            await gen.__anext__()  # warm up (chunk 0, no gain)
            await gen_ref.__anext__()
            # +6 dB ≈ ×2 amplitude.
            assert src.set_runtime_gain(6.0) is True
            boosted = await gen.__anext__()
            base = await gen_ref.__anext__()
            np.testing.assert_allclose(
                boosted, base * np.float32(10.0 ** (6.0 / 20.0)), rtol=1e-4
            )
            # auto (None) → back to unit gain.
            assert src.set_runtime_gain(None) is True
            restored = await gen.__anext__()
            base2 = await gen_ref.__anext__()
            np.testing.assert_allclose(restored, base2, rtol=1e-4)
        finally:
            await gen.aclose()
            await gen_ref.aclose()

    async def test_spawn_time_gain_seeds_runtime_gain(self) -> None:
        src = SimulatedSource(realtime=False)
        gen = src.spawn(center_freq=14_205_000, sample_rate=2_400_000, gain=-6.0)
        try:
            await gen.__anext__()
            assert src._runtime_gain_db == -6.0
        finally:
            await gen.aclose()


class TestFileSourceGain:
    async def test_gain_scales_replayed_samples(self, tmp_path: Path) -> None:
        iq = (0.1 + 0.2j) * np.ones(2048, dtype=np.complex64)
        path = tmp_path / "gain_test.cf32"
        path.write_bytes(iq.tobytes())
        src = FileSource(file_path=path, realtime=False, chunk_size=1024)
        gen = src.spawn(center_freq=14_205_000, sample_rate=2_400_000, gain=None)
        try:
            base = await gen.__anext__()
            assert src.set_runtime_gain(20.0) is True
            boosted = await gen.__anext__()
            np.testing.assert_allclose(
                boosted, base * np.float32(10.0), rtol=1e-4
            )
        finally:
            await gen.aclose()


# ---------------------------------------------------------------------------
# ReceiverSession.set_gain
# ---------------------------------------------------------------------------


async def _drain_sessions(*ids: str) -> None:
    for rid in ids:
        await destroy_session(rid)


class TestSessionSetGain:
    async def test_live_gain_on_started_session(self) -> None:
        session = create_session(
            receiver_id="rx-gain-live",
            source_type="simulated",
            source_kwargs={"realtime": False},
        )
        try:
            await session.start()
            # Simulated advertises ±20 dB — inside range applies.
            applied, reason = await session.set_gain(6.0)
            assert applied, reason
            assert session.gain == 6.0
            assert session.source._runtime_gain_db == 6.0  # type: ignore[attr-defined]
            # Out of range → rejected, state unchanged.
            applied, reason = await session.set_gain(25.0)
            assert not applied
            assert "outside the simulated range" in reason
            assert session.gain == 6.0
            # auto → None.
            applied, reason = await session.set_gain(None)
            assert applied, reason
            assert session.gain is None
            assert session.source._runtime_gain_db is None  # type: ignore[attr-defined]
        finally:
            await _drain_sessions("rx-gain-live")

    async def test_pre_start_gain_flows_through_hub_spawn(self) -> None:
        session = create_session(
            receiver_id="rx-gain-prestart",
            source_type="simulated",
            source_kwargs={"realtime": False},
        )
        try:
            applied, reason = await session.set_gain(-4.5)
            assert applied, reason
            await session.start()
            # Let the hub's pump task run: it calls source.spawn(...) (which
            # seeds the runtime gain on first iteration).
            for _ in range(50):
                if session.source._runtime_gain_db is not None:  # type: ignore[attr-defined]
                    break
                await asyncio.sleep(0.02)
            # The hub passed session.gain into source.spawn().
            assert session.source._runtime_gain_db == -4.5  # type: ignore[attr-defined]
        finally:
            await _drain_sessions("rx-gain-prestart")

    async def test_source_without_runtime_gain_rejected(self) -> None:
        # A bare ReceiverSession whose source has info but no
        # set_runtime_gain (e.g. a VFO tap).
        from openwebrx_plus.sources.base import SourceInfo

        class _NoGainSource:
            info = SourceInfo(type="vfo", label="stub")  # type: ignore[arg-type]

            async def spawn(self, *args: object, **kwargs: object) -> object:
                raise NotImplementedError

            async def close(self) -> None:
                return None

        session = ReceiverSession(receiver_id="rx-gain-nocap", source=_NoGainSource())
        # Not started → accepted (stored for spawn time).
        applied, _ = await session.set_gain(3.0)
        assert applied
        # Started (fake it) + no capability → rejected.
        session._stream_task = asyncio.current_task()
        try:
            applied, reason = await session.set_gain(4.0)
            assert not applied
            assert "no runtime gain control" in reason
        finally:
            session._stream_task = None


# ---------------------------------------------------------------------------
# ReceiverSession.set_dsp_mode + the set_mode rebuild fix
# ---------------------------------------------------------------------------


class TestSessionDspMode:
    async def test_raw_classic_rebuild_the_chain(self) -> None:
        session = create_session(
            receiver_id="rx-dsp-mode",
            source_type="simulated",
            source_kwargs={"realtime": False},
        )
        try:
            await session.start()
            assert session._audio_chain is not None
            assert session._audio_chain.conditioning is True

            applied, reason = await session.set_dsp_mode("raw")
            assert applied, reason
            assert session.dsp_mode == "raw"
            assert session._audio_chain.conditioning is False

            # Same mode → no-op success.
            applied, reason = await session.set_dsp_mode("raw")
            assert applied, reason

            applied, reason = await session.set_dsp_mode("classic")
            assert applied, reason
            assert session._audio_chain.conditioning is True
        finally:
            await _drain_sessions("rx-dsp-mode")

    async def test_ai_and_cascade_accepted_with_denoiser(self) -> None:
        """Slice-10: ai/cascade are now LIVE — the in-process AIDenoiser
        (Stage 2a, spectral subtraction) ships. The denoiser is built
        + reset on entry, dropped on exit."""
        session = create_session(
            receiver_id="rx-dsp-ai",
            source_type="simulated",
            source_kwargs={"realtime": False},
        )
        try:
            for mode in ("ai", "cascade"):
                applied, _ = await session.set_dsp_mode(mode)
                assert applied, f"mode {mode!r} should be accepted (slice-10 gate flipped)"
                assert session._ai_denoiser is not None, (
                    f"denoiser must be instantiated when entering {mode!r}"
                )
            # Switching back to raw/classic drops the denoiser (no overhead
            # on the wire path).
            applied, _ = await session.set_dsp_mode("raw")
            assert applied
            assert session._ai_denoiser is None
            # Unknown mode is still rejected.
            applied, reason = await session.set_dsp_mode("turbo")
            assert not applied
            assert "unknown DSP mode" in reason
        finally:
            await _drain_sessions("rx-dsp-ai")

    async def test_set_mode_rebuilds_demodulator(self) -> None:
        """Regression (slice-4.7 fix): a mode switch used to leave the
        ORIGINAL demodulator running — only the metadata echo changed."""
        session = create_session(
            receiver_id="rx-mode-rebuild",
            source_type="simulated",
            source_kwargs={"realtime": False},
        )
        try:
            await session.start()
            assert session._audio_chain is not None
            assert session._audio_chain.mode == "USB"
            applied = await session.set_mode("AM")
            assert applied is True
            assert session._audio_chain.mode == "AM"
            assert session._audio_chain.conditioning is True
            # Mode change must preserve the DSP mode.
            await session.set_dsp_mode("raw")
            applied = await session.set_mode("NFM")
            assert applied is True
            assert session._audio_chain.mode == "NFM"
            assert session._audio_chain.conditioning is False
        finally:
            await _drain_sessions("rx-mode-rebuild")


# ---------------------------------------------------------------------------
# rtl_tcp runtime gain over the wire
# ---------------------------------------------------------------------------


class TestRtlTcpRuntimeGain:
    async def test_runtime_gain_sends_wire_commands(self) -> None:
        from openwebrx_plus.sources import RtlTcpSource

        server, commands, port = await _start_fake_rtl_tcp(bytes(256))
        try:
            src = RtlTcpSource(host="127.0.0.1", port=port, chunk_size=16)
            gen = src.spawn(center_freq=7_100_000, sample_rate=250_000, gain=None)
            try:
                await gen.__anext__()
                # Auto at connect.
                await asyncio.sleep(0.25)
                assert (0x03, 1) in commands

                assert src.set_runtime_gain(32.5) is True
                await gen.__anext__()
                await asyncio.sleep(0.25)
                assert (0x03, 0) in commands  # manual mode
                assert (0x04, 325) in commands  # 32.5 dB → 325 tenths

                # Back to auto.
                assert src.set_runtime_gain(None) is True
                await gen.__anext__()
                await asyncio.sleep(0.25)
                # The latest auto request re-enabled tuner AGC.
                assert commands.count((0x03, 1)) >= 2
            finally:
                await gen.aclose()
        finally:
            server.close()
            await server.wait_closed()

    async def test_latest_wins_queue_semantics(self) -> None:
        server, commands, port = await _start_fake_rtl_tcp(bytes(256))
        try:
            from openwebrx_plus.sources import RtlTcpSource

            src = RtlTcpSource(host="127.0.0.1", port=port, chunk_size=16)
            gen = src.spawn(center_freq=7_100_000, sample_rate=250_000, gain=None)
            try:
                await gen.__anext__()
                # Two rapid requests before the stream loop drains the queue:
                # only the last may survive.
                assert src.set_runtime_gain(10.0) is True
                assert src.set_runtime_gain(20.0) is True
                await gen.__anext__()
                await asyncio.sleep(0.25)
                assert (0x04, 200) in commands  # 20 dB
                assert (0x04, 100) not in commands  # stale 10 dB dropped
            finally:
                await gen.aclose()
        finally:
            server.close()
            await server.wait_closed()


# ---------------------------------------------------------------------------
# End-to-end over the WebSocket (control frames → metadata echo → REST)
# ---------------------------------------------------------------------------


def _recv_json(ws, predicate, tries: int = 80):
    """Scan incoming WS frames until a JSON text frame matches predicate.

    Uses the RAW ``ws.receive()`` (message dicts) instead of
    receive_text/receive_bytes: starlette's TestClient CONSUMES a frame and
    raises KeyError on a type mismatch, so naive drain patterns silently eat
    the frames they're looking for (found while debugging this very test).
    The pump alternates [binary, text] pairs and error frames interleave —
    scanning with a generous bound is the honest way through.
    """
    for _ in range(tries):
        msg = ws.receive()
        # This starlette/httpx build wraps server frames as
        # {"type": "websocket.send", "text"|"bytes": ...} — discriminate on
        # the payload key, not the type field.
        if "text" not in msg:
            continue  # binary FFT/audio frame — dropped deliberately
        try:
            payload = json.loads(msg["text"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and predicate(payload):
            return payload
    raise AssertionError("expected JSON frame not received within bound")


class TestWsGainDspEndToEnd:
    def test_control_frames_echo_in_metadata_and_rest(self) -> None:
        # Use a DEDICATED receiver (not the global rx-default): this test
        # starts the session's stream task inside TestClient's portal loop,
        # and leaving a frozen task bound to that (now closed) loop under
        # the global rx-default id would starve later WS tests
        # (test_ws_fft_frame). Spawning + destroying here keeps the whole
        # lifecycle inside this test's live loop.
        from openwebrx_plus.sessions.receiver_session import _default_fixture_path

        fixture = _default_fixture_path(Settings())
        app = create_app(Settings(tier="dev"))
        with TestClient(app) as client:
            spawn = client.post(
                "/api/receivers",
                json={
                    "receiver_id": "rx-gain-e2e",
                    "source_type": "file",
                    "source_kwargs": {"file_path": str(fixture)},
                    "center_freq": 14_150_000,
                    "sample_rate": 250_000,
                },
            )
            assert spawn.status_code == 201, spawn.text
            try:
                with client.websocket_connect("/ws/rx-gain-e2e") as ws:
                    # Baseline metadata: the file source advertises digital
                    # gain ±20 dB, no AGC.
                    meta = _recv_json(ws, lambda p: p.get("type") == "metadata")
                    assert meta["gain"] is None
                    assert meta["dspMode"] == "classic"
                    assert meta["source"]["gainRange"] == [-20.0, 20.0]
                    assert meta["source"]["supportsAgc"] is False

                    # Manual gain: +6 dB.
                    ws.send_text(
                        json.dumps(
                            {
                                "type": "control",
                                "receiverId": "rx-gain-e2e",
                                "command": "setGain",
                                "value": 6,
                            }
                        )
                    )
                    meta = _recv_json(
                        ws, lambda p: p.get("type") == "metadata" and p.get("gain") == 6.0
                    )
                    assert meta["dspMode"] == "classic"

                    # DSP mode: raw.
                    ws.send_text(
                        json.dumps(
                            {
                                "type": "control",
                                "receiverId": "rx-gain-e2e",
                                "command": "setDSPMode",
                                "value": "raw",
                            }
                        )
                    )
                    meta = _recv_json(
                        ws, lambda p: p.get("type") == "metadata" and p.get("dspMode") == "raw"
                    )
                    assert meta["gain"] == 6.0

                    # Out-of-range gain → error frame, state unchanged.
                    ws.send_text(
                        json.dumps(
                            {
                                "type": "control",
                                "receiverId": "rx-gain-e2e",
                                "command": "setGain",
                                "value": 99,
                            }
                        )
                    )
                    err = _recv_json(ws, lambda p: p.get("type") == "error")
                    assert err["command"] == "setGain"
                    assert "outside the file range" in err["message"]

                    # AI mode → honest rejection.
                    ws.send_text(
                        json.dumps(
                            {
                                "type": "control",
                                "receiverId": "rx-gain-e2e",
                                "command": "setDSPMode",
                                "value": "ai",
                            }
                        )
                    )
                    err = _recv_json(ws, lambda p: p.get("type") == "error")
                    assert err["command"] == "setDSPMode"
                    assert "DeepFilterNet" in err["message"]

                # REST reflects the same control state (checked before the
                # teardown delete).
                listed = client.get("/api/receivers").json()
                match = next(
                    (r for r in listed if r["receiver_id"] == "rx-gain-e2e"), None
                )
                assert match is not None, listed
                assert match["gain"] == 6.0
                assert match["dsp_mode"] == "raw"
            finally:
                # Destroy while the portal loop is still alive so the
                # session stops cleanly (no frozen tasks).
                deleted = client.delete("/api/receivers/rx-gain-e2e")
                assert deleted.status_code == 204
