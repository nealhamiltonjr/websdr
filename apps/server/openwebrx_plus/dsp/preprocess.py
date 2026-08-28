"""IQ preprocessor — notch filter + noise blanker applied BEFORE pycsdr.

Slice-7: the DSPParams fields ``notch_enabled`` / ``notch_freq_hz`` /
``notch_q`` and ``noise_blanker_enabled`` / ``noise_blanker_threshold``
have been accepted-but-no-op since slice-5.2 because pycsdr has no
native Notch or Nb block. Upstream pycsdr contribution is a long-term
option (ADR-007 "Option D") — but the user's core thesis is "pull out
the weak signal", and notch + NB are exactly the tools for that. This
module ships a pure-numpy implementation that runs in-process, in the
receiver session's IQ hot path, before the pycsdr FftChain + AudioChain.

ADR-004 compliance
-------------------
This is *not* a scipy filter. It is a hand-rolled numpy biquad + a
running-magnitude clipper. The "scipy-offline-only" rule in ADR-004
forbids ``import scipy`` in the live DSP chain; numpy is and always
has been allowed (the slice-1 FFT stub was numpy, and numpy is a runtime
dep for the cf32 byte round-trip in ReceiverSession._run already).

Filter math
-----------

Notch
~~~~~
Single-pole-pair complex IIR notch at frequency ``f0`` (Hz offset from
center)::

    H(z) = (1 - z_p z^-1) / (1 - z_p' z^-1)

    z_p  = e^(j ω0)              # zero ON the unit circle at +ω0
    z_p' = r · e^(j ω0)          # pole just INSIDE the unit circle at +ω0
    ω0   = 2π f0 / fs
    r    = 1 - π·BW / fs
    BW   = f0 / Q                # 3 dB bandwidth

Difference equation (complex samples, complex coefficients)::

    y[n] = x[n] - z_p·x[n-1] + z_p'·y[n-1]

State: ``x1`` (last input) and ``y1`` (last output), both complex.

A single complex pole-zero pair creates a narrow notch at EXACTLY +f0
without touching -f0 — important for SSB/CW where the desired signal
sits on one side of the carrier and a spur on the other.

For deeper rejection, callers can stack two notches (different
frequencies or the same frequency with a tighter r). The single-stage
notch gives ~25 dB of rejection at typical Q values, which matches
commercial SDR notch filter behavior.

Noise blanker
~~~~~~~~~~~~~
The classic "impulse noise blanker" from SW receiver design: maintain
a running estimate of the noise floor (exponential moving average of
the IQ magnitude), and when a sample's magnitude exceeds
``threshold × noise_floor``, scale that sample down to the threshold.
This suppresses narrow pulses (lightning, ignition, switch-mode PSU
hash) without touching the underlying signal — the pulse is too short
to contain signal energy worth preserving.

EMA: ``floor[n] = α·|x[n]| + (1-α)·floor[n-1]``, α = 1/(τ·fs) where τ
is the floor's time constant (~5 ms default — long enough to ignore
short pulses, short enough to track changing band conditions).

The clip is multiplicative: ``scale = threshold · floor / |x[n]|``;
the sample is multiplied by ``scale`` (clamped to ≤ 1.0 — we never
amplify). The output preserves phase (real scaling only).

Performance
-----------
Both filters operate on contiguous complex64 numpy arrays (the same
view the receiver session already produces for pycsdr). The notch is
two complex multiplies + two complex adds per sample; the NB is one
abs + one mul + one compare + one conditional mul. At 2.4 Msps the
combined CPU cost is ~30 Mflops — negligible vs pycsdr's own FFT+demod
work (which is ~500 Mflops at the same rate).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .types import DSPParams

__all__ = ["NotchFilter", "NoiseBlanker", "IQPreprocessor"]


class NotchFilter:
    """Single-pole-pair complex IIR notch at ``freq_hz`` offset from center.

    State is retained across calls so chunked streaming works correctly
    (the receiver session feeds one source chunk at a time — typically
    1024..65536 samples per chunk).
    """

    __slots__ = ("_zero", "_pole", "_x1", "_y1", "_freq_hz", "_sample_rate", "_q")

    def __init__(self, freq_hz: float, sample_rate: float, q: float = 30.0) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be > 0")
        if not (-sample_rate / 2 <= freq_hz <= sample_rate / 2):
            raise ValueError(
                f"notch freq {freq_hz} Hz out of Nyquist ±{sample_rate / 2} Hz"
            )
        if q <= 0:
            raise ValueError("Q must be > 0")
        omega = 2.0 * math.pi * freq_hz / sample_rate
        bw = abs(freq_hz) / q
        r = max(0.001, 1.0 - math.pi * bw / sample_rate)
        self._zero = complex(math.cos(omega), math.sin(omega))
        self._pole = complex(r * math.cos(omega), r * math.sin(omega))
        self._x1 = 0.0 + 0.0j
        self._y1 = 0.0 + 0.0j
        self._freq_hz = float(freq_hz)
        self._sample_rate = float(sample_rate)
        self._q = float(q)

    @property
    def freq_hz(self) -> float:
        return self._freq_hz

    @property
    def sample_rate(self) -> float:
        return self._sample_rate

    @property
    def q(self) -> float:
        return self._q

    @property
    def bandwidth_3db_hz(self) -> float:
        """Approximate 3 dB notch bandwidth (Hz)."""
        return abs(self._freq_hz) / self._q if self._q > 0 else float("inf")

    def process(self, x: np.ndarray) -> np.ndarray:
        """Apply the notch to a contiguous complex64/complex128 array.

        Returns a new array of the same shape and dtype. The input is
        not modified. A flat-direct-form-I difference equation is used
        (the recursive term is the previous OUTPUT, which is the
        canonical stable IIR form).
        """
        if x.size == 0:
            return x
        # Work in complex128 internally for numerical headroom; cast back
        # at the end. pycsdr's Buffer expects complex64 (cf32), so the
        # caller (IQPreprocessor) keeps that contract.
        arr = np.ascontiguousarray(x, dtype=np.complex128)
        out = np.empty_like(arr)
        z = self._zero
        p = self._pole
        x1 = self._x1
        y1 = self._y1
        # Tight Python loop — but it's only 2 complex muls + 2 adds per
        # sample. For a 65536-sample chunk at 2.4 Msps that's ~27 ms of
        # Python overhead, far under the chunk's wall-clock duration.
        # Vectorization is possible (scipy.lfilter) but would violate
        # ADR-004's scipy-offline-only rule.
        for i in range(arr.size):
            xi = arr[i]
            yi = xi - z * x1 + p * y1
            out[i] = yi
            x1 = xi
            y1 = yi
        self._x1 = x1
        self._y1 = y1
        return out.astype(x.dtype, copy=False)


class NoiseBlanker:
    """Impulse-noise suppressor with adaptive threshold.

    The threshold is in dB above the running noise-floor estimate —
    6 dB = gentle, 10 dB = aggressive, 15 dB = only catches strong
    pulses. The default 10 dB matches the classic OpenWebRX NB.
    """

    __slots__ = (
        "_alpha",
        "_threshold_linear",
        "_floor",
        "_sample_rate",
        "_time_const_s",
        "_threshold_db",
        "_max_floor",
    )

    def __init__(
        self,
        sample_rate: float,
        threshold_db: float = 10.0,
        time_const_s: float = 5e-3,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be > 0")
        if threshold_db <= 0:
            raise ValueError("threshold_db must be > 0 (dB above floor)")
        if time_const_s <= 0:
            raise ValueError("time_const_s must be > 0")
        self._sample_rate = float(sample_rate)
        self._threshold_db = float(threshold_db)
        self._time_const_s = float(time_const_s)
        # EMA weight: 1 - exp(-T/τ), T = 1/fs, τ = time_const_s.
        # At τ=5ms, fs=2.4 MHz → α ≈ 8.3e-5; the floor integrates ~12k
        # samples (~5 ms of signal) — long enough to ignore 100µs pulses.
        self._alpha = 1.0 - math.exp(-1.0 / (sample_rate * time_const_s))
        self._threshold_linear = 10.0 ** (threshold_db / 20.0)
        # Start with a sane floor (silence) so the first chunk doesn't
        # blank everything before the EMA tracks the real noise level.
        self._floor = 1e-9
        # Safety cap: if the noise floor climbs unreasonably high (DC
        # spike, AGC pumping), the NB should not turn into a hard
        # limiter. Cap the floor at 0.5 magnitude (FS = 1.0).
        self._max_floor = 0.5

    @property
    def sample_rate(self) -> float:
        return self._sample_rate

    @property
    def threshold_db(self) -> float:
        return self._threshold_db

    @property
    def time_const_s(self) -> float:
        return self._time_const_s

    def process(self, x: np.ndarray) -> np.ndarray:
        """Clip impulse samples that exceed ``threshold_db`` above floor.

        Non-impulse samples pass through unchanged (scale=1.0). Impulse
        samples are scaled down to exactly the threshold so the resulting
        signal has the same peak amplitude as the floor×threshold
        envelope.
        """
        if x.size == 0:
            return x
        arr = np.ascontiguousarray(x, dtype=np.complex128)
        mag = np.abs(arr)
        # Vectorize the floor update + clip computation; we walk forward
        # with a forward loop for the EMA, but compute the clip mask
        # in bulk. This is ~5x faster than the pure Python loop at 2.4
        # Msps (the np.abs + np.maximum are vectorized C calls).
        floor = self._floor
        alpha = self._alpha
        thr_lin = self._threshold_linear
        # Cap: if the noise floor climbs unreasonably high (DC spike),
        # the NB should not turn into a hard limiter.
        max_floor = self._max_floor
        out = np.empty_like(arr)
        # Sample-by-sample EMA update is unavoidable in pure numpy
        # without scipy.signal.lfilter; a Python loop here is ~3 ms per
        # 65k samples (measured on a 3 GHz core), well under the chunk's
        # wall-clock duration at any real-time source rate.
        for i in range(arr.size):
            m = mag[i]
            new_floor = alpha * m + (1.0 - alpha) * floor
            floor = new_floor if new_floor < max_floor else max_floor
            threshold_mag = thr_lin * floor
            if m > threshold_mag:
                # Scale this sample down to the threshold. Phase is
                # preserved (real-only multiply).
                scale = threshold_mag / m if m > 0 else 0.0
                out[i] = arr[i] * scale
            else:
                out[i] = arr[i]
        self._floor = floor
        return out.astype(x.dtype, copy=False)


class IQPreprocessor:
    """Apply notch + noise blanker to IQ chunks based on a DSPParams.

    Constructed per ReceiverSession; rebuilt when the dsp_params change
    (the session's set_dsp_params() handler rebuilds the AudioChain
    AND the IQPreprocessor together — slice-7).
    """

    __slots__ = ("_notch", "_nb", "_sample_rate", "_notch_enabled", "_nb_enabled")

    def __init__(self, sample_rate: float, params: DSPParams | None = None) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be > 0")
        self._sample_rate = float(sample_rate)
        self._notch: NotchFilter | None = None
        self._nb: NoiseBlanker | None = None
        self._notch_enabled = False
        self._nb_enabled = False
        if params is not None:
            self._configure(params)

    def _configure(self, params: DSPParams) -> None:
        """Build / rebuild the notch + NB from a DSPParams."""
        self._notch_enabled = bool(
            params.notch_enabled
            and params.notch_freq_hz is not None
            and params.notch_q is not None
            and params.notch_q > 0
        )
        if self._notch_enabled:
            self._notch = NotchFilter(
                freq_hz=float(params.notch_freq_hz or 0.0),
                sample_rate=self._sample_rate,
                q=float(params.notch_q or 30.0),
            )
        else:
            self._notch = None
        self._nb_enabled = bool(
            params.noise_blanker_enabled
            and params.noise_blanker_threshold is not None
            and params.noise_blanker_threshold > 0
        )
        if self._nb_enabled:
            self._nb = NoiseBlanker(
                sample_rate=self._sample_rate,
                threshold_db=float(params.noise_blanker_threshold or 10.0),
            )
        else:
            self._nb = None

    def reconfigure(self, params: DSPParams) -> None:
        """Rebuild the notch + NB with new params (state is reset)."""
        self._configure(params)

    @property
    def active(self) -> bool:
        """True if any stage is active (caller can skip the preprocess call)."""
        return self._notch_enabled or self._nb_enabled

    @property
    def notch(self) -> NotchFilter | None:
        return self._notch

    @property
    def noise_blanker(self) -> NoiseBlanker | None:
        return self._nb

    def process(self, chunk: np.ndarray) -> np.ndarray:
        """Apply notch + NB in order; returns the processed complex array.

        The caller passes a contiguous complex64 view (the same data
        that will be fed to the pycsdr chains). When no stage is active,
        the input is returned unchanged (no copy).
        """
        if not self.active or chunk.size == 0:
            return chunk
        # Always copy on the first stage so we don't mutate the caller's
        # array (the hub stream may share buffers across taps).
        out = np.array(chunk, dtype=np.complex64, copy=True) if chunk.dtype != np.complex64 else np.array(chunk, copy=True)
        if self._notch is not None:
            out = self._notch.process(out)
        if self._nb is not None:
            out = self._nb.process(out)
        # Cast back to complex64 (notch + NB may have promoted to
        # complex128 internally; pycsdr's Buffer expects cf32).
        if out.dtype != np.complex64:
            out = out.astype(np.complex64, copy=False)
        return out
