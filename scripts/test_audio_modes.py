"""Quick multi-mode verification of AudioChain: USB, LSB, FM."""
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

from openwebrx_plus.dsp import AudioChain


def run_mode(mode: str, input_rate: int, output_rate: int, tone_hz: float, channel_offset_hz: float = 0.0) -> bool:
    """Feed a synthetic signal appropriate for the mode; verify audio comes out."""
    chain = AudioChain(
        mode=mode,  # type: ignore[arg-type]
        input_rate=input_rate,
        output_rate=output_rate,
        channel_offset_hz=channel_offset_hz,
    )
    try:
        total = input_rate  # 1 second
        chunk = input_rate // 20
        for start in range(0, total, chunk):
            n = min(chunk, total - start)
            t = np.arange(n, dtype=np.float32) / input_rate
            if mode in ("USB", "LSB"):
                # SSB: tone on the correct side of the suppressed carrier.
                # USB selects +150..+2850 Hz; LSB selects -2850..-150 Hz.
                carrier = 1500.0 if mode == "USB" else -1500.0
                iq = np.exp(1j * 2 * np.pi * (carrier + tone_hz) * (t + start / input_rate))
                iq = (iq * 0.5).astype(np.complex64)
            elif mode in ("NFM", "WFM"):
                # FM: constant-frequency offset tone.
                # FM mod: phase = 2π * (carrier_dev + tone) * t
                # Simplest: a complex exponential at tone_hz (frequency deviation).
                freq = tone_hz
                phase = 2 * np.pi * freq * (t + start / input_rate)
                # FM signal has constant amplitude; phase encodes the signal.
                # For test purposes, generate a complex exponential whose
                # *instantaneous frequency* varies at tone_hz.
                inst_freq = 3000.0 * np.sin(2 * np.pi * tone_hz * (t + start / input_rate))
                phase = 2 * np.pi * np.cumsum(inst_freq) / input_rate
                iq = np.exp(1j * phase).astype(np.complex64)
            else:
                raise ValueError(f"unhandled mode {mode}")
            chain.feed(iq.tobytes())
            time.sleep(0.005)

        audio = bytearray()
        deadline = time.time() + 8.0
        want = output_rate * 2 // 2  # 0.5 sec
        while time.time() < deadline and len(audio) < want:
            for f in chain.drain():
                audio += bytes(f.pcm)
            time.sleep(0.02)

        if not audio:
            print(f"  {mode}: FAIL — no audio")
            return False
        pcm = np.frombuffer(bytes(audio), dtype=np.int16)
        peak = float(np.abs(pcm).max())
        ok = peak > 100
        print(f"  {mode}: {len(audio)}B, peak={peak:.0f} -> {'OK' if ok else 'NEAR-SILENT'}")
        return ok
    finally:
        chain.stop()


def main() -> int:
    print("Multi-mode AudioChain verification:")
    results = []
    results.append(run_mode("USB", 240_000, 8_000, 700.0))
    results.append(run_mode("LSB", 240_000, 8_000, 700.0))
    results.append(run_mode("NFM", 240_000, 8_000, 1000.0))
    passed = sum(results)
    print(f"{passed}/{len(results)} modes passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
