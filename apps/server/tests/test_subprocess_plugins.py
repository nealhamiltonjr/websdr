"""Subprocess decoder plugin tests — ADR-003's second family (slice-4.9).

Drives the generic PluginRunner + the dump1090 plugin against a
protocol-faithful fake binary (``tests/fakes/fake_dump1090.py``) which
implements the pinned contract: IQ on stdin (cf32/cs16/cu8), NDJSON on
stdout, optional ``{"kind": "ready"}`` handshake, plus deliberate
failure modes (crash / garbage / stall) for the runner's restart and
backpressure policies.

The fake demodulates with the REAL production Mode S receiver, so the
fixture oracle from test_adsb_decoder.py applies here too: 14 CRC-valid
frames, 3 aircraft with callsigns + 2 distant fragments.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from openwebrx_plus.api.rest import create_app
from openwebrx_plus.config import Settings
from openwebrx_plus.plugins import decoder_registry
from openwebrx_plus.plugins.base import DecoderAttachContext, DecoderAttachError
from openwebrx_plus.plugins.dump1090 import Dump1090Plugin
from openwebrx_plus.plugins.subprocess import DecoderBinaryMissing, SubprocessSpec, iq_to_bytes

FAKE = Path(__file__).resolve().parent / "fakes" / "fake_dump1090.py"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "iq" / "adsb_1090.cf32"

AIRCRAFT = {"4D22AA": "OWRX001", "3C70EE": "N42OWRX", "06A1B2": "OPENWEB1"}

CTX = DecoderAttachContext(
    receiver_id="rx-subproc-test", sample_rate=2_000_000, center_freq=1_090_000_000
)


def _spec(*extra_args: str, **kw: Any) -> SubprocessSpec:
    return SubprocessSpec(
        argv=(sys.executable, str(FAKE), *extra_args),
        iq_format=kw.pop("iq_format", "cs16"),
        ready_timeout=kw.pop("ready_timeout", 5.0),
        restart_backoff=kw.pop("restart_backoff", (0.5, 2.0, 8.0)),
        max_buffered_bytes=kw.pop("max_buffered_bytes", 64 * 1024 * 1024),
    )


def _plugin(*extra_args: str, **kw: Any) -> Dump1090Plugin:
    return Dump1090Plugin(spec_override=_spec(*extra_args, **kw))


def _fixture_iq() -> np.ndarray:
    return np.fromfile(FIXTURE, dtype=np.complex64)


_EMPTY = np.zeros(0, dtype=np.complex64)


async def _feed(
    plugin: Dump1090Plugin, iq: np.ndarray, *, chunk: int = 65536, delay: float = 0.02
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for k in range(0, iq.size, chunk):
        events.extend(plugin.feed_iq(iq[k : k + chunk]))
        await asyncio.sleep(delay)
    return events


async def _poll(
    plugin: Dump1090Plugin, predicate: Any, deadline_secs: float = 4.0
) -> list[dict[str, Any]]:
    """Drain events (empty feeds) until predicate(events) is true."""
    events: list[dict[str, Any]] = []
    deadline = time.monotonic() + deadline_secs
    while time.monotonic() < deadline:
        events.extend(plugin.feed_iq(_EMPTY))
        if predicate(events):
            break
        await asyncio.sleep(0.05)
    return events


def _snapshot_has_callsigns(event: dict[str, Any]) -> bool:
    """True once every fixture aircraft reported its callsign (the DF17
    callsign frames sit at 0.73 s — DF11 all-calls appear much earlier)."""
    if event.get("kind") != "aircraft":
        return False
    table = {a["icao"]: a for a in event["aircraft"]}
    return all(
        icao in table and table[icao]["callsign"] == callsign
        for icao, callsign in AIRCRAFT.items()
    )


# ---------------------------------------------------------------------------
# IQ format conversion (pure, no subprocess)
# ---------------------------------------------------------------------------


def test_iq_to_bytes_cs16_scaling_and_clip() -> None:
    iq = np.array([0.5 + 0.0j, -0.25 + 0.25j, 2.0 - 2.0j], dtype=np.complex64)
    raw = iq_to_bytes(iq, "cs16")
    vals = np.frombuffer(raw, dtype="<i2")
    assert list(vals) == [16383, 0, -8191, 8191, 32767, -32767]


def test_iq_to_bytes_cu8_scaling_and_clip() -> None:
    iq = np.array([0.0 + 0.0j, 0.5 - 0.5j, 1.5 + 0.0j], dtype=np.complex64)
    raw = iq_to_bytes(iq, "cu8")
    vals = np.frombuffer(raw, dtype=np.uint8)
    assert list(vals) == [127, 127, 191, 63, 255, 127]


def test_iq_to_bytes_cf32_passthrough() -> None:
    iq = np.array([0.25 - 0.75j], dtype=np.complex64)
    raw = iq_to_bytes(iq, "cf32")
    assert np.frombuffer(raw, dtype="<c8")[0] == iq[0]


def test_iq_to_bytes_empty() -> None:
    assert iq_to_bytes(np.zeros(0, dtype=np.complex64), "cs16") == b""


# ---------------------------------------------------------------------------
# Manifest + registry
# ---------------------------------------------------------------------------


def test_dump1090_registered() -> None:
    manifest = next(m for m in decoder_registry.manifests() if m.name == "dump1090")
    assert manifest.tap_point == "rf_band"
    assert manifest.required_sample_rate == 2_000_000
    assert set(manifest.events) >= {"frame", "aircraft", "decoder_state"}
    # Registry-create uses the class default spec (env-configurable).
    plugin = decoder_registry.create("dump1090")
    assert isinstance(plugin, Dump1090Plugin)


# ---------------------------------------------------------------------------
# Spawn / handshake / feed — the happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_ready_handshake_and_env() -> None:
    plugin = _plugin("--echo-stats")
    try:
        await plugin.on_attach(CTX)
        ready = plugin._runner.ready_payload  # noqa: SLF001 — test seam
        assert ready is not None
        assert ready["software"] == "fake-dump1090/1.0"
        assert ready["iq_format"] == "cs16"
        assert ready["receiver_id"] == CTX.receiver_id
        assert ready["sample_rate"] == str(CTX.sample_rate)
        assert "--echo-stats" in ready["argv"]
        assert isinstance(ready["pid"], int)
        status = plugin.status()
        assert status["state"] == "running"
        assert status["pid"] == ready["pid"]
    finally:
        await plugin.astop()
    assert plugin.status()["state"] == "stopped"
    assert "pid" not in plugin.status()  # child reaped


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fmt", "amp", "tol"),
    [("cf32", 0.5, 1e-4), ("cs16", 0.5, 1e-3), ("cu8", 0.4, 0.02)],
)
async def test_format_conversion_roundtrip(fmt: str, amp: float, tol: float) -> None:
    """Feed a complex tone; the fake echoes the RMS it decoded from stdin."""
    plugin = _plugin("--echo-stats", iq_format=fmt)  # type: ignore[arg-type]
    try:
        await plugin.on_attach(CTX)
        n = 4096
        t = np.arange(n, dtype=np.float32)
        tone = (amp * np.exp(2j * np.pi * t * 0.01)).astype(np.complex64)
        plugin.feed_iq(tone)  # one write; the fake echoes stats per stdin read
        stats = await _poll(
            plugin,
            lambda ev: sum(
                e["count"] for e in ev if e.get("kind") == "iq_stats"
            ) >= n,
        )
        stats = [e for e in stats if e.get("kind") == "iq_stats"]
        assert stats, "no iq_stats events arrived"
        assert sum(e["count"] for e in stats) == n
        assert all(abs(e["rms"] - amp) < tol for e in stats), stats
    finally:
        await plugin.astop()


@pytest.mark.asyncio
async def test_fixture_demodulates_through_subprocess() -> None:
    """Full path: hub cf32 → cs16 stdin → real demod → NDJSON events."""
    plugin = _plugin()
    try:
        await plugin.on_attach(CTX)
        iq = _fixture_iq()
        events = await _feed(plugin, iq, chunk=65536, delay=0.025)
        events += await _poll(plugin, lambda ev: any(_snapshot_has_callsigns(e) for e in ev))
        frames = [e for e in events if e.get("kind") == "frame"]
        snapshots = [e for e in events if e.get("kind") == "aircraft"]
        assert len(frames) == 14, f"expected 14 frames, got {len(frames)}"
        assert snapshots, "no aircraft snapshots"

        final = snapshots[-1]["aircraft"]
        by_icao = {a["icao"]: a for a in final}
        assert set(by_icao) == set(AIRCRAFT) | {"AABBCC"}
        for icao, callsign in AIRCRAFT.items():
            row = by_icao[icao]
            assert row["callsign"] == callsign
            assert row["altitude_ft"] == 12_500
            # Subprocess-family extras: synthetic position fields flow through
            assert -90 < row["lat"] < 90
            assert -180 < row["lon"] < 180
            assert row["position_source"] == "synthetic"
            assert row["groundspeed_kt"] > 0

        status = plugin.status()
        assert status["state"] == "running"
        assert status["parse_errors"] == 0
        assert status["dropped_chunks"] == 0
    finally:
        await plugin.astop()


# ---------------------------------------------------------------------------
# Failure modes: ready timeout, missing binary, garbage, crash/restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ready_timeout_rejects_attach() -> None:
    plugin = _plugin("--stall-secs", "1.0", ready_timeout=0.1)
    with pytest.raises(DecoderAttachError, match="did not signal ready"):
        await plugin.on_attach(CTX)
    assert plugin.status()["state"] == "stopped"  # reaped, not leaked


@pytest.mark.asyncio
async def test_missing_binary_raises_binary_missing() -> None:
    bad = SubprocessSpec(argv=("/nonexistent/dump1090-binary", "--ifile", "-"))
    plugin = Dump1090Plugin(spec_override=bad)
    with pytest.raises(DecoderBinaryMissing, match="not executable"):
        await plugin.on_attach(CTX)


@pytest.mark.asyncio
async def test_garbage_lines_are_counted_not_fatal() -> None:
    plugin = _plugin("--garbage-lines", "3")
    try:
        await plugin.on_attach(CTX)
        iq = _fixture_iq()
        events = await _feed(plugin, iq, chunk=65536, delay=0.025)
        events += await _poll(plugin, lambda ev: any(e.get("kind") == "aircraft" for e in ev))
        assert any(e.get("kind") == "frame" for e in events)
        assert plugin.status()["parse_errors"] == 3
        assert plugin.status()["state"] == "running"
    finally:
        await plugin.astop()


@pytest.mark.asyncio
async def test_crash_restart_recovers(tmp_path: Path) -> None:
    """One crash → backoff → respawn with a NEW pid → decoding continues."""
    marker = tmp_path / "crashed"
    plugin = _plugin(
        "--crash-after", "3", "--crash-marker", str(marker),
        restart_backoff=(0.05,),
    )
    try:
        await plugin.on_attach(CTX)
        first_pid = plugin._runner.ready_payload["pid"]  # noqa: SLF001
        iq = _fixture_iq()
        # Pass 1 kills the first incarnation mid-stream (keep its events:
        # the "restarting" decoder_state lands while it feeds).
        events: list[dict[str, Any]] = await _feed(plugin, iq, chunk=65536, delay=0.01)
        # Backoff + interpreter startup for the replacement; keep draining.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            events.extend(plugin.feed_iq(_EMPTY))
            if plugin._runner.ready_payload and plugin._runner.ready_payload["pid"] != first_pid:  # noqa: SLF001
                break
            await asyncio.sleep(0.05)
        second_pid = plugin._runner.ready_payload["pid"]  # noqa: SLF001
        assert second_pid != first_pid, "runner never respawned"
        assert marker.exists(), "first incarnation never crashed"

        # Pass 2 decodes fully in the fresh process.
        events += await _feed(plugin, iq, chunk=65536, delay=0.025)
        events += await _poll(plugin, lambda ev: any(_snapshot_has_callsigns(e) for e in ev))
        frames = [e for e in events if e.get("kind") == "frame"]
        assert len(frames) >= 14
        state_events = [e for e in events if e.get("kind") == "decoder_state"]
        assert any(e["state"] == "restarting" for e in state_events)
        assert plugin.status()["restarts"] == 1
        assert plugin.status()["state"] == "running"
    finally:
        await plugin.astop()


@pytest.mark.asyncio
async def test_crash_gives_up_after_backoff_exhausted() -> None:
    """A binary that always dies ends in a terminal 'failed' state."""
    plugin = _plugin("--crash-after", "2", restart_backoff=(0.05, 0.05))
    try:
        await plugin.on_attach(CTX)
        iq = _fixture_iq()
        collected: list[dict[str, Any]] = []
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            collected += await _feed(plugin, iq[:262144], chunk=65536, delay=0.05)
            if plugin.status()["state"] == "failed":
                break
        assert plugin.status()["state"] == "failed"
        assert plugin.status()["restarts"] == 2  # 3 incarnations total

        # The failure surfaced as a decoder_state event for the frontend…
        events = collected + await _poll(plugin, lambda ev: any(
            e.get("kind") == "decoder_state" and e.get("state") == "failed" for e in ev
        ))
        failed = [e for e in events if e.get("state") == "failed"]
        assert failed and failed[0]["restarts"] == 2

        # …and further feeds are dropped, not written to a dead pipe.
        before = plugin.status()["dropped_chunks"]
        plugin.feed_iq(np.ones(1024, dtype=np.complex64))
        assert plugin.status()["dropped_chunks"] == before + 1
    finally:
        await plugin.astop()


# ---------------------------------------------------------------------------
# Backpressure + teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stalled_child_drops_chunks_bounded() -> None:
    """A child that never reads must not grow the parent's memory."""
    plugin = _plugin(
        "--stall-secs", "5.0",
        ready_timeout=None, max_buffered_bytes=64 * 1024,
    )
    try:
        await plugin.on_attach(CTX)
        chunk = np.ones(8192, dtype=np.complex64)  # 32 KiB as cs16
        # 512 KiB total with no awaits: the OS pipe (~64 KiB) plus the
        # transport-buffer ceiling (64 KiB) accept ~4 writes; the rest drop.
        for _ in range(16):
            plugin.feed_iq(chunk)
        status = plugin.status()
        assert status["dropped_chunks"] >= 8  # metered, never unbounded
        assert status["state"] == "running"  # still healthy, just metering
    finally:
        t0 = time.monotonic()
        await plugin.astop()
        elapsed = time.monotonic() - t0
    # The stall outlives the graceful window → SIGKILL path, still bounded.
    assert elapsed < 4.5
    assert plugin.status()["state"] == "stopped"


