"""JT65 MFSK demodulator — 65-tone FSK for weak-signal EME.

JT65 is a weak-signal digital mode designed for Earth-Moon-Earth (EME)
communications. It uses 65-tone MFSK (Multi-Frequency Shift Keying):

  * **65 tones** spaced 11025/4096 ≈ 2.6917 Hz apart
  * **Center frequency**: 1270.5 Hz (tone spacing/2 offset from base)
  * **Symbol rate**: 11025/4096 ≈ 2.6917 baud (very slow — 0.3715 s per symbol)
  * **Transmission length**: 126 symbols = ~46.8 seconds
  * **Sub-mode A**: tone spacing 2.6917 Hz (default)
  * **Sub-mode B**: tone spacing doubled (5.3834 Hz, faster but less sensitive)
  * **Sub-mode C**: tone spacing tripled (8.0751 Hz)

The 65 tones are numbered 0-64. Tone 0 is the sync/reference tone;
tones 1-64 carry 6-bit symbols (2^6 = 64 data symbols).

The demodulator:
  1. Computes the Goertzel magnitude for each of the 65 tone frequencies
     per symbol period.
  2. The tone with the highest magnitude is the received symbol (0-64).
  3. Symbols are accumulated until all 126 are received.

This module is pure-numpy (ADR-004 compliant — no scipy in the live path).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# --- JT65 wire constants ---
DEFAULT_SAMPLE_RATE = 8000
DEFAULT_TONE_SPACING_HZ = 11025.0 / 4096.0  # 2.6917 Hz — JT65A spec
DEFAULT_BASE_FREQ_HZ = 1270.5  # tone 0 frequency
DEFAULT_NUM_SYMBOLS = 126  # JT65 transmission length
DEFAULT_NUM_TONES = 65


def _tone_freqs(base_hz: float, spacing: float, num_tones: int) -> list[float]:
    """Compute the JT65 tone frequencies.

    Tone 0 is at base_hz, tone N is at base_hz + N * spacing.
    """
    return [base_hz + i * spacing for i in range(num_tones)]


def _samples_per_symbol(sample_rate: int, spacing: float) -> int:
    """Samples per JT65 symbol = sample_rate / spacing."""
    return max(1, int(round(sample_rate / spacing)))


@dataclass
class Jt65Receiver:
    """Streaming JT65 MFSK demodulator — int16 PCM → 6-bit symbols.

    Args:
        sample_rate: audio sample rate in Hz (default 8000).
        base_freq_hz: tone 0 frequency (default 1270.5 Hz).
        tone_spacing: tone spacing in Hz (default 11025/4096 ≈ 2.6917 Hz).
        num_tones: number of MFSK tones (default 65).
    """

    sample_rate: int = DEFAULT_SAMPLE_RATE
    base_freq_hz: float = DEFAULT_BASE_FREQ_HZ
    tone_spacing: float = DEFAULT_TONE_SPACING_HZ
    num_tones: int = DEFAULT_NUM_TONES

    # Internal state.
    _tone_freqs: list[float] = field(default_factory=list)
    _sps: int = 0  # samples per symbol
    _goertzel_coeffs: list[tuple[float, float, float]] = field(default_factory=list)
    _symbols: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError(f"sample_rate must be > 0, got {self.sample_rate}")
        if self.base_freq_hz <= 0 or self.base_freq_hz >= self.sample_rate / 2:
            raise ValueError(
                f"base_freq_hz must be in (0, sample_rate/2={self.sample_rate / 2}), "
                f"got {self.base_freq_hz}"
            )
        if self.tone_spacing <= 0:
            raise ValueError(f"tone_spacing must be > 0, got {self.tone_spacing}")
        if self.num_tones < 2:
            raise ValueError(f"num_tones must be >= 2, got {self.num_tones}")
        freqs = _tone_freqs(self.base_freq_hz, self.tone_spacing, self.num_tones)
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
        """Feed int16 PCM samples, return a list of decoded symbols (0-64).

        Each symbol represents which tone was detected. The caller
        accumulates symbols until 126 are collected, then calls the
        protocol decoder.
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
        """True when a full 126-symbol JT65 message has been received."""
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
        """The tone frequencies in Hz (for diagnostics)."""
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
