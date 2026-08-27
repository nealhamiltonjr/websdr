"""Wire-format dataclasses returned by the pycsdr DSP chains.

These mirror the binary wire format defined in
``packages/shared-types/src/fft.ts`` and ``packages/shared-types/src/audio.ts``.
The DSP chains return the raw payload (float32 bins for FFT, int16 PCM for
audio) plus enough metadata to pack the wire header — packing itself is the
caller's responsibility so that this module stays free of struct / asyncio
imports.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class FftFrame:
    """One frame of FFT output — float32 dB power per bin, DC-centered.

    ``bins`` is a ``memoryview`` over a contiguous ``fft_size * 4`` byte
    float32 buffer. The DSP chain retains ownership of the underlying
    buffer; callers that need to hold the data past the next ``drain()``
    call should copy ``bytes(bins)``.
    """

    bins: memoryview
    fft_size: int
    center_freq: int
    sample_rate: int
    min_db: float
    max_db: float


@dataclass(slots=True, frozen=True)
class AudioFrame:
    """One frame of audio output — int16 mono PCM at ``sample_rate`` Hz."""

    pcm: memoryview
    sample_rate: int
    frame_count: int
