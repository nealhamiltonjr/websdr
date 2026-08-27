"""VFO sub-receiver + IqHub tests (ADR-005).

Hardware-free: SimulatedSource parents (realtime=False for speed) feed
IqHubs; VfoTapSources extract slices via the real pycsdr Shift →
FirDecimate chain. Spectral assertions prove the DDC centers the wanted
signal at DC. REST tests cover parent/child lifecycle.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from openwebrx_plus.api.rest import create_app
from openwebrx_plus.config import Settings
from openwebrx_plus.sessions.receiver_session import ReceiverSession
from openwebrx_plus.sources import (
    IqHub,
    SimulatedSource,
    VfoTapSource,
    get_hub,
)
from openwebrx_plus.sources.wideband import destroy_hub, register_hub


def _make_parent(
    rate: int = 240_000, signal_set: str = "default", realtime: bool = False
) -> SimulatedSource:
    return SimulatedSource(
        signal_set=signal_set,  # type: ignore[arg-type]
        sample_rate=rate,
        chunk_size=8192,
        realtime=realtime,
    )


async def _start_hub(receiver_id: str, source: SimulatedSource, center: int) -> IqHub:
    hub = register_hub(IqHub(
        receiver_id=receiver_id,
        source=source,
        center_freq=center,
        sample_rate=source.sample_rate,
    ))
    await hub.start()
    return hub


async def _collect_tap(
    tap: VfoTapSource, center_freq: int, rate: int, min_samples: int,
    timeout_s: float = 10.0,
) -> np.ndarray:
    gen = tap.spawn(center_freq=center_freq, sample_rate=rate)
    parts: list[np.ndarray] = []
    try:
        async def _collect() -> None:
            async for frame in gen:
                parts.append(frame)
                if sum(p.size for p in parts) >= min_samples:
                    return

        await asyncio.wait_for(_collect(), timeout=timeout_s)
    finally:
        await gen.aclose()
    return np.concatenate(parts)


def _smoke_fixture() -> Path | None:
    import contextlib

    with contextlib.suppress(Exception):
        path = Path(__file__).resolve().parent.parent / "fixtures" / "iq" / "smoke.cf32"
        return path if path.exists() else None
    return None


def _dominant_freq(iq: np.ndarray, rate: int) -> float:
    n = 1 << int(np.log2(iq.size))
    spec = np.abs(np.fft.fft(iq[:n] * np.hanning(n)))
    freqs = np.fft.fftfreq(n, 1 / rate)
    return float(freqs[np.argmax(spec)])


# ---------------------------------------------------------------------------
# IqHub
# ---------------------------------------------------------------------------


class TestIqHub:
    async def test_fanout_two_subscribers_get_identical_chunks(self) -> None:
        hub = await _start_hub("rx-hub-test", _make_parent(), 7_100_000)
        try:
            # Subscribe both queues BEFORE pulling: hub.stream() generators
            # subscribe lazily on first __anext__, so pulling g1 first would
            # advance it ahead of g2 (late subscribers miss earlier chunks —
            # correct live-stream semantics, wrong for this assertion).
            q1 = hub.subscribe()
            q2 = hub.subscribe()
            c1 = await asyncio.wait_for(q1.get(), timeout=5.0)
            c2 = await asyncio.wait_for(q2.get(), timeout=5.0)
            np.testing.assert_array_equal(c1, c2)
            hub.unsubscribe(q1)
            hub.unsubscribe(q2)
        finally:
            await hub.stop()

    async def test_stop_ends_subscriber_streams(self) -> None:
        hub = await _start_hub("rx-hub-test2", _make_parent(), 7_100_000)
        gen = hub.stream()
        await gen.__anext__()
        await hub.stop()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), timeout=2.0)

    async def test_subscriber_budget_enforced(self) -> None:
        hub = IqHub(
            receiver_id="rx-budget",
            source=_make_parent(),
            center_freq=7_100_000,
            sample_rate=240_000,
            max_subscribers=2,
        )
        hub.subscribe()
        hub.subscribe()
        with pytest.raises(RuntimeError, match="subscriber budget"):
            hub.subscribe()

    async def test_drop_oldest_under_backpressure(self) -> None:
        """A slow subscriber must not block the pump; oldest chunks drop."""
        hub = IqHub(
            receiver_id="rx-drop",
            source=_make_parent(),
            center_freq=7_100_000,
            sample_rate=240_000,
            queue_size=4,
        )
        await hub.start()
        try:
            q = hub.subscribe()
            # Let the pump run ahead (fast source, tiny queue).
            await asyncio.sleep(0.2)
            assert q.qsize() <= 4
            assert hub.dropped_chunks > 0
        finally:
            await hub.stop()


# ---------------------------------------------------------------------------
# VfoTapSource
# ---------------------------------------------------------------------------


class TestVfoTap:
    async def test_tap_centers_signal_at_dc(self) -> None:
        """'ham_band' preset: CW carrier at −30 kHz offset → tap there, and
        the CW carrier must land at DC in the VFO domain.

        The parent is REAL-TIME paced: an unpaced source outruns the tap's
        Shift+FirDecimate chain, the hub queue drops chunks, and the phase
        gaps splatter the spectrum (correct backpressure, wrong for this
        spectral assertion)."""
        parent = _make_parent(signal_set="ham_band", realtime=True)
        hub = await _start_hub("rx-vfo-parent", parent, 7_100_000)
        try:
            tap = VfoTapSource(parent_receiver_id="rx-vfo-parent")
            iq = await _collect_tap(tap, 7_100_000 - 30_000, 12_000, 12_288)
            assert iq.dtype == np.complex64
            # Decimation 240k/12k = 20; CW carrier → dominant tone at ~0 Hz.
            # Amplitude check: the 0.6-amp carrier must SURVIVE — an aliased
            # out-of-span remnant would peak near 0 Hz at ~1e-4 amplitude.
            assert abs(_dominant_freq(iq, 12_000)) < 100
            assert 0.3 < float(np.mean(np.abs(iq))) < 0.9
        finally:
            await hub.stop()

    async def test_two_concurrent_vfos(self) -> None:
        parent = _make_parent(signal_set="ham_band", realtime=True)
        hub = await _start_hub("rx-vfo-parent2", parent, 7_100_000)
        try:
            tap_a = VfoTapSource(parent_receiver_id="rx-vfo-parent2")
            tap_b = VfoTapSource(parent_receiver_id="rx-vfo-parent2")
            # ham_band "SSB" carriers are amp × sin(2π·f_audio·t) — no DC
            # component. Tap A (+25 kHz, 1100 Hz tone) → tone at ±1100 Hz.
            # Tap B (0 Hz, 750 Hz tone) → tone at ±750 Hz. Collect concurrently.
            iq_a, iq_b = await asyncio.gather(
                _collect_tap(tap_a, 7_100_000 + 25_000, 12_000, 12_288),
                _collect_tap(tap_b, 7_100_000, 12_000, 12_288),
            )
            assert abs(abs(_dominant_freq(iq_a, 12_000)) - 1_100) < 150
            assert abs(abs(_dominant_freq(iq_b, 12_000)) - 750) < 100
            # The 0.55-amp SSB tone survives tap_b's DDC (mean |sin| ≈ 0.35).
            assert 0.2 < float(np.mean(np.abs(iq_b))) < 0.9
        finally:
            await hub.stop()

    async def test_non_integer_decimation_rejected(self) -> None:
        parent = _make_parent(rate=250_000)
        hub = await _start_hub("rx-vfo-parent3", parent, 7_100_000)
        try:
            tap = VfoTapSource(parent_receiver_id="rx-vfo-parent3")
            gen = tap.spawn(center_freq=7_100_000, sample_rate=12_000)  # 250k/12k not integer
            with pytest.raises(ValueError, match="divisible"):
                await gen.__anext__()
        finally:
            await hub.stop()

    async def test_out_of_span_rejected(self) -> None:
        parent = _make_parent()
        hub = await _start_hub("rx-vfo-parent4", parent, 7_100_000)
        try:
            tap = VfoTapSource(parent_receiver_id="rx-vfo-parent4")
            gen = tap.spawn(center_freq=7_100_000 + 130_000, sample_rate=12_000)
            with pytest.raises(ValueError, match="outside the parent span"):
                await gen.__anext__()
        finally:
            await hub.stop()

    async def test_missing_parent_rejected(self) -> None:
        tap = VfoTapSource(parent_receiver_id="rx-never-started")
        gen = tap.spawn(center_freq=7_100_000, sample_rate=12_000)
        with pytest.raises(RuntimeError, match="not streaming"):
            await gen.__anext__()


# ---------------------------------------------------------------------------
# ReceiverSession + hub integration
# ---------------------------------------------------------------------------


class TestSessionHubIntegration:
    async def test_session_streams_through_hub(self) -> None:
        session = ReceiverSession(
            receiver_id="rx-hub-session",
            source=_make_parent(),
            center_freq=7_100_000,
            sample_rate=240_000,
            mode="USB",
        )
        await session.start()
        try:
            hub = get_hub("rx-hub-session")
            assert hub is not None
            # Let the _run task take its first tick and subscribe.
            await asyncio.sleep(0.1)
            assert hub.subscriber_count >= 1  # the session itself
        finally:
            await session.stop()
        assert get_hub("rx-hub-session") is None

    async def test_file_session_adopts_fixed_rate(self) -> None:
        """A FileSource session adopts the recording's rate/center even when
        constructed with mismatched values."""
        smoke = _smoke_fixture()
        if smoke is None:
            pytest.skip("fixture not baked")
        from openwebrx_plus.sources import FileSource

        session = ReceiverSession(
            receiver_id="rx-file-adopt",
            source=FileSource(file_path=smoke, realtime=False),
            center_freq=999_000_000,  # deliberately wrong
            sample_rate=1_000_000,
        )
        await session.start()
        try:
            assert session.sample_rate == 250_000
            assert session.center_freq == 100_000_000
        finally:
            await session.stop()

    async def test_destroying_parent_ends_child_stream(self) -> None:
        """Parent stop → hub sentinel → VFO tap stream ends gracefully."""
        parent = ReceiverSession(
            receiver_id="rx-parent-sess",
            source=_make_parent(),
            center_freq=7_100_000,
            sample_rate=240_000,
        )
        await parent.start()
        tap = VfoTapSource(parent_receiver_id="rx-parent-sess")
        gen = tap.spawn(center_freq=7_100_000 - 30_000, sample_rate=12_000)
        first = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
        assert first.dtype == np.complex64
        await parent.stop()
        with pytest.raises((StopAsyncIteration, asyncio.TimeoutError)):
            await asyncio.wait_for(gen.__anext__(), timeout=3.0)


# ---------------------------------------------------------------------------
# REST: VFO lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    settings = Settings(tier="dev")
    app = create_app(settings)
    return TestClient(app)


class TestVfoRest:
    def test_spawn_vfo_child(self, client: TestClient) -> None:
        r = client.post(
            "/api/receivers",
            json={
                "receiver_id": "rx-wide-parent",
                "source_type": "simulated",
                "source_kwargs": {"signal_set": "default", "realtime": False},
                "center_freq": 7_100_000,
                "sample_rate": 240_000,
            },
        )
        assert r.status_code == 201, r.text

        r = client.post(
            "/api/receivers",
            json={
                "receiver_id": "rx-vfo-child",
                "source_type": "vfo",
                "source_kwargs": {"parent_receiver_id": "rx-wide-parent"},
                "center_freq": 7_100_000 - 37_500,
                "sample_rate": 12_000,
                "mode": "CW",
            },
        )
        assert r.status_code == 201, r.text

        r = client.get("/api/receivers")
        sessions = {s["receiver_id"]: s for s in r.json()}
        assert sessions["rx-vfo-child"]["source"]["type"] == "vfo"

        # Cleanup child then parent.
        assert client.delete("/api/receivers/rx-vfo-child").status_code == 204
        assert client.delete("/api/receivers/rx-wide-parent").status_code == 204

    def test_vfo_without_parent_kwarg_400(self, client: TestClient) -> None:
        r = client.post("/api/receivers", json={"source_type": "vfo"})
        assert r.status_code == 400
        assert "parent_receiver_id" in r.json()["detail"]

    def test_vfo_with_unknown_parent_400(self, client: TestClient) -> None:
        r = client.post(
            "/api/receivers",
            json={
                "source_type": "vfo",
                "source_kwargs": {"parent_receiver_id": "rx-nope"},
            },
        )
        assert r.status_code == 400
        assert "not found" in r.json()["detail"]

    def test_vfo_on_unstarted_parent_400(self, client: TestClient) -> None:
        """Sessions created directly (not via POST) have no hub until
        started — the registry must reject VFO children of those."""
        from openwebrx_plus.sessions import create_session

        create_session(receiver_id="rx-unstarted-parent", source_type="simulated")
        try:
            r = client.post(
                "/api/receivers",
                json={
                    "source_type": "vfo",
                    "source_kwargs": {"parent_receiver_id": "rx-unstarted-parent"},
                },
            )
            assert r.status_code == 400
            assert "not streaming" in r.json()["detail"]
        finally:
            import asyncio

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(
                    destroy_hub("rx-unstarted-parent")
                )
            finally:
                loop.close()

    def _spawn_streaming_parent(self, client: TestClient, rid: str) -> None:
        """POST a parent and start it so its hub exists."""
        r = client.post(
            "/api/receivers",
            json={
                "receiver_id": rid,
                "source_type": "simulated",
                "source_kwargs": {"signal_set": "default", "realtime": False},
                "center_freq": 7_100_000,
                "sample_rate": 240_000,
            },
        )
        assert r.status_code == 201, r.text
        # POST /api/receivers already awaited session.start() → hub exists.

    def test_vfo_indivisible_rate_400(self, client: TestClient) -> None:
        """240 kHz parent + 25 kHz child = 9.6 decimation → must fail fast
        with 400 (not 201 + async hub-pump death)."""
        import asyncio

        from openwebrx_plus.sessions import destroy_session

        self._spawn_streaming_parent(client, "rx-odd-rate-parent")
        try:
            r = client.post(
                "/api/receivers",
                json={
                    "source_type": "vfo",
                    "source_kwargs": {"parent_receiver_id": "rx-odd-rate-parent"},
                    "center_freq": 7_100_000,
                    "sample_rate": 25_000,  # 240_000 % 25_000 == 15_000
                },
            )
            assert r.status_code == 400, r.text
            assert "divisible" in r.json()["detail"]
        finally:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(destroy_session("rx-odd-rate-parent"))
            finally:
                loop.close()

    def test_vfo_slice_outside_parent_band_400(self, client: TestClient) -> None:
        """VFO center outside ±parent_rate/2 must fail fast with 400."""
        import asyncio

        from openwebrx_plus.sessions import destroy_session

        self._spawn_streaming_parent(client, "rx-band-parent")
        try:
            r = client.post(
                "/api/receivers",
                json={
                    "source_type": "vfo",
                    "source_kwargs": {"parent_receiver_id": "rx-band-parent"},
                    "center_freq": 7_100_000 + 400_000,  # > ±120 kHz
                    "sample_rate": 12_000,
                },
            )
            assert r.status_code == 400, r.text
            assert "outside" in r.json()["detail"]
        finally:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(destroy_session("rx-band-parent"))
            finally:
                loop.close()


# Keep get_or_create_hub referenced (session integration uses it).
_ = ReceiverSession  # noqa: F841 — imports kept stable for future assertions
