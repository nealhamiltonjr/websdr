"""JT9 MFSK demodulator — 9-tone FSK for weak-signal HF.

JT9 is a weak-signal digital mode related to JT65 but using only 9 tones
instead of 65. It's designed for the LF/MF/HF bands and offers sub-modes
with different timing:

  * **9 tones** spaced 12000/4096 ≈ 1.4648 Hz apart (JT9-1, default)
  * **Center frequency**: 1000 Hz (tone 0)
  * **Symbol rate**: 12000/4096 ≈ 1.4648 baud (JT9-1, ~1 min)
  * **Sub-modes**: JT9-1 (1 min), JT9-2 (30 s), JT9-5 (5 min),
    JT9-10 (10 min), JT9-30 (30 min) — the number is the approximate
    duration in minutes.
  * **Transmission length**: 85 symbols for JT9-1
  * **Payload**: 72 bits (callsign1 + callsign2 + grid/report), same
    as JT65, with different FEC + interleaving.

The demodulator mirrors the JT65 demodulator but with 9 tones instead of 65.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# --- JT9 wire constants ---
DEFAULT_SAMPLE_RATE = 8000
DEFAULT_TONE_SPACING_HZ = 12000.0 / 4096.0  # 1.4648 Hz — same as WSPR
DEFAULT_BASE_FREQ_HZ = 1000.0  # tone 0 frequency
DEFAULT_NUM_SYMBOLS = 85  # JT9-1 transmission length
DEFAULT_NUM_TONES = 9


def _tone_freqs(base_hz: float, spacing: float, num_tones: int) -> list[float]:
    """Compute the JT9 tone frequencies."""
    return [base_hz + i * spacing for i in range(num_tones)]


def _samples_per_symbol(sample_rate: int, spacing: float) -> int:
    """Samples per JT9 symbol = sample_rate / spacing."""
    return max(1, int(round(sample_rate / spacing)))


@dataclass
class Jt9Receiver:
    """Streaming JT9 MFSK demodulator — int16 PCM → 4-bit symbols.

    Args:
        sample_rate: audio sample rate in Hz (default 8000).
        base_freq_hz: tone 0 frequency (default 1000 Hz).
        tone_spacing: tone spacing in Hz (default 12000/4096 ≈ 1.4648 Hz).
        num_tones: number of MFSK tones (default 9).
    """

    sample_rate: int = DEFAULT_SAMPLE_RATE
    base_freq_hz: float = DEFAULT_BASE_FREQ_HZ
    tone_spacing: float = DEFAULT_TONE_SPACING_HZ
    num_tones: int = DEFAULT_NUM_TONES

    _tone_freqs: list[float] = field(default_factory=list)
    _sps: int = 0
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
        """Feed int16 PCM samples, return a list of decoded symbols (0-8)."""
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
        return len(self._symbols)

    @property
    def is_complete(self) -> bool:
        """True when a full 85-symbol JT9 message has been received."""
        return len(self._symbols) >= DEFAULT_NUM_SYMBOLS

    @property
    def symbols(self) -> list[int]:
        return list(self._symbols)

    def consume_symbols(self, n: int) -> list[int]:
        consumed = self._symbols[:n]
        self._symbols = self._symbols[n:]
        return consumed

    def reset(self) -> None:
        self._symbols.clear()

    @property
    def tone_freqs(self) -> list[float]:
        return list(self._tone_freqs)


def _goertzel_coeff(freq: float, sample_rate: int) -> tuple[float, float, float]:
    k = 2.0 * math.pi * freq / sample_rate
    return (2.0 * math.cos(k), math.cos(k), math.sin(k))


def _goertzel_mag(samples: np.ndarray, coeff: float, cos_term: float, sin_term: float) -> float:
    s_prev = 0.0
    s_prev2 = 0.0
    for x in samples:
        s = x + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    mag_sq = s_prev * s_prev + s_prev2 * s_prev2 - coeff * s_prev * s_prev2
    return math.sqrt(max(0.0, mag_sq))
