"""Hardware detection — probe all drivers for connected SDRs.

Powers ``GET /api/hardware`` (frontend source picker shows what's actually
plugged in) and gives bring-up day a single "what do you see?" answer.

Design notes:
  * Every probe is defensive: a driver whose library is missing, a USB
    permission error, an API mismatch — all resolve to "no devices", never
    an exception. One flaky driver must not hide the others.
  * ctypes/cffi probes block; they run in worker threads
    (``asyncio.to_thread``) and the whole sweep completes within the
    connect timeout.
  * rtl_tcp is a network transport, so we also probe the default
    host:port — a remote stick looks like local hardware.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class HardwareDevice:
    """One connected (or reachable) SDR."""

    driver: str  # source_type key: rtl_sdr | airspy | sdrplay | soapy
    label: str
    serial: str | None = None
    transport: str = "usb"
    endpoint: str | None = None  # e.g. "127.0.0.1:1234", "usb:0"
    index: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "driver": self.driver,
            "label": self.label,
            "serial": self.serial,
            "transport": self.transport,
            "endpoint": self.endpoint,
            "index": self.index,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# Individual probes — each returns list[HardwareDevice] or raises (swallowed)
# ---------------------------------------------------------------------------


def _probe_rtl_usb() -> list[HardwareDevice]:
    from .rtl_sdr import _configure_librtlsdr, _load_librtlsdr

    lib = _load_librtlsdr()
    if lib is None:
        return []
    _configure_librtlsdr(lib)
    count = int(lib.rtlsdr_get_device_count())
    devices: list[HardwareDevice] = []
    for idx in range(count):
        name_bytes = lib.rtlsdr_get_device_name(idx) or b"RTL2832U"
        name = name_bytes.decode("utf-8", errors="replace")
        # R828D tuners are what the RTL-SDR Blog V3/V4 use; distinguishing
        # V4 needs librtlsdr >= 0.8 (V4 RF path) — report the tuner honestly.
        devices.append(
            HardwareDevice(
                driver="rtl_sdr",
                label=f"RTL-SDR #{idx} ({name})",
                transport="usb",
                endpoint=f"usb:{idx}",
                index=idx,
                details={"tuner": name},
            )
        )
    return devices


async def _probe_rtl_tcp(host: str = "127.0.0.1", port: int = 1234,
                         timeout_s: float = 0.4) -> list[HardwareDevice]:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout_s
        )
    except (TimeoutError, OSError):
        return []
    try:
        header = await asyncio.wait_for(reader.readexactly(12), timeout=timeout_s)
        if header[:4] != b"RTL0":
            return []
        tuner_type, gain_count = struct.unpack("<II", header[4:12])
        return [
            HardwareDevice(
                driver="rtl_sdr",
                label=f"rtl_tcp @ {host}:{port}",
                transport="tcp",
                endpoint=f"{host}:{port}",
                details={
                    "tuner_type": tuner_type,
                    "gain_count": gain_count,
                    "host": host,
                    "port": port,
                },
            )
        ]
    except (TimeoutError, asyncio.IncompleteReadError):
        return []
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()


def _probe_airspy() -> list[HardwareDevice]:
    from .airspy import AirspyBinding

    binding = AirspyBinding()
    serials = binding.list_serials()
    return [
        HardwareDevice(
            driver="airspy",
            label=f"Airspy {hex(serial)}",
            serial=str(serial),
            transport="usb",
            endpoint=f"usb-serial:{serial}",
            details={"serial": serial},
        )
        for serial in serials
    ]


def _probe_sdrplay() -> list[HardwareDevice]:
    from .sdrplay import SdrplayBinding

    binding = SdrplayBinding()
    devices = binding.list_devices()
    return [
        HardwareDevice(
            driver="sdrplay",
            label=(
                f"SDRplay {d.get('hw_ver') or 'RSP'} "
                f"{d.get('serial') or ''}".strip()
            ),
            serial=d.get("serial"),
            transport="usb",
            endpoint=f"usb-devnum:{d.get('dev_num')}",
            index=int(d.get("dev_num") or 0),
            details={"hw_ver": d.get("hw_ver"), "duo_mode": d.get("duo_mode")},
        )
        for d in devices
    ]


def _probe_soapy() -> list[HardwareDevice]:
    from .soapy import SoapyBinding

    binding = SoapyBinding()
    out: list[HardwareDevice] = []
    for idx, kwargs in enumerate(binding.enumerate()):
        label = f"SoapySDR {kwargs.get('driver', '?')}"
        if kwargs.get("serial"):
            label += f" serial={kwargs['serial']}"
        out.append(
            HardwareDevice(
                driver="soapy",
                label=label,
                serial=str(kwargs.get("serial")) if kwargs.get("serial") else None,
                transport="usb",
                endpoint=str(kwargs),
                index=idx,
                details=kwargs,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


async def detect_hardware(
    rtl_tcp_hosts: list[tuple[str, int]] | None = None,
    timeout_s: float = 0.4,
) -> list[HardwareDevice]:
    """Probe every driver; return everything that answers.

    ``rtl_tcp_hosts`` defaults to [(127.0.0.1, 1234)]; pass additional
    (host, port) pairs to discover networked sticks.
    """
    hosts = rtl_tcp_hosts if rtl_tcp_hosts is not None else [("127.0.0.1", 1234)]
    tasks: list[Any] = [
        asyncio.to_thread(_probe_rtl_usb),
        *(_probe_rtl_tcp(h, p, timeout_s) for h, p in hosts),
        asyncio.to_thread(_probe_airspy),
        asyncio.to_thread(_probe_sdrplay),
        asyncio.to_thread(_probe_soapy),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    devices: list[HardwareDevice] = []
    for result in results:
        if isinstance(result, BaseException):
            # A driver whose SDK is missing / incompatible → absent, not fatal.
            log.debug("hardware probe skipped", reason=str(result))
            continue
        devices.extend(result)
    log.info("hardware detection complete", devices=[d.label for d in devices])
    return devices
