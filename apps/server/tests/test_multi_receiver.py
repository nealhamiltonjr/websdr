"""Tests for the multi-receiver SessionRegistry + REST API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from openwebrx_plus.api.rest import create_app
from openwebrx_plus.config import Settings
from openwebrx_plus.sessions import (
    create_session,
    destroy_session,
    get_session,
    list_sessions,
)


@pytest.fixture
def client() -> TestClient:
    settings = Settings(tier="dev")
    app = create_app(settings)
    return TestClient(app)


def test_default_session_exists_after_init(client: TestClient) -> None:
    """GET /api/receivers should include rx-default after startup."""
    r = client.get("/api/receivers")
    assert r.status_code == 200
    sessions = r.json()
    assert any(s["receiver_id"] == "rx-default" for s in sessions)


def test_spawn_new_receiver_via_rest(client: TestClient) -> None:
    """POST /api/receivers creates a new session, returns its id."""
    r = client.post(
        "/api/receivers",
        json={
            "center_freq": 7_205_000,
            "mode": "LSB",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert "receiver_id" in body
    assert body["center_freq"] == 7_205_000
    assert body["mode"] == "LSB"
    rid = body["receiver_id"]

    # The new session should appear in the list.
    r = client.get("/api/receivers")
    sessions = r.json()
    assert any(s["receiver_id"] == rid for s in sessions)

    # And be directly queryable via get_session.
    assert get_session(rid) is not None


def test_cannot_destroy_default_session(client: TestClient) -> None:
    """DELETE /api/receivers/rx-default returns 403."""
    r = client.delete("/api/receivers/rx-default")
    assert r.status_code == 403


def test_destroy_unknown_receiver_returns_404(client: TestClient) -> None:
    r = client.delete("/api/receivers/rx-nonexistent")
    assert r.status_code == 404


def test_direct_registry_create_and_destroy() -> None:
    """Bypass REST and use the registry directly."""
    session = create_session(center_freq=14_205_000, mode="USB")
    rid = session.receiver_id
    assert get_session(rid) is session
    assert session in list_sessions()

    # Cleanup via async destroy.
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        destroyed = loop.run_until_complete(destroy_session(rid))
        assert destroyed is True
    finally:
        loop.close()

    assert get_session(rid) is None


def test_create_with_explicit_id_conflict() -> None:
    """Creating with an id that already exists should raise ValueError."""
    create_session(receiver_id="rx-conflict-test")
    with pytest.raises(ValueError, match="already in use"):
        create_session(receiver_id="rx-conflict-test")
    # Cleanup
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(destroy_session("rx-conflict-test"))
    finally:
        loop.close()
