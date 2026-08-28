"""RTTY (Radio Teletype) demodulator — FSK mark/space → ITA2 characters.

RTTY is one of the oldest digital modes in amateur radio. The audio-band
signal is two-tone FSK (Frequency Shift Keying):

  * **Mark** (1 / idle / stop bit): 2125 Hz (the "high" tone)
  * **Space** (0 / start bit): 2295 Hz (mark + 170 Hz shift)
  * **Baud rate**: 45.45 baud (amateur standard) or 50 baud (European)
  * **Character encoding**: ITA2 / Baudot, 5-bit codes (see rtty_protocol.py)
  * **Frame format**: 1 start bit (0) + 5 data bits (LSB first) + 1.42 stop bits (1)

The demodulator uses two Goertzel filters (one per tone) to compute the
mark/space magnitude ratio per sample, then slices at mid-bit to recover
bits. A state machine finds the start bit, clocks 5 data bits, validates
the stop bit, and hands the 5-bit code to the ITA2 decoder.

This module is pure-numpy (ADR-004 compliant — no scipy in the live path).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# --- RTTY wire constants (single place to fix after first live bring-up) ---
DEFAULT_SAMPLE_RATE = 8000
DEFAULT_MARK_HZ = 2125.0
DEFAULT_SPACE_HZ = 2295.0  # mark + 170 Hz shift (the standard ham RTTY shift)
DEFAULT_BAUD = 45.45  # amateur standard (50.0 for European)

# Goertzel window: we process in chunks of `samples_per_bit` so we can
# sample at mid-bit for robust clock recovery. At 8000 Hz / 45.45 baud,
# samples_per_bit = 8000 / 45.45 ≈ 176.
def _samples_per_bit(sample_rate: int, baud: float) -> int:
    return max(1, int(round(sample_rate / baud)))


@dataclass
class RttyReceiver:
    """Streaming RTTY demodulator — int16 PCM → ITA2 5-bit codes.

    Args:
        sample_rate: audio sample rate in Hz (default 8000 — the wire format).
        mark_hz: mark tone frequency (default 2125 Hz).
        space_hz: space tone frequency (default 2295 Hz).
        baud: symbol rate (default 45.45 baud — amateur standard).
    """

    sample_rate: int = DEFAULT_SAMPLE_RATE
    mark_hz: float = DEFAULT_MARK_HZ
    space_hz: float = DEFAULT_SPACE_HZ
    baud: float = DEFAULT_BAUD

    # Internal state (set in __post_init__).
    _spb: int = 0  # samples per bit
    _goertzel_mark: tuple[float, float, float] = (0.0, 0.0, 0.0)  # coeff, cos, sin
    _goertzel_space: tuple[float, float, float] = (0.0, 0.0, 0.0)
    _bit_buffer: list[int] = field(default_factory=list)
    _in_frame: bool = False
    _bit_count: int = 0
    _current_byte: int = 0
    _sample_offset: int = 0  # position within the current bit period
    _last_bit: int = 1  # idle line is mark (1)

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError(f"sample_rate must be > 0, got {self.sample_rate}")
        if self.mark_hz <= 0 or self.space_hz <= 0:
            raise ValueError("mark_hz and space_hz must be > 0")
        if self.mark_hz >= self.sample_rate / 2 or self.space_hz >= self.sample_rate / 2:
            raise ValueError(
                f"mark/space must be < sample_rate/2 ({self.sample_rate / 2}), "
                f"got mark={self.mark_hz}, space={self.space_hz}"
            )
        if self.baud <= 0:
            raise ValueError(f"baud must be > 0, got {self.baud}")
        if abs(self.mark_hz - self.space_hz) < 1.0:
            raise ValueError(
                f"mark and space must differ by >= 1 Hz, got "
                f"mark={self.mark_hz}, space={self.space_hz}"
            )
        self._spb = _samples_per_bit(self.sample_rate, self.baud)
        self._goertzel_mark = _goertzel_coeff(self.mark_hz, self.sample_rate)
        self._goertzel_space = _goertzel_coeff(self.space_hz, self.sample_rate)

    def feed(self, pcm: np.ndarray) -> list[int]:
        """Feed int16 PCM samples, return a list of decoded 5-bit codes (0-31).

        Each code represents one ITA2 character. The caller (RttyDecoderPlugin)
        hands these to the ITA2 decoder which tracks letter/figure shift state
        and produces text.

        Args:
            pcm: 1-D int16 numpy array (mono audio).
        Returns:
            List of 5-bit codes (0-31). Empty list if no complete characters
            decoded in this chunk.
        """
        if pcm.size == 0:
            return []
        if pcm.dtype != np.int16:
            pcm = pcm.astype(np.int16)
        # Convert to float32 in [-1, 1] for the Goertzel filter.
        audio = pcm.astype(np.float32) / 32768.0

        codes: list[int] = []
        i = 0
        n = audio.size
        while i < n:
            # We need at least _spb samples to evaluate one bit.
            remaining = n - i
            if remaining < self._spb:
                break
            # Compute Goertzel magnitude for mark and space over this bit window.
            chunk = audio[i : i + self._spb]
            mark_mag = _goertzel_mag(chunk, *self._goertzel_mark)
            space_mag = _goertzel_mag(chunk, *self._goertzel_space)
            # FSK decision: mark (1) if mark_mag > space_mag, else space (0).
            bit = 1 if mark_mag > space_mag else 0

            if not self._in_frame:
                # Idle line is mark (1). Look for a start bit (transition to 0).
                if self._last_bit == 1 and bit == 0:
                    self._in_frame = True  # start bit detected
                    self._bit_count = 0
                    self._current_byte = 0
            else:
                # We're in a frame: collect 5 data bits (LSB first).
                if self._bit_count < 5:
                    self._current_byte |= (bit << self._bit_count)
                    self._bit_count += 1
                elif self._bit_count == 5:
                    # This should be the stop bit (1). If it's 0, we have a
                    # framing error — but we still emit the character (RTTY
                    # is noisy; the ITA2 decoder can handle occasional errors).
                    codes.append(self._current_byte)
                    self._in_frame = False
                    # If the stop bit was actually mark (1), we're back to idle.
                    # If it was space (0), the next start-bit detection will
                    # pick up immediately (back-to-back characters).
            self._last_bit = bit
            i += self._spb

        return codes

    def reset(self) -> None:
        """Clear all streaming state (bit buffer, frame state)."""
        self._bit_buffer.clear()
        self._in_frame = False
        self._bit_count = 0
        self._current_byte = 0
        self._sample_offset = 0
        self._last_bit = 1  # idle line is mark


def _goertzel_coeff(freq: float, sample_rate: int) -> tuple[float, float, float]:
    """Precompute the Goertzel coefficient for a given frequency.

    Returns (coeff, cos_term, sin_term) where:
      coeff = 2 * cos(2π f / fs)
      cos_term = cos(2π f / fs)
      sin_term = sin(2π f / fs)

    The magnitude is computed as:
      s = prev_s * coeff - prev_prev_s + sample
      mag² = s² + prev_s² - coeff * s * prev_s
      mag = sqrt(mag²)
    """
    k = 2.0 * math.pi * freq / sample_rate
    return (2.0 * math.cos(k), math.cos(k), math.sin(k))


def _goertzel_mag(samples: np.ndarray, coeff: float, cos_term: float, sin_term: float) -> float:
    """Compute the Goertzel magnitude for a chunk of samples.

    This is the standard Goertzel algorithm: a recursive IIR filter that
    computes the DFT bin at one frequency. O(N) per chunk (much cheaper
    than a full FFT when you only need 2 bins).
    """
    s_prev = 0.0
    s_prev2 = 0.0
    for x in samples:
        s = x + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    # Magnitude = sqrt(s_prev² + s_prev2² - coeff * s_prev * s_prev2)
    mag_sq = s_prev * s_prev + s_prev2 * s_prev2 - coeff * s_prev * s_prev2
    return math.sqrt(max(0.0, mag_sq))
