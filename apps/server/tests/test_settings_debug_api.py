"""Tests for the settings/debug REST endpoints (slice-5.1).

Covers: GET /api/settings, PUT /api/settings (partial update, validation
errors, unknown sections/fields ignored), POST /api/settings/reset,
GET /api/debug/logs (filters, pagination), GET /api/debug/errors,
GET /api/debug/stats, POST /api/debug/clear, GET /api/debug/export.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openwebrx_plus.api.rest import create_app
from openwebrx_plus.config import Settings
from openwebrx_plus.config.user_settings import (
    reset_user_settings_service,
)
from openwebrx_plus.observability import get_debug_log_buffer
from openwebrx_plus.observability.debug_log import LogEntry


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Return a FastAPI TestClient with a fresh user-settings file."""
    # Isolate the user settings file to tmp_path
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    reset_user_settings_service()
    settings = Settings(tier="dev")
    app = create_app(settings)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    reset_user_settings_service()
    # Clear the debug log buffer so each test starts clean.
    asyncio.run(get_debug_log_buffer().clear())
    yield
    reset_user_settings_service()
    asyncio.run(get_debug_log_buffer().clear())


def test_get_settings_returns_defaults(client: TestClient) -> None:
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["display"]["theme"] == "dark"
    assert body["audio"]["master_volume"] == 0.8
    assert body["dsp"]["default_dsp_mode"] == "classic"


