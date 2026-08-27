"""Tests for the SDRangel source scaffold (slice-20).

The Source class is a manifest-only scaffold: registered so the UI
can advertise SDRangel support, but spawn()/tune()/set_mode() raise
NotImplementedError. These tests verify:

  - the manifest is registered in SourceRegistry.builtin_manifests()
  - the manifest advertises the right source_type, sdk, gain_range
  - the class validates constructor args (host, port, device_set)
  - spawn()/tune()/set_mode() all raise NotImplementedError with an
    actionable message pointing to the implementation plan

The actual REST+WS streaming implementation lands in a future slice.
"""

from __future__ import annotations

import pytest

from openwebrx_plus.sources import SDRangelSource, SourceRegistry


def test_manifest_is_registered() -> None:
    """The SDRangel manifest must appear in SourceRegistry's builtin list
    so the UI can advertise SDRangel support."""
    manifests = SourceRegistry.builtin_manifests()
    types = [m.source_type for m in manifests]
    assert "sdrangel" in types, f"sdrangel missing from {types}"


def test_manifest_fields_match_class() -> None:
    """The manifest's source_type, sdk, and gain_range must match the
    SDRangelSource class."""
    manifests = SourceRegistry.builtin_manifests()
    sdrangel = next(m for m in manifests if m.source_type == "sdrangel")
    assert sdrangel.label.startswith("SDRangel")
    assert "REST API" in sdrangel.sdk
    assert sdrangel.hardware_required is False  # remote
    assert sdrangel.gain_range is not None
    lo, hi = sdrangel.gain_range
    assert lo == 0.0
    assert hi == 49.0
    assert sdrangel.factory_entrypoint == "openwebrx_plus.sources.sdrangel:SDRangelSource"
    # The manifest description must mention the slice-20 scaffold
    # status so operators don't expect a working impl.
    assert "scaffold" in sdrangel.description.lower()


def test_constructor_validates_host() -> None:
    with pytest.raises(ValueError, match="host is required"):
        SDRangelSource(host="")


def test_constructor_validates_port() -> None:
    with pytest.raises(ValueError, match="port"):
        SDRangelSource(host="sdr.example.com", port=70_000)


def test_constructor_validates_device_set() -> None:
    with pytest.raises(ValueError, match="device_set"):
        SDRangelSource(host="sdr.example.com", device_set=-1)


def test_constructor_validates_sample_rate() -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        SDRangelSource(host="sdr.example.com", sample_rate=100)


def test_constructor_validates_connect_timeout() -> None:
    with pytest.raises(ValueError, match="connect_timeout"):
        SDRangelSource(host="sdr.example.com", connect_timeout=0)


def test_constructor_advertises_fixed_sample_rate() -> None:
    """`fixed_sample_rate` is the contract Source uses to tell
    ReceiverSession what rate the DSP chains should expect."""
    s = SDRangelSource(host="sdr.example.com", sample_rate=1_800_000)
    assert s.fixed_sample_rate == 1_800_000


def test_spawn_raises_not_implemented() -> None:
    """Slice-20: the spawn() path raises NotImplementedError with an
    actionable message. The manifest is the scaffold; the REST+WS
    impl lands in a future slice.

    spawn is declared ``async def ... -> AsyncGenerator`` so calling
    it returns an async generator. The body raises before any yield;
    driving the AG via ``.asend(None)`` runs the body and re-raises.
    """
    s = SDRangelSource(host="sdr.example.com")
    gen = s.spawn(center_freq=14_150_000, sample_rate=2_400_000)
    # Async generators use .asend() (not .send()) to advance.
    coro = gen.asend(None)
    with pytest.raises(NotImplementedError, match="slice-20 manifest scaffold"):
        coro.send(None)


def test_tune_raises_not_implemented() -> None:
    s = SDRangelSource(host="sdr.example.com")
    # Calling tune() returns a coroutine that raises NotImplementedError
    # when awaited. We construct the coroutine and call .send(None) to
    # drive it (avoids pytest-asyncio's AUTO mode intercepting
    # asyncio.run inside a sync test).
    coro = s.tune(14_200_000)
    with pytest.raises(NotImplementedError, match="slice-20"):
        coro.send(None)


def test_set_mode_raises_not_implemented() -> None:
    s = SDRangelSource(host="sdr.example.com")
    coro = s.set_mode("NFM")
    with pytest.raises(NotImplementedError, match="slice-20"):
        coro.send(None)


def test_close_is_noop() -> None:
    """close() must not raise — even before any spawn was attempted."""
    s = SDRangelSource(host="sdr.example.com")
    # close() is async; awaiting it returns None.
    coro = s.close()
    try:
        result = coro.send(None)
    except StopIteration as stop:
        result = stop.value
    assert result is None


def test_default_port_is_8091() -> None:
    """SDRangel's default REST API port is 8091 (its upstream default)."""
    s = SDRangelSource(host="sdr.example.com")
    assert s.port == 8091


def test_default_sample_rate_is_2_4_msps() -> None:
    """The default sample rate matches a typical SDRangel device's
    rate for an RTL-SDR-class device (2.4 MSPS — same as the local
    RTL-SDR default)."""
    s = SDRangelSource(host="sdr.example.com")
    assert s.sample_rate == 2_400_000


def test_user_agent_identifies_client() -> None:
    """The User-Agent must identify the client honestly (ADR-006
    federation etiquette — public SDRangel instances are volunteer-run)."""
    s = SDRangelSource(host="sdr.example.com")
    assert "openwebrx_plus" in s.user
