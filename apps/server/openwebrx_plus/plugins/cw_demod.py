"""CW (Morse code) demodulator — pure Python + numpy.

Streams audio PCM, finds the sidetone via a Goertzel filter at the
configured frequency, computes the envelope, threshold-splits into
on/off intervals, and feeds them to the MorseDecoder state machine.

Architecture:
  int16 PCM (mono, 8 kHz default) → Goertzel(sidetone) → envelope
  → adaptive-threshold bit slice → on/off intervals → MorseDecoder

The Goertzel filter is the standard single-bin DFT (O(N), no FFT cost)
that's optimal for detecting the power at one specific frequency —
exactly the case for a CW sidetone.

Sample-rate contract: any rate ≥ 4 kHz works; the sidetone frequency
must be in (200, 4000) Hz. 8 kHz mono int16 is the wire format the
session pushes, so that's the default.
"""

from __future__ import annotations

import math

import numpy as np

from .cw_protocol import MorseDecoder

# Default sidetone: 600 Hz is the standard "CW offset" — operators tune
# to make the sidetone land at the comfortable 600 Hz beat note.
DEFAULT_SIDETONE_HZ = 600.0
DEFAULT_SAMPLE_RATE = 8000
DEFAULT_WPM = 20.0

# Block size for the Goertzel filter — 10 ms at 8 kHz = 80 samples.
# Smaller blocks → faster response but more noise; 10 ms is a good
# balance for CW (the dit at 20 WPM is 60 ms, so 10 ms resolves it).
_BLOCK_MS = 10.0

# Adaptive threshold: noise floor = EMA of envelope power; signal
# threshold = noise + margin. Conservative defaults for clean signals.
_NOISE_ADAPT_RATE = 0.02
_THRESHOLD_MARGIN_DB = 12.0  # 12 dB above noise

# Hysteresis factor (0-1): off-threshold = on-threshold * factor.
# Prevents oscillation at the threshold boundary.
_HYSTERESIS = 0.7


