"""Real-time DSP chains built on pycsdr.

This package wraps the pycsdr (libcsdr) blocks that already power upstream
OpenWebRX. Two chains are exported:

* :class:`FftChain` — IQ → Fft → LogAveragePower → FftSwap, emits
  ``fft_size`` float32 dB power bins per frame (DC-centered).
* :class:`AudioChain` — IQ → Shift → Bandpass → demod → AudioResampler →
  Convert → int16 PCM at the configured output sample rate.

Both chains are push-in / drain-out: the receiver session pushes raw
complex64 IQ bytes via ``feed()``, and pycsdr's AsyncRunner threads process
the data in the background. Callers drain ready frames via ``drain()``
(non-blocking).

Design notes
------------
* pycsdr is the primary DSP library (see ADR-004). It wraps libcsdr's
  autovectorized SIMD C++ for AM/FM/SSB demods, FIR decimation, AGC, FFT,
  and audio resampling — far faster than numpy for live IQ.
* The pycsdr ``Buffer.read()`` call blocks until data is available, so the
  drain loop uses a background reader thread to avoid stalling the asyncio
  event loop.
* pycsdr-dependent modules (:mod:`.audio`, :mod:`.fft`) are lazy-imported
  so pure-numpy modules (:mod:`.types`, :mod:`.preprocess`, :mod:`.ai_denoise`)
  stay importable in environments without pycsdr (the dev sandbox, CI's
  libcsdr-build step, unit tests for the noise reducer / IQ preprocessor).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Eager: pure-numpy modules (no pycsdr dependency).
from .ai_denoise import AIDenoiser, AIDenoiserConfig
from .preprocess import IQPreprocessor, NoiseBlanker, NotchFilter
from .types import AudioFrame, DSPParams, FftFrame

# Lazy: pycsdr-dependent modules. Imported on first attribute access via
# __getattr__ below so `from openwebrx_plus.dsp import AIDenoiser` works
# in a pycsdr-free env (the dev sandbox, CI's libcsdr-build step, unit
# tests for the noise reducer / IQ preprocessor), while
# `from openwebrx_plus.dsp import AudioChain` still works in the full
# dev/CI venv. The TYPE_CHECKING imports make mypy see the real types.
if TYPE_CHECKING:
    from .audio import AudioChain, DemodMode
    from .fft import FftChain

__all__ = [
    "AIDenoiser",
    "AIDenoiserConfig",
    "AudioChain",
    "AudioFrame",
    "DemodMode",
    "DSPParams",
    "FftChain",
    "FftFrame",
    "IQPreprocessor",
    "NoiseBlanker",
    "NotchFilter",
]


def __getattr__(name: str) -> object:
    if name in ("AudioChain", "DemodMode"):
        from .audio import AudioChain, DemodMode

        return {"AudioChain": AudioChain, "DemodMode": DemodMode}[name]
    if name == "FftChain":
        from .fft import FftChain

        return FftChain
    raise AttributeError(f"module 'openwebrx_plus.dsp' has no attribute {name!r}")
