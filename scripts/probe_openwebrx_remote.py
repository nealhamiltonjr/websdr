#!/usr/bin/env python3
"""Live probe for the OpenWebRX federation client (ADR-006).

Connects to a real public OpenWebRX / OpenWebRX+ receiver, performs the full
handshake, and reports what it sees: server version, config, FFT/audio frame
rates, ADPCM interop, and the strongest signals in the passband. Exits after
--seconds (default 12) and closes the connection politely (one user slot,
released promptly — ADR-006 etiquette).

Usage (from apps/server, with the venv active):

    .venv/bin/python ../../scripts/probe_openwebrx_remote.py \
        "http://boomerthedog.com:8073/#freq=3570000,mod=lsb,sql=-150"

    # or pick any receiver from the directory:
    .venv/bin/python ../../scripts/probe_openwebrx_remote.py --directory

    # or probe a whole batch (one at a time, politely):
    .venv/bin/python ../../scripts/probe_openwebrx_remote.py URL1 URL2 URL3

This script is the FIRST-LIVE-CONNECTION verification tool for the protocol
literals in openwebrx_plus/sources/openwebrx_remote.py: if anything disagrees
with a real server, the probe makes the mismatch visible immediately.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# Make the server package importable when run from the repo.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "server"))

from openwebrx_plus.sources import RemoteDisplaySource, parse_openwebrx_url  # noqa: E402
from openwebrx_plus.sources.base import RemoteAudioFrame, RemoteFftFrame  # noqa: E402

# A few well-known public receivers (verify liveness at receiverbook.de).
KNOWN_RECEIVERS = [
    "http://boomerthedog.com:8073/#freq=3570000,mod=lsb,sql=-150",
]


async def probe(url: str, seconds: float, tune: int | None, mode: str | None) -> int:
    target = parse_openwebrx_url(url)
    print(f"\n=== {target.host}:{target.port} "
          f"({'wss' if target.use_tls else 'ws'}) ===")
    if target.freq:
        print(f"    deep link: freq={target.freq} mod={target.mod} sql={target.squelch}")

    source = RemoteDisplaySource(url=url)
    fft_count = audio_count = 0
    fft_bytes = audio_bytes = 0
    last_fft: RemoteFftFrame | None = None
    started = time.monotonic()
    status = 0

    gen = source.display_stream()
    deadline = started + seconds
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                frame = await asyncio.wait_for(gen.__anext__(), timeout=remaining)
            except (StopAsyncIteration, TimeoutError):
                break
            if isinstance(frame, RemoteFftFrame):
                fft_count += 1
                fft_bytes += frame.bins.nbytes
                last_fft = frame
            elif isinstance(frame, RemoteAudioFrame):
                audio_count += 1
                audio_bytes += frame.pcm.nbytes

        elapsed = max(time.monotonic() - started, 1e-9)

        # Optional mid-stream tune (verifies dspcontrol interop live)
        if tune is not None:
            await source.tune(tune)
            if mode:
                await source.set_mode(mode)
            print(f"    tuned to {tune} Hz (mode={mode or 'unchanged'})")

        cfg = source.remote_config
        print(f"    server version : {source.server_version}")
        print(f"    receiver       : "
              f"{source.receiver_details.get('receiver', {}).get('name', '?')}")
        print(f"    center / rate  : {cfg.get('center_freq', '?')} Hz / "
              f"{cfg.get('samp_rate', '?')} Hz")
        print(f"    fft            : size={cfg.get('fft_size', '?')} "
              f"compression={cfg.get('fft_compression', '?')}")
        print(f"    audio          : compression={cfg.get('audio_compression', '?')} "
              f"rate={source.output_rate}")
        print(f"    waterfall lvl  : {cfg.get('waterfall_levels', '?')}")
        print(f"    clients        : max={cfg.get('max_clients', '?')}")
        print(f"    frames         : {fft_count} FFT ({fft_count / elapsed:.1f}/s, "
              f"{fft_bytes / elapsed / 1024:.1f} KiB/s) | "
              f"{audio_count} audio ({audio_count / elapsed:.1f}/s, "
              f"{audio_bytes / elapsed / 1024:.1f} KiB/s)")
        if last_fft is not None:
            top = sorted(enumerate(last_fft.bins), key=lambda kv: -kv[1])[:5]
            span = last_fft.sample_rate
            center = last_fft.center_freq
            print("    strongest bins :")
            for bin_idx, level in top:
                freq = center - span / 2 + (bin_idx + 0.5) * span / len(last_fft.bins)
                print(f"        {freq / 1e3:10.3f} kHz  {level:7.1f} dB")
        if fft_count == 0:
            print("    !! no FFT frames — check fft_compression / server config")
            status = 1
    except RuntimeError as exc:
        print(f"    !! {exc}")
        status = 1
    finally:
        await gen.aclose()

    print(f"    closed politely after {time.monotonic() - started:.1f}s")
    return status


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="*", default=None,
                        help="receiver URLs (deep links welcome)")
    parser.add_argument("--directory", action="store_true",
                        help="probe the built-in known-receiver list")
    parser.add_argument("--seconds", type=float, default=12.0,
                        help="seconds to stream per receiver (default 12)")
    parser.add_argument("--tune", type=int, default=None,
                        help="tune to this frequency (Hz) mid-probe")
    parser.add_argument("--mode", default=None,
                        help="switch mode mid-probe (e.g. am, usb, nfm)")
    args = parser.parse_args()

    urls = args.urls or (KNOWN_RECEIVERS if args.directory else [])
    if not urls:
        parser.error("give at least one receiver URL (or --directory)")
        return 2

    failures = 0
    for url in urls:
        failures += await probe(url, args.seconds, args.tune, args.mode)
        await asyncio.sleep(1.0)  # be polite between receivers
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
