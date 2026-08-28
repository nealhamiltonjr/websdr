"""Hardware-detection probe tests — monkeypatched driver probes.

One flaky/missing SDK must never hide other devices; the sweep merges
results and swallows probe failures.
"""

from __future__ import annotations

from typing import Any

from openwebrx_plus.sources import probe
from openwebrx_plus.sources.probe import HardwareDevice, detect_hardware


def _patch_all(monkeypatch: Any, **overrides: Any) -> None:
    """Neutralize every probe; individual tests override specific ones."""
    defaults: dict[str, Any] = {
        "_probe_rtl_usb": lambda: [],
        "_probe_rtl_tcp": _no_rtl_tcp,
        "_probe_airspy": lambda: [],
        "_probe_sdrplay": lambda: [],
        "_probe_soapy": lambda: [],
    }
    defaults.update(overrides)
    for name, impl in defaults.items():
        monkeypatch.setattr(probe, name, impl)


async def _no_rtl_tcp(
    host: str = "127.0.0.1", port: int = 1234, timeout_s: float = 0.4
) -> list[dict[str, Any]]:
    return []


async def test_detect_merges_devices_across_drivers(monkeypatch: Any) -> None:
    _patch_all(
        monkeypatch,
        _probe_rtl_usb=lambda: [
            HardwareDevice(driver="rtl_sdr", label="RTL-SDR #0 (R828D)", endpoint="usb:0"),
        ],
        _probe_airspy=lambda: [
            HardwareDevice(driver="airspy", label="Airspy 0x6440", serial="0x6440"),
        ],
    )
    devices = await detect_hardware()
    drivers = {d.driver for d in devices}
    assert drivers == {"rtl_sdr", "airspy"}


async def test_detect_swallows_failing_probe(monkeypatch: Any) -> None:
    def broken_sdrplay() -> list[HardwareDevice]:
        raise RuntimeError("libmirsdrapi-rsp.so not found")

    _patch_all(
        monkeypatch,
        _probe_sdrplay=broken_sdrplay,
        _probe_rtl_usb=lambda: [HardwareDevice(driver="rtl_sdr", label="stick")],
    )
    devices = await detect_hardware()
    # SDRplay SDK missing → swallowed; the RTL stick is still reported.
    assert [d.driver for d in devices] == ["rtl_sdr"]


async def test_detect_empty_when_nothing_answers(monkeypatch: Any) -> None:
    _patch_all(monkeypatch)
    devices = await detect_hardware()
    assert devices == []


async def test_device_serialization(monkeypatch: Any) -> None:
    _patch_all(
        monkeypatch,
        _probe_soapy=lambda: [
            HardwareDevice(
                driver="soapy",
                label="SoapySDR airspy",
                serial="42",
                endpoint="usb",
                details={"driver": "airspy"},
            )
        ],
    )
    devices = await detect_hardware()
    assert devices[0].to_dict() == {
        "driver": "soapy",
        "label": "SoapySDR airspy",
        "serial": "42",
        "transport": "usb",
        "endpoint": "usb",
        "index": 0,
        "details": {"driver": "airspy"},
    }


async def test_detect_without_monkeypatch_is_safe() -> None:
    """The real sweep on this host (no SDR hardware expected) must not raise —
    it returns whatever it finds, possibly nothing."""
    devices = await detect_hardware(timeout_s=0.2)
    assert isinstance(devices, list)
