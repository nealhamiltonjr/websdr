"""Smoke test for apps/server/openwebrx_plus/dsp/fft.py.

Pushes a complex sine into FftChain and verifies the peak bin lands at the
expected position. Equivalent to test_pycsdr_smoke.py but exercises the
high-level wrapper class instead of raw pycsdr blocks.
"""
from __future__ import annotations

import os
import sys
import time

# Portable path setup: resolve apps/server relative to this script so the
# test runs anywhere without hardcoded absolute paths.
_THIS = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS, ".."))
_SERVER_DIR = os.path.join(_REPO_ROOT, "apps", "server")
sys.path.insert(0, _SERVER_DIR)

# pycsdr needs libcsdr on the library path; honor an existing LD_LIBRARY_PATH
# if the caller has already set one.
os.environ.setdefault(
    "LD_LIBRARY_PATH",
    os.path.expanduser("~/.local/usr/lib/x86_64-linux-gnu") + ":" + os.path.expanduser("~/.local/usr/lib"),
)

import numpy as np

from openwebrx_plus.dsp import FftChain


def main() -> int:
    sample_rate = 1_000_000
    fft_size = 1024
    sine_freq = 100_000  # +100 kHz tone

    chain = FftChain(
        fft_size=fft_size,
        avg_number=1,
        add_db=-10.0,
        center_freq=0,
        sample_rate=sample_rate,
        min_db=-100.0,
        max_db=-20.0,
    )

    try:
        # Push enough samples for several FFT frames.
        # NOTE: pycsdr modules use strictly-greater-than in canProcess(),
        # so the pipeline needs MORE than n*fft_size samples to emit the
        # nth frame. Push 8x fft_size to guarantee >= 4 frames out.
        n_samples = fft_size * 8
        t = np.arange(n_samples, dtype=np.float32) / sample_rate
        iq = np.exp(1j * 2 * np.pi * sine_freq * t).astype(np.complex64)
        chain.feed(iq.tobytes())

        # Drain — poll for up to 5 seconds for the first batch.
        deadline = time.time() + 5.0
        frames: list = []
        while time.time() < deadline and len(frames) < 4:
            frames.extend(chain.drain())
            if len(frames) < 4:
                time.sleep(0.05)

        if not frames:
            print("FAIL: FftChain produced no frames within 5s", file=sys.stderr)
            return 1

        print(f"got {len(frames)} frames")
        frame = frames[0]
        if len(frame.bins) != fft_size * 4:
            print(
                f"FAIL: expected {fft_size * 4} bytes, got {len(frame.bins)}",
                file=sys.stderr,
            )
            return 1

        power = np.frombuffer(frame.bins, dtype=np.float32)
        peak_bin = int(np.argmax(power))
        expected_bin = int(fft_size // 2 + (sine_freq * fft_size) / sample_rate)

        print(f"  peak_bin={peak_bin} (power={power[peak_bin]:.2f} dB)")
        print(f"  expected_bin={expected_bin}")
        print(f"  power range: min={power.min():.2f} max={power.max():.2f} dB")

        if abs(peak_bin - expected_bin) > 2:
            print(
                f"FAIL: peak_bin {peak_bin} is more than 2 bins away from expected {expected_bin}",
                file=sys.stderr,
            )
            return 1

        print("PASS: FftChain produces correctly-positioned FFT peak.")
        return 0
    finally:
        chain.stop()


if __name__ == "__main__":
    sys.exit(main())
