"""SDRplay real-driver tests — FakeSdrplayBinding impersonates the API v3 lib.

Covers: device selection by serial substring, callback interleaving
(separate xi/xq int16 arrays → complex64), gain inversion (protocol gain →
gRdB), bandwidth pick, chunk framing, and teardown ordering
(stream_uninit → release_device).
"""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np
import pytest

from openwebrx_plus.sources.sdrplay import SDRplaySource, _pick_bandwidth


class FakeSdrplayBinding:
    """Hardware-free stand-in for SdrplayBinding (same narrow surface)."""

    def __init__(self, devices: list[dict[str, Any]] | None = None) -> None:
        self.devices = (
            devices
            if devices is not None
            else [{"serial": "ABC123", "hw_ver": "RSP1A", "dev_num": 0, "duo_mode": None}]
        )
        self.calls: list[tuple[Any, ...]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def list_devices(self) -> list[dict[str, Any]]:
        return [dict(d) for d in self.devices]

    def select_device(self, dev_num: int) -> None:
        self.calls.append(("select", dev_num))

    def release_device(self) -> None:
        self.calls.append(("release",))

    def stream_init(
        self,
        *,
        sample_rate: int,
        center_freq: int,
        bandwidth_khz: int,
        lna_state: int,
        grdb: int,
        agc: bool,
        callback: Any,
    ) -> int:
        self.calls.append(("init", sample_rate, center_freq, bandwidth_khz, lna_state, grdb))
        self._stop.clear()

        def run() -> None:
            n = 4096
            while not self._stop.is_set():
                # I-only tone: xi = real sine, xq = zeros. The driver must
                # interleave xi→real, xq→imag.
                t = np.arange(n) / 48_000.0
                xi = (np.sin(2 * np.pi * 1000.0 * t) * 8000).astype(np.int16)
                xq = np.zeros(n, dtype=np.int16)
                callback(xi, xq)
                time.sleep(0.002)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        return 1024

    def stream_uninit(self) -> None:
        self.calls.append(("uninit",))
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def set_rf(self, freq_hz: int) -> None:
        self.calls.append(("setrf", freq_hz))

    def set_fs(self, rate: int) -> None:
        self.calls.append(("setfs", rate))

    def gain_change_request(self, grdb: int, lna_state: int) -> None:
        self.calls.append(("gain", grdb, lna_state))

    def agc_control(self, enable: bool) -> None:
        self.calls.append(("agc", enable))


async def _collect(source: SDRplaySource, n_chunks: int) -> list[np.ndarray]:
    gen = source.spawn(center_freq=7_100_000, sample_rate=2_000_000, gain=None)
    chunks: list[np.ndarray] = []
    try:
        for _ in range(n_chunks):
            chunks.append(await gen.__anext__())
    finally:
        await gen.aclose()
    return chunks


class TestSdrplayDriver:
    async def test_streams_interleaved_cf32(self) -> None:
        binding = FakeSdrplayBinding()
        src = SDRplaySource(binding=binding, chunk_size=1024)
        chunks = await _collect(src, 2)

        assert len(chunks) == 2
        for c in chunks:
            assert c.dtype == np.complex64
            assert c.shape == (1024,)
        # I-only tone: real part ≈ ±8000/32768, imag ≈ 0.
        peak_real = float(np.max(np.abs(chunks[0].real)))
        peak_imag = float(np.max(np.abs(chunks[0].imag)))
        assert 0.15 < peak_real < 0.30
        assert peak_imag < 1e-6

    async def test_gain_inverted_to_grdb(self) -> None:
        binding = FakeSdrplayBinding()
        src = SDRplaySource(binding=binding)
        gen = src.spawn(7_100_000, 2_000_000, gain=39.0)  # max protocol gain
        try:
            await gen.__anext__()
        finally:
            await gen.aclose()
        inits = [c for c in binding.calls if c[0] == "init"]
        assert inits, "stream_init not called"
        assert inits[0][5] == 20  # 59 - 39 → minimum gain reduction

    async def test_explicit_grdb_wins(self) -> None:
        binding = FakeSdrplayBinding()
        src = SDRplaySource(binding=binding, grdb=42, lna_state=4)
        gen = src.spawn(7_100_000, 2_000_000, gain=0.0)
        try:
            await gen.__anext__()
        finally:
            await gen.aclose()
        inits = [c for c in binding.calls if c[0] == "init"]
        assert inits[0][5] == 42  # explicit grdb, not 59-gain
        assert inits[0][4] == 4  # lna_state

    async def test_bandwidth_picked_for_rate(self) -> None:
        binding = FakeSdrplayBinding()
        src = SDRplaySource(binding=binding)
        gen = src.spawn(7_100_000, 2_000_000, None)
        try:
            await gen.__anext__()
        finally:
            await gen.aclose()
        inits = [c for c in binding.calls if c[0] == "init"]
        # 2 MSPS → needs ≥ 2400 kHz → 5000 kHz enum.
        assert inits[0][3] == 5000

    async def test_serial_substring_selects_device(self) -> None:
        binding = FakeSdrplayBinding(
            devices=[
                {"serial": "AAA111", "hw_ver": "RSP1", "dev_num": 0, "duo_mode": None},
                {"serial": "BBB222", "hw_ver": "RSPduo", "dev_num": 1, "duo_mode": "master"},
            ]
        )
        src = SDRplaySource(binding=binding, serial="BBB")
        gen = src.spawn(7_100_000, 2_000_000, None)
        try:
            await gen.__anext__()
        finally:
            await gen.aclose()
        assert ("select", 1) in binding.calls
        assert "BBB222" in src.info.label

    async def test_no_devices_raises(self) -> None:
        binding = FakeSdrplayBinding(devices=[])
        src = SDRplaySource(binding=binding)
        gen = src.spawn(7_100_000, 2_000_000, None)
        with pytest.raises(RuntimeError, match="no SDRplay RSP devices"):
            await gen.__anext__()

    async def test_teardown_order(self) -> None:
        binding = FakeSdrplayBinding()
        src = SDRplaySource(binding=binding, chunk_size=1024)
        await _collect(src, 1)
        order = [c[0] for c in binding.calls]
        assert order.index("uninit") < order.index("release")

    def test_invalid_antenna_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid antenna"):
            SDRplaySource(antenna="d")


class TestBandwidthPick:
    def test_narrow_rate_gets_narrow_bw(self) -> None:
        assert _pick_bandwidth(200_000) in (200, 300)

    def test_wide_rate_clamps_to_max(self) -> None:
        assert _pick_bandwidth(7_900_000) == 8000
