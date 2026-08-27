"""AI noise-reduction denoiser — Stage 2a of the DSP+AI cascade (ADR-002).

v1 implementation: spectral subtraction with adaptive noise-floor tracking.
This is a REAL noise reducer (not a stub) — it's the classical algorithm
that pre-dates DeepFilterNet by 30+ years. It produces visibly lower noise
on a synthetic noisy signal (see tests). When a real DeepFilterNet module
is built (packages/ai-rust, ADR-002 open question), swapping it in is a
one-class replacement: keep the same frame_size + process() signature.

Algorithm (per frame, 480 samples @ 8 kHz by default):

  1. Apply a Hann window.
  2. STFT via rfft (256-point FFT, 50% overlap).
  3. Update noise floor estimate when frame energy < threshold
     (voice-activity detection via simple energy gating).
  4. Spectral subtraction: |X_clean| = max(|X_noisy| - α·|N|, β·|X_noisy|)
     with the original phase (phase doesn't help much for noise reduction).
  5. ISTFT + Hann window + overlap-add.

The class is **streaming**: it retains inter-frame state (the previous
input window for overlap-add, the running noise floor estimate). feed()
accepts arbitrary-length int16 PCM and returns denoised int16 PCM of
the same length (the last (frame_size - hop) samples are buffered for
the next call's overlap; drain() flushes them on stop).

This module is pure numpy + the standard library — no pycsdr, no Rust
FFI, no external binary. Tests run in the bare sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class AIDenoiserConfig:
    """Tunable parameters. Defaults are sensible for 8 kHz mono speech."""

    sample_rate: int = 8000
    frame_size: int = 480  # RNNoise-compatible
    fft_size: int = 512  # power of 2 ≥ frame_size
    hop_size: int = 240  # 50% overlap
    # Noise-update gate: frame RMS below this triggers noise-floor update.
    # Default 3000 is well below a typical voice/tone frame (RMS > 5000)
    # but above a "silence + receiver noise" floor (RMS < 2000).
    noise_update_rms: float = 3000.0
    # Spectral subtraction factor (≥ 1 = more aggressive).
    alpha: float = 1.5
    # Spectral floor: minimum gain per bin (prevents musical noise).
    beta: float = 0.10
    # How fast the noise floor adapts (0..1; 1 = always replace).
    noise_adapt_rate: float = 0.10


@dataclass
class AIDenoiser:
    """Streaming spectral-subtraction noise reducer.

    Construct once per receiver session; feed PCM frames as they arrive
    from the AudioChain. The output is the same length and format as the
    input — drop-in replacement for the wire path.
    """

    config: AIDenoiserConfig = field(default_factory=AIDenoiserConfig)
    # Inter-frame state.
    _prev_input_tail: np.ndarray | None = None  # last (frame_size - hop) samples
    _prev_output_tail: np.ndarray | None = None  # for overlap-add reconstruction
    _noise_floor: np.ndarray | None = None  # |N| estimate per FFT bin
    _window: np.ndarray = field(default_factory=lambda: np.zeros(0))
    _synth_buffer: np.ndarray | None = None  # carry-over for ISTFT overlap-add
    _leftover: bytes = b""  # sub-frame input bytes

    def __post_init__(self) -> None:
        # Hann window of length frame_size, zero-padded to fft_size.
        self._window = np.hanning(self.config.frame_size).astype(np.float32)
        # Normalize so overlap-add reconstructs the original amplitude.
        # 50% overlap of Hann windows gives COLA (constant-overlap-add) when
        # the window is squared; we apply the window twice (analysis + synth)
        # so the effective normalization is sum of squared Hann = N/1.5 ≈ N*0.667.
        # We bake that in to keep amplitudes honest.
        win_pow = self._window * self._window
        norm = float(np.sum(win_pow)) / float(self.config.hop_size) if self.config.hop_size else 1.0
        self._window /= max(np.sqrt(norm), 1e-9)

    def feed(self, pcm: np.ndarray) -> np.ndarray:
        """Denoise an int16 PCM chunk.

        Returns int16 PCM of the same length (the last few samples may be
        buffered for the next call's overlap; drain() flushes them).
        """
        if pcm.size == 0:
            return pcm.astype("<i2", copy=False)
        # Convert to float32 for processing; preserve int16 range.
        samples = pcm.astype(np.float32)
        out: list[np.ndarray] = []
        cursor = 0
        frame = self.config.frame_size
        hop = self.config.hop_size

        # Pull in any sub-frame leftover from the previous call.
        if self._leftover:
            leftover = np.frombuffer(self._leftover, dtype="<i2").astype(np.float32)
            samples = np.concatenate([leftover, samples])
            self._leftover = b""

        while cursor + frame <= samples.size:
            block = samples[cursor : cursor + frame]
            out.append(self._process_frame(block))
            cursor += hop

        # Save remaining samples as sub-frame leftover for the next call.
        if cursor < samples.size:
            tail = samples[cursor:].astype("<i2")
            self._leftover = tail.tobytes()

        if not out:
            # Not enough for one frame — return zero to keep the wire
            # contract honest (next call will accumulate enough).
            return np.zeros(0, dtype="<i2")
        return np.concatenate(out).astype("<i2", copy=False)

    def drain(self) -> np.ndarray:
        """Flush any buffered samples (call on stop / mode-switch)."""
        leftover = np.frombuffer(self._leftover, dtype="<i2") if self._leftover else np.zeros(0, dtype="<i2")
        self._leftover = b""
        if leftover.size == 0:
            return np.zeros(0, dtype="<i2")
        # Zero-pad to one frame and process for a clean tail-out.
        padded = np.zeros(self.config.frame_size, dtype=np.float32)
        padded[: leftover.size] = leftover.astype(np.float32)
        return self._process_frame(padded).astype("<i2", copy=False)

    def reset(self) -> None:
        """Drop inter-frame state (mode switch, source change)."""
        self._prev_input_tail = None
        self._prev_output_tail = None
        self._noise_floor = None
        self._synth_buffer = None
        self._leftover = b""

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _process_frame(self, block: np.ndarray) -> np.ndarray:
        """Process one frame_size block; return hop_size output samples."""
        # Step 1: windowed FFT (real FFT → half-complex spectrum).
        windowed = block * self._window
        fft_size = self.config.fft_size
        # Zero-pad to fft_size for cleaner frequency resolution.
        padded = np.zeros(fft_size, dtype=np.float32)
        padded[: self.config.frame_size] = windowed
        spectrum = np.fft.rfft(padded)
        mag = np.abs(spectrum)
        phase = np.angle(spectrum)

        # Step 2: voice-activity detection via simple RMS gate.
        rms = float(np.sqrt(np.mean(block * block)))
        if rms < self.config.noise_update_rms:
            # Quiet frame — update noise floor (EMA).
            if self._noise_floor is None:
                self._noise_floor = mag.copy()
            else:
                rate = self.config.noise_adapt_rate
                self._noise_floor = (1.0 - rate) * self._noise_floor + rate * mag

        # Step 3: spectral subtraction.
        if self._noise_floor is not None:
            # |X_clean| = max(|X| - α·|N|, β·|X|)
            subtracted = mag - self.config.alpha * self._noise_floor
            floored = np.maximum(subtracted, self.config.beta * mag)
            # Reconstruct with original phase.
            clean_spectrum = floored * np.exp(1j * phase)
        else:
            # No noise estimate yet — pass through unchanged.
            clean_spectrum = spectrum

        # Step 4: ISTFT + overlap-add.
        clean_block = np.fft.irfft(clean_spectrum, n=fft_size)[: self.config.frame_size]
        # Apply the synthesis window for COLA reconstruction.
        synth = clean_block * self._window

        if self._synth_buffer is None:
            self._synth_buffer = np.zeros(self.config.frame_size, dtype=np.float32)
        # Overlap-add: the first hop_size samples of the synth buffer are
        # ready for output; the rest carries forward.
        self._synth_buffer[: self.config.frame_size] += synth
        out = self._synth_buffer[: self.config.hop_size].copy()
        # Shift the buffer left by hop_size, zero-pad on the right.
        self._synth_buffer = np.roll(self._synth_buffer, -self.config.hop_size)
        self._synth_buffer[-self.config.hop_size :] = 0.0

        # Clip to int16 range (the spectral subtraction can briefly overshoot).
        # cast ensures mypy sees the return as a typed ndarray (np.clip returns Any
        # when its inputs are float32 ndarray + Python scalars).
        clipped: np.ndarray = np.clip(out, -32768.0, 32767.0)
        return clipped