@pytest.mark.asyncio
async def test_astop_is_deterministic_and_idempotent() -> None:
    plugin = _plugin()
    await plugin.on_attach(CTX)
    plugin.feed_iq(np.ones(4096, dtype=np.complex64))
    await plugin.astop()
    await plugin.astop()  # second stop is a no-op
    status = plugin.status()
    assert status["state"] == "stopped"
    assert "pid" not in status


# ---------------------------------------------------------------------------
# REST + WS end-to-end (fake binary patched in as the class default)
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    settings = Settings(tier="dev")
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_default_spec(monkeypatch: pytest.MonkeyPatch) -> SubprocessSpec:
    spec = _spec()
    monkeypatch.setattr(Dump1090Plugin, "spec", spec)
    return spec


def _spawn_adsb_receiver(client: TestClient) -> str:
    r = client.post(
        "/api/receivers",
        json={
            "source_type": "file",
            "source_kwargs": {"file_path": str(FIXTURE), "loop": True, "realtime": True},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["receiver_id"]


def test_rest_lists_dump1090(client: TestClient) -> None:
    r = client.get("/api/decoders")
    assert r.status_code == 200
    dump = next(d for d in r.json() if d["name"] == "dump1090")
    assert dump["tap_point"] == "rf_band"
    assert "frame" in dump["events"]


def test_rest_attach_502_when_binary_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _spawn_adsb_receiver(client)
    try:
        monkeypatch.setattr(
            Dump1090Plugin, "spec", SubprocessSpec(argv=("/nonexistent/dump1090",))
        )
        r = client.post(f"/api/receivers/{rid}/decoders", json={"name": "dump1090"})
        assert r.status_code == 502
        assert "not executable" in r.json()["detail"]
    finally:
        client.delete(f"/api/receivers/{rid}")


def test_rest_rejects_wrong_rate(client: TestClient, fake_default_spec: SubprocessSpec) -> None:
    """rx-default runs 250 kSPS — dump1090 needs 2 MSPS like the adsb plugin."""
    r = client.post("/api/receivers/rx-default/decoders", json={"name": "dump1090"})
    assert r.status_code == 400
    assert "requires 2000000" in r.json()["detail"]


def test_rest_attach_detach_lifecycle(
    client: TestClient, fake_default_spec: SubprocessSpec
) -> None:
    rid = _spawn_adsb_receiver(client)
    try:
        r = client.post(f"/api/receivers/{rid}/decoders", json={"name": "dump1090"})
        assert r.status_code == 201, r.text
        r = client.get(f"/api/receivers/{rid}/decoders")
        assert r.status_code == 200
        status = r.json()[0]
        assert status["name"] == "dump1090"
        assert status["state"] == "running"
        assert "pid" in status

        assert client.delete(f"/api/receivers/{rid}/decoders/dump1090").status_code == 204
        assert client.get(f"/api/receivers/{rid}/decoders").json() == []
        # Repeat detach → 404
        assert client.delete(f"/api/receivers/{rid}/decoders/dump1090").status_code == 404
    finally:
        client.delete(f"/api/receivers/{rid}")


def test_ws_decoder_events_from_subprocess(
    client: TestClient, fake_default_spec: SubprocessSpec
) -> None:
    """Fixture replay → dump1090 subprocess → WS text frames carry events."""
    import json

    rid = _spawn_adsb_receiver(client)
    try:
        r = client.post(f"/api/receivers/{rid}/decoders", json={"name": "dump1090"})
        assert r.status_code == 201, r.text

        frame_events: list[dict] = []
        aircraft_events: list[dict] = []

        def complete(snapshot: dict) -> bool:
            table = {a["icao"]: a for a in snapshot["aircraft"]}
            return all(
                icao in table and table[icao]["callsign"] == callsign
                for icao, callsign in AIRCRAFT.items()
            )

        with client.websocket_connect(f"/ws/{rid}") as ws:
            deadline = time.monotonic() + 6.0  # 1 loop = 1 s; callsigns by ~1.8 s
            while time.monotonic() < deadline:
                if aircraft_events and complete(aircraft_events[-1]):
                    break
                try:
                    text = ws.receive_text()
                except KeyError:
                    continue  # binary FFT/audio frame
                if not text:
                    continue
                data = json.loads(text)
                if data.get("type") != "decoder":
                    continue
                assert data["decoder"] == "dump1090"
                assert data["receiverId"] == rid
                event = data["event"]
                if event["kind"] == "frame":
                    frame_events.append(event)
                elif event["kind"] == "aircraft":
                    aircraft_events.append(event)

        assert frame_events, "no frame events from the subprocess"
        assert aircraft_events, "no aircraft snapshots from the subprocess"
        final = {a["icao"]: a for a in aircraft_events[-1]["aircraft"]}
        assert set(AIRCRAFT) <= set(final)
        row = final["4D22AA"]
        assert row["callsign"] == "OWRX001"
        assert row["altitude_ft"] == 12_500
        assert "lat" in row and "lon" in row  # subprocess-family extras arrived

        r = client.get(f"/api/receivers/{rid}/decoders")
        status = r.json()[0]
        assert status["state"] == "running"
        assert status["parse_errors"] == 0
    finally:
        client.delete(f"/api/receivers/{rid}")


def test_destroy_receiver_reaps_subprocess(
    client: TestClient, fake_default_spec: SubprocessSpec
) -> None:
    """DELETE /api/receivers/{id} must tear the child process down."""
    rid = _spawn_adsb_receiver(client)
    assert client.post(f"/api/receivers/{rid}/decoders", json={"name": "dump1090"}).status_code == 201
    assert client.delete(f"/api/receivers/{rid}").status_code == 204


# ---------------------------------------------------------------------------
# Slice-31 failure modes: vanish-after-ready, partial-JSON-die
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vanish_after_ready_triggers_restart_then_failure() -> None:
    """Fake emits ready, closes stdout without exiting, then exits 0.

    The runner must:
      * accept the ready event (handshake completes)
      * notice stdout EOF on the next pump cycle
      * treat the unexpected exit (rc=0 but _stopping is False) as a
        crash → respawn via restart_backoff
      * after the restart budget is exhausted, declare state="failed"
    """
    # restart_backoff=(0.05,) → 1 restart allowed. After the respawned
    # child also vanishes, the runner declares failure.
    plugin = _plugin(
        "--vanish-after-ready-secs", "0.1",
        restart_backoff=(0.05,),
    )
    try:
        await plugin.on_attach(CTX)
        first_ready = plugin._runner.ready_payload  # noqa: SLF001
        assert first_ready is not None
        assert first_ready["software"] == "fake-dump1090/1.0"

        # Drain events until the runner declares failure. Budget ~5s
        # for the vanish + 0.05s backoff + second vanish + state change.
        events: list[dict[str, Any]] = []
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            events.extend(plugin.feed_iq(_EMPTY))
            st = plugin.status()
            if st["state"] == "failed":
                break
            await asyncio.sleep(0.05)

        final_status = plugin.status()
        assert final_status["state"] == "failed"
        # At least one restart attempt was made.
        assert final_status["restarts"] >= 1
        # At least one decoder_state event with state="failed" surfaced.
        failed_events = [
            e for e in events
            if e.get("kind") == "decoder_state" and e.get("state") == "failed"
        ]
        assert failed_events, "expected a failed decoder_state event"
    finally:
        await plugin.astop()


@pytest.mark.asyncio
async def test_emit_partial_json_die_counts_parse_error_then_fails() -> None:
    """Fake emits ready, writes a truncated JSON line, then exits 0.

    The runner must:
      * accept the ready event (handshake completes)
      * try to parse the partial JSON line, fail, count it as a
        parse_error (NOT crash)
      * notice stdout EOF on the next pump cycle
      * treat the unexpected exit (rc=0 but _stopping is False) as a
        crash → respawn via restart_backoff
      * after the restart budget is exhausted, declare state="failed"
    """
    plugin = _plugin(
        "--emit-partial-json-die",
        restart_backoff=(0.05,),
    )
    try:
        await plugin.on_attach(CTX)
        first_ready = plugin._runner.ready_payload  # noqa: SLF001
        assert first_ready is not None
        assert first_ready["software"] == "fake-dump1090/1.0"

        # Drain events until the runner declares failure.
        events: list[dict[str, Any]] = []
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            events.extend(plugin.feed_iq(_EMPTY))
            st = plugin.status()
            if st["state"] == "failed":
                break
            await asyncio.sleep(0.05)

        final_status = plugin.status()
        assert final_status["state"] == "failed"
        # The partial JSON line was counted as a parse_error on at least
        # one incarnation of the child (each respawn re-emits the partial
        # JSON, so total parse_errors >= 1 across the lifecycle).
        assert final_status["parse_errors"] >= 1
        # Failed decoder_state event surfaced.
        failed_events = [
            e for e in events
            if e.get("kind") == "decoder_state" and e.get("state") == "failed"
        ]
        assert failed_events, "expected a failed decoder_state event"
    finally:
        await plugin.astop()
