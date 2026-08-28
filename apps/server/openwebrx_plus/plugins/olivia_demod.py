"""Olivia MFSK demodulator — Multi-Frequency Shift Keying → text.

Olivia is a robust MFSK digital mode designed for weak-signal HF work.
It uses multiple tones (32 or 64) with forward error correction (Folgers
/Golay code), making it readable even at -10 dB SNR.

This v1 implements Olivia 32-1000 (the most common variant):
  * **32 tones** spaced 1000/32 = 31.25 Hz apart
  * **Center frequency**: 1500 Hz (tones span 1000-2000 Hz)
  * **Symbol rate**: 1000/32 = 31.25 baud (one tone per symbol)
  * **Character encoding**: 7-bit ASCII with interleave + FEC

The demodulator:
  1. Computes the Goertzel magnitude for each of the 32 tone frequencies
     per symbol period.
  2. The tone with the highest magnitude is the received symbol.
  3. Symbols are accumulated until a complete character (7 bits + FEC)
     is decoded.

This module is pure-numpy (ADR-004 compliant — no scipy in the live path).
The FEC (Golay code) is deferred to v2 — v1 uses the raw 7-bit ASCII
without error correction, which works for strong signals.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# --- Olivia wire constants ---
DEFAULT_SAMPLE_RATE = 8000
DEFAULT_TONES = 32
DEFAULT_BANDWIDTH_HZ = 1000.0  # Olivia 32-1000
DEFAULT_CENTER_HZ = 1500.0

# Tone frequencies: center ± bandwidth/2, spaced bandwidth/tones apart.
# For Olivia 32-1000: tones at 1000, 1031.25, 1062.5, ..., 1968.75 Hz.


def _tone_freqs(
    num_tones: int,
    bandwidth: float,
    center: float,
) -> list[float]:
    """Compute the tone frequencies for Olivia MFSK.

    Tones are spaced bandwidth/num_tones apart, centered at `center`.
    The lowest tone is at center - bandwidth/2 + spacing/2.
    """
    spacing = bandwidth / num_tones
    start = center - bandwidth / 2 + spacing / 2
    return [start + i * spacing for i in range(num_tones)]


def _samples_per_symbol(sample_rate: int, bandwidth: float, num_tones: int) -> int:
    """Samples per symbol = sample_rate / (bandwidth / num_tones)."""
    symbol_rate = bandwidth / num_tones
    return max(1, int(round(sample_rate / symbol_rate)))


@dataclass
class OliviaReceiver:
    """Streaming Olivia MFSK demodulator — int16 PCM → 5-bit symbols.

    Args:
        sample_rate: audio sample rate in Hz (default 8000).
        num_tones: number of MFSK tones (default 32 for Olivia 32-1000).
        bandwidth: total bandwidth in Hz (default 1000).
        center_hz: center frequency in Hz (default 1500).
    """

    sample_rate: int = DEFAULT_SAMPLE_RATE
    num_tones: int = DEFAULT_TONES
    bandwidth: float = DEFAULT_BANDWIDTH_HZ
    center_hz: float = DEFAULT_CENTER_HZ

    # Internal state.
    _tone_freqs: list[float] = field(default_factory=list)
    _sps: int = 0  # samples per symbol
    _goertzel_coeffs: list[tuple[float, float, float]] = field(default_factory=list)
    _symbols: list[int] = field(default_factory=list)
    _bit_buffer: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError(f"sample_rate must be > 0, got {self.sample_rate}")
        if self.num_tones < 2:
            raise ValueError(f"num_tones must be >= 2, got {self.num_tones}")
        if self.bandwidth <= 0:
            raise ValueError(f"bandwidth must be > 0, got {self.bandwidth}")
        if self.center_hz <= 0 or self.center_hz >= self.sample_rate / 2:
            raise ValueError(
                f"center_hz must be in (0, sample_rate/2={self.sample_rate / 2}), "
                f"got {self.center_hz}"
            )
        # Verify all tones fit within Nyquist.
        freqs = _tone_freqs(self.num_tones, self.bandwidth, self.center_hz)
        for f in freqs:
            if f <= 0 or f >= self.sample_rate / 2:
                raise ValueError(
                    f"tone {f:.2f} Hz out of range (0, {self.sample_rate / 2})"
                )
        self._tone_freqs = freqs
        self._sps = _samples_per_symbol(self.sample_rate, self.bandwidth, self.num_tones)
        # Precompute Goertzel coefficients for each tone.
        self._goertzel_coeffs = [
            _goertzel_coeff(f, self.sample_rate) for f in freqs
        ]

    def feed(self, pcm: np.ndarray) -> list[int]:
        """Feed int16 PCM samples, return a list of decoded symbols (0 to num_tones-1).

        Each symbol represents which tone was detected. The caller
        (OliviaDecoderPlugin) assembles symbols into characters.
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
            # Compute Goertzel magnitude for each tone.
            mags = [
                _goertzel_mag(chunk, *coeffs)
                for coeffs in self._goertzel_coeffs
            ]
            # The symbol is the tone with the highest magnitude.
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
    def tone_freqs(self) -> list[float]:
        """The tone frequencies in Hz (for diagnostics)."""
        return list(self._tone_freqs)

    def reset(self) -> None:
        """Clear all streaming state."""
        self._symbols.clear()
        self._bit_buffer.clear()


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
