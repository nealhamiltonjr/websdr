"""Remote receiver directories — discovery for network sources (ADR-006).

A *directory* answers "which receivers exist on the internet right now?":

  * **KiwiSDR** — https://rx.kiwisdr.com/json/ lists the 1000+ public Kiwi
    receivers (name, URL, lat/lon, user count, online flag). Every entry is
    spawnable today via ``source_type="kiwi"`` with the host/port parsed
    from the URL.
  * **receiverbook.de** — the registry of public OpenWebRX / OpenWebRX+
    receivers. Entries map to the federation client
    (``source_type="openwebrx_remote"``, ADR-006): every entry is
    spawnable — POST /api/receivers with the host/port parsed from the
    entry's URL (deep-link fragments included when present).

Design points:

  * TTL cache (default 5 min) with single-flight locking — the directory is
    shared by every client of this process; we fetch it at most once per
    TTL window.
  * Graceful degradation: if a refresh fails but a stale list exists, the
    stale list is served (logged) instead of erroring.
  * The HTTP fetcher is injectable so tests run without network (ADR-006
    test strategy: never depend on live directories in CI).
  * Parsers are deliberately field-tolerant: these are third-party JSON
    APIs whose exact shapes have shifted over the years, and a single weird
    entry must never poison the whole list (skip, don't crash).

Schema note: the Kiwi JSON shape is documented from its public use
(``{"recvs": [{"id", "name", "url", "loc": [lat, lon], "users", "online",
...}]}``); receiverbook's API is parsed defensively. Both are marked for
verification on first live use — see ADR-006 bring-up notes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)

JsonFetcher = Callable[[str], Awaitable[Any]]


@dataclass(frozen=True)
class RemoteReceiver:
    """One entry in a remote receiver directory (wire-safe)."""

    directory: str  # "kiwi" | "receiverbook"
    source_type: str  # what to pass to POST /api/receivers ("kiwi", "openwebrx_remote")
    id: str
    name: str
    url: str
    lat: float | None = None
    lon: float | None = None
    users: str | None = None  # e.g. "1/4" (in use / capacity)
    online: bool | None = None  # None = unknown
    extra: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "directory": self.directory,
            "source_type": self.source_type,
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "lat": self.lat,
            "lon": self.lon,
            "users": self.users,
            "online": self.online,
            **({"extra": self.extra} if self.extra else {}),
        }


class DirectoryUnavailable(RuntimeError):
    """The directory endpoint could not be fetched (and no stale copy)."""


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_kiwi(doc: Any) -> list[RemoteReceiver]:
    """Parse https://rx.kiwisdr.com/json/ → RemoteReceiver list."""
    entries: list[dict[str, Any]] = []
    if isinstance(doc, dict):
        raw = doc.get("recvs") or doc.get("receivers") or []
        if isinstance(raw, list):
            entries = [e for e in raw if isinstance(e, dict)]
    elif isinstance(doc, list):
        entries = [e for e in doc if isinstance(e, dict)]

    receivers: list[RemoteReceiver] = []
    for e in entries:
        name = _as_str(e.get("name")) or _as_str(e.get("id"))
        url = _as_str(e.get("url"))
        if not name or not url:
            continue
        loc = e.get("loc")
        lat = lon = None
        if isinstance(loc, (list, tuple)) and len(loc) >= 2:
            lat = _as_float(loc[0])
            lon = _as_float(loc[1])
        online = e.get("online")
        extra = {
            k: v
            for k, v in (
                ("flags", e.get("flags")),
                ("notes", e.get("notes")),
            )
            if _as_str(v) is not None
        }
        receivers.append(
            RemoteReceiver(
                directory="kiwi",
                source_type="kiwi",
                id=_as_str(e.get("id")) or url,
                name=name,
                url=url,
                lat=lat,
                lon=lon,
                users=_as_str(e.get("users")),
                online=online if isinstance(online, bool) else None,
                extra=extra,  # type: ignore[arg-type]
            )
        )
    return receivers


