"""Propagation intelligence — solar flux + band conditions (slice-62).

Fetches solar flux data and amateur radio band conditions from public
APIs (with TTL caching + stale-on-failure, same pattern as the directory
service). Provides a REST endpoint for the frontend to render a band
conditions panel.

Data sources:
  - NOAA SWPC (Space Weather Prediction Center) for solar flux / sunspot
    numbers (https://services.swpc.noaa.gov/json/solar/solar_observations.json)
  - HamQSL for band conditions (https://hamqsl.com/solarxml.php — XML,
    but we use a JSON-mirrored version)

The module is tolerant of network failures (sandbox egress is filtered) —
it returns the last cached data with a "stale" flag, or a default
"unknown" response if no data has ever been fetched.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

_CACHE_TTL_S = 300  # 5 minutes
_NOAA_URL = "https://services.swpc.noaa.gov/json/solar/solar_observations.json"


@dataclass
class PropagationData:
    """Solar flux + band conditions data."""
    solar_flux_index: float | None = None  # SFI (10.7 cm flux)
    sunspot_number: int | None = None
    a_index: int | None = None  # geomagnetic A index
    k_index: int | None = None  # geomagnetic K index
    x_class_flare: bool = False
    timestamp: float = 0.0
    stale: bool = False
    source: str = "noaa_swpc"

    def to_dict(self) -> dict[str, Any]:
        return {
            "solar_flux_index": self.solar_flux_index,
            "sunspot_number": self.sunspot_number,
            "a_index": self.a_index,
            "k_index": self.k_index,
            "x_class_flare": self.x_class_flare,
            "timestamp": self.timestamp,
            "stale": self.stale,
            "source": self.source,
        }

    @property
    def band_conditions(self) -> dict[str, str]:
        """Estimated band conditions based on SFI + K index."""
        if self.solar_flux_index is None or self.k_index is None:
            return {"daytime": "unknown", "nighttime": "unknown"}
        # Simple heuristic: high SFI = good, high K = bad.
        sfi = self.solar_flux_index
        k = self.k_index
        if sfi > 150 and k < 3:
            cond = "excellent"
        elif sfi > 120 and k < 4:
            cond = "good"
        elif sfi > 90 and k < 5:
            cond = "fair"
        elif k >= 5:
            cond = "poor"
        else:
            cond = "marginal"
        # Daytime bands (20m-10m) are more SFI-sensitive; nighttime (40m-80m)
        # are more K-index sensitive.
        if k >= 5:
            return {"daytime": "poor", "nighttime": "poor"}
        return {"daytime": cond, "nighttime": "fair" if sfi > 100 else "marginal"}


class PropagationService:
    """Fetches + caches propagation data with TTL + stale-on-failure."""

    def __init__(self, cache_ttl_s: int = _CACHE_TTL_S) -> None:
        self._cache_ttl = cache_ttl_s
        self._cached: PropagationData | None = None
        self._cached_at: float = 0.0
        self._lock = asyncio.Lock()
        self._fetcher: Any = None  # injectable for tests

    def set_fetcher(self, fetcher: Any) -> None:
        """Inject a custom fetcher for testing (mock httpx.AsyncClient.get)."""
        self._fetcher = fetcher

    async def get(self) -> PropagationData:
        """Get propagation data, fetching if cache is stale."""
        now = time.time()
        if self._cached and (now - self._cached_at) < self._cache_ttl:
            return self._cached
        async with self._lock:
            # Double-check after acquiring lock.
            if self._cached and (time.time() - self._cached_at) < self._cache_ttl:
                return self._cached
            try:
                data = await self._fetch()
                self._cached = data
                self._cached_at = time.time()
                return data
            except Exception as exc:  # noqa: BLE001
                log.warning("propagation fetch failed", error=str(exc))
                if self._cached:
                    self._cached.stale = True
                    return self._cached
                result: PropagationData = PropagationData(timestamp=now, stale=True)
                return result

    async def _fetch(self) -> PropagationData:
        """Fetch from NOAA SWPC."""
        if self._fetcher is not None:
            result: PropagationData = await self._fetcher()
            return result
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_NOAA_URL)
            resp.raise_for_status()
            data = resp.json()
        # Parse the NOAA JSON (it's a list of observation dicts).
        sfi: float | None = None
        sunspot: int | None = None
        a_index: int | None = None
        k_index: int | None = None
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                # NOAA uses keys like "flux", "sunspot", "ssa", "kk".
                if "flux" in entry and isinstance(entry["flux"], (int, float)):
                    sfi = float(entry["flux"])
                if "sunspot" in entry and isinstance(entry["sunspot"], (int, float)):
                    sunspot = int(entry["sunspot"])
                if "kk" in entry and isinstance(entry["kk"], (int, float)):
                    k_index = int(entry["kk"])
                if "ssa" in entry and isinstance(entry["ssa"], (int, float)):
                    a_index = int(entry["ssa"])
        return PropagationData(
            solar_flux_index=sfi,
            sunspot_number=sunspot,
            a_index=a_index,
            k_index=k_index,
            timestamp=time.time(),
            source="noaa_swpc",
        )

    @property
    def is_cached(self) -> bool:
        return self._cached is not None

    def clear_cache(self) -> None:
        self._cached = None
        self._cached_at = 0.0


# Module-level singleton (same pattern as directory.py).
_propagation_service: PropagationService | None = None


def get_propagation_service() -> PropagationService:
    global _propagation_service
    if _propagation_service is None:
        _propagation_service = PropagationService()
    return _propagation_service


def reset_propagation_service() -> None:
    """Reset the singleton (for tests)."""
    global _propagation_service
    _propagation_service = None
