"""Tests for propagation intelligence (slice-62)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openwebrx_plus.propagation import (  # noqa: E402
    PropagationData,
    PropagationService,
    get_propagation_service,
    reset_propagation_service,
)


def test_propagation_data_defaults():
    d = PropagationData()
    assert d.solar_flux_index is None
    assert d.k_index is None
    assert d.band_conditions == {"daytime": "unknown", "nighttime": "unknown"}


def test_band_conditions_excellent():
    d = PropagationData(solar_flux_index=180, k_index=1)
    bc = d.band_conditions
    assert bc["daytime"] == "excellent"


def test_band_conditions_poor():
    d = PropagationData(solar_flux_index=80, k_index=6)
    bc = d.band_conditions
    assert bc["daytime"] == "poor"
    assert bc["nighttime"] == "poor"


def test_band_conditions_good():
    d = PropagationData(solar_flux_index=140, k_index=2)
    bc = d.band_conditions
    assert bc["daytime"] == "good"


def test_propagation_data_to_dict():
    d = PropagationData(solar_flux_index=120, sunspot_number=80, k_index=3)
    result = d.to_dict()
    assert result["solar_flux_index"] == 120
    assert result["sunspot_number"] == 80
    assert result["k_index"] == 3
    assert result["stale"] is False


async def test_service_caches():
    svc = PropagationService(cache_ttl_s=60)
    call_count = 0

    async def fetcher():
        nonlocal call_count
        call_count += 1
        return PropagationData(solar_flux_index=150, k_index=2, timestamp=1.0)

    svc.set_fetcher(fetcher)
    # First call fetches.
    d1 = await svc.get()
    assert d1.solar_flux_index == 150
    assert call_count == 1
    # Second call uses cache.
    d2 = await svc.get()
    assert d2.solar_flux_index == 150
    assert call_count == 1  # no new fetch


async def test_service_stale_on_failure():
    svc = PropagationService(cache_ttl_s=0)  # always stale → always fetch

    async def good_fetcher():
        return PropagationData(solar_flux_index=100, k_index=2, timestamp=1.0)

    async def bad_fetcher():
        raise RuntimeError("network error")

    svc.set_fetcher(good_fetcher)
    d1 = await svc.get()
    assert d1.solar_flux_index == 100
    assert not d1.stale
    # Now switch to a failing fetcher — should return stale cached data.
    svc.set_fetcher(bad_fetcher)
    svc._cached_at = 0  # force cache expiry
    d2 = await svc.get()
    assert d2.solar_flux_index == 100  # cached
    assert d2.stale is True


async def test_service_returns_unknown_on_first_failure():
    svc = PropagationService()

    async def bad_fetcher():
        raise RuntimeError("no network")

    svc.set_fetcher(bad_fetcher)
    d = await svc.get()
    assert d.stale is True
    assert d.solar_flux_index is None


async def test_singleton():
    reset_propagation_service()
    s1 = get_propagation_service()
    s2 = get_propagation_service()
    assert s1 is s2
    reset_propagation_service()