def _parse_receiverbook(doc: Any) -> list[RemoteReceiver]:
    """Parse receiverbook.de's receiver list → RemoteReceiver list.

    Field-tolerant on purpose: accepts a bare list or a dict wrapping one,
    and looks for name/url/lat/lon under the common key spellings. These
    receivers speak the OpenWebRX protocol — every entry is spawnable via
    the federation client (``source_type="openwebrx_remote"``).
    """
    entries: list[dict[str, Any]] = []
    if isinstance(doc, dict):
        for key in ("receivers", "results", "data", "items"):
            raw = doc.get(key)
            if isinstance(raw, list):
                entries = [e for e in raw if isinstance(e, dict)]
                break
    elif isinstance(doc, list):
        entries = [e for e in doc if isinstance(e, dict)]

    receivers: list[RemoteReceiver] = []
    for e in entries:
        name = _as_str(e.get("name")) or _as_str(e.get("title"))
        url = _as_str(e.get("url")) or _as_str(e.get("address"))
        if not name or not url:
            continue
        lat = _as_float(e.get("lat") or e.get("latitude"))
        lon = _as_float(e.get("lon") or e.get("lng") or e.get("longitude"))
        if lat is None or lon is None:
            loc = e.get("location")
            if isinstance(loc, dict):
                lat = lat if lat is not None else _as_float(loc.get("lat"))
                lon = lon if lon is not None else _as_float(loc.get("lon") or loc.get("lng"))
        receivers.append(
            RemoteReceiver(
                directory="receiverbook",
                source_type="openwebrx_remote",
                id=_as_str(e.get("id")) or url,
                name=name,
                url=url,
                lat=lat,
                lon=lon,
                users=_as_str(e.get("users")),
                online=None,  # registry has no live-status field we can rely on
            )
        )
    return receivers


async def _default_fetch_json(url: str) -> Any:
    import httpx

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


@dataclass
class _CacheEntry:
    fetched_at: float
    receivers: list[RemoteReceiver]


class DirectoryService:
    """TTL-cached access to remote receiver directories."""

    KIWI_URL = "https://rx.kiwisdr.com/json/"
    RECEIVERBOOK_URL = "https://receiverbook.de/api/receivers.json"

    def __init__(
        self,
        ttl_s: float = 300.0,
        fetch_json: JsonFetcher | None = None,
    ) -> None:
        self._ttl = ttl_s
        self._fetch_json = fetch_json or _default_fetch_json
        self._cache: dict[str, _CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def _list(
        self,
        key: str,
        url: str,
        parser: Callable[[Any], list[RemoteReceiver]],
        refresh: bool = False,
    ) -> list[RemoteReceiver]:
        cached = self._cache.get(key)
        fresh = cached is not None and (time.monotonic() - cached.fetched_at) < self._ttl
        if cached is not None and fresh and not refresh:
            return cached.receivers

        lock = self._lock(key)
        if lock.locked():  # a fetch is already in flight — wait for it, reuse it
            async with lock:
                updated = self._cache.get(key)
                if updated is not None:
                    return updated.receivers
        async with lock:
            # Re-check under the lock: another waiter may have refreshed.
            cached = self._cache.get(key)
            fresh = (
                cached is not None
                and (time.monotonic() - cached.fetched_at) < self._ttl
            )
            if cached is not None and fresh and not refresh:
                return cached.receivers
            try:
                doc = await self._fetch_json(url)
                receivers = parser(doc)
                self._cache[key] = _CacheEntry(time.monotonic(), receivers)
                log.info("directory refreshed", directory=key, count=len(receivers))
                return receivers
            except Exception as exc:
                reason = str(exc) or type(exc).__name__
                if cached is not None:
                    log.warning(
                        "directory refresh failed — serving stale copy",
                        directory=key,
                        error=reason,
                        age_s=round(time.monotonic() - cached.fetched_at, 1),
                    )
                    return cached.receivers
                raise DirectoryUnavailable(
                    f"{key} directory unreachable: {reason}"
                ) from exc

    async def list_kiwi(self, refresh: bool = False) -> list[RemoteReceiver]:
        """Public KiwiSDR receivers. Every entry is spawnable as source_type='kiwi'."""
        return await self._list("kiwi", self.KIWI_URL, _parse_kiwi, refresh)

    async def list_receiverbook(self, refresh: bool = False) -> list[RemoteReceiver]:
        """Public OpenWebRX receivers (receiverbook.de) — federation roadmap."""
        return await self._list("receiverbook", self.RECEIVERBOOK_URL, _parse_receiverbook, refresh)

    def invalidate(self) -> None:
        """Drop all cached lists (next call refetches)."""
        self._cache.clear()


# Process-wide singleton used by the REST layer; tests monkeypatch its
# fetcher or construct their own DirectoryService.
directory_service = DirectoryService()


__all__ = [
    "DirectoryService",
    "DirectoryUnavailable",
    "RemoteReceiver",
    "directory_service",
]
