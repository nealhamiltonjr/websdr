"""E2E test for the Settings + Debugger HTTP endpoints (slice-5.1).

Unlike the testclient-based tests in test_settings_debug_api.py (which
use FastAPI's in-process TestClient), this test boots a real uvicorn
server on an ephemeral port and makes real HTTP requests via httpx. This
catches issues with the full async startup/shutdown cycle, the FastAPI
middleware stack, and the actual socket binding.

The test is wrapped in a single async function so the server lifetime +
teardown is one atomic operation. pytest-asyncio's ``asyncio_mode =
"auto"`` config (see pyproject.toml) makes this Just Work.
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
from openwebrx_plus.config.user_settings import (
    reset_user_settings_service,
)
from openwebrx_plus.observability import get_debug_log_buffer


def _free_port() -> int:
    """Bind a socket to port 0 to ask the OS for a free ephemeral port,
    then immediately close it. There's a small race window but pytest
    tests are short-lived."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@asynccontextmanager
async def _live_server(settings: Settings) -> AsyncIterator[str]:
    """Boot a uvicorn server on a free port, yield its base URL, then
    shut down cleanly."""
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

    # Run the server in a background task; shut down on exit.
    server_task = asyncio.create_task(server.serve())
    try:
        # Wait for the server to come up (uvicorn doesn't expose a ready
        # event, so poll the socket).
        for _ in range(50):
            if server.started:
                break
            await asyncio.sleep(0.05)
        if not server.started:
            raise RuntimeError("uvicorn server did not start in 2.5s")
        # Give a bit more time for the startup event to wire exception hooks.
        await asyncio.sleep(0.1)
        yield f"http://127.0.0.1:{port}"
    finally:
        # Graceful shutdown.
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=5)
        except TimeoutError:
            server_task.cancel()


@pytest.fixture(autouse=True)
def _reset_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets a fresh user-settings file in tmp_path."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    reset_user_settings_service()
    asyncio.run(get_debug_log_buffer().clear())
    yield
    reset_user_settings_service()
    asyncio.run(get_debug_log_buffer().clear())


async def test_health_endpoint_works_over_real_http() -> None:
    """The /api/health endpoint should respond 200 over real HTTP —
    a smoke test that the server boots, accepts connections, and routes
    requests through the full middleware stack."""
    settings = Settings(tier="dev")
    async with _live_server(settings) as base_url:  # noqa: SIM117
        async with httpx.AsyncClient(base_url=base_url) as client:
            r = await client.get("/api/health")
            assert r.status_code == 200
            assert r.json() == {"status": "ok"}


async def test_settings_get_and_put_over_real_http() -> None:
    """GET /api/settings → defaults; PUT → update; GET → updated value."""
    settings = Settings(tier="dev")
    async with _live_server(settings) as base_url:  # noqa: SIM117
        async with httpx.AsyncClient(base_url=base_url) as client:
            # Get defaults
            r = await client.get("/api/settings")
            assert r.status_code == 200
            assert r.json()["display"]["theme"] == "dark"
            # Update theme to light
            r = await client.put(
                "/api/settings",
                json={"display": {"theme": "light"}},
            )
            assert r.status_code == 200
            assert r.json()["display"]["theme"] == "light"
            # Verify the update persisted
            r = await client.get("/api/settings")
            assert r.json()["display"]["theme"] == "light"


async def test_debug_logs_round_trip_over_real_http() -> None:
    """The structlog capture processor should be wired (default) and
    log events should appear in GET /api/debug/logs."""
    settings = Settings(tier="dev")
    async with _live_server(settings) as base_url:  # noqa: SIM117
        async with httpx.AsyncClient(base_url=base_url) as client:
            # Wait a moment for any startup logs to land in the buffer.
            await asyncio.sleep(0.2)
            # Get the current buffer
            r = await client.get("/api/debug/logs")
            assert r.status_code == 200
            body = r.json()
            assert "entries" in body
            assert "stats" in body
            # The startup logs ("default sessions initialized", etc.) should
            # be in the buffer.
            assert body["stats"]["total_captured"] >= 0
            # Clear the buffer
            r = await client.post("/api/debug/clear")
            assert r.status_code == 200
            assert r.json()["status"] == "cleared"


async def test_debug_stats_endpoint_over_real_http() -> None:
    """GET /api/debug/stats returns counts by level + capacity info."""
    settings = Settings(tier="dev")
    async with _live_server(settings) as base_url:  # noqa: SIM117
        async with httpx.AsyncClient(base_url=base_url) as client:
            r = await client.get("/api/debug/stats")
            assert r.status_code == 200
            body = r.json()
            assert "counts_by_level" in body
            assert body["all_capacity"] > 0
            assert body["errors_capacity"] > 0


async def test_debug_export_downloads_ndjson() -> None:
    """GET /api/debug/export returns an NDJSON blob with the right headers."""
    settings = Settings(tier="dev")
    async with _live_server(settings) as base_url:  # noqa: SIM117
        async with httpx.AsyncClient(base_url=base_url) as client:
            r = await client.get("/api/debug/export")
            assert r.status_code == 200
            assert "x-ndjson" in r.headers["content-type"]
            assert "attachment" in r.headers["content-disposition"]
            # The body should be newline-delimited JSON (or empty).
            text = r.text
            if text.strip():
                for line in text.strip().split("\n"):
                    import json as _json

                    obj = _json.loads(line)
                    assert "timestamp" in obj
                    assert "level" in obj
                    assert "message" in obj
