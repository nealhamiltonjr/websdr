"""Smoke test for apps/server/openwebrx_plus/dsp/audio.py.

Pushes an AM-modulated complex IQ signal into AudioChain (mode=AM) and
verifies that int16 PCM audio comes out at the expected rate, with the
modulation tone present in the audio spectrum.

IMPORTANT: pycsdr ring buffers overwrite unconsumed data (writeable() is a
constant size-1; there is no reader backpressure). The chain processes at
~11 Msps, but the test must pace its pushes below that rate or data will
be silently dropped. We push in modest chunks with small inter-chunk
sleeps to stay under the chain's throughput ceiling.
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault(
    "LD_LIBRARY_PATH",
    "/home/z/.local/usr/lib/x86_64-linux-gnu:/home/z/.local/usr/lib",
)

sys.path.insert(0, "/home/z/my-project/openwebrx-plus/apps/server")

import numpy as np

from openwebrx_plus.dsp import AudioChain


def build_am_iq(
    carrier_hz: float,
    mod_hz: float,
    sample_rate: int,
    n: int,
    mod_depth: float = 0.7,
) -> np.ndarray:
    """AM signal: carrier at +carrier_hz, amplitude-modulated at mod_hz."""
    t = np.arange(n, dtype=np.float32) / sample_rate
    envelope = 0.5 * (1.0 + mod_depth * np.sin(2 * np.pi * mod_hz * t))
    iq = envelope * np.exp(1j * 2 * np.pi * carrier_hz * t)
    return iq.astype(np.complex64)


def main() -> int:
    sample_rate = 240_000
    output_rate = 8_000
    carrier_hz = 0.0  # carrier at DC — channel already centered
    mod_hz = 1_000.0  # 1 kHz audio tone
    mode = "AM"

    chain = AudioChain(
        mode=mode,  # type: ignore[arg-type]
        input_rate=sample_rate,
        output_rate=output_rate,
        channel_offset_hz=0.0,
    )

    try:
        # Push 1 second of IQ in 20 paced chunks (50 ms of IQ each).
        # The 5 ms sleep between chunks keeps the push rate at ~2.4 Msps,
        # well under the chain's ~11 Msps ceiling.
        total_samples = sample_rate  # 1 second
        chunk_samples = sample_rate // 20  # 12000 samples per chunk
        t0 = time.time()
        pushed = 0
        for start in range(0, total_samples, chunk_samples):
            n = min(chunk_samples, total_samples - start)
            iq = build_am_iq(carrier_hz, mod_hz, sample_rate, n)
            chain.feed(iq.tobytes())
            pushed += n
            time.sleep(0.005)
        feed_elapsed = time.time() - t0
        print(f"pushed {pushed} samples in {feed_elapsed:.2f}s ({pushed / feed_elapsed / 1e6:.2f} Msps)")

        # Drain — poll until we have ~0.5 second of audio or 10 s timeout.
        want_bytes = output_rate * 2 // 2  # int16 mono, 0.5 second
        audio = bytearray()
        deadline = time.time() + 10.0
        while time.time() < deadline and len(audio) < want_bytes:
            for frame in chain.drain():
                audio += bytes(frame.pcm)
            time.sleep(0.02)

        print(f"got {len(audio)} bytes of audio ({len(audio) / 2 / output_rate:.2f} sec)")

        if len(audio) < want_bytes:
            print(
                f"FAIL: expected at least {want_bytes} bytes of audio, got {len(audio)}",
                file=sys.stderr,
            )
            return 1

        pcm = np.frombuffer(bytes(audio), dtype=np.int16)
        print(f"  pcm range: min={pcm.min()} max={pcm.max()} (int16)")

        # Non-trivial audio energy must be present.
        peak = float(np.abs(pcm).max())
        if peak < 500:
            print(f"FAIL: audio peak {peak:.0f} is near-silent; demod produced no signal", file=sys.stderr)
            return 1

        # Spectral check: 1 kHz tone should dominate the audio FFT.
        audio_f32 = pcm.astype(np.float32) / 32768.0
        spectrum = np.abs(np.fft.rfft(audio_f32 * np.hanning(len(audio_f32))))
        freqs = np.fft.rfftfreq(len(audio_f32), d=1.0 / output_rate)
        peak_freq = freqs[int(np.argmax(spectrum))]
        print(f"  dominant audio freq: {peak_freq:.0f} Hz (expect ~{mod_hz:.0f} Hz)")
        if abs(peak_freq - mod_hz) > 150:
            print(
                f"FAIL: dominant frequency {peak_freq:.0f} Hz is not near {mod_hz:.0f} Hz",
                file=sys.stderr,
            )
            return 1

        print("PASS: AudioChain demodulates AM with the expected audio tone.")
        return 0
    finally:
        chain.stop()


if __name__ == "__main__":
    sys.exit(main())
