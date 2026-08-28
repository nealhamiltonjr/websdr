"""WSPR (Weak Signal Propagation Reporter) demodulator — 4-tone FSK.

WSPR is a weak-signal HF mode designed for propagation studies. It uses
4-tone FSK (Frequency Shift Keying) with very tight parameters:

  * **4 tones** spaced 12000/8192 ≈ 1.4648 Hz apart
  * **Center frequency**: typically 1500 Hz (tones at 1400, 1401.46, 1402.93, 1404.39)
  * **Symbol rate**: 12000/8192 ≈ 1.4648 baud (very slow — 0.6827 s per symbol)
  * **Transmission length**: 162 symbols = ~110.6 seconds
  * **Payload**: callsign (28 bits) + grid locator (15 bits) + power dBm (7 bits) = 50 bits
  * **FEC**: Convolutional code (rate 1/2, K=32) → 100 bits + interleaving → 162 symbols

The demodulator:
  1. Computes the Goertzel magnitude for each of the 4 tone frequencies
     per symbol period (~0.68 s at 8 kHz — long integration for weak signals).
  2. The tone with the highest magnitude is the received 2-bit symbol.
  3. Symbols are accumulated until all 162 are received.
  4. The full symbol set is de-interleaved and Viterbi-decoded (v1 uses
     a simplified hard-decision decoder; full soft-decision Viterbi is v2).

This module is pure-numpy (ADR-004 compliant — no scipy in the live path).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# --- WSPR wire constants ---
DEFAULT_SAMPLE_RATE = 8000
DEFAULT_TONE_SPACING_HZ = 12000.0 / 8192.0  # 1.4648 Hz — WSPR spec
DEFAULT_CENTER_HZ = 1500.0  # the "frequency 0" tone is at center - 1.5 * spacing
DEFAULT_NUM_SYMBOLS = 162  # WSPR transmission length
DEFAULT_BAUD = DEFAULT_TONE_SPACING_HZ  # 1 symbol per baud period


def _tone_freqs(center_hz: float, spacing: float) -> list[float]:
    """Compute the 4 WSPR tone frequencies.

    WSPR tone 0 is at center - 1.5 * spacing, tone 1 at center - 0.5 * spacing,
    tone 2 at center + 0.5 * spacing, tone 3 at center + 1.5 * spacing.
    (The center frequency is between tones 1 and 2.)
    """
    return [
        center_hz - 1.5 * spacing,  # tone 0
        center_hz - 0.5 * spacing,  # tone 1
        center_hz + 0.5 * spacing,  # tone 2
        center_hz + 1.5 * spacing,  # tone 3
    ]


def _samples_per_symbol(sample_rate: int, spacing: float) -> int:
    """Samples per WSPR symbol = sample_rate / spacing."""
    return max(1, int(round(sample_rate / spacing)))


@dataclass
class WsprReceiver:
    """Streaming WSPR demodulator — int16 PCM → 2-bit symbols.

    Args:
        sample_rate: audio sample rate in Hz (default 8000).
        center_hz: center frequency (default 1500 Hz).
        tone_spacing: tone spacing in Hz (default 12000/8192 ≈ 1.4648 Hz).
    """

    sample_rate: int = DEFAULT_SAMPLE_RATE
    center_hz: float = DEFAULT_CENTER_HZ
    tone_spacing: float = DEFAULT_TONE_SPACING_HZ

    # Internal state.
    _tone_freqs: list[float] = field(default_factory=list)
    _sps: int = 0  # samples per symbol
    _goertzel_coeffs: list[tuple[float, float, float]] = field(default_factory=list)
    _symbols: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError(f"sample_rate must be > 0, got {self.sample_rate}")
        if self.center_hz <= 0 or self.center_hz >= self.sample_rate / 2:
            raise ValueError(
                f"center_hz must be in (0, sample_rate/2={self.sample_rate / 2}), "
                f"got {self.center_hz}"
            )
        if self.tone_spacing <= 0:
            raise ValueError(f"tone_spacing must be > 0, got {self.tone_spacing}")
        freqs = _tone_freqs(self.center_hz, self.tone_spacing)
        for f in freqs:
            if f <= 0 or f >= self.sample_rate / 2:
                raise ValueError(
                    f"tone {f:.2f} Hz out of range (0, {self.sample_rate / 2})"
                )
        self._tone_freqs = freqs
        self._sps = _samples_per_symbol(self.sample_rate, self.tone_spacing)
        self._goertzel_coeffs = [
            _goertzel_coeff(f, self.sample_rate) for f in freqs
        ]

    def feed(self, pcm: np.ndarray) -> list[int]:
        """Feed int16 PCM samples, return a list of decoded 2-bit symbols (0-3).

        Most calls will return a partial list (WSPR takes 110.6 s for a full
        162-symbol transmission). The caller accumulates symbols until 162
        are collected, then calls the protocol decoder.
        """
        if pcm.size == 0:
            return []
        if pcm.dtype != np.int16:
            pcm = pcm.astype(np.int16)
        audio = pcm.astype(np.float32) / 32768.0
        symbols: list[int] = []
        i = 0
        while i + self._sps <= audio.size:
            chunk = audio[i : i + self._sps]
            mags = [
                _goertzel_mag(chunk, *coeffs)
                for coeffs in self._goertzel_coeffs
            ]
            best = int(np.argmax(mags))
            symbols.append(best)
            i += self._sps
        self._symbols.extend(symbols)
        return symbols

    @property
    def symbol_count(self) -> int:
        """Total symbols decoded since creation."""
        return len(self._symbols)

    @property
    def is_complete(self) -> bool:
        """True when a full 162-symbol WSPR message has been received."""
        return len(self._symbols) >= DEFAULT_NUM_SYMBOLS

    @property
    def symbols(self) -> list[int]:
        """The accumulated symbol buffer."""
        return list(self._symbols)

    def consume_symbols(self, n: int) -> list[int]:
        """Remove and return the first n symbols from the buffer."""
        consumed = self._symbols[:n]
        self._symbols = self._symbols[n:]
        return consumed

    def reset(self) -> None:
        """Clear all streaming state."""
        self._symbols.clear()

    @property
    def tone_freqs(self) -> list[float]:
        """The 4 tone frequencies in Hz (for diagnostics)."""
        return list(self._tone_freqs)


def _goertzel_coeff(freq: float, sample_rate: int) -> tuple[float, float, float]:
    """Precompute the Goertzel coefficient for a given frequency."""
    k = 2.0 * math.pi * freq / sample_rate
    return (2.0 * math.cos(k), math.cos(k), math.sin(k))


def _goertzel_mag(samples: np.ndarray, coeff: float, cos_term: float, sin_term: float) -> float:
    """Compute the Goertzel magnitude for a chunk of samples."""
    s_prev = 0.0
    s_prev2 = 0.0
    for x in samples:
        s = x + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    mag_sq = s_prev * s_prev + s_prev2 * s_prev2 - coeff * s_prev * s_prev2
    return math.sqrt(max(0.0, mag_sq))
