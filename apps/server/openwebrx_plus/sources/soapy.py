"""SoapySDR universal source backend (slice-3).

SoapySDR is the universal driver layer: Airspy, HackRF, BladeRF, LimeSDR,
PlutoSDR, USRP, RTL-SDR (via SoapyRTLSDR), SDRplay (via SoapySDRPlay3),
SDR++ remote, and dozens more all expose Soapy modules. This backend is
the "support any SDR with a driver, via a plugin" promise from ADR-004:
if a Soapy module exists for the hardware, it works here — no per-device
code required.

Requires the SoapySDR Python bindings (``import SoapySDR``). Debian/Ubuntu:
``apt install python3-soapysdr soapysdr-module-<driver>``; conda:
``conda install -c conda-forge soapysdr``.

The binding is seamed (:class:`SoapyBinding`) so tests inject a fake module.
Streaming runs ``readStream`` in the default executor and yields complex64
directly — SoapySDR can deliver CF32 natively, so no conversion needed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import structlog

from .base import SourceInfo

log = structlog.get_logger(__name__)


class SoapyBinding:
    """Narrow wrapper around the SoapySDR python module.

    Surface: enumerate(), make(args), and a Device proxy exposing only what
    this driver calls. Keeps the fake in tests honest about what we use.
    """

    def __init__(self, module: Any | None = None) -> None:
        if module is None:
            try:
                import SoapySDR  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "SoapySDR Python bindings not installed. "
                    "apt install python3-soapysdr (plus soapysdr-module-* "
                    "for your hardware), or conda install -c conda-forge "
                    "soapysdr."
                ) from exc
            module = SoapySDR
        self._m = module

    def enumerate(self) -> list[dict[str, Any]]:
        return [dict(d) for d in self._m.Device.enumerate()]

    def make(self, args: dict[str, Any]) -> Any:
        dev = self._m.Device(args)
        return _SoapyDeviceProxy(dev, self._m)


class _SoapyDeviceProxy:
    """Typed facade over a SoapySDR.Device instance."""

    def __init__(self, dev: Any, module: Any) -> None:
        self._dev = dev
        self._m = module
        self._stream: Any = None

    @property
    def hw_info(self) -> dict[str, Any]:
        try:
            return {
                "driver": self._dev.getDriverKey(),
                "hardware": self._dev.getHardwareKey(),
            }
        except Exception:  # noqa: BLE001 — informational only
            return {}

    def set_frequency(self, freq_hz: int) -> None:
        self._dev.setFrequency(self._m.SOAPY_SDR_RX, 0, freq_hz)

    def set_sample_rate(self, rate: int) -> None:
        self._dev.setSampleRate(self._m.SOAPY_SDR_RX, 0, rate)

    def actual_sample_rate(self) -> float:
        return float(self._dev.getSampleRate(self._m.SOAPY_SDR_RX, 0))

    def set_antenna(self, antenna: str) -> None:
        self._dev.setAntenna(self._m.SOAPY_SDR_RX, 0, antenna)

    def antennas(self) -> list[str]:
        try:
            return list(self._dev.getAntennas(self._m.SOAPY_SDR_RX, 0))
        except Exception:  # noqa: BLE001 — informational only
            return []

    def set_gain(self, name: str, value: float) -> None:
        self._dev.setGain(self._m.SOAPY_SDR_RX, 0, name, value)

    def gain_names(self) -> list[str]:
        try:
            return list(self._dev.listGains(self._m.SOAPY_SDR_RX, 0))
        except Exception:  # noqa: BLE001 — informational only
            return []

    def set_agc(self, enable: bool) -> None:
        try:
            self._dev.setGainMode(self._m.SOAPY_SDR_RX, 0, enable)
        except Exception:  # noqa: BLE001 — not all drivers support AGC mode
            log.debug("soapy setGainMode unsupported", exc_info=True)

    def open_stream(self, chunk_size: int) -> None:
        self._stream = self._dev.setupStream(
            self._m.SOAPY_SDR_RX, self._m.SOAPY_SDR_CF32
        )
        self._dev.activateStream(self._stream)

    def read(self, buf: np.ndarray, timeout_us: int) -> int:
        """Blocking read — call from a worker thread."""
        result = self._dev.readStream(self._stream, buf, buf.size, timeoutUs=timeout_us)
        return int(result.ret)

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._dev.deactivateStream(self._stream)
                self._dev.closeStream(self._stream)
            finally:
                self._stream = None


@dataclass
class SoapySource:
    """Universal SoapySDR source.

    Args:
        soapy_args: device construction args, e.g. ``{"driver": "airspy"}``,
            ``{"driver": "rtlsdr", "serial": "00000001"}``,
            ``{"driver": "remote", "remote": "tcp://other-host:1234"}``.
            Empty dict → first enumerated device.
        antenna: antenna port name, or None for the driver default.
        gain: dB applied to the FIRST gain element (most drivers expose one
            principal gain), or None for driver default/AGC.
        agc: request hardware AGC (where supported).
        chunk_size: complex samples per yielded chunk.
        binding: inject a SoapyBinding-compatible object (tests).
    """

    soapy_args: dict[str, Any] = field(default_factory=dict)
    antenna: str | None = None
    gain: float | None = None
    agc: bool = False
    chunk_size: int = 65536
    binding: Any | None = None
    info: SourceInfo = field(default_factory=lambda: SourceInfo(
        type="soapy",
        label="SoapySDR",
        sample_rate=1_000_000,
    ))

    def _make_binding(self) -> Any:
        if self.binding is not None:
            return self.binding
        return SoapyBinding()

    async def spawn(
        self,
        center_freq: int,
        sample_rate: int,
        gain: float | None = None,
    ) -> AsyncGenerator[np.ndarray, None]:
        binding = self._make_binding()
        args = dict(self.soapy_args)
        if not args:
            devices = binding.enumerate()
            if not devices:
                raise RuntimeError("no SoapySDR devices found (enumerate empty)")
            args = devices[0]
        dev = binding.make(args)
        try:
            dev.set_sample_rate(sample_rate)
            dev.set_frequency(center_freq)
            if self.antenna:
                dev.set_antenna(self.antenna)
            if self.agc:
                dev.set_agc(True)
            eff_gain = gain if gain is not None else self.gain
            if eff_gain is not None:
                names = dev.gain_names()
                if names:
                    dev.set_gain(names[0], float(eff_gain))
                else:
                    log.warning("soapy device exposes no named gains; gain ignored")

            actual_rate = dev.actual_sample_rate()
            hw = dev.hw_info
            object.__setattr__(
                self,
                "info",
                SourceInfo(
                    type="soapy",
                    label=f"SoapySDR {hw.get('driver', '?')} ({hw.get('hardware', '?')})",
                    endpoint=str(args),
                    sample_rate=int(actual_rate) or sample_rate,
                ),
            )
            dev.open_stream(self.chunk_size)
            log.info(
                "SoapySource streaming",
                args=args,
                center_freq=center_freq,
                sample_rate=actual_rate,
            )
            buf = np.empty(self.chunk_size, dtype=np.complex64)
            # 1 s read timeout: wakes us even when the device is quiet so the
            # consumer can observe cancellation promptly.
            timeout_us = 1_000_000
            while True:
                n = await asyncio.to_thread(dev.read, buf, timeout_us)
                if n > 0:
                    yield np.array(buf[:n], copy=True)
                elif n == 0:
                    continue  # timeout, no data
                elif n in (-2, -3):  # SOAPY_SDR_TIMEOUT / OVERFLOW
                    log.debug("soapy readStream non-fatal", ret=n)
                    continue
                else:
                    raise RuntimeError(f"soapy readStream failed ret={n}")
        finally:
            dev.close()

    async def close(self) -> None:
        return None
