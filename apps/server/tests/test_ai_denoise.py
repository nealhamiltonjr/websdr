"""AI denoiser tests — spectral-subtraction noise reducer (Stage 2a).

Pure-numpy; no pycsdr, no Rust FFI. Verifies:
  - Frame processing shape + overlap-add continuity
  - Noise-floor adaptation on quiet frames
  - Real noise reduction: noise power drops ≥ 6 dB on a synthetic noisy tone
  - Streaming: chunks split across calls produce same output as one big call
  - drain() flushes the last buffered samples cleanly
"""

from __future__ import annotations

import numpy as np

from openwebrx_plus.dsp.ai_denoise import AIDenoiser, AIDenoiserConfig


def _make_tone(freq_hz: float, duration_s: float, sr: int = 8000, amp: float = 8000.0) -> np.ndarray:
    """Pure int16 sinusoid — simulates a clean speech-like signal."""
    t = np.arange(int(duration_s * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq_hz * t)).astype("<i2")


def _add_noise(signal: np.ndarray, noise_rms: float = 800.0, seed: int = 42) -> np.ndarray:
    """Add white Gaussian noise at the given RMS (int16 units)."""
    rng = np.random.default_rng(seed)
    noise = (rng.standard_normal(signal.size) * noise_rms).astype(np.float32)
    return np.clip(signal.astype(np.float32) + noise, -32768, 32767).astype("<i2")


def _signal_power(samples: np.ndarray) -> float:
    """Mean-square power in int16 units."""
    if samples.size == 0:
        return 0.0
    return float(np.mean(samples.astype(np.float64) ** 2))


def _snr_db(signal_power: float, noise_power: float) -> float:
    """SNR in dB; safely returns 0 when either side is 0."""
    if signal_power <= 0 or noise_power <= 0:
        return 0.0
    return 10.0 * float(np.log10(signal_power / noise_power))


def test_denoiser_processes_a_clean_frame() -> None:
    """One frame in → one frame out (≤ hop_size; rest buffered for next call)."""
    d = AIDenoiser(AIDenoiserConfig(frame_size=480, hop_size=240))
    frame = _make_tone(440, 0.5, sr=8000)  # 4000 samples
    out = d.feed(frame)
    # First feed produces (n / hop - 1) * hop = (4000/240 - 1) * 240 ≈ 3840 samples.
    # Buffer holds the final partial frame.
    assert out.size > 0
    assert out.size <= frame.size
    assert out.dtype == np.dtype("<i2")


def test_denoiser_reduces_noise_on_synthetic_signal() -> None:
    """Feed silence+noise then tone+noise; verify noise power drops in the
    silence section (the denoiser's noise-floor tracking should kick in
    there and remove the noise).
    """
    sr = 8000
    duration = 2.0
    tone = _make_tone(440, duration, sr=sr, amp=8000.0)
    # 2 s of silence+noise (calibrates noise floor), 2 s of tone+noise.
    noise_only = _add_noise(np.zeros(int(sr * duration), dtype="<i2"), noise_rms=800.0, seed=42)
    noisy = np.concatenate([
        noise_only,
        _add_noise(tone, noise_rms=800.0, seed=99),
    ])

    d = AIDenoiser(AIDenoiserConfig(frame_size=480, hop_size=240))
    out = d.feed(noisy)
    out = np.concatenate([out, d.drain()])

    # Align output to input length (may be shorter due to buffering).
    n = min(out.size, noisy.size)
    out_aligned = out[:n].astype(np.float32)
    noisy_aligned = noisy[:n].astype(np.float32)

    # Noise power in the silence-only section (first half).
    silence_end = int(sr * duration)
    silence_end = min(silence_end, n)
    in_noise_power = float(np.mean(noisy_aligned[:silence_end] ** 2))
    out_noise_power = float(np.mean(out_aligned[:silence_end] ** 2))

    # The denoiser should reduce noise power by ≥ 6 dB in the silence
    # section (where there's no signal — pure noise reduction).
    noise_reduction_db = 10.0 * float(np.log10(in_noise_power / max(out_noise_power, 1e-9)))
    assert noise_reduction_db >= 6.0, (
        f"noise reduction = {noise_reduction_db:.1f} dB "
        f"(in={in_noise_power:.0f}, out={out_noise_power:.0f}) — expected ≥ 6 dB"
    )


