"""Airspy real-driver tests — FakeAirspyBinding impersonates libairspy.

Covers: device selection (serial), cs16 → complex64 conversion with chunk
framing, gain mode mapping (protocol gain → linearity, manual stages), bias
tee, teardown ordering, and the no-device error.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np
import pytest

from openwebrx_plus.sources.airspy import AirspySource


class FakeAirspyBinding:
    """Hardware-free stand-in for AirspyBinding (same narrow surface)."""

    def __init__(self, serials: list[int] | None = None) -> None:
        self.serials = [0x6440642340] if serials is None else serials
        self.calls: list[tuple[Any, ...]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def list_serials(self) -> list[int]:
        return list(self.serials)

    def open(self, serial: int | None) -> str:
        self.calls.append(("open", serial))
        return "fake-dev"

    def close(self, dev: Any) -> None:
        self.calls.append(("close", dev))

    def set_iq16(self, dev: Any) -> None:
        self.calls.append(("iq16",))

    def set_samplerate(self, dev: Any, rate: int) -> None:
        self.calls.append(("rate", rate))

    def set_freq(self, dev: Any, freq_hz: int) -> None:
        self.calls.append(("freq", freq_hz))

    def set_gains(self, dev: Any, **kwargs: Any) -> None:
        self.calls.append(("gains", kwargs))

    def set_rf_bias(self, dev: Any, on: bool) -> None:
        self.calls.append(("bias", on))

    def start_rx(self, dev: Any, callback: Any) -> None:
        self.calls.append(("start",))
        self._stop.clear()

        def run() -> None:
            t = np.arange(1024) / 48_000.0
            tone = np.exp(2j * np.pi * 1000.0 * t)
            cs16 = np.empty(2048, dtype=np.int16)
            cs16[0::2] = (tone.real * 10000).astype(np.int16)
            cs16[1::2] = (tone.imag * 10000).astype(np.int16)
            while not self._stop.is_set():
                callback(cs16.tobytes())
                time.sleep(0.002)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop_rx(self, dev: Any) -> None:
        self.calls.append(("stop",))
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


async def _collect(source: AirspySource, n_chunks: int) -> list[np.ndarray]:
    gen = source.spawn(center_freq=100_000_000, sample_rate=2_500_000, gain=None)
    chunks: list[np.ndarray] = []
    try:
        for _ in range(n_chunks):
            chunks.append(await gen.__anext__())
    finally:
        await gen.aclose()
    return chunks


class TestAirspyDriver:
    async def test_streams_converted_cf32_chunks(self) -> None:
        binding = FakeAirspyBinding()
        src = AirspySource(binding=binding, chunk_size=512)
        chunks = await _collect(src, 2)

        assert len(chunks) == 2
        for c in chunks:
            assert c.dtype == np.complex64
            assert c.shape == (512,)
        # cs16 amplitude 10000 → 10000/32768 ≈ 0.305; tone → both components.
        amplitude = float(np.mean(np.abs(chunks[0])))
        assert 0.25 < amplitude < 0.36, f"unexpected tone amplitude {amplitude}"

    async def test_protocol_gain_maps_to_linearity(self) -> None:
        binding = FakeAirspyBinding()
        src = AirspySource(binding=binding, gain_mode="linearity")
        gen = src.spawn(100_000_000, 2_500_000, gain=15.0)
        try:
            await gen.__anext__()
        finally:
            await gen.aclose()
        gains = [c for c in binding.calls if c[0] == "gains"]
        assert gains and gains[0][1]["gain_mode"] == "linearity"
        assert gains[0][1]["linearity"] == 15

    async def test_manual_gain_mode_uses_stages(self) -> None:
        binding = FakeAirspyBinding()
        src = AirspySource(binding=binding, gain_mode="manual", lna_gain=5, mixer_gain=6, vga_gain=7)
        gen = src.spawn(100_000_000, 2_500_000, gain=None)
        try:
            await gen.__anext__()
        finally:
            await gen.aclose()
        gains = [c for c in binding.calls if c[0] == "gains"]
        assert gains and gains[0][1]["gain_mode"] == "manual"
        assert gains[0][1]["lna"] == 5
        assert gains[0][1]["mixer"] == 6
        assert gains[0][1]["vga"] == 7

    async def test_bias_tee_requested(self) -> None:
        binding = FakeAirspyBinding()
        src = AirspySource(binding=binding, bias_tee=True)
        gen = src.spawn(100_000_000, 2_500_000, None)
        try:
            await gen.__anext__()
        finally:
            await gen.aclose()
        assert ("bias", True) in binding.calls

    async def test_frequency_and_rate_forwarded(self) -> None:
        binding = FakeAirspyBinding()
        src = AirspySource(binding=binding)
        gen = src.spawn(center_freq=433_920_000, sample_rate=10_000_000, gain=None)
        try:
            await gen.__anext__()
        finally:
            await gen.aclose()
        assert ("freq", 433_920_000) in binding.calls
        assert ("rate", 10_000_000) in binding.calls

    async def test_missing_serial_rejected(self) -> None:
        binding = FakeAirspyBinding(serials=[111, 222])
        src = AirspySource(binding=binding, serial_number=999)
        gen = src.spawn(100_000_000, 2_500_000, None)
        with pytest.raises(RuntimeError, match="not present"):
            await gen.__anext__()

    async def test_no_devices_raises(self) -> None:
        binding = FakeAirspyBinding(serials=[])
        src = AirspySource(binding=binding)
        gen = src.spawn(100_000_000, 2_500_000, None)
        with pytest.raises(RuntimeError, match="no Airspy devices"):
            await gen.__anext__()

    async def test_teardown_stops_rx_then_closes(self) -> None:
        binding = FakeAirspyBinding()
        src = AirspySource(binding=binding, chunk_size=256)
        await _collect(src, 1)
        # stop must be requested before close for a clean libairspy shutdown.
        order = [c[0] for c in binding.calls]
        assert order.index("stop") < order.index("close")

    def test_invalid_gain_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid gain_mode"):
            AirspySource(gain_mode="magic")
