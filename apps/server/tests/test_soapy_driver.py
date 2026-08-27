"""SoapySDR universal-driver tests — FakeSoapyBinding impersonates the module.

Covers: first-device selection, args passthrough, gain application on the
first named gain element, antenna forwarding, CF32 streaming with short-read
tolerance, and the missing-bindings error path.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from openwebrx_plus.sources.soapy import SoapySource


class _FakeStream:
    pass


class _FakeDeviceProxy:
    def __init__(self, module: _FakeSoapyModule) -> None:
        self._m = module
        self.calls: list[tuple[Any, ...]] = []
        self._stream: Any = None
        self._t = 0.0

    @property
    def hw_info(self) -> dict[str, Any]:
        return {"driver": "fake", "hardware": "sim"}

    def set_frequency(self, freq_hz: int) -> None:
        self.calls.append(("freq", freq_hz))

    def set_sample_rate(self, rate: int) -> None:
        self.calls.append(("rate", rate))

    def actual_sample_rate(self) -> float:
        return float(self._m.sample_rate)

    def set_antenna(self, antenna: str) -> None:
        self.calls.append(("antenna", antenna))

    def antennas(self) -> list[str]:
        return ["RX", "TX/RX"]

    def set_gain(self, name: str, value: float) -> None:
        self.calls.append(("gain", name, value))

    def gain_names(self) -> list[str]:
        return ["RF", "BB"]

    def set_agc(self, enable: bool) -> None:
        self.calls.append(("agc", enable))

    def open_stream(self, chunk_size: int) -> None:
        self.calls.append(("open", chunk_size))

    def read(self, buf: np.ndarray, timeout_us: int) -> int:
        # Emit a 1 kHz complex tone; occasionally a short read.
        n = buf.size if self._t % 3 else buf.size // 2
        t = (self._t + np.arange(n)) / 48_000.0
        buf[:n] = np.exp(2j * np.pi * 1000.0 * t).astype(np.complex64)
        self._t += n
        return n

    def close(self) -> None:
        self.calls.append(("close",))


class _FakeSoapyModule:
    SOAPY_SDR_RX = 0
    SOAPY_SDR_CF32 = 1
    SOAPY_SDR_TIMEOUT = -2
    SOAPY_SDR_OVERFLOW = -3
    sample_rate = 2_000_000

    class Device:  # noqa: N801 — mirrors the real module's nested class
        last_args: dict[str, Any] = {}

        def __init__(self, args: dict[str, Any]) -> None:
            type(self).last_args = dict(args)

        @staticmethod
        def enumerate() -> list[dict[str, Any]]:
            return [{"driver": "fake", "serial": "00000001"}]


class FakeSoapyBinding:
    def __init__(self, module: Any | None = None) -> None:
        self._m = module or _FakeSoapyModule()
        self.proxy: _FakeDeviceProxy | None = None
        self.last_make_args: dict[str, Any] | None = None

    def enumerate(self) -> list[dict[str, Any]]:
        return [dict(d) for d in self._m.Device.enumerate()]

    def make(self, args: dict[str, Any]) -> _FakeDeviceProxy:
        self.last_make_args = dict(args)
        self.proxy = _FakeDeviceProxy(self._m)
        return self.proxy


async def _collect(source: SoapySource, n_chunks: int) -> list[np.ndarray]:
    gen = source.spawn(center_freq=100_000_000, sample_rate=2_000_000, gain=None)
    chunks: list[np.ndarray] = []
    try:
        for _ in range(n_chunks):
            chunks.append(await gen.__anext__())
    finally:
        await gen.aclose()
    return chunks


class TestSoapyDriver:
    async def test_streams_cf32_and_forwards_config(self) -> None:
        binding = FakeSoapyBinding()
        src = SoapySource(binding=binding, chunk_size=4096, antenna="RX", agc=True)
        chunks = await _collect(src, 3)

        assert binding.proxy is not None
        calls = binding.proxy.calls
        assert ("freq", 100_000_000) in calls
        assert ("rate", 2_000_000) in calls
        assert ("antenna", "RX") in calls
        assert ("agc", True) in calls
        assert ("open", 4096) in calls
        assert ("close",) in calls

        # Short reads tolerated; chunks ≤ chunk_size.
        for c in chunks:
            assert c.dtype == np.complex64
            assert 0 < c.size <= 4096
        assert any(c.size == 4096 for c in chunks)
        assert any(c.size == 2048 for c in chunks)  # the deliberate short read

    async def test_first_gain_element_used(self) -> None:
        binding = FakeSoapyBinding()
        src = SoapySource(binding=binding, chunk_size=1024)
        gen = src.spawn(100_000_000, 2_000_000, gain=12.5)
        try:
            await gen.__anext__()
        finally:
            await gen.aclose()
        assert binding.proxy is not None
        assert ("gain", "RF", 12.5) in binding.proxy.calls

    async def test_empty_args_picks_first_enumerated_device(self) -> None:
        binding = FakeSoapyBinding()
        src = SoapySource(binding=binding, chunk_size=1024)  # soapy_args = {}
        gen = src.spawn(100_000_000, 2_000_000, None)
        try:
            await gen.__anext__()
        finally:
            await gen.aclose()
        assert binding.last_make_args is not None
        assert binding.last_make_args.get("driver") == "fake"
        assert "fake" in src.info.label

    async def test_no_devices_raises(self) -> None:
        class EmptyModule:
            SOAPY_SDR_RX = 0
            SOAPY_SDR_CF32 = 1
            sample_rate = 1_000_000

            class Device:  # noqa: N801
                @staticmethod
                def enumerate() -> list[dict[str, Any]]:
                    return []

        binding = FakeSoapyBinding(module=EmptyModule())
        src = SoapySource(binding=binding, chunk_size=1024)
        gen = src.spawn(100_000_000, 1_000_000, None)
        with pytest.raises(RuntimeError, match="no SoapySDR devices"):
            await gen.__anext__()

    def test_missing_bindings_error_mentions_install(self) -> None:
        if _soapy_installed():
            pytest.skip("SoapySDR installed on this host")
        from openwebrx_plus.sources.soapy import SoapyBinding

        with pytest.raises(RuntimeError, match="SoapySDR Python bindings"):
            SoapyBinding()

    async def test_read_error_surfaces(self) -> None:
        class BrokenProxy(_FakeDeviceProxy):
            def read(self, buf: np.ndarray, timeout_us: int) -> int:
                return -5  # SOAPY_SDR_STREAM_ERROR-ish

        class BrokenBinding(FakeSoapyBinding):
            def make(self, args: dict[str, Any]) -> _FakeDeviceProxy:
                self.proxy = BrokenProxy(self._m)
                return self.proxy

        binding = BrokenBinding()
        src = SoapySource(binding=binding, chunk_size=1024)
        gen = src.spawn(100_000_000, 1_000_000, None)
        with pytest.raises(RuntimeError, match="readStream failed"):
            await gen.__anext__()


def _soapy_installed() -> bool:
    """True when the real SoapySDR python module is importable."""
    try:
        import SoapySDR  # type: ignore[import-not-found]  # noqa: F401

        return True
    except ImportError:
        return False
