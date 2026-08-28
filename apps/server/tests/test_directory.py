"""Tests for the remote-receiver directory service (ADR-006).

Everything runs against an injected JSON fetcher — CI never talks to
rx.kiwisdr.com or receiverbook.de (same hardware-free rule as the fake
SDR servers). Covers: parsing + normalization, field tolerance, TTL
caching, forced refresh, stale-on-failure graceful degradation, and the
REST endpoints' happy/503 paths.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openwebrx_plus.api.rest import create_app
from openwebrx_plus.config import Settings
from openwebrx_plus.sources.directory import (
    DirectoryService,
    DirectoryUnavailable,
    directory_service,
)

KIWI_DOC: dict[str, Any] = {
    "recvs": [
        {
            "id": "xx0001",
            "name": "Test Kiwi East",
            "url": "http://kiwi-east.example.com:8073/",
            "loc": [40.7128, -74.0060],
            "users": "2/8",
            "online": True,
            "flags": "low_bandwidth",
        },
        {
            "id": "xx0002",
            "name": "Test Kiwi West",
            "url": "http://kiwi-west.example.com:8073/",
            "loc": [37.7749, -122.4194],
            "users": "0/4",
            "online": False,
            "notes": "antenna: 40m dipole",
        },
        {"id": "xx0003", "url": "http://no-name.example.com:8073/"},  # no name → skip
        {"id": "xx0004", "name": "No URL Kiwi"},  # no url → skip
        "garbage-not-a-dict",  # tolerated → skip
    ]
}

RECEIVERBOOK_LIST: list[dict[str, Any]] = [
    {
        "id": 1,
        "name": "Owrx Alpha",
        "url": "http://alpha.example.com:8073/",
        "lat": 52.52,
        "lon": 13.405,
    },
    {
        "name": "Owrx Beta",
        "url": "http://beta.example.com/",
        "location": {"lat": 1.0, "lng": 2.0},  # nested geo
    },
]


class _CountingFetcher:
    """Injectable fetcher: returns canned docs, counts calls, can fail."""

    def __init__(self, docs: dict[str, Any] | None = None, fail_on_call: int | None = None) -> None:
        self.calls: list[str] = []
        self.docs = docs or {}
        self.fail_on_call = fail_on_call

    async def __call__(self, url: str) -> Any:
        self.calls.append(url)
        if self.fail_on_call is not None and len(self.calls) >= self.fail_on_call:
            raise OSError("simulated network failure")
        if url in self.docs:
            return self.docs[url]
        raise OSError(f"unexpected url: {url}")


class TestKiwiParsing:
    async def test_parses_and_normalizes(self) -> None:
        fetcher = _CountingFetcher({DirectoryService.KIWI_URL: KIWI_DOC})
        svc = DirectoryService(fetch_json=fetcher)
        receivers = await svc.list_kiwi()
        # 2 well-formed entries + the nameless-but-usable one (id fallback);
        # "No URL Kiwi" and the garbage string are skipped as unusable.
        assert len(receivers) == 3
        east, west, nameless = receivers
        assert east.directory == "kiwi"
        assert east.source_type == "kiwi"  # spawnable today
        assert east.id == "xx0001"
        assert east.name == "Test Kiwi East"
        assert east.url == "http://kiwi-east.example.com:8073/"
        assert east.lat == pytest.approx(40.7128)
        assert east.lon == pytest.approx(-74.006)
        assert east.users == "2/8"
        assert east.online is True
        assert east.extra.get("flags") == "low_bandwidth"
        assert west.online is False
        assert west.extra.get("notes") == "antenna: 40m dipole"
        assert nameless.name == "xx0003"  # id fallback keeps it usable
        assert nameless.url == "http://no-name.example.com:8073/"

    async def test_to_dict_is_wire_safe(self) -> None:
        fetcher = _CountingFetcher({DirectoryService.KIWI_URL: KIWI_DOC})
        svc = DirectoryService(fetch_json=fetcher)
        receivers = await svc.list_kiwi()
        d = receivers[0].to_dict()
        assert d["source_type"] == "kiwi"
        assert d["name"] == "Test Kiwi East"
        assert d["lat"] == pytest.approx(40.7128)
        assert d["online"] is True


class TestReceiverbookParsing:
    async def test_bare_list(self) -> None:
        fetcher = _CountingFetcher({DirectoryService.RECEIVERBOOK_URL: RECEIVERBOOK_LIST})
        svc = DirectoryService(fetch_json=fetcher)
        receivers = await svc.list_receiverbook()
        assert len(receivers) == 2
        alpha, beta = receivers
        assert alpha.directory == "receiverbook"
        assert alpha.source_type == "openwebrx_remote"  # federation client (ADR-006)
        assert alpha.lat == pytest.approx(52.52)
        assert beta.lat == pytest.approx(1.0)  # from nested location
        assert beta.lon == pytest.approx(2.0)

    async def test_wrapped_dict(self) -> None:
        fetcher = _CountingFetcher(
            {DirectoryService.RECEIVERBOOK_URL: {"receivers": RECEIVERBOOK_LIST}}
        )
        svc = DirectoryService(fetch_json=fetcher)
        receivers = await svc.list_receiverbook()
        assert len(receivers) == 2


class TestCaching:
    async def test_ttl_cache_prevents_refetch(self) -> None:
        fetcher = _CountingFetcher({DirectoryService.KIWI_URL: KIWI_DOC})
        svc = DirectoryService(ttl_s=60.0, fetch_json=fetcher)
        first = await svc.list_kiwi()
        second = await svc.list_kiwi()
        assert len(fetcher.calls) == 1
        assert first == second

    async def test_refresh_forces_refetch(self) -> None:
        fetcher = _CountingFetcher({DirectoryService.KIWI_URL: KIWI_DOC})
        svc = DirectoryService(ttl_s=60.0, fetch_json=fetcher)
        await svc.list_kiwi()
        await svc.list_kiwi(refresh=True)
        assert len(fetcher.calls) == 2

    async def test_expiry_refetches(self) -> None:
        fetcher = _CountingFetcher({DirectoryService.KIWI_URL: KIWI_DOC})
        svc = DirectoryService(ttl_s=0.05, fetch_json=fetcher)
        await svc.list_kiwi()
        await asyncio.sleep(0.06)
        await svc.list_kiwi()
        assert len(fetcher.calls) == 2

    async def test_stale_served_when_refresh_fails(self) -> None:
        # Fails from the 2nd call onward — but a stale copy exists by then.
        fetcher = _CountingFetcher({DirectoryService.KIWI_URL: KIWI_DOC}, fail_on_call=2)
        svc = DirectoryService(ttl_s=0.05, fetch_json=fetcher)
        first = await svc.list_kiwi()
        await asyncio.sleep(0.06)
        second = await svc.list_kiwi()  # does NOT raise
        assert second == first  # stale copy served

    async def test_unavailable_when_never_fetched(self) -> None:
        fetcher = _CountingFetcher({}, fail_on_call=1)
        svc = DirectoryService(fetch_json=fetcher)
        with pytest.raises(DirectoryUnavailable, match="unreachable"):
            await svc.list_kiwi()


class TestDirectoryRest:
    def test_kiwi_endpoint_lists_receivers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_fetch(url: str) -> Any:
            return KIWI_DOC

        directory_service.invalidate()
        monkeypatch.setattr(directory_service, "_fetch_json", fake_fetch)
        app = create_app(Settings(tier="dev"))
        with TestClient(app) as client:
            resp = client.get("/api/directory/kiwi")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["directory"] == "kiwi"
            assert body["count"] == 3
            entry = body["receivers"][0]
            assert entry["source_type"] == "kiwi"
            assert entry["name"] == "Test Kiwi East"
            assert entry["users"] == "2/8"

    def test_kiwi_endpoint_503_when_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def broken_fetch(url: str) -> Any:
            raise OSError("no route to host")

        directory_service.invalidate()
        monkeypatch.setattr(directory_service, "_fetch_json", broken_fetch)
        app = create_app(Settings(tier="dev"))
        with TestClient(app) as client:
            resp = client.get("/api/directory/kiwi")
            assert resp.status_code == 503
            assert "unreachable" in resp.json()["detail"]

    def test_receiverbook_endpoint_lists_receivers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_fetch(url: str) -> Any:
            return RECEIVERBOOK_LIST

        directory_service.invalidate()
        monkeypatch.setattr(directory_service, "_fetch_json", fake_fetch)
        app = create_app(Settings(tier="dev"))
        with TestClient(app) as client:
            resp = client.get("/api/directory/receiverbook")
            assert resp.status_code == 200
            body = resp.json()
            assert body["directory"] == "receiverbook"
            assert body["count"] == 2
            assert body["receivers"][0]["source_type"] == "openwebrx_remote"
        directory_service.invalidate()  # don't leak fake data into other tests