class CwReceiver:
    """Streaming CW demodulator + Morse decoder.

    Feed int16 PCM chunks in any size; decoded text characters accumulate
    in `text` (a read-only property on the MorseDecoder).
    """

    def __init__(
        self,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        sidetone_hz: float = DEFAULT_SIDETONE_HZ,
        wpm_estimate: float = DEFAULT_WPM,
    ) -> None:
        if int(sample_rate) <= 0:
            raise ValueError(f"CW demodulator requires positive sample_rate, got {sample_rate}")
        if not (50.0 <= sidetone_hz <= sample_rate / 2.0):
            raise ValueError(
                f"sidetone_hz={sidetone_hz} out of range (50..{sample_rate / 2})"
            )
        self.sample_rate = int(sample_rate)
        self.sidetone_hz = float(sidetone_hz)
        self._block_size = max(8, int(self.sample_rate * _BLOCK_MS / 1000.0))
        self._decoder = MorseDecoder(wpm_estimate=wpm_estimate)
        self._leftover: bytes = b""
        # Pre-compute Goertzel coefficients for the sidetone.
        k = round(self._block_size * self.sidetone_hz / self.sample_rate)
        omega = 2.0 * math.pi * k / self._block_size
        self._goertzel_coeff = 2.0 * math.cos(omega)
        self._goertzel_mag2 = 4.0 * (math.sin(omega / 2.0) ** 2)
        # Envelope buffer (one power value per block).
        self._envelope: list[float] = []
        # Adaptive threshold state.
        self._noise_floor: float = 0.0
        self._on_threshold: float = 0.0
        self._currently_on = False
        self._interval_start_idx = 0
        # Total block counter (for interval → ms conversion).
        self._block_idx = 0

    def feed(self, pcm: np.ndarray) -> str:
        """Process int16 PCM; return any text decoded this batch."""
        if pcm.size == 0:
            return ""
        # Pull in leftover bytes from the previous call.
        if self._leftover:
            prev = np.frombuffer(self._leftover, dtype="<i2").astype(np.float32)
            samples = np.concatenate([prev, pcm.astype(np.float32)])
            self._leftover = b""
        else:
            samples = pcm.astype(np.float32)

        bs = self._block_size
        # Process whole blocks; leave the sub-block tail for next call.
        n_full_blocks = samples.size // bs
        if n_full_blocks == 0:
            # Not enough for a block — buffer and wait.
            self._leftover = samples.astype("<i2").tobytes()
            return ""

        processed = n_full_blocks * bs
        block_view = samples[:processed].reshape(n_full_blocks, bs)
        powers = self._goertzel_blocks(block_view)
        self._envelope.extend(powers.tolist())

        # Adaptive noise floor + threshold (initialize on first batch).
        if self._noise_floor == 0.0:
            # Initial noise floor = median of the first batch's powers (silence
            # or low signal dominates the median).
            self._noise_floor = max(float(np.median(powers)), 1.0)
            self._on_threshold = self._noise_floor * (10 ** (_THRESHOLD_MARGIN_DB / 10.0))

        # Hysteresis threshold — compute once per batch (the noise floor
        # EMA doesn't change fast enough within a batch to matter).
        on_thr = self._on_threshold
        off_thr = on_thr * _HYSTERESIS

        # Update the noise floor ONLY on blocks classified as "off"
        # (below off_thr). Updating during tone blocks (the bug this
        # fixes) raises the noise floor up to the signal level, which
        # then exceeds the (now-raised) threshold and false-transitions
        # to off mid-tone. Tracking noise during silence is the right
        # discipline — the same one agc-free receivers use.
        for p in powers:
            if p < off_thr:
                self._noise_floor = (
                    (1.0 - _NOISE_ADAPT_RATE) * self._noise_floor
                    + _NOISE_ADAPT_RATE * p
                )
                self._on_threshold = self._noise_floor * (10 ** (_THRESHOLD_MARGIN_DB / 10.0))

        # Buffer the tail for the next call.
        if processed < samples.size:
            self._leftover = samples[processed:].astype("<i2").tobytes()

        # Threshold-slice the envelope into on/off intervals.
        return self._emit_intervals(len(powers))

    @property
    def text(self) -> str:
        """All decoded text so far."""
        return self._decoder.text

    def flush(self) -> str:
        """Flush any buffered samples + the pending char (call on stop)."""
        if self._leftover:
            self._leftover = b""
        return self._decoder.flush()

    def reset(self) -> None:
        """Drop all state (mode switch / source change)."""
        self._decoder.reset()
        self._envelope = []
        self._noise_floor = 0.0
        self._on_threshold = 0.0
        self._currently_on = False
        self._interval_start_idx = 0
        self._block_idx = 0
        self._leftover = b""

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _goertzel_blocks(self, blocks: np.ndarray) -> np.ndarray:
        """Compute Goertzel power at the sidetone for each block.

        `blocks` is shape (n_blocks, block_size), float32.
        Returns shape (n_blocks,) power values.
        """
        # Vectorized Goertzel: process all blocks via numpy.
        # s_prev2 = 0, s_prev1 = 0 (per block). We compute s0 for k=0..block_size-1.
        coeff = self._goertzel_coeff
        # We can't trivially vectorize the recurrence across blocks (each
        # block has its own state). The naive Python loop is fine for ~80-sample
        # blocks at 100 blocks/sec (8000 samples/sec) — the cost is 8 ops/block.
        # For a 1-second feed at 8 kHz that's 100 blocks × 80 samples = 8000 ops.
        out = np.zeros(blocks.shape[0], dtype=np.float32)
        for i in range(blocks.shape[0]):
            s_prev2 = 0.0
            s_prev1 = 0.0
            for x in blocks[i]:
                s0 = x + coeff * s_prev1 - s_prev2
                s_prev2 = s_prev1
                s_prev1 = s0
            # Power = s_prev2^2 + s_prev1^2 - coeff*s_prev1*s_prev2.
            power = s_prev2 * s_prev2 + s_prev1 * s_prev1 - coeff * s_prev1 * s_prev2
            out[i] = float(max(power, 0.0))
        return out

    def _emit_intervals(self, n_new_blocks: int) -> str:
        """Threshold-slice the new envelope samples into intervals and feed
        the MorseDecoder. Returns any decoded text."""
        # The envelope is one value per block; block_ms = block_size / sample_rate * 1000.
        block_ms = self._block_size / self.sample_rate * 1000.0

        # Hysteresis threshold.
        on_thr = self._on_threshold
        off_thr = on_thr * _HYSTERESIS

        intervals: list[tuple[bool, float]] = []
        start_idx = self._interval_start_idx
        on_state = self._currently_on

        for i in range(n_new_blocks):
            idx = self._block_idx + i
            if idx >= len(self._envelope):
                break
            p = self._envelope[idx]
            transition = False
            if not on_state and p > on_thr:
                transition = True
                on_state = True
            elif on_state and p < off_thr:
                transition = True
                on_state = False
            if transition:
                # Close out the previous interval.
                if idx > start_idx:
                    duration = (idx - start_idx) * block_ms
                    intervals.append((self._currently_on, duration))
                # Start the new interval.
                start_idx = idx
                self._currently_on = on_state
        # Save running state.
        self._interval_start_idx = start_idx
        self._block_idx += n_new_blocks

        if not intervals:
            return ""
        return self._decoder.feed_intervals(intervals)
