"""Tests for dsp.preprocess — notch filter + noise blanker (slice-7).

Covers:
  - NotchFilter: tone at notch freq attenuated, tone elsewhere unchanged
  - NoiseBlanker: impulse noise suppressed, normal signal untouched
  - IQPreprocessor: conditional activation (no-op when disabled)
  - ReceiverSession integration: set_dsp_params with notch fields
    reconfigures the preprocessor (state reset)
"""

from __future__ import annotations

import asyncio
import math

import numpy as np
import pytest

from openwebrx_plus.dsp.preprocess import IQPreprocessor, NoiseBlanker, NotchFilter
from openwebrx_plus.dsp.types import DSPParams

# ----------------------------------------------------------------------------
# Helpers — build clean test signals
# ----------------------------------------------------------------------------


def _tone(freq_hz: float, sample_rate: float, n: int, amplitude: float = 1.0) -> np.ndarray:
    """A complex baseband tone at +freq_hz offset from center.

    Returns complex64 samples of shape (n,)."""
    t = np.arange(n, dtype=np.float64) / sample_rate
    return (amplitude * np.exp(1j * 2 * math.pi * freq_hz * t)).astype(np.complex64)


def _white_noise(sample_rate: float, n: int, amplitude: float = 0.01) -> np.ndarray:
    """Low-amplitude Gaussian noise (simulated band noise floor)."""
    rng = np.random.default_rng(seed=42)
    return (amplitude * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(
        np.complex64
    )


def _impulse_train(
    sample_rate: float,
    n: int,
    impulse_period_hz: float = 100.0,
    impulse_width_samples: int = 2,
    impulse_amplitude: float = 10.0,
    base_amplitude: float = 0.01,
) -> np.ndarray:
    """A weak baseband noise plus periodic high-magnitude impulses.

    Models the classic "ignition noise" interference: short bursts of
    high-magnitude samples riding on a low-level noise floor. The
    noise blanker should clip the impulses down to the floor."""
    rng = np.random.default_rng(seed=7)
    base = base_amplitude * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    period_samples = int(sample_rate / impulse_period_hz)
    for k in range(0, n, period_samples):
        if k + impulse_width_samples <= n:
            base[k : k + impulse_width_samples] += impulse_amplitude * (
                1.0 + 0.0j
            )  # DC spike for clarity
    return base.astype(np.complex64)


def _signal_power(arr: np.ndarray) -> float:
    """Mean power (|x|² ) of a complex array."""
    if arr.size == 0:
        return 0.0
    return float(np.mean(np.abs(arr) ** 2))


def _bin_power_at(arr: np.ndarray, freq_hz: float, sample_rate: float) -> float:
    """Power at a specific frequency bin via direct DFT projection.

    Uses Goertzel-like single-bin DFT — accurate for a pure tone."""
    n = arr.size
    if n == 0:
        return 0.0
    t = np.arange(n, dtype=np.float64) / sample_rate
    kernel = np.exp(-1j * 2 * math.pi * freq_hz * t)
    return float(np.abs(np.sum(arr * kernel)) ** 2) / (n * n)


# ----------------------------------------------------------------------------
# NotchFilter tests
# ----------------------------------------------------------------------------


class TestNotchFilter:
    def test_constructor_validates_sample_rate(self) -> None:
        with pytest.raises(ValueError, match="sample_rate"):
            NotchFilter(freq_hz=1000.0, sample_rate=0)

    def test_constructor_validates_q(self) -> None:
        with pytest.raises(ValueError, match="Q"):
            NotchFilter(freq_hz=1000.0, sample_rate=48000, q=0)

    def test_constructor_validates_freq_in_nyquist(self) -> None:
        with pytest.raises(ValueError, match="Nyquist"):
            NotchFilter(freq_hz=30000, sample_rate=48000)  # >fs/2

    def test_constructor_accepts_negative_freq(self) -> None:
        """Negative notch frequencies (lower sideband) are valid."""
        notch = NotchFilter(freq_hz=-1000.0, sample_rate=48000, q=30.0)
        assert notch.freq_hz == -1000.0
        assert notch.sample_rate == 48000

    def test_notch_attenuates_target_tone(self) -> None:
        """A pure tone at the notch frequency should be heavily attenuated."""
        fs = 48_000.0
        notch_freq = 1000.0
        notch = NotchFilter(freq_hz=notch_freq, sample_rate=fs, q=30.0)
        # 1 second of tone at notch freq
        n = int(fs)
        signal = _tone(notch_freq, fs, n, amplitude=1.0)
        out = notch.process(signal)
        # Power at the notch frequency should drop by ≥20 dB (10× linear)
        power_in = _bin_power_at(signal, notch_freq, fs)
        power_out = _bin_power_at(out, notch_freq, fs)
        assert power_out < power_in * 0.01, (
            f"notch didn't attenuate: power_in={power_in:.3e}, "
            f"power_out={power_out:.3e} (ratio={power_out / power_in:.3f})"
        )

    def test_notch_preserves_other_tones(self) -> None:
        """A tone far from the notch frequency should pass through nearly
        unchanged."""
        fs = 48_000.0
        notch_freq = 1000.0
        other_freq = 5000.0  # well outside the notch BW (~33 Hz at Q=30)
        notch = NotchFilter(freq_hz=notch_freq, sample_rate=fs, q=30.0)
        n = int(fs)
        signal = _tone(other_freq, fs, n, amplitude=1.0)
        out = notch.process(signal)
        power_in = _bin_power_at(signal, other_freq, fs)
        power_out = _bin_power_at(out, other_freq, fs)
        # Should be within ~1 dB (0.79 ratio). We allow generous slack
        # for the filter's start-up transient.
        assert power_out > power_in * 0.5, (
            f"notch shouldn't affect off-frequency tone: "
            f"power_in={power_in:.3e}, power_out={power_out:.3e} "
            f"(ratio={power_out / power_in:.3f})"
        )

    def test_notch_preserves_complex_phase(self) -> None:
        """The output should remain complex — the notch must not collapse
        to real-only (which would lose the imaginary channel)."""
        fs = 48_000.0
        notch = NotchFilter(freq_hz=1000.0, sample_rate=fs, q=30.0)
        signal = _tone(2000.0, fs, 1024)
        out = notch.process(signal)
        assert np.iscomplexobj(out), "notch output should be complex"
        # And the imaginary part should not be uniformly zero
        assert np.any(np.abs(out.imag) > 0), "imaginary channel should have content"

    def test_notch_preserves_dtype(self) -> None:
        """Complex64 in → complex64 out (pycsdr expects cf32)."""
        notch = NotchFilter(freq_hz=1000.0, sample_rate=48_000, q=30.0)
        signal = _tone(1000.0, 48_000, 1024)
        assert signal.dtype == np.complex64
        out = notch.process(signal)
        assert out.dtype == np.complex64

    def test_notch_empty_input_returns_empty(self) -> None:
        """Edge case: empty array in → empty array out (no crash)."""
        notch = NotchFilter(freq_hz=1000.0, sample_rate=48_000, q=30.0)
        empty = np.array([], dtype=np.complex64)
        out = notch.process(empty)
        assert out.size == 0

    def test_notch_state_persists_across_calls(self) -> None:
        """The IIR state should carry over between chunked calls — applying
        the filter in two halves should give the same result as one call."""
        fs = 48_000.0
        signal = _tone(1000.0, fs, 1024)
        # Apply in one call
        notch1 = NotchFilter(freq_hz=1000.0, sample_rate=fs, q=30.0)
        full = notch1.process(signal)
        # Apply in two halves
        notch2 = NotchFilter(freq_hz=1000.0, sample_rate=fs, q=30.0)
        half1 = notch2.process(signal[:512])
        half2 = notch2.process(signal[512:])
        chunked = np.concatenate([half1, half2])
        # Outputs should match (allow tiny float drift)
        assert np.allclose(full, chunked, atol=1e-5), (
            "notch state should carry across chunked calls"
        )

    def test_notch_rejects_only_positive_freq(self) -> None:
        """A complex (single-pole) notch should reject +f0 but NOT -f0."""
        fs = 48_000.0
        notch_freq = 1000.0
        notch = NotchFilter(freq_hz=notch_freq, sample_rate=fs, q=30.0)
        n = int(fs)
        # Tone at NEGATIVE notch freq (lower sideband)
        signal = _tone(-notch_freq, fs, n, amplitude=1.0)
        out = notch.process(signal)
        # Power at the negative frequency should be PRESERVED
        power_in = _bin_power_at(signal, -notch_freq, fs)
        power_out = _bin_power_at(out, -notch_freq, fs)
        # Should pass through with at most a tiny edge-effect loss
        assert power_out > power_in * 0.7, (
            f"complex notch should not reject -f0: power_in={power_in:.3e}, "
            f"power_out={power_out:.3e} (ratio={power_out / power_in:.3f})"
        )


# ----------------------------------------------------------------------------
# NoiseBlanker tests
# ----------------------------------------------------------------------------


class TestNoiseBlanker:
    def test_constructor_validates_sample_rate(self) -> None:
        with pytest.raises(ValueError, match="sample_rate"):
            NoiseBlanker(sample_rate=0, threshold_db=10.0)

    def test_constructor_validates_threshold(self) -> None:
        with pytest.raises(ValueError, match="threshold_db"):
            NoiseBlanker(sample_rate=48000, threshold_db=0)

    def test_constructor_validates_time_const(self) -> None:
        with pytest.raises(ValueError, match="time_const"):
            NoiseBlanker(sample_rate=48000, threshold_db=10.0, time_const_s=0)

    def test_nb_preserves_clean_signal(self) -> None:
        """A clean tone with no impulses should pass through unchanged."""
        fs = 48_000.0
        signal = _tone(1000.0, fs, int(fs), amplitude=0.1)  # well below FS
        nb = NoiseBlanker(sample_rate=fs, threshold_db=10.0)
        out = nb.process(signal)
        # Power should be preserved (within a few percent for start-up
        # floor tracking drift)
        power_in = _signal_power(signal)
        power_out = _signal_power(out)
        assert power_out > power_in * 0.9, (
            f"clean signal should pass through: power_in={power_in:.3e}, "
            f"power_out={power_out:.3e}"
        )

    def test_nb_suppresses_impulses(self) -> None:
        """An impulse train riding on a low noise floor should be
        suppressed — the impulses should be clipped down to near the
        floor level."""
        fs = 48_000.0
        n = int(fs)
        signal = _impulse_train(fs, n)
        # Verify the input has impulses (sanity)
        in_peak = float(np.max(np.abs(signal)))
        assert in_peak > 5.0, "test signal should have high-magnitude impulses"
        nb = NoiseBlanker(sample_rate=fs, threshold_db=10.0)
        out = nb.process(signal)
        out_peak = float(np.max(np.abs(out)))
        # The peak should drop by at least 10× (20 dB) after blanking
        assert out_peak < in_peak * 0.1, (
            f"NB should suppress impulse peaks: in_peak={in_peak:.3f}, "
            f"out_peak={out_peak:.3f} (ratio={out_peak / in_peak:.3f})"
        )

    def test_nb_preserves_dtype(self) -> None:
        """Complex64 in → complex64 out."""
        nb = NoiseBlanker(sample_rate=48_000, threshold_db=10.0)
        signal = _tone(1000.0, 48_000, 1024, amplitude=0.1)
        out = nb.process(signal)
        assert out.dtype == np.complex64

    def test_nb_empty_input_returns_empty(self) -> None:
        """Edge case: empty array in → empty array out."""
        nb = NoiseBlanker(sample_rate=48_000, threshold_db=10.0)
        empty = np.array([], dtype=np.complex64)
        out = nb.process(empty)
        assert out.size == 0

    def test_nb_phase_preserved(self) -> None:
        """The NB multiplies by a real scalar — phase should be unchanged
        on non-impulse samples."""
        fs = 48_000.0
        signal = _tone(1000.0, fs, 1024, amplitude=0.1)
        nb = NoiseBlanker(sample_rate=fs, threshold_db=10.0)
        out = nb.process(signal)
        # For non-clipped samples, out[i] == signal[i] exactly (no phase change)
        # Find samples where no clip happened
        mag_in = np.abs(signal)
        # Compare phase where magnitude is non-trivial
        nonzero = mag_in > 0.01
        if np.any(nonzero):
            phase_in = np.angle(signal[nonzero])
            phase_out = np.angle(out[nonzero])
            # Phases should match (modulo 2π) for unclipped samples
            # On a clean tone, no clipping should occur, so they should match
            assert np.allclose(phase_in, phase_out, atol=1e-3), (
                "NB should preserve phase on unclipped samples"
            )


# ----------------------------------------------------------------------------
# IQPreprocessor tests
# ----------------------------------------------------------------------------


class TestIQPreprocessor:
    def test_no_params_means_noop(self) -> None:
        """A preprocessor with no dsp_params should be inactive and return
        the input unchanged."""
        pre = IQPreprocessor(sample_rate=48_000.0)
        assert not pre.active
        signal = _tone(1000.0, 48_000, 1024)
        out = pre.process(signal)
        # Same array returned (no copy)
        assert out is signal

    def test_with_notch_only(self) -> None:
        """A preprocessor with notch enabled should activate and attenuate
        the target frequency."""
        params = DSPParams(
            notch_enabled=True,
            notch_freq_hz=1000.0,
            notch_q=30.0,
        )
        pre = IQPreprocessor(sample_rate=48_000.0, params=params)
        assert pre.active
        assert pre.notch is not None
        assert pre.noise_blanker is None
        signal = _tone(1000.0, 48_000, 48_000)
        out = pre.process(signal)
        # Power at notch freq should be heavily reduced
        power_in = _bin_power_at(signal, 1000.0, 48_000)
        power_out = _bin_power_at(out, 1000.0, 48_000)
        assert power_out < power_in * 0.01

    def test_with_nb_only(self) -> None:
        """A preprocessor with NB enabled but no notch should activate NB only."""
        params = DSPParams(
            noise_blanker_enabled=True,
            noise_blanker_threshold=10.0,
        )
        pre = IQPreprocessor(sample_rate=48_000.0, params=params)
        assert pre.active
        assert pre.notch is None
        assert pre.noise_blanker is not None

    def test_with_both_notch_and_nb(self) -> None:
        """Both stages active — notch first, then NB."""
        params = DSPParams(
            notch_enabled=True,
            notch_freq_hz=1000.0,
            notch_q=30.0,
            noise_blanker_enabled=True,
            noise_blanker_threshold=10.0,
        )
        pre = IQPreprocessor(sample_rate=48_000.0, params=params)
        assert pre.active
        assert pre.notch is not None
        assert pre.noise_blanker is not None

    def test_reconfigure_resets_state(self) -> None:
        """Reconfiguring with new params resets the IIR state (the new
        notch freq is applied fresh)."""
        params1 = DSPParams(
            notch_enabled=True,
            notch_freq_hz=1000.0,
            notch_q=30.0,
        )
        pre = IQPreprocessor(sample_rate=48_000.0, params=params1)
        assert pre.notch is not None
        assert pre.notch.freq_hz == 1000.0
        # Apply some signal to populate state
        pre.process(_tone(1000.0, 48_000, 1024))
        # Reconfigure with new freq
        params2 = DSPParams(
            notch_enabled=True,
            notch_freq_hz=2000.0,
            notch_q=30.0,
        )
        pre.reconfigure(params2)
        assert pre.notch is not None
        assert pre.notch.freq_hz == 2000.0
        # Fresh state — the previous IIR x1/y1 should be reset
        # (we can verify by checking that a clean tone gets fully
        # attenuated without long-tail ringing from prior state)
        out = pre.process(_tone(2000.0, 48_000, 48_000))
        power_in = _bin_power_at(_tone(2000.0, 48_000, 48_000), 2000.0, 48_000)
        power_out = _bin_power_at(out, 2000.0, 48_000)
        assert power_out < power_in * 0.01

    def test_reconfigure_disabling(self) -> None:
        """Reconfiguring with notch_enabled=False should disable the notch."""
        params1 = DSPParams(
            notch_enabled=True,
            notch_freq_hz=1000.0,
            notch_q=30.0,
        )
        pre = IQPreprocessor(sample_rate=48_000.0, params=params1)
        assert pre.active
        pre.reconfigure(DSPParams(notch_enabled=False))
        assert not pre.active
        assert pre.notch is None

    def test_empty_chunk_passthrough(self) -> None:
        """Empty input should pass through cleanly even with stages active."""
        params = DSPParams(
            notch_enabled=True,
            notch_freq_hz=1000.0,
            notch_q=30.0,
        )
        pre = IQPreprocessor(sample_rate=48_000.0, params=params)
        empty = np.array([], dtype=np.complex64)
        out = pre.process(empty)
        assert out.size == 0

    def test_output_is_complex64(self) -> None:
        """Output dtype must be complex64 (pycsdr expects cf32)."""
        params = DSPParams(
            notch_enabled=True,
            notch_freq_hz=1000.0,
            notch_q=30.0,
        )
        pre = IQPreprocessor(sample_rate=48_000.0, params=params)
        signal = _tone(1000.0, 48_000, 1024)
        out = pre.process(signal)
        assert out.dtype == np.complex64

    def test_input_array_not_mutated(self) -> None:
        """The preprocessor must not modify the caller's input array (the
        hub may share buffers across VFO taps)."""
        params = DSPParams(
            notch_enabled=True,
            notch_freq_hz=1000.0,
            notch_q=30.0,
        )
        pre = IQPreprocessor(sample_rate=48_000.0, params=params)
        signal = _tone(1000.0, 48_000, 1024).copy()
        signal_copy = signal.copy()
        _ = pre.process(signal)
        assert np.array_equal(signal, signal_copy), "input array should not be mutated"


# ----------------------------------------------------------------------------
# ReceiverSession integration tests
# ----------------------------------------------------------------------------


def test_session_builds_preprocessor_on_start() -> None:
    """A started session should have an IQPreprocessor instance."""
    from openwebrx_plus.config import Settings
    from openwebrx_plus.sessions import (
        create_session,
        destroy_session,
        init_default_sessions,
    )

    Settings(tier="dev")  # ensure config defaults
    init_default_sessions(Settings(tier="dev"))
    session = create_session(
        receiver_id="rx-preproc-start",
        source_type="simulated",
        source_kwargs={"signal_set": "default"},
        center_freq=1_000_000,
        sample_rate=250_000,
        mode="AM",
    )
    try:
        asyncio.run(session.start())  # type: ignore[attr-defined]
        assert session._iq_preprocessor is not None  # type: ignore[attr-defined]
        # Default DSPParams → not active
        assert not session._iq_preprocessor.active  # type: ignore[attr-defined]
    finally:
        asyncio.run(session.stop())  # type: ignore[attr-defined]
        destroy_session("rx-preproc-start")


def test_session_set_dsp_params_reconfigures_preprocessor() -> None:
    """Setting notch params via set_dsp_params should reconfigure the
    preprocessor — it becomes active and the notch is set to the new freq."""
    from openwebrx_plus.config import Settings
    from openwebrx_plus.sessions import (
        create_session,
        destroy_session,
        init_default_sessions,
    )

    init_default_sessions(Settings(tier="dev"))
    session = create_session(
        receiver_id="rx-preproc-reconf",
        source_type="simulated",
        source_kwargs={"signal_set": "default"},
        center_freq=1_000_000,
        sample_rate=250_000,
        mode="AM",
    )
    try:
        asyncio.run(session.start())  # type: ignore[attr-defined]
        # Initially inactive
        assert not session._iq_preprocessor.active  # type: ignore[attr-defined]
        # Apply notch params
        asyncio.run(  # type: ignore[attr-defined]
            session.set_dsp_params(  # type: ignore[attr-defined]
                DSPParams(
                    notch_enabled=True,
                    notch_freq_hz=1234.0,
                    notch_q=30.0,
                )
            )
        )
        # Now active, with the notch at 1234 Hz
        assert session._iq_preprocessor.active  # type: ignore[attr-defined]
        assert session._iq_preprocessor.notch is not None  # type: ignore[attr-defined]
        assert session._iq_preprocessor.notch.freq_hz == 1234.0  # type: ignore[attr-defined]
    finally:
        asyncio.run(session.stop())  # type: ignore[attr-defined]
        destroy_session("rx-preproc-reconf")


def test_session_set_dsp_params_disabling_notch() -> None:
    """Setting notch_enabled back to False should deactivate the preprocessor."""
    from openwebrx_plus.config import Settings
    from openwebrx_plus.sessions import (
        create_session,
        destroy_session,
        init_default_sessions,
    )

    init_default_sessions(Settings(tier="dev"))
    session = create_session(
        receiver_id="rx-preproc-disable",
        source_type="simulated",
        source_kwargs={"signal_set": "default"},
        center_freq=1_000_000,
        sample_rate=250_000,
        mode="AM",
    )
    try:
        asyncio.run(session.start())  # type: ignore[attr-defined]
        # Enable notch
        asyncio.run(  # type: ignore[attr-defined]
            session.set_dsp_params(  # type: ignore[attr-defined]
                DSPParams(notch_enabled=True, notch_freq_hz=1000.0, notch_q=30.0)
            )
        )
        assert session._iq_preprocessor.active  # type: ignore[attr-defined]
        # Disable notch
        asyncio.run(  # type: ignore[attr-defined]
            session.set_dsp_params(DSPParams(notch_enabled=False))  # type: ignore[attr-defined]
        )
        assert not session._iq_preprocessor.active  # type: ignore[attr-defined]
    finally:
        asyncio.run(session.stop())  # type: ignore[attr-defined]
        destroy_session("rx-preproc-disable")


def test_session_stop_clears_preprocessor() -> None:
    """Stopping the session should drop the preprocessor reference."""
    from openwebrx_plus.config import Settings
    from openwebrx_plus.sessions import (
        create_session,
        destroy_session,
        init_default_sessions,
    )

    init_default_sessions(Settings(tier="dev"))
    session = create_session(
        receiver_id="rx-preproc-stop",
        source_type="simulated",
        source_kwargs={"signal_set": "default"},
        center_freq=1_000_000,
        sample_rate=250_000,
        mode="AM",
    )
    try:
        asyncio.run(session.start())  # type: ignore[attr-defined]
        assert session._iq_preprocessor is not None  # type: ignore[attr-defined]
        asyncio.run(session.stop())  # type: ignore[attr-defined]
        # After stop, the preprocessor is cleared
        assert session._iq_preprocessor is None  # type: ignore[attr-defined]
    finally:
        destroy_session("rx-preproc-stop")