def test_put_settings_partial_update(client: TestClient) -> None:
    r = client.put(
        "/api/settings",
        json={"display": {"theme": "light"}, "audio": {"master_volume": 0.4}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["display"]["theme"] == "light"
    assert body["audio"]["master_volume"] == 0.4
    # Un-touched section is preserved.
    assert body["dsp"]["default_dsp_mode"] == "classic"


def test_put_settings_invalid_enum_returns_422(client: TestClient) -> None:
    r = client.put("/api/settings", json={"display": {"theme": "purple"}})
    assert r.status_code == 422


def test_put_settings_out_of_range_returns_422(client: TestClient) -> None:
    r = client.put("/api/settings", json={"audio": {"master_volume": 5.0}})
    assert r.status_code == 422


def test_put_settings_unknown_section_ignored(client: TestClient) -> None:
    r = client.put(
        "/api/settings",
        json={"bogus_section": {"x": 1}, "display": {"theme": "light"}},
    )
    assert r.status_code == 200
    assert r.json()["display"]["theme"] == "light"


def test_post_reset_settings(client: TestClient) -> None:
    # Make a change first.
    client.put("/api/settings", json={"display": {"theme": "light"}})
    # Reset.
    r = client.post("/api/settings/reset")
    assert r.status_code == 200
    assert r.json()["display"]["theme"] == "dark"


def test_get_debug_logs_empty_when_no_capture(client: TestClient) -> None:
    r = client.get("/api/debug/logs")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["entries"] == []
    assert "counts_by_level" in body["stats"]


def test_get_debug_logs_after_capture(client: TestClient) -> None:
    buf = get_debug_log_buffer()
    buf.add(LogEntry(timestamp="2026-01-01T00:00:00Z", level="info", logger="t", message="hello"))
    buf.add(LogEntry(timestamp="2026-01-01T00:00:01Z", level="error", logger="t", message="boom"))
    r = client.get("/api/debug/logs")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    # Newest first.
    assert body["entries"][0]["message"] == "boom"
    assert body["entries"][1]["message"] == "hello"


def test_get_debug_logs_level_filter(client: TestClient) -> None:
    buf = get_debug_log_buffer()
    buf.add(LogEntry(timestamp="t1", level="info", logger="t", message="i"))
    buf.add(LogEntry(timestamp="t2", level="warning", logger="t", message="w"))
    buf.add(LogEntry(timestamp="t3", level="error", logger="t", message="e"))
    r = client.get("/api/debug/logs?level=error")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["entries"][0]["message"] == "e"


def test_get_debug_logs_logger_substring_filter(client: TestClient) -> None:
    buf = get_debug_log_buffer()
    buf.add(LogEntry(timestamp="t1", level="info", logger="openwebrx_plus.api.rest", message="m"))
    buf.add(LogEntry(timestamp="t2", level="info", logger="openwebrx_plus.sources.kiwi", message="m"))
    r = client.get("/api/debug/logs?logger=rest")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert "rest" in body["entries"][0]["logger"]


def test_get_debug_logs_message_substring_filter(client: TestClient) -> None:
    buf = get_debug_log_buffer()
    buf.add(LogEntry(timestamp="t1", level="info", logger="t", message="starting receiver"))
    buf.add(LogEntry(timestamp="t2", level="info", logger="t", message="stopping receiver"))
    r = client.get("/api/debug/logs?message=starting")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["entries"][0]["message"] == "starting receiver"


def test_get_debug_logs_pagination(client: TestClient) -> None:
    buf = get_debug_log_buffer()
    for i in range(10):
        buf.add(LogEntry(timestamp=f"t{i}", level="info", logger="t", message=f"m-{i:02d}"))
    r = client.get("/api/debug/logs?limit=3&offset=0")
    body = r.json()
    assert body["count"] == 3
    assert body["entries"][0]["message"] == "m-09"
    r2 = client.get("/api/debug/logs?limit=3&offset=3")
    body2 = r2.json()
    assert body2["entries"][0]["message"] == "m-06"


def test_get_debug_errors_returns_only_warnings_and_above(client: TestClient) -> None:
    buf = get_debug_log_buffer()
    buf.add(LogEntry(timestamp="t1", level="info", logger="t", message="i"))
    buf.add(LogEntry(timestamp="t2", level="warning", logger="t", message="w"))
    buf.add(LogEntry(timestamp="t3", level="error", logger="t", message="e"))
    buf.add(LogEntry(timestamp="t4", level="critical", logger="t", message="c"))
    r = client.get("/api/debug/errors")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    messages = [e["message"] for e in body["entries"]]
    assert "w" in messages and "e" in messages and "c" in messages
    assert "i" not in messages


def test_get_debug_stats(client: TestClient) -> None:
    buf = get_debug_log_buffer()
    buf.add(LogEntry(timestamp="t1", level="info", logger="t", message="a"))
    buf.add(LogEntry(timestamp="t2", level="error", logger="t", message="b"))
    r = client.get("/api/debug/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["counts_by_level"]["info"] == 1
    assert body["counts_by_level"]["error"] == 1
    assert body["total_captured"] == 2


def test_post_clear_debug_logs(client: TestClient) -> None:
    buf = get_debug_log_buffer()
    for i in range(5):
        buf.add(LogEntry(timestamp=f"t{i}", level="info", logger="t", message=f"m-{i}"))
    r = client.post("/api/debug/clear")
    assert r.status_code == 200
    assert r.json()["status"] == "cleared"
    # The endpoint logs "debug log buffer cleared" via structlog AFTER the
    # clear, so the buffer may have one entry from that. We just assert
    # the entries we added are gone.
    entries = asyncio.run(buf.get_entries())
    messages = [e.message for e in entries]
    for i in range(5):
        assert f"m-{i}" not in messages


def test_get_debug_export_returns_ndjson(client: TestClient) -> None:
    buf = get_debug_log_buffer()
    buf.add(LogEntry(timestamp="t1", level="info", logger="t", message="hello"))
    buf.add(LogEntry(timestamp="t2", level="error", logger="t", message="boom", fields={"reason": "bad"}))
    r = client.get("/api/debug/export")
    assert r.status_code == 200
    assert "application/x-ndjson" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.strip().split("\n")
    assert len(lines) == 2
    obj0 = json.loads(lines[0])
    obj1 = json.loads(lines[1])
    # Newest first.
    assert obj0["message"] == "boom"
    assert obj1["message"] == "hello"
    assert obj0["fields"]["reason"] == "bad"


def test_settings_persist_across_app_instances(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A change via PUT /api/settings should be visible to a fresh app instance."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    reset_user_settings_service()
    # First app instance: make a change.
    settings = Settings(tier="dev")
    client1 = TestClient(create_app(settings))
    client1.put("/api/settings", json={"display": {"theme": "light"}})
    # Second app instance: should see the change.
    reset_user_settings_service()
    client2 = TestClient(create_app(settings))
    r = client2.get("/api/settings")
    assert r.json()["display"]["theme"] == "light"


def test_async_exception_capture_via_endpoint(client: TestClient) -> None:
    """A structlog error event should be retrievable via the debug endpoint."""
    import structlog

    log = structlog.get_logger("test.endpoint")
    log.error("synthetic-test-error", extra="x")
    r = client.get("/api/debug/errors")
    body = r.json()
    messages = [e["message"] for e in body["entries"]]
    assert "synthetic-test-error" in messages
