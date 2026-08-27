"""Tests for the binary FFT wire format packing in receiver_session.py.

Verifies the contract defined in packages/shared-types/src/fft.ts:
  - Header is 32 bytes
  - Magic = 0x4F465257
  - Version = 1
  - Field offsets and types match the spec
  - Body is binCount * 4 bytes of float32

Slice-3: the FFT is now computed by the pycsdr FftChain (Fft →
LogAveragePower → FftSwap, all SIMD C++). These tests drive the chain
through ReceiverSession._pack_fft_frame and verify the same wire format
the slice-1 numpy implementation produced.
"""

from __future__ import annotations

import asyncio
import struct
import time

import numpy as np
import pytest

from openwebrx_plus.sessions.receiver_session import (
    AUDIO_SAMPLE_RATE,
    FFT_HEADER_MAGIC,
    FFT_HEADER_SIZE_BYTES,
    FFT_HEADER_VERSION,
    ReceiverSession,
)
from openwebrx_plus.sources.simulated import SimulatedSource

# Must match the frontend constant in packages/shared-types/src/fft.ts
EXPECTED_TS_HEADER_SIZE_BYTES = 32


def _make_test_session() -> ReceiverSession:
    # Slice-3: RtlSdrSource is a real driver now (no hardware on CI) — the
    # hardware-free SimulatedSource stands in; the wire format it feeds is
    # identical (complex64 chunks at a known rate).
    return ReceiverSession(
        receiver_id="rx-test",
        source=SimulatedSource(
            signal_set="ham_band", sample_rate=2_400_000, realtime=False
        ),
        center_freq=14_205_000,
        sample_rate=2_400_000,
        fft_size=256,
        mode="USB",
    )


def test_header_size_matches_spec() -> None:
    assert FFT_HEADER_SIZE_BYTES == EXPECTED_TS_HEADER_SIZE_BYTES


def _start_and_feed(session: ReceiverSession, chunks: list[np.ndarray], timeout: float = 5.0) -> list[bytes]:
    """Start the session's chains, feed chunks, drain + pack FFT frames."""
    session._fft_chain = session._fft_chain or __import__(
        "openwebrx_plus.dsp", fromlist=["FftChain"]
    ).FftChain(
        fft_size=session.fft_size,
        avg_number=1,
        add_db=-10.0,
        center_freq=session.center_freq,
        sample_rate=session.sample_rate,
        min_db=session.min_db,
        max_db=session.max_db,
    )
    chain = session._fft_chain
    try:
        for chunk in chunks:
            chain.feed(np.ascontiguousarray(chunk, dtype=np.complex64).tobytes())
        deadline = time.time() + timeout
        frames: list[bytes] = []
        while time.time() < deadline and not frames:
            frames = [session._pack_fft_frame(f.bins) for f in chain.drain()]
            if not frames:
                time.sleep(0.02)
        return frames
    finally:
        chain.stop()
        session._fft_chain = None


def test_frame_packing_round_trips() -> None:
    """Feed a known input through the pycsdr FftChain → verify header + body."""
    session = _make_test_session()
    n = session.fft_size

    # Synthetic full-scale complex sine at 1 kHz within 2.4 MHz —
    # with fft_size=256, the tone lands essentially at DC (bin 128 after
    # FftSwap) since 1 kHz << 9.375 kHz bin width.
    t = np.arange(n * 8, dtype=np.float32) / session.sample_rate
    tone = 1000.0  # Hz
    phase = 2 * np.pi * tone * t
    chunk = (np.cos(phase) + 1j * np.sin(phase)).astype(np.complex64)

    frames = _start_and_feed(session, [chunk])
    assert frames, "pycsdr FftChain produced no frames"

    frame = frames[0]
    # Header + body.
    assert len(frame) == FFT_HEADER_SIZE_BYTES + n * 4

    # Parse header — 8 fields: magic version rx_hash center_freq sample_rate min_db max_db bin_count
    (magic, version, rx_hash, center_freq, sample_rate, min_db, max_db, bin_count) = (
        struct.unpack("<IIIffffI", frame[:FFT_HEADER_SIZE_BYTES])
    )
    assert magic == FFT_HEADER_MAGIC
    assert version == FFT_HEADER_VERSION
    assert rx_hash != 0
    assert center_freq == 14_205_000
    assert sample_rate == 2_400_000
    assert min_db == -100.0
    assert max_db == -20.0
    assert bin_count == n

    # Parse body.
    bins = np.frombuffer(frame[FFT_HEADER_SIZE_BYTES:], dtype=np.float32)
    assert len(bins) == n

    # A full-scale complex sine must produce a strong peak. pycsdr's
    # LogAveragePower scale differs slightly from the numpy dBFS scale, so
    # assert a broad window rather than ~0 dBFS.
    assert bins.max() > -10.0, f"peak too low: {bins.max()} dB"
    assert bins.max() <= 60.0, f"peak implausibly high: {bins.max()} dB"


def test_short_chunks_still_frame() -> None:
    """Chunks smaller than fft_size accumulate in the chain until a full
    frame can be emitted — bin count in the header is always fft_size."""
    session = _make_test_session()

    # Feed 16 chunks of 64 samples (= 1024 = 4 × fft_size). The pycsdr
    # modules' canProcess() uses strictly-greater-than, so slightly more
    # than n × fft_size samples are needed for the nth frame to emerge.
    chunks = []
    for _ in range(16):
        t = np.arange(64, dtype=np.float32) / session.sample_rate
        chunks.append(np.exp(1j * 2 * np.pi * 1000.0 * t).astype(np.complex64))

    frames = _start_and_feed(session, chunks)
    assert frames, "no frames from accumulated short chunks"
    bin_count = struct.unpack("<I", frames[0][28:32])[0]
    assert bin_count == session.fft_size


@pytest.mark.asyncio
async def test_session_streams_fft_and_audio() -> None:
    """Full integration: start() the session, subscribe, verify both the
    FFT and audio binary wire formats arrive within a few seconds."""
    session = _make_test_session()
    await session.start()
    try:
        q = session.subscribe()
        fft_frame: bytes | None = None
        audio_frame: bytes | None = None
        deadline = time.time() + 10.0
        while time.time() < deadline and (fft_frame is None or audio_frame is None):
            try:
                frame = q.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.05)
                continue
            if len(frame) >= 4:
                magic = struct.unpack("<I", frame[:4])[0]
                if magic == FFT_HEADER_MAGIC and fft_frame is None:
                    fft_frame = frame
                elif magic == 0x41554449 and audio_frame is None:  # "AUDI"
                    audio_frame = frame

        assert fft_frame is not None, "no FFT frame received"
        assert audio_frame is not None, "no audio frame received"

        # FFT frame: 32-byte header + 256 bins × 4 bytes.
        (magic, version, rx_hash, cf, sr, min_db, max_db, bin_count) = struct.unpack(
            "<IIIffffI", fft_frame[:32]
        )
        assert magic == FFT_HEADER_MAGIC
        assert bin_count == session.fft_size
        assert len(fft_frame) == 32 + bin_count * 4

        # Audio frame: 16-byte header + int16 PCM.
        (a_magic, a_version, a_rate, a_count) = struct.unpack("<IIII", audio_frame[:16])
        assert a_magic == 0x41554449
        assert a_version == 1
        assert a_rate == AUDIO_SAMPLE_RATE
        assert len(audio_frame) == 16 + a_count * 2
    finally:
        await session.stop()
