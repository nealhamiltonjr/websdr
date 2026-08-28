"""Smoke test — boot the FastAPI app and hit /api/health."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from openwebrx_plus.api.rest import create_app
from openwebrx_plus.config import Settings


@pytest.fixture
def client() -> TestClient:
    settings = Settings(tier="dev")
    app = create_app(settings)
    return TestClient(app)


def test_health(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_version(client: TestClient) -> None:
    r = client.get("/api/version")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "0.1.0"
    assert body["tier"] == "dev"
