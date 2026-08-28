"""PSK31 (Phase Shift Keying, 31.25 baud) demodulator — BPSK → Varicode bits.

PSK31 is a popular keyboard-to-keyboard digital mode for HF. The audio-band
signal is BPSK (Binary Phase Shift Keying):

  * **Center frequency**: typically 1000 Hz (the audio tone)
  * **Baud rate**: 31.25 baud (32 ms per symbol)
  * **Modulation**: 0° phase = bit 1, 180° phase = bit 0 (differential BPSK
    is common in practice, but this v1 demod uses coherent phase tracking)
  * **Character encoding**: Varicode (variable-length, see psk31_protocol.py)
  * **Separator**: two consecutive 0 bits separate characters

The demodulator:
  1. Mixes the signal down to baseband (multiply by complex LO at the center freq)
  2. Low-pass filters to remove the 2× LO component (simple moving average)
  3. Computes the instantaneous phase via np.angle()
  4. Detects phase reversals (π jumps) → bit 0; no change → bit 1
  5. Samples at mid-bit (clock recovery at 31.25 baud)
  6. Hands the bit stream to the Varicode decoder

This module is pure-numpy (ADR-004 compliant — no scipy in the live path).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# --- PSK31 wire constants ---
DEFAULT_SAMPLE_RATE = 8000
DEFAULT_CENTER_HZ = 1000.0  # the audio tone
DEFAULT_BAUD = 31.25  # PSK31 standard


def _samples_per_bit(sample_rate: int, baud: float) -> int:
    return max(1, int(round(sample_rate / baud)))


@dataclass
class Psk31Receiver:
    """Streaming PSK31 demodulator — int16 PCM → Varicode bits.

    Args:
        sample_rate: audio sample rate in Hz (default 8000 — the wire format).
        center_hz: BPSK carrier frequency (default 1000 Hz).
        baud: symbol rate (default 31.25 baud — PSK31 standard).
    """

    sample_rate: int = DEFAULT_SAMPLE_RATE
    center_hz: float = DEFAULT_CENTER_HZ
    baud: float = DEFAULT_BAUD

    # Internal state.
    _spb: int = 0  # samples per bit
    _phase_accum: float = 0.0  # LO phase accumulator
    _lo_phase_inc: float = 0.0  # LO phase increment per sample
    _lpf_buf: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.complex64))
    _lpf_len: int = 0  # low-pass filter window length
    _prev_phase: float = 0.0  # previous bit's phase (for reversal detection)
    _bit_offset: int = 0  # sample position within current bit period
    _bits: list[int] = field(default_factory=list)  # decoded bit stream

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError(f"sample_rate must be > 0, got {self.sample_rate}")
        if self.center_hz <= 0 or self.center_hz >= self.sample_rate / 2:
            raise ValueError(
                f"center_hz must be in (0, sample_rate/2={self.sample_rate / 2}), "
                f"got {self.center_hz}"
            )
        if self.baud <= 0:
            raise ValueError(f"baud must be > 0, got {self.baud}")
        self._spb = _samples_per_bit(self.sample_rate, self.baud)
        self._lo_phase_inc = 2.0 * math.pi * self.center_hz / self.sample_rate
        # LPF window: ~2 bit periods (enough to smooth the 2× LO component
        # without excessive group delay).
        self._lpf_len = self._spb * 2

    def feed(self, pcm: np.ndarray) -> list[int]:
        """Feed int16 PCM samples, return a list of decoded bits (0 or 1).

        The caller (Psk31DecoderPlugin) hands these bits to the Varicode
        decoder which assembles them into characters.

        Args:
            pcm: 1-D int16 numpy array (mono audio).
        Returns:
            List of bits (0 or 1). Empty list if no bits decoded.
        """
        if pcm.size == 0:
            return []
        if pcm.dtype != np.int16:
            pcm = pcm.astype(np.int16)
        # Convert to float32 in [-1, 1].
        audio = pcm.astype(np.float32) / 32768.0

        # 1. Mix down to baseband: multiply by complex LO at center_hz.
        n = audio.size
        phases = self._phase_accum + np.arange(n) * self._lo_phase_inc
        lo = np.exp(-1j * phases).astype(np.complex64)
        self._phase_accum = phases[-1] + self._lo_phase_inc
        # Wrap phase to [0, 2π) to avoid float precision loss over long runs.
        self._phase_accum = math.fmod(self._phase_accum, 2.0 * math.pi)
        baseband = audio.astype(np.complex64) * lo

        # 2. Low-pass filter (moving average over _lpf_len samples).
        # Prepend the leftover from last call for continuity.
        buf = np.concatenate([self._lpf_buf, baseband])
        # Compute the moving average via cumsum (O(N), not O(N*window)).
        if buf.size >= self._lpf_len:
            cumsum = np.cumsum(buf)
            # Moving average: (cumsum[lpf_len-1:] - cumsum[:-lpf_len]) / lpf_len
            # But we need to handle the initial partial window too.
            filtered = np.zeros(buf.size, dtype=np.complex64)
            filtered[self._lpf_len - 1 :] = (cumsum[self._lpf_len - 1 :] - np.concatenate([[0], cumsum[: -self._lpf_len]])) / self._lpf_len
            # Save the tail for next call.
            self._lpf_buf = buf[-self._lpf_len + 1 :] if buf.size >= self._lpf_len - 1 else buf
            baseband_filt = filtered[self._lpf_len - 1 :]
        else:
            # Not enough samples for one filter window — save for next call.
            self._lpf_buf = buf
            return []

        # 3. Compute instantaneous phase.
        phase = np.angle(baseband_filt)

        # 4. Detect phase reversals + sample at mid-bit.
        bits: list[int] = []
        i = 0
        while i < phase.size:
            # We sample one bit per _spb samples, at the mid-bit position.
            if i + self._spb > phase.size:
                break
            # Mid-bit phase.
            mid = i + self._spb // 2
            current_phase = phase[mid]
            # Phase difference (wrapped to [-π, π]).
            dphi = _wrap_pi(current_phase - self._prev_phase)
            # If |dphi| > π/2, it's a phase reversal → bit 0.
            # Otherwise, no reversal → bit 1.
            bit = 0 if abs(dphi) > math.pi / 2 else 1
            bits.append(bit)
            self._prev_phase = current_phase
            i += self._spb

        # Append to the internal bit stream (for Varicode decoder continuity).
        self._bits.extend(bits)
        return bits

    @property
    def bit_stream(self) -> list[int]:
        """The accumulated bit stream (for the Varicode decoder)."""
        return self._bits

    def consume_bits(self, n: int) -> None:
        """Remove the first n bits from the internal stream (after the
        Varicode decoder has consumed them)."""
        self._bits = self._bits[n:]

    def reset(self) -> None:
        """Clear all streaming state."""
        self._phase_accum = 0.0
        self._lpf_buf = np.zeros(0, dtype=np.complex64)
        self._prev_phase = 0.0
        self._bit_offset = 0
        self._bits.clear()


def _wrap_pi(angle: float) -> float:
    """Wrap an angle to [-π, π)."""
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle < -math.pi:
        angle += 2.0 * math.pi
    return angle
