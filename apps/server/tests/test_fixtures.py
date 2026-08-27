"""Fixture content tests — prove the baked IQ is *real* signal, not noise.

  * smoke: three CW carriers at the exact configured offsets.
  * 20 m evening: CW CQ at +2.1 kHz, beacon at −50 kHz, FT8 trace at −76 kHz,
    SSB voice band at +8 kHz, AM carrier at +85 kHz.
  * FM broadcast: five station peaks at their offsets.
  * ADS-B: burst detection + an independent Mode S PPM decoder that
    verifies CRC-24 on every decoded frame (dump1090-grade validity).
  * FileSource integration: sidecar metadata feeds center/rate hints.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from openwebrx_plus.sources.file_source import FileSource

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "iq"


def _load(name: str) -> tuple[np.ndarray, int, int]:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"fixture {name} not baked — run scripts/generate_iq_fixtures.py")
    iq = np.fromfile(path, dtype=np.complex64)
    meta = eval_sidecar(path)
    return iq, int(meta["sample_rate"]), int(meta["center"])


def eval_sidecar(path: Path) -> dict[str, Any]:
    import json

    meta_path = path.with_suffix(".meta")
    meta = json.loads(meta_path.read_text())
    sample_rate = int(meta.get("global", {}).get("core:sample_rate", 0))
    center = int(meta.get("captures", [{}])[0].get("core:frequency", 0))
    return {"sample_rate": sample_rate, "center": center}


def _avg_spectrum(iq: np.ndarray, fs: int, nperseg: int = 250_000) -> tuple[np.ndarray, np.ndarray]:
    """Segment-averaged power spectrum. Returns (freqs_hz, power_db)."""
    nseg = iq.size // nperseg
    segments = iq[: nseg * nperseg].reshape(nseg, nperseg)
    win = np.hanning(nperseg).astype(np.float32)
    spec = np.fft.fft(segments * win, axis=1)
    power = np.mean(np.abs(spec) ** 2, axis=0)
    freqs = np.fft.fftfreq(nperseg, 1 / fs)
    order = np.argsort(freqs)
    return freqs[order], 10 * np.log10(power[order] + 1e-20)


def _peak_offset_hz(freqs: np.ndarray, power_db: np.ndarray, lo: float, hi: float) -> float:
    mask = (freqs >= lo) & (freqs <= hi)
    idx = np.argmax(power_db[mask])
    return float(freqs[mask][idx])


def _band_power_db(freqs: np.ndarray, power_db: np.ndarray, lo: float, hi: float) -> float:
    mask = (freqs >= lo) & (freqs <= hi)
    return float(np.mean(power_db[mask]))


class TestSmokeFixture:
    def test_three_cw_carriers_at_expected_offsets(self) -> None:
        iq, fs, center = _load("smoke.cf32")
        assert fs == 250_000
        assert center == 100_000_000
        freqs, power = _avg_spectrum(iq, fs, nperseg=100_000)
        for offset, tol in ((-37_500.0, 50.0), (1_200.0, 50.0), (12_500.0, 50.0)):
            peak = _peak_offset_hz(freqs, power, offset - 2_000, offset + 2_000)
            assert abs(peak - offset) < tol, f"expected carrier at {offset}, got {peak}"

    def test_carriers_above_noise_floor(self) -> None:
        iq, fs, _ = _load("smoke.cf32")
        freqs, power = _avg_spectrum(iq, fs, nperseg=100_000)
        floor = _band_power_db(freqs, power, 40_000, 60_000)
        carrier = _band_power_db(freqs, power, 1_200 - 10, 1_200 + 10)
        assert carrier - floor > 20, f"carrier only {carrier - floor:.1f} dB over floor"


class TestHf20mFixture:
    def test_metadata(self) -> None:
        _iq, fs, center = _load("hf_20m_evening.cf32")
        assert fs == 250_000
        assert center == 14_150_000

    def test_cw_cq_caller_at_2p1khz(self) -> None:
        iq, fs, _ = _load("hf_20m_evening.cf32")
        freqs, power = _avg_spectrum(iq, fs)
        peak = _peak_offset_hz(freqs, power, 2_100 - 300, 2_100 + 300)
        assert abs(peak - 2_100) < 20, f"CW peak at {peak} Hz, expected 2100"

    def test_beacon_at_14m100(self) -> None:
        iq, fs, _ = _load("hf_20m_evening.cf32")
        freqs, power = _avg_spectrum(iq, fs)
        peak = _peak_offset_hz(freqs, power, -50_000 - 300, -50_000 + 300)
        assert abs(peak - (-50_000)) < 20

    def test_ft8_trace_at_14m074(self) -> None:
        iq, fs, _ = _load("hf_20m_evening.cf32")
        freqs, power = _avg_spectrum(iq, fs)
        # FT8 stack: 50 Hz wide around −76 kHz; compare to a 1 kHz-away guard band.
        ft8 = _band_power_db(freqs, power, -76_050, -75_950)
        guard = _band_power_db(freqs, power, -77_100, -76_900)
        assert ft8 - guard > 6, f"FT8 band only {ft8 - guard:.1f} dB over guard"

    def test_ssb_energy_at_8khz(self) -> None:
        iq, fs, _ = _load("hf_20m_evening.cf32")
        freqs, power = _avg_spectrum(iq, fs)
        ssb = _band_power_db(freqs, power, 8_300, 10_700)
        guard = _band_power_db(freqs, power, 11_500, 13_500)
        assert ssb - guard > 10, f"SSB band only {ssb - guard:.1f} dB over guard"

    def test_am_carrier_at_85khz(self) -> None:
        iq, fs, _ = _load("hf_20m_evening.cf32")
        freqs, power = _avg_spectrum(iq, fs)
        peak = _peak_offset_hz(freqs, power, 85_000 - 500, 85_000 + 500)
        assert abs(peak - 85_000) < 30

    def test_qrn_present(self) -> None:
        """QRN impulses make the time-domain amplitude distribution heavy-tailed."""
        iq, fs, _ = _load("hf_20m_evening.cf32")
        env = np.abs(iq)
        # Deterministic fixture: burst max towers over the steady-signal median.
        assert float(env.max()) > 4 * float(np.median(env))


class TestFmBroadcastFixture:
    def test_five_station_peaks(self) -> None:
        """Wideband FM: the carrier is Bessel-suppressed at high deviation, so
        assert BAND power (station ±60 kHz vs. guard bands between stations)
        rather than a spectral peak at the exact carrier offset."""
        iq, fs, center = _load("vhf_fm_broadcast.cf32")
        assert fs == 1_000_000
        freqs, power = _avg_spectrum(iq, fs, nperseg=250_000)
        # Guards sit >90 kHz from every station (Carson bandwidth ~180 kHz).
        guards = (-280_000.0, -65_000.0, 125_000.0, 320_000.0)
        guard_db = float(np.mean([_band_power_db(freqs, power, g - 10_000, g + 10_000)
                                   for g in guards]))
        for offset in (-380_000.0, -160_000.0, 30_000.0, 220_000.0, 420_000.0):
            station = _band_power_db(freqs, power, offset - 60_000, offset + 60_000)
            assert station - guard_db > 6, (
                f"station @{offset/1e3:+.0f} kHz only {station - guard_db:.1f} dB "
                "over the inter-station guard level"
            )


# ---------------------------------------------------------------------------
# ADS-B: independent PPM decode + CRC-24 (dump1090-grade check)
# ---------------------------------------------------------------------------


def crc24(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0xFFF409
    return crc & 0xFFFFFF


def detect_bursts(env: np.ndarray, threshold: float) -> list[int]:
    """Indices where the envelope first crosses the threshold (rising edges)."""
    above = env > threshold
    starts = np.flatnonzero(above[1:] & ~above[:-1]) + 1
    if above[0]:
        starts = np.r_[0, starts]
    # Merge edges closer than 60 µs (same burst).
    merged: list[int] = []
    for s in starts:
        if not merged or s - merged[-1] > 120:
            merged.append(int(s))
    return merged


def decode_frame_at(env: np.ndarray, start: int, fs: int) -> bytes | None:
    """Decode one Mode S frame given its first preamble sample."""
    def level(i: int) -> float:
        return float(env[start + i]) if 0 <= start + i < env.size else 0.0

    # Preamble check: pulses at 0, 2, 5, 7, 9 samples (2 MSPS); gaps between.
    pulses = [0, 2, 5, 7, 9]
    gaps = [1, 3, 4, 6, 8, 10, 11]
    p_min = min(level(i) for i in pulses)
    g_max = max(level(i) for i in gaps)
    if p_min < 0.2 or g_max > p_min * 0.6:
        return None

    # Downlink format determines length.
    def bit(k: int) -> int:
        first = 16 + 2 * k  # first half-chip sample
        return 1 if level(first) >= level(first + 1) else 0

    df = (bit(0) << 4) | (bit(1) << 3) | (bit(2) << 2) | (bit(3) << 1) | bit(4)
    if df == 11:
        nbits = 56
    elif df == 17:
        nbits = 112
    else:
        return None  # other DFs not present in the fixture

    value = 0
    for k in range(nbits):
        value = (value << 1) | bit(k)
    msg = value.to_bytes(nbits // 8, "big")
    if crc24(msg[:-3]) != int.from_bytes(msg[-3:], "big"):
        return None
    return msg


class TestAdsbFixture:
    def test_bursts_detected(self) -> None:
        iq, fs, _ = _load("adsb_1090.cf32")
        env = np.abs(iq)
        bursts = detect_bursts(env, threshold=0.08)
        assert len(bursts) >= 10, f"only {len(bursts)} bursts detected"

    def test_frames_have_valid_crc24(self) -> None:
        iq, fs, center = _load("adsb_1090.cf32")
        assert fs == 2_000_000
        assert center == 1_090_000_000
        env = np.abs(iq)
        decoded: list[bytes] = []
        for start in detect_bursts(env, threshold=0.08):
            msg = decode_frame_at(env, start, fs)
            if msg is not None:
                decoded.append(msg)
        assert len(decoded) >= 10, f"only {len(decoded)} CRC-valid frames"

        icaos = set()
        callsigns: set[str] = set()
        for msg in decoded:
            icaos.add(msg[1:4].hex().upper())
            if (msg[0] >> 3) == 17 and (msg[4] >> 3) == 4:
                callsigns.add(msg[5:11].hex().upper())
        assert len(icaos) == 3, f"expected 3 aircraft, got {sorted(icaos)}"

    def test_owns_fixture_decoder_agreement(self) -> None:
        """Cross-check: the generator's df17_callsign output decodes cleanly."""
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
        from generate_iq_fixtures import (  # type: ignore[import-not-found]  # offline repo script
            df11_all_call,
            df17_callsign,
        )

        for icao, cs in ((0x4D22AA, "OWRX001"), (0x3C70EE, "N42OWRX"), (0x06A1B2, "OPENWEB1")):
            for msg in (df11_all_call(icao), df17_callsign(icao, cs)):
                assert crc24(msg[:-3]) == int.from_bytes(msg[-3:], "big")


class TestFileSourceIntegration:
    def test_fixture_sidecar_feeds_hints(self) -> None:
        path = FIXTURES / "hf_20m_evening.cf32"
        if not path.exists():
            pytest.skip("fixture not baked")
        src = FileSource(file_path=path, loop=True)
        assert src.center_freq_hint == 14_150_000
        assert src.sample_rate_hint == 250_000
        assert src.info.sample_rate == 250_000

    def test_smoke_fixture_replays(self) -> None:
        import asyncio

        path = FIXTURES / "smoke.cf32"
        if not path.exists():
            pytest.skip("fixture not baked")
        src = FileSource(file_path=path, chunk_size=4096, loop=True, realtime=False)
        # realtime kwarg lands in slice-3.5 wiring; plain replay still works.

        async def _first() -> np.ndarray:
            gen = src.spawn(100_000_000, 250_000, None)
            try:
                return await gen.__anext__()
            finally:
                await gen.aclose()

        chunk = asyncio.run(_first())
        assert chunk.dtype == np.complex64
        assert chunk.shape == (4096,)
