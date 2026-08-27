"""Self-listing endpoint tests (slice-14, ADR-006 federation polish).

Verifies:
  - GET /api/listing returns 404 when settings.listing.enabled is False
    (the privacy-preserving default — opt-in self-listing).
  - When enabled, returns the receiverbook-compatible JSON shape with
    the operator's configured fields.
  - The 'extra' sub-object carries the version/tier/source/decoder counts
    for downstream discovery tooling.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from openwebrx_plus.api.rest import create_app
from openwebrx_plus.config import Settings
from openwebrx_plus.config.user_settings import reset_user_settings_service
from openwebrx_plus.observability import get_debug_log_buffer


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@asynccontextmanager
async def _live_server(settings: Settings) -> AsyncIterator[str]:
    import uvicorn

    port = _free_port()
    config = uvicorn.Config(
        app=create_app(settings),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    try:
        for _ in range(50):
            if server.started:
                break
            await asyncio.sleep(0.05)
        if not server.started:
            raise RuntimeError("uvicorn server did not start in 2.5s")
        await asyncio.sleep(0.1)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=5)
        except TimeoutError:
            server_task.cancel()


@pytest.fixture(autouse=True)
def _reset_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    reset_user_settings_service()
    asyncio.run(get_debug_log_buffer().clear())
    yield
    reset_user_settings_service()
    asyncio.run(get_debug_log_buffer().clear())


async def test_self_listing_disabled_by_default() -> None:
    """Listing is OFF by default — the privacy-preserving default."""
    settings = Settings(tier="dev")
    async with _live_server(settings) as base_url:  # noqa: SIM117
        async with httpx.AsyncClient(base_url=base_url) as client:
            r = await client.get("/api/listing")
            assert r.status_code == 404
            assert "self-listing disabled" in r.json()["detail"]


async def test_self_listing_returns_receiverbook_shape_when_enabled() -> None:
    """When enabled, returns receiverbook-compatible JSON."""
    settings = Settings(
        tier="dev",
        listing={
            "enabled": True,
            "id": "neal-001",
            "name": "Neal's 20m receiver (KH6)",
            "url": "https://sdr.example.com:8073/ws",
            "lat": 21.3,
            "lon": -157.8,
            "description": "IC-7300 + dipole; HF bands",
        },
    )
    async with _live_server(settings) as base_url:  # noqa: SIM117
        async with httpx.AsyncClient(base_url=base_url) as client:
            r = await client.get("/api/listing")
            assert r.status_code == 200
            body = r.json()
            assert body["directory"] == "self"
            assert body["source_type"] == "openwebrx_remote"
            assert body["id"] == "neal-001"
            assert body["name"] == "Neal's 20m receiver (KH6)"
            assert body["url"] == "https://sdr.example.com:8073/ws"
            assert body["lat"] == 21.3
            assert body["lon"] == -157.8
            assert body["online"] is True
            assert body["users"] is None
            # The 'extra' sub-object carries version + source/decoder counts.
            assert body["extra"]["software"] == "openwebrx-plus"
            assert body["extra"]["version"] == "0.1.0"
            assert body["extra"]["tier"] == "dev"
            assert "default_source" in body["extra"]
            assert isinstance(body["extra"]["source_count"], int)
            assert isinstance(body["extra"]["decoder_count"], int)
            assert body["extra"]["decoder_count"] >= 5  # adsb, ais, dump1090, dump978, cw