def test_denoiser_streaming_equivalence() -> None:
    """Feeding in chunks must produce the same output as one big feed."""
    sr = 8000
    tone = _make_tone(440, 1.0, sr=sr, amp=8000.0)
    noisy = _add_noise(tone, noise_rms=400.0)

    # All at once.
    d1 = AIDenoiser(AIDenoiserConfig(frame_size=480, hop_size=240))
    out1 = np.concatenate([d1.feed(noisy), d1.drain()])

    # In 250-sample chunks.
    d2 = AIDenoiser(AIDenoiserConfig(frame_size=480, hop_size=240))
    out2_chunks: list[np.ndarray] = []
    for i in range(0, noisy.size, 250):
        out2_chunks.append(d2.feed(noisy[i : i + 250]))
    out2 = np.concatenate(out2_chunks + [d2.drain()])

    # The streaming version may be 1 hop shorter than the bulk version
    # due to rounding at the tail; compare the overlapping prefix.
    n = min(out1.size, out2.size)
    assert n > 0
    # The signals should be very close (small numerical drift from float32 round-off).
    diff = np.abs(out1[:n].astype(np.float32) - out2[:n].astype(np.float32))
    assert float(np.max(diff)) < 100.0, f"max abs diff {float(np.max(diff))} — streaming divergence"


def test_denoiser_drain_flushes_remaining_samples() -> None:
    """After drain(), the denoiser holds no buffered state."""
    d = AIDenoiser(AIDenoiserConfig(frame_size=480, hop_size=240))
    tone = _make_tone(440, 0.1, sr=8000, amp=4000.0)
    _ = d.feed(tone)
    out = d.drain()
    assert out.size > 0
    # Drain again — should be empty.
    assert d.drain().size == 0


def test_denoiser_reset_clears_state() -> None:
    """reset() drops inter-frame state so the next feed is fresh."""
    d = AIDenoiser(AIDenoiserConfig(frame_size=480, hop_size=240))
    _ = d.feed(_make_tone(440, 0.5, sr=8000, amp=4000.0))
    d.reset()
    assert d._leftover == b""
    assert d._noise_floor is None
    assert d._synth_buffer is None


def test_denoiser_empty_input_returns_empty() -> None:
    d = AIDenoiser(AIDenoiserConfig(frame_size=480, hop_size=240))
    out = d.feed(np.zeros(0, dtype="<i2"))
    assert out.size == 0


def test_denoiser_handles_sub_frame_input() -> None:
    """Feeding < frame_size samples must buffer; subsequent feed combines."""
    d = AIDenoiser(AIDenoiserConfig(frame_size=480, hop_size=240))
    out1 = d.feed(np.zeros(100, dtype="<i2"))
    assert out1.size == 0  # buffered, no output yet
    # Feed more to push past one frame.
    out2 = d.feed(np.zeros(500, dtype="<i2"))
    assert out2.size > 0


def test_denoiser_preserves_int16_format() -> None:
    """Output must always be int16 (the wire contract)."""
    d = AIDenoiser(AIDenoiserConfig(frame_size=480, hop_size=240))
    out = d.feed(_make_tone(440, 0.5, sr=8000, amp=8000.0))
    assert out.dtype == np.dtype("<i2")
    assert out.max() <= 32767
    assert out.min() >= -32768


def test_denoiser_does_not_corrupt_phase() -> None:
    """Denoising a clean tone shouldn't introduce a different frequency."""
    sr = 8000
    # Calibrate noise floor on silence first, then a pure tone.
    silence = np.zeros(sr, dtype="<i2")
    tone = _make_tone(440, 1.0, sr=sr, amp=8000.0)
    d = AIDenoiser(AIDenoiserConfig(frame_size=480, hop_size=240, noise_update_rms=100.0))
    _ = d.feed(silence)
    out = d.feed(tone)
    # FFT of the output should show a peak near 440 Hz ± ~25 Hz.
    fft = np.abs(np.fft.rfft(out.astype(np.float32)))
    freqs = np.fft.rfftfreq(out.size, d=1.0 / sr)
    peak_idx = int(np.argmax(fft))
    peak_freq = freqs[peak_idx]
    assert abs(peak_freq - 440) < 50.0, f"peak at {peak_freq} Hz, expected near 440 Hz"
