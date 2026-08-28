#!/usr/bin/env python3
"""Bake realistic IQ fixtures for hardware-free development (ADR-004/005).

Produces cf32 recordings + SigMF-style sidecars (``<stem>.meta`` — the
naming FileSource auto-loads) into apps/server/fixtures/iq/:

  hf_20m_evening.cf32    250 kSPS × 8 s @ 14.150 MHz — 20 m after dark:
                         CW stations (incl. 14.100 beacon + drifter),
                         two USB voice-like QSOs, AM broadcast spill,
                         an FT8 trace at 14.074, QRN + ionospheric fading.
  vhf_fm_broadcast.cf32  1 MSPS × 2 s @ 98.0 MHz — five FM stations
                         (stereo w/ 19 kHz pilot + 57 kHz RDS-like
                         subcarrier on two of them, one weak/distant).
  adsb_1090.cf32         2 MSPS × 1 s @ 1090 MHz — Mode S PPM frames
                         (DF11 all-call + DF17 callsign) with VALID CRC-24
                         for three aircraft: decodable by dump1090-style
                         decoders.
  smoke.cf32             250 kSPS × 0.4 s — three CW carriers + noise.
                         Fast fixture for CI.

numpy-only (offline tooling — ADR-004's scipy boundary rule). Deterministic
seed → byte-identical output on every run.

Usage:
    python3 scripts/generate_iq_fixtures.py [--out DIR] [--which all|smoke|...]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

SEED = 20260826

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "apps" / "server" / "fixtures" / "iq"

# ---------------------------------------------------------------------------
# Mode S / ADS-B (valid CRC-24 → genuinely decodable frames)
# ---------------------------------------------------------------------------

MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..", "0": "-----", "1": ".----", "2": "..---",
    "3": "...--", "4": "....-", "5": ".....", "6": "-....", "7": "--...",
    "8": "---..", "9": "----.", "/": "-..-.", "=": "-...-",
}


def crc24_mode_s(data: bytes) -> int:
    """Mode S parity (poly 0xFFF409, MSB-first, init 0)."""
    crc = 0
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0xFFF409
    return crc & 0xFFFFFF


_CRC_SELF_CHECK = ("8D4840D6202CC371C32CE0", 0x576098)  # dump1090 test vector


def df11_all_call(icao: int) -> bytes:
    """DF11 all-call reply (56 bits): [5A-ish DF/CA][ICAO 3B][PI = CRC(4B)]."""
    msg = bytes([(11 << 3) | 5, (icao >> 16) & 0xFF, (icao >> 8) & 0xFF, icao & 0xFF])
    return msg + crc24_mode_s(msg).to_bytes(3, "big")


def _callsign_6bit(callsign: str) -> bytes:
    """Pack 8 chars (A–Z 0–9 space) into 48 bits (6 bits/char)."""
    chars = (callsign + "        ")[:8]
    acc = 0
    for ch in chars:
        if ch == " ":
            val = 32
        elif ch.isdigit():
            val = 48 + int(ch)
        else:
            val = ord(ch.upper()) - 64
        acc = (acc << 6) | val
    return acc.to_bytes(6, "big")


def df17_callsign(icao: int, callsign: str) -> bytes:
    """DF17 identification (TC=4, 112 bits) with valid CRC."""
    me = bytes([4 << 3]) + _callsign_6bit(callsign)  # TC=4, flight status 0
    msg = bytes([(17 << 3) | 5, (icao >> 16) & 0xFF, (icao >> 8) & 0xFF, icao & 0xFF]) + me
    return msg + crc24_mode_s(msg).to_bytes(3, "big")


def df17_altitude(icao: int, altitude_ft: int) -> bytes:
    """DF17 surveillance altitude (TC=0..4 CA=0, 25 ft Gillham-ish encoding
    approximated as binary — enough for decoders to read a stable field)."""
    ac = int(altitude_ft / 25) & 0x7FF
    me = bytes([ac >> 8 & 0x07, ac & 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00])
    msg = bytes([(17 << 3) | 5, (icao >> 16) & 0xFF, (icao >> 8) & 0xFF, icao & 0xFF]) + me
    return msg + crc24_mode_s(msg).to_bytes(3, "big")


def ppm_waveform(message: bytes, fs: int) -> np.ndarray:
    """Mode S PPM: preamble (pulses at 0.0/1.0/2.5/3.5/4.5 µs) + 1 Mbps data."""
    total_bits = len(message) * 8
    total_us = 8.0 + total_bits  # 8 µs preamble + 1 µs per bit
    spm = fs / 1e6  # samples per microsecond
    n = int(round(total_us * spm)) + 2
    env = np.zeros(n, dtype=np.float32)

    def pulse(t_us: float) -> None:
        i0 = int(round(t_us * spm))
        i1 = int(round((t_us + 0.5) * spm))
        env[i0:i1] = 1.0

    for t in (0.0, 1.0, 2.5, 3.5, 4.5):
        pulse(t)
    for k, bit in enumerate(f"{int.from_bytes(message, 'big'):0{total_bits}b}"):
        t_bit = 8.0 + k
        pulse(t_bit if bit == "1" else t_bit + 0.5)
    return env


# ---------------------------------------------------------------------------
# Analog signal synthesis helpers
# ---------------------------------------------------------------------------


def morse_keying(text: str, wpm: float, n: int, fs: float, rng: np.random.Generator,
                 start_s: float = 0.0) -> np.ndarray:
    """0/1 keying envelope for `text` repeated to fill n samples."""
    dit = 1.2 / wpm  # seconds
    unit = dit
    segs: list[tuple[float, float]] = []  # (on/off, duration)
    for word in text.split():
        for ci, ch in enumerate(word):
            code = MORSE.get(ch, "")
            for si, sym in enumerate(code):
                segs.append((1.0, unit if sym == "." else 3 * unit))
                if si < len(code) - 1:
                    segs.append((0.0, unit))
            if ci < len(word) - 1:
                segs.append((0.0, 3 * unit))
        segs.append((0.0, 7 * unit))  # word space

    env = np.zeros(n, dtype=np.float32)
    t = start_s
    idx = 0
    ramp = int(0.005 * fs)
    while t < n / fs:
        level, dur = segs[idx % len(segs)]
        i0 = int(t * fs)
        i1 = min(int((t + dur) * fs), n)
        if level > 0 and i1 > i0:
            env[i0:i1] = 1.0
            if ramp > 0:  # 5 ms keying edges (bandwidth hygiene)
                for arr, sl in ((env, slice(i0, i0 + ramp)), (env, slice(i1 - ramp, i1))):
                    m = sl.stop - sl.start
                    if m > 0:
                        arr[sl] = np.minimum(arr[sl], 0.5 * (1 - np.cos(np.pi * np.arange(m) / m)))
        t += dur
        idx += 1
    _ = rng  # reserved for jitter variants
    return env


def band_limited_noise(n: int, fs: float, low: float, high: float,
                       rng: np.random.Generator) -> np.ndarray:
    """White noise band-limited via whole-array FFT (offline bake only)."""
    spec = rng.standard_normal(n) * np.exp(1j * rng.uniform(0, 2 * np.pi, n))
    freqs = np.fft.fftfreq(n, 1 / fs)
    mask = (np.abs(freqs) >= low) & (np.abs(freqs) <= high)
    out = np.fft.ifft(spec * mask)
    out /= max(1e-12, np.max(np.abs(out)))
    return out.astype(np.complex64)


def speech_cadence(n: int, fs: float, rng: np.random.Generator) -> np.ndarray:
    """On/off envelope that breathes like an SSB operator (syllabic rate)."""
    env = np.zeros(n, dtype=np.float32)
    t = 0.0
    while t < n / fs:
        on = rng.uniform(0.08, 0.35)
        off = rng.uniform(0.06, 0.45)
        i0, i1 = int(t * fs), min(int((t + on) * fs), n)
        if i1 > i0:
            env[i0:i1] = 1.0
        t += on + off
    # 15 ms smoothing
    k = max(1, int(0.015 * fs))
    kernel = np.ones(k) / k
    return np.convolve(env, kernel, mode="same").astype(np.float32)


def rayleigh_fading(n: int, fs: float, fd_hz: float,
                    rng: np.random.Generator) -> np.ndarray:
    """|low-passed complex Gaussian|, normalized to unit mean."""
    spec = (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    freqs = np.fft.fftfreq(n, 1 / fs)
    spec *= np.abs(freqs) <= fd_hz  # Doppler-limited
    g = np.abs(np.fft.ifft(spec))
    g /= max(1e-12, g.mean())
    return np.clip(g, 0.05, 2.5).astype(np.float32)


def music_tones(n: int, fs: float, rng: np.random.Generator, base: float = 220.0) -> np.ndarray:
    """Chords with vibrato + tremolo + note changes — 'shortwave music'."""
    t = np.arange(n) / fs
    out = np.zeros(n, dtype=np.float32)
    scale = np.array([1.0, 9 / 8, 5 / 4, 3 / 2, 5 / 3])
    t_start = 0.0
    while t_start < n / fs:
        dur = rng.uniform(1.2, 2.8)
        i0, i1 = int(t_start * fs), min(int((t_start + dur) * fs), n)
        if i1 <= i0:
            break
        seg_t = t[i0:i1] - t[i0]
        chord = base * scale[rng.integers(0, len(scale), size=3)]
        wave = np.zeros(i1 - i0, dtype=np.float32)
        for f in chord:
            vib = 1.0 + 0.004 * np.sin(2 * np.pi * 5.0 * seg_t + rng.uniform(0, 6))
            wave += np.sin(2 * np.pi * f * vib * seg_t).astype(np.float32) / 3
        trem = 0.85 + 0.15 * np.sin(2 * np.pi * 0.8 * seg_t)
        out[i0:i1] = (wave * trem).astype(np.float32)
        t_start += dur
    return out / max(1e-9, np.max(np.abs(out)))


def ssb_signal(n: int, fs: float, amp: float, rng: np.random.Generator,
               fading_hz: float = 0.0) -> np.ndarray:
    """USB voice-like: band-limited noise × speech cadence, upper sideband."""
    audio = band_limited_noise(n, fs, 300.0, 2700.0, rng).real
    audio *= speech_cadence(n, fs, rng)
    # Analytic signal → upper sideband via complex audio (positive freqs only).
    spec = np.fft.fft(audio)
    spec[np.fft.fftfreq(n, 1 / fs) < 0] = 0
    ssb = np.fft.ifft(spec) * 2.0
    out = amp * ssb.real.astype(np.float32)
    if fading_hz > 0:
        out *= rayleigh_fading(n, fs, fading_hz, rng)
    return out.astype(np.float32)


def cw_signal(offset_hz: float, n: int, fs: float, amp: float, text: str, wpm: float,
              rng: np.random.Generator, fading_hz: float = 0.0,
              drift_hz: float = 0.0) -> np.ndarray:
    """Phase-continuous CW with keying envelope, optional fading and drift."""
    t = np.arange(n) / fs
    drift = drift_hz * np.sin(2 * np.pi * 0.05 * t + rng.uniform(0, 6))
    phase = 2 * np.pi * np.cumsum(offset_hz + drift) / fs
    env = morse_keying(text, wpm, n, fs, rng)
    out = (amp * env * np.exp(1j * phase)).astype(np.complex64)
    if fading_hz > 0:
        out *= rayleigh_fading(n, fs, fading_hz, rng)
    return out


def am_signal(offset_hz: float, n: int, fs: float, carrier: float,
              audio: np.ndarray, depth: float = 0.6) -> np.ndarray:
    """Full-carrier AM (shortwave broadcast style)."""
    t = np.arange(n) / fs
    m = 1.0 + depth * audio
    return (carrier * m * np.exp(2j * np.pi * offset_hz * t)).astype(np.complex64)


def ft8_like(offset_hz: float, n: int, fs: float, amp: float,
             rng: np.random.Generator, start_s: float = 0.5) -> np.ndarray:
    """8-FSK, 6.25 Hz spacing, 12.5 baud, Costas bookends — the FT8 look.

    Symbols are random (no LDPC payload) → spectrally identical, not
    decodable as valid FT8. Real FT8 encoding lands with the decoder
    plugin slice (ADR-003).
    """
    costas = [3, 1, 4, 0, 6, 5, 2]
    symbols = costas + list(rng.integers(0, 8, size=65)) + costas
    baud = 12.5
    sym_dur = 1.0 / baud
    out = np.zeros(n, dtype=np.complex64)
    freqs = offset_hz + np.array(symbols) * 6.25
    # Per-sample instantaneous frequency (CPFSK → phase-continuous).
    inst = np.zeros(n, dtype=np.float64)
    for k, f in enumerate(freqs):
        i0 = int((start_s + k * sym_dur) * fs)
        i1 = int((start_s + (k + 1) * sym_dur) * fs)
        if 0 <= i0 < n:
            inst[i0:min(i1, n)] = f
    phase = 2 * np.pi * np.cumsum(inst) / fs
    env = np.zeros(n, dtype=np.float32)
    i0 = int(start_s * fs)
    i1 = min(int((start_s + len(freqs) * sym_dur) * fs), n)
    env[i0:i1] = 1.0
    out = (amp * env * np.exp(1j * phase)).astype(np.complex64)
    return out


def fm_station_signal(offset_hz: float, n: int, fs: float, amp: float,
                      rng: np.random.Generator, stereo: bool = True,
                      rds: bool = False, fading_hz: float = 0.0) -> np.ndarray:
    """Broadcast FM: mono/stereo multiplex + optional RDS-like subcarrier."""
    t = np.arange(n) / fs
    music = music_tones(n, fs, rng)
    base = music.astype(np.float64)
    if stereo:
        music_b = music_tones(n, fs, rng, base=180.0)
        pilot = 0.09 * np.sin(2 * np.pi * 19_000.0 * t)
        dsb = 0.45 * music_b * np.cos(2 * np.pi * 38_000.0 * t)
        base = base + pilot + dsb
    if rds:
        # Biphase-ish data at 1187.5 bps on a 57 kHz subcarrier.
        bit_rate = 1187.5
        bits = rng.integers(0, 2, size=int(n / fs * bit_rate) + 2)
        chip = np.repeat(bits * 2.0 - 1.0, int(fs / bit_rate / 2))[:n]
        rds_wave = np.zeros(n)
        rds_wave[: len(chip)] = chip[:n]
        base = base + 0.08 * rds_wave * np.cos(2 * np.pi * 57_000.0 * t)
    base /= max(1e-9, np.max(np.abs(base)))
    dev = 75_000.0
    inst_freq = offset_hz + dev * base  # instantaneous freq: carrier + deviation
    phase = 2 * np.pi * np.cumsum(inst_freq / fs)
    out = (amp * np.exp(1j * phase)).astype(np.complex64)
    if fading_hz > 0:
        out *= rayleigh_fading(n, fs, fading_hz, rng)
    return out


def qrn_bursts(n: int, fs: float, rng: np.random.Generator,
               count: int = 6) -> np.ndarray:
    """Lightning crashes: decaying noise bursts across the whole band."""
    out = np.zeros(n, dtype=np.complex64)
    for _ in range(count):
        t0 = rng.uniform(0, n / fs)
        dur = 0.06
        amp = rng.uniform(0.35, 0.7)
        i0, i1 = int(t0 * fs), min(int((t0 + dur) * fs), n)
        if i1 <= i0:
            continue
        m = i1 - i0
        decay = np.exp(-np.arange(m) / (0.015 * fs))
        noise = rng.standard_normal(m) * decay + 1j * rng.standard_normal(m) * decay
        out[i0:i1] += (amp / np.sqrt(2) * noise).astype(np.complex64)
    return out


def complex_noise(n: int, sigma: float, rng: np.random.Generator) -> np.ndarray:
    return (sigma / np.sqrt(2) * (
        rng.standard_normal(n) + 1j * rng.standard_normal(n)
    )).astype(np.complex64)


# ---------------------------------------------------------------------------
# Fixture bakers
# ---------------------------------------------------------------------------


def bake_hf_20m(out: Path) -> dict[str, object]:
    fs, dur, center = 250_000.0, 8.0, 14_150_000
    n = int(fs * dur)
    rng = np.random.default_rng(SEED)
    iq = complex_noise(n, 0.012, rng)

    signals: list[dict[str, object]] = []

    def add(sig: np.ndarray, offset: float, label: str) -> None:
        nonlocal iq
        iq += sig
        signals.append({
            "core:freq_start": center + offset, "core:freq_end": center + offset,
            "core:label": label,
        })

    add(cw_signal(2_100, n, fs, 0.45, "CQ CQ DE OW1RX OW1RX K", 20, rng, fading_hz=0.3),
        2_100, "CW CQ caller 20 WPM")
    add(cw_signal(-50_000, n, fs, 0.50, "VVVDEBECN B", 10, rng),
        -50_000, "CW beacon (14.100)")
    add(cw_signal(45_000, n, fs, 0.30, "RR TU FER NICE QSO 73 HPE CUAGN", 12, rng,
                  drift_hz=25.0),
        45_000, "CW drifter (old VFO)")
    add(cw_signal(-87_000, n, fs, 0.12, "TEST DE QRQ QRQ", 28, rng, fading_hz=0.5),
        -87_000, "CW weak/faded")
    add(ft8_like(-76_000, n, fs, 0.22, rng), -76_000, "FT8 trace (14.074)")
    add(ssb_signal(n, fs, 0.40, rng, fading_hz=0.25).astype(np.complex64)
        * np.exp(2j * np.pi * 8_000 * np.arange(n) / fs), 8_000, "USB QSO (14.158)")
    add(ssb_signal(n, fs, 0.28, rng).astype(np.complex64)
        * np.exp(2j * np.pi * -60_000 * np.arange(n) / fs), -60_000, "USB QSO (14.090)")
    am_audio = music_tones(n, fs, rng, base=440.0)
    add(am_signal(85_000, n, fs, 0.32, am_audio), 85_000, "AM broadcast (14.235)")
    iq += qrn_bursts(n, fs, rng)

    peak = np.max(np.abs(iq))
    if peak > 0.95:
        iq *= 0.95 / peak
    _write(out / "hf_20m_evening.cf32", iq, fs, center, signals)
    return {"file": "hf_20m_evening.cf32", "n": n, "fs": fs, "center": center}


def bake_fm_broadcast(out: Path) -> dict[str, object]:
    fs, dur, center = 1_000_000.0, 2.0, 98_000_000
    n = int(fs * dur)
    rng = np.random.default_rng(SEED + 1)
    iq = complex_noise(n, 0.004, rng)

    stations = [
        (-380_000.0, 1.00, True, True, 0.0, "97.6 stereo+RDS (strong)"),
        (-160_000.0, 0.35, False, False, 0.0, "97.8 mono speech"),
        (30_000.0, 0.85, True, False, 0.0, "98.0 stereo rock"),
        (220_000.0, 0.50, False, False, 0.0, "98.2 mono music"),
        (420_000.0, 0.15, True, False, 0.4, "98.4 stereo (distant, fading)"),
    ]
    signals = []
    for offset, amp, stereo, rds, fading, label in stations:
        iq += fm_station_signal(offset, n, fs, amp, rng, stereo, rds, fading)
        signals.append({
            "core:freq_start": center + offset - 100_000,
            "core:freq_end": center + offset + 100_000,
            "core:label": label,
        })
    peak = np.max(np.abs(iq))
    if peak > 0.95:
        iq *= 0.95 / peak
    _write(out / "vhf_fm_broadcast.cf32", iq, fs, center, signals)
    return {"file": "vhf_fm_broadcast.cf32", "n": n, "fs": fs, "center": center}


def bake_adsb(out: Path) -> dict[str, object]:
    fs, dur, center = 2_000_000.0, 1.0, 1_090_000_000
    n = int(fs * dur)
    rng = np.random.default_rng(SEED + 2)
    iq = complex_noise(n, 0.02, rng)

    aircraft = [
        (0x4D22AA, "OWRX001", 0.95),
        (0x3C70EE, "N42OWRX", 0.65),
        (0x06A1B2, "OPENWEB1", 0.45),
    ]
    signals = []
    frames = 0
    for icao, callsign, amp in aircraft:
        msgs: list[bytes] = [df11_all_call(icao), df17_callsign(icao, callsign),
                             df11_all_call(icao), df17_altitude(icao, 12_500)]
        for msg in msgs:
            t0 = rng.uniform(0.02, dur - 0.15)
            wave = ppm_waveform(msg, int(fs))
            i0 = int(t0 * fs)
            i1 = min(i0 + len(wave), n)
            amp_i = amp * rng.uniform(0.8, 1.0)
            iq[i0:i1] += (amp_i * wave[: i1 - i0]).astype(np.complex64)
            frames += 1
    # Two distant fragments.
    for _ in range(2):
        msg = df11_all_call(0xAABBCC)
        wave = ppm_waveform(msg, int(fs))
        i0 = int(rng.uniform(0.2, dur - 0.15) * fs)
        i1 = min(i0 + len(wave), n)
        iq[i0:i1] += (0.12 * wave[: i1 - i0]).astype(np.complex64)
        frames += 1

    signals.append({
        "core:freq_start": center, "core:freq_end": center,
        "core:label": f"Mode S 1090 MHz — {frames} CRC-valid frames, 3 aircraft",
    })
    _write(out / "adsb_1090.cf32", iq, fs, center, signals)
    return {"file": "adsb_1090.cf32", "n": n, "fs": fs, "center": center,
            "frames": frames}


def bake_smoke(out: Path) -> dict[str, object]:
    fs, dur, center = 250_000.0, 0.4, 100_000_000
    n = int(fs * dur)
    rng = np.random.default_rng(SEED + 3)
    iq = complex_noise(n, 0.02, rng)
    t = np.arange(n) / fs
    signals = []
    for offset, amp in ((-37_500.0, 0.5), (1_200.0, 0.4), (12_500.0, 0.3)):
        iq += (amp * np.exp(2j * np.pi * offset * t)).astype(np.complex64)
        signals.append({
            "core:freq_start": center + offset, "core:freq_end": center + offset,
            "core:label": f"CW {offset/1000:+.1f} kHz",
        })
    _write(out / "smoke.cf32", iq, fs, center, signals)
    return {"file": "smoke.cf32", "n": n, "fs": fs, "center": center}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _write(path: Path, iq: np.ndarray, fs: float, center: int,
           annotations: list[dict[str, object]]) -> None:
    iq.astype(np.complex64).tofile(path)
    meta = {
        "global": {
            "core:datatype": "cf32",
            "core:sample_rate": int(fs),
            "core:version": "1.0.0",
            "core:description": "OpenWebRX+ dev fixture (synthetic, deterministic)",
        },
        "captures": [
            {"core:sample_start": 0, "core:frequency": int(center)}
        ],
        "annotations": annotations,
    }
    meta_path = path.with_suffix(".meta")
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    size_mb = path.stat().st_size / 1e6
    print(f"  {path.name:26s} {iq.size:>9,d} samples  {size_mb:5.1f} MB  "
          f"@ {center/1e6:.3f} MHz, {int(fs):,} SPS")


BAKERS = {
    "hf_20m": bake_hf_20m,
    "fm": bake_fm_broadcast,
    "adsb": bake_adsb,
    "smoke": bake_smoke,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--which", default="all", choices=["all", *BAKERS])
    args = parser.parse_args()

    # Self-check the CRC against the dump1090 test vector BEFORE baking.
    msg_hex, expected = _CRC_SELF_CHECK
    got = crc24_mode_s(bytes.fromhex(msg_hex))
    if got != expected:
        print(f"CRC-24 self-check FAILED: got {got:06X}, expected {expected:06X}",
              file=sys.stderr)
        return 1
    print(f"CRC-24 self-check OK ({msg_hex}... → {got:06X})")

    args.out.mkdir(parents=True, exist_ok=True)
    print(f"Baking fixtures into {args.out}")
    names = list(BAKERS) if args.which == "all" else [args.which]
    for name in names:
        BAKERS[name](args.out)
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
