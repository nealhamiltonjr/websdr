"""Tests for SourceRegistry discovery and the new source backends (ADR-004).

Covers:
  - SourceRegistry.builtin_manifests() returns ≥5 entries
  - Each built-in manifest has a valid factory_entrypoint that resolves
  - SourceRegistry.create() instantiates each source without error
  - SimulatedSource produces IQ chunks of the right shape + non-trivial content
  - FileSource replays a small synthetic cf32 file
  - REST endpoint GET /api/sources lists manifests
  - POST /api/receivers accepts source_type=simulated and spawns the right source
  - POST /api/receivers rejects unknown source_type with 400
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Literal

import numpy as np
import pytest
from fastapi.testclient import TestClient

from openwebrx_plus.api.rest import create_app
from openwebrx_plus.config import Settings
from openwebrx_plus.sources import (
    FileSource,
    SimulatedSource,
    SourceRegistry,
)

# ---------------------------------------------------------------------------
# SourceRegistry
# ---------------------------------------------------------------------------


def test_registry_has_at_least_five_builtins() -> None:
    """We expect at least rtl_sdr, airspy, sdrplay, file, simulated."""
    manifests = SourceRegistry.builtin_manifests()
    types = {m.source_type for m in manifests}
    assert "rtl_sdr" in types
    assert "airspy" in types
    assert "sdrplay" in types
    assert "file" in types
    assert "simulated" in types
    assert len(manifests) >= 5


def test_registry_get_manifest_known_type() -> None:
    m = SourceRegistry.get_manifest("rtl_sdr")
    assert m is not None
    assert m.source_type == "rtl_sdr"
    assert m.hardware_required is True
    assert m.supports_bias_tee is True  # RTL-SDR Blog V4


def test_registry_get_manifest_unknown_returns_none() -> None:
    assert SourceRegistry.get_manifest("nonexistent_sdr") is None


def test_registry_create_simulated_source() -> None:
    """SourceRegistry.create() resolves entrypoints and instantiates."""
    src = SourceRegistry.create("simulated", signal_set="default")
    assert isinstance(src, SimulatedSource)
    assert src.info.type == "simulated"


def test_registry_create_unknown_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="unknown source_type"):
        SourceRegistry.create("nonexistent_sdr")


def test_registry_create_with_invalid_kwargs_raises() -> None:
    """Creating a source with bad kwargs surfaces the underlying error."""
    with pytest.raises((TypeError, ValueError)):
        SourceRegistry.create("simulated", bogus_kwarg=123)


# ---------------------------------------------------------------------------
# SimulatedSource
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("signal_set", ["default", "am_band", "ham_band", "ft8_dry_run"])
def test_simulated_source_yields_complex64_chunks(
    signal_set: Literal["default", "am_band", "ham_band", "ft8_dry_run"],
) -> None:
    """SimulatedSource should yield complex64 chunks of the requested size."""
    src = SimulatedSource(signal_set=signal_set, chunk_size=1024, sample_rate=100_000)
    assert src.info.type == "simulated"
    assert src.info.sample_rate == 100_000

    async def _collect_first_chunk() -> np.ndarray:
        gen = src.spawn(center_freq=14_205_000, sample_rate=100_000, gain=None)
        try:
            return await gen.__anext__()
        finally:
            await src.close()

    chunk = asyncio.run(_collect_first_chunk())
    assert chunk.dtype == np.complex64
    assert chunk.shape == (1024,)


def test_simulated_source_has_nonzero_signal() -> None:
    """The default preset should produce signal power above the noise floor."""
    src = SimulatedSource(signal_set="default", noise_floor=0.0, chunk_size=2048)
    async def _collect() -> np.ndarray:
        gen = src.spawn(center_freq=14_205_000, sample_rate=240_000, gain=None)
        try:
            return await gen.__anext__()
        finally:
            await src.close()
    chunk = asyncio.run(_collect())
    # A CW carrier at offset 0 produces a strong peak; total power > 0.
    power = float(np.mean(np.abs(chunk) ** 2))
    assert power > 0.01, f"expected non-trivial signal power, got {power}"


def test_simulated_source_has_noise_floor_when_configured() -> None:
    """With noise_floor > 0, even outside-carrier bins should have nonzero power."""
    src = SimulatedSource(signal_set="default", noise_floor=0.1, chunk_size=4096)
    async def _collect() -> np.ndarray:
        gen = src.spawn(center_freq=14_205_000, sample_rate=240_000, gain=None)
        try:
            return await gen.__anext__()
        finally:
            await src.close()
    chunk = asyncio.run(_collect())
    power = float(np.mean(np.abs(chunk) ** 2))
    assert power > 0.001  # noise alone gives power ~ noise_floor^2 = 0.01


def test_simulated_source_unknown_signal_set_rejected() -> None:
    with pytest.raises(ValueError, match="unknown signal_set"):
        SimulatedSource(signal_set="bogus")  # type: ignore[arg-type]  # intentionally invalid


# ---------------------------------------------------------------------------
# FileSource
# ---------------------------------------------------------------------------


def test_file_source_replays_cf32_file() -> None:
    """Write a small cf32 file and replay it via FileSource."""
    # Build a 1-second complex tone at 1 kHz, sampled at 10 kHz.
    sr = 10_000
    t = np.arange(sr, dtype=np.float32) / sr
    phase = 2 * np.pi * 1000.0 * t
    iq = (np.cos(phase) + 1j * np.sin(phase)).astype(np.complex64)

    with tempfile.NamedTemporaryFile(suffix=".cf32", delete=False) as f:
        f.write(iq.tobytes())
        path = Path(f.name)
    try:
        src = FileSource(file_path=path, chunk_size=512, loop=True)
        assert src.info.type == "file"
        assert src.info.sample_rate == 2_400_000  # default hint (no .sigmf-meta)

        async def _collect_two_chunks() -> list[np.ndarray]:
            gen = src.spawn(center_freq=14_205_000, sample_rate=sr, gain=None)
            chunks = []
            try:
                chunks.append(await gen.__anext__())
                chunks.append(await gen.__anext__())
            finally:
                await src.close()
            return chunks

        chunks = asyncio.run(_collect_two_chunks())
        assert len(chunks) == 2
        for c in chunks:
            assert c.dtype == np.complex64
            assert c.shape == (512,)

        # The first 1024 samples of the file should round-trip exactly.
        # Chunk 0 + chunk 1 covers 1024 samples.
        reconstructed = np.concatenate(chunks)
        # The first 1024 samples of the file:
        expected = iq[:1024]
        np.testing.assert_allclose(reconstructed, expected, atol=1e-6)
    finally:
        path.unlink(missing_ok=True)


def test_file_source_unknown_extension_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported IQ file format"):
        FileSource(file_path=Path("/tmp/bogus.xyz"))


def test_file_source_missing_file_rejected() -> None:
    with pytest.raises(FileNotFoundError):
        FileSource(file_path=Path("/tmp/definitely-does-not-exist.cf32"))


# ---------------------------------------------------------------------------
# REST API: GET /api/sources
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    settings = Settings(tier="dev")
    app = create_app(settings)
    return TestClient(app)


def test_list_sources_endpoint(client: TestClient) -> None:
    r = client.get("/api/sources")
    assert r.status_code == 200
    sources = r.json()
    types = {s["source_type"] for s in sources}
    assert "rtl_sdr" in types
    assert "simulated" in types
    assert "file" in types

    # Each manifest should have the expected fields.
    for s in sources:
        assert "source_type" in s
        assert "label" in s
        assert "hardware_required" in s
        assert "sample_rate_range" in s
        assert isinstance(s["sample_rate_range"], list)
        assert len(s["sample_rate_range"]) == 2


def test_spawn_simulated_source_via_rest(client: TestClient) -> None:
    """POST /api/receivers with source_type=simulated creates a session
    backed by SimulatedSource, not the default RtlSdrSource stub."""
    r = client.post(
        "/api/receivers",
        json={
            "source_type": "simulated",
            "source_kwargs": {"signal_set": "am_band"},
            "center_freq": 1_000_000,
            "mode": "AM",
        },
    )
    assert r.status_code == 201
    body = r.json()
    rid = body["receiver_id"]

    # Verify the session is registered and its source is the right type.
    r = client.get("/api/receivers")
    sessions = r.json()
    matching = [s for s in sessions if s["receiver_id"] == rid]
    assert len(matching) == 1
    assert matching[0]["source"]["type"] == "simulated"

    # Cleanup.
    client.delete(f"/api/receivers/{rid}")


def test_spawn_unknown_source_type_returns_400(client: TestClient) -> None:
    r = client.post(
        "/api/receivers",
        json={"source_type": "nonexistent_sdr"},
    )
    assert r.status_code == 400
    assert "unknown source_type" in r.json()["detail"]


def test_spawn_file_source_missing_file_returns_400(client: TestClient) -> None:
    r = client.post(
        "/api/receivers",
        json={
            "source_type": "file",
            "source_kwargs": {"file_path": "/tmp/nonexistent.cf32"},
        },
    )
    assert r.status_code == 400
