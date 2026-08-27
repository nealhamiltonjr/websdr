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
"""

from __future__ import annotations

from .audio import AudioChain, DemodMode
from .fft import FftChain
from .types import AudioFrame, FftFrame

__all__ = [
    "AudioChain",
    "AudioFrame",
    "DemodMode",
    "FftChain",
    "FftFrame",
]
