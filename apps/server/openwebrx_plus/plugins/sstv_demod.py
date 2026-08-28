"""SSTV (Slow-Scan Television) demodulator — FM frequency-to-pixel mapping.

SSTV transmits still images over voice-grade audio. The pixel intensity
maps linearly to frequency:

  * **1500 Hz** = black (0% intensity)
  * **2300 Hz** = white (100% intensity)
  * **1900 Hz** = sync pulse (calibration tone)
  * **1200 Hz** = sync leader / VIS code tone

The demodulator:
  1. Detects the VIS (Vertical Interval Signaling) leader — 1200 Hz for
     100 ms, then 1900 Hz for 100 ms — to identify the start of a frame.
  2. Reads the 8-bit VIS code (10 bits: 7 data + 1 parity + start/stop)
     to identify the SSTV mode (Scottie 1, Martin 1, etc.).
  3. Decodes the image scanlines using FM demodulation (np.diff of phase)
     to map instantaneous frequency → pixel intensity.

Supported modes (v1):
  * Scottie 1: 320×256, 4-tone color (YCrCb), ~110 s transmission
  * Scottie 2: 320×256, faster scan, ~71 s transmission
  * Martin 1: 320×256, 4-tone color, ~114 s transmission
  * Robot 36: 320×240, YCrCb, ~36 s transmission (partially supported)

This module is pure-numpy (ADR-004 compliant — no scipy in the live path).
The frequency-to-pixel mapping is a simple linear interpolation; the FM
demod uses np.diff of the unwrapped phase (the standard FM discriminator).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import numpy as np

# --- SSTV wire constants ---
DEFAULT_SAMPLE_RATE = 8000

# Frequency map (Hz).
FREQ_BLACK = 1500.0
FREQ_WHITE = 2300.0
FREQ_SYNC = 1900.0
FREQ_LEADER = 1200.0

# VIS leader timing (ms).
VIS_LEADER_1200_MS = 100.0
VIS_LEADER_1900_MS = 100.0
VIS_BIT_MS = 30.0
VIS_START_BIT_MS = 30.0
VIS_STOP_BIT_MS = 30.0

# VIS mode codes (8-bit, sent LSB-first, 7 data + 1 even parity).
VIS_SCOTTIE_1 = 0x3C  # 60
VIS_SCOTTIE_2 = 0x38  # 56
VIS_MARTIN_1 = 0x2C   # 44
VIS_MARTIN_2 = 0x28   # 40
VIS_ROBOT_36 = 0x08   # 8


class SstvMode(IntEnum):
    """Supported SSTV modes."""
    SCOTTIE_1 = VIS_SCOTTIE_1
    SCOTTIE_2 = VIS_SCOTTIE_2
    MARTIN_1 = VIS_MARTIN_1
    MARTIN_2 = VIS_MARTIN_2
    ROBOT_36 = VIS_ROBOT_36


# Mode parameters: (width, height, color_seq, sync_ms, pixel_us)
# color_seq: the order of color channels per scanline.
# sync_ms: sync pulse duration at the start of each scanline.
# pixel_us: microseconds per pixel (determines scan rate).
_MODE_PARAMS: dict[SstvMode, dict[str, Any]] = {
    SstvMode.SCOTTIE_1: {
        "width": 320, "height": 256,
        "color_seq": ("green", "blue", "red"),
        "sync_ms": 9.0,
        "porch_ms": 1.5,
        "pixel_us": 432,  # 0.432 ms per pixel
    },
    SstvMode.SCOTTIE_2: {
        "width": 320, "height": 256,
        "color_seq": ("green", "blue", "red"),
        "sync_ms": 9.0,
        "porch_ms": 1.5,
        "pixel_us": 276,  # faster scan
    },
    SstvMode.MARTIN_1: {
        "width": 320, "height": 256,
        "color_seq": ("green", "blue", "red", "sync"),
        "sync_ms": 4.862,
        "porch_ms": 0.572,
        "pixel_us": 458,
    },
    SstvMode.MARTIN_2: {
        "width": 320, "height": 256,
        "color_seq": ("green", "blue", "red", "sync"),
        "sync_ms": 4.862,
        "porch_ms": 0.572,
        "pixel_us": 287,
    },
    SstvMode.ROBOT_36: {
        "width": 320, "height": 240,
        "color_seq": ("y", "c", "sync"),
        "sync_ms": 9.0,
        "porch_ms": 3.0,
        "pixel_us": 333,
    },
}


@dataclass
class SstvImage:
    """One decoded SSTV image frame."""
    mode: SstvMode
    width: int
    height: int
    # RGB pixel data, shape (height, width, 3), uint8.
    pixels: np.ndarray
    # Timestamp the image completed.
    timestamp: float = 0.0


class SstvReceiver:
    """Streaming SSTV demodulator — int16 PCM → SstvImage frames.

    The receiver detects the VIS leader, reads the mode code, then
    decodes scanlines one at a time. A complete image is emitted when
    all scanlines for the mode's height are decoded.

    Args:
        sample_rate: audio sample rate in Hz (default 8000).
    """

    def __init__(self, sample_rate: int = DEFAULT_SAMPLE_RATE) -> None:
        if sample_rate <= 0:
            raise ValueError(f"sample_rate must be > 0, got {sample_rate}")
        self.sample_rate = sample_rate
        self._audio_buf: list[np.ndarray] = []
        self._state: str = "idle"  # idle | leader_1200 | leader_1900 | vis | scanning
        self._mode: SstvMode | None = None
        self._scanlines: list[np.ndarray] = []
        self._images: list[SstvImage] = []

    def feed(self, pcm: np.ndarray) -> list[SstvImage]:
        """Feed int16 PCM samples, return a list of completed SstvImage frames.

        Most calls return an empty list (image frames complete only when
        all scanlines are decoded). A completed image is removed from the
        internal buffer and returned to the caller.
        """
        if pcm.size == 0:
            return []
        if pcm.dtype != np.int16:
            pcm = pcm.astype(np.int16)
        self._audio_buf.append(pcm)
        # Process the buffer when we have enough samples for a VIS leader
        # (200 ms = 1600 samples at 8 kHz) or a scanline (~0.5 s).
        total = sum(len(c) for c in self._audio_buf)
        if total < 1600:
            return []
        # Concatenate the buffer.
        audio = np.concatenate(self._audio_buf)
        self._audio_buf.clear()
        # If we're in scanning mode, keep the tail for the next call.
        images: list[SstvImage] = []
        offset = 0
        while offset < len(audio):
            if self._state == "idle":
                # Look for the VIS leader: 1200 Hz for 100 ms, then 1900 Hz for 100 ms.
                leader_end = self._detect_vis_leader(audio, offset)
                if leader_end is None:
                    # Not found — discard all but the last 1600 samples
                    # (the leader could straddle the boundary).
                    keep = min(1600, len(audio) - offset)
                    if keep > 0:
                        self._audio_buf.append(audio[len(audio) - keep:])
                    break
                offset = leader_end
                self._state = "vis"
            elif self._state == "vis":
                # Read the 8-bit VIS code.
                vis_code, vis_end = self._read_vis_code(audio, offset)
                if vis_code is None:
                    # Not enough samples — save the tail.
                    self._audio_buf.append(audio[offset:])
                    break
                offset = vis_end
                try:
                    self._mode = SstvMode(vis_code)
                except ValueError:
                    # Unknown VIS code — go back to idle.
                    self._state = "idle"
                    continue
                self._state = "scanning"
                self._scanlines = []
            elif self._state == "scanning":
                assert self._mode is not None
                params = _MODE_PARAMS[self._mode]
                height = int(params["height"])
                # Decode one scanline.
                scanline, scan_end = self._decode_scanline(audio, offset, self._mode)
                if scanline is None:
                    self._audio_buf.append(audio[offset:])
                    break
                offset = scan_end
                self._scanlines.append(scanline)
                if len(self._scanlines) >= height:
                    # Complete image — assemble + emit.
                    img = self._assemble_image(self._mode)
                    self._images.append(img)
                    images.append(img)
                    self._state = "idle"
                    self._mode = None
                    self._scanlines = []
        return images

    def _detect_vis_leader(self, audio: np.ndarray, offset: int) -> int | None:
        """Detect the VIS leader (1200 Hz for 100 ms then 1900 Hz for 100 ms).

        Returns the end offset (start of the VIS code) or None if not found.
        """
        sr = self.sample_rate
        leader_1200_samples = int(sr * VIS_LEADER_1200_MS / 1000)
        leader_1900_samples = int(sr * VIS_LEADER_1900_MS / 1000)
        total_needed = leader_1200_samples + leader_1900_samples
        if offset + total_needed > len(audio):
            return None
        # Check the first 100 ms segment for ~1200 Hz.
        seg1 = audio[offset : offset + leader_1200_samples].astype(np.float32) / 32768.0
        freq1 = _estimate_freq(seg1, sr)
        if abs(freq1 - FREQ_LEADER) > 100:
            return None
        # Check the next 100 ms segment for ~1900 Hz.
        seg2 = audio[offset + leader_1200_samples : offset + total_needed].astype(np.float32) / 32768.0
        freq2 = _estimate_freq(seg2, sr)
        if abs(freq2 - FREQ_SYNC) > 100:
            return None
        return offset + total_needed

    def _read_vis_code(self, audio: np.ndarray, offset: int) -> tuple[int | None, int]:
        """Read the 8-bit VIS code (10 bits: start + 7 data + parity + stop).

        Returns (code, end_offset) or (None, offset) if not enough samples.
        """
        sr = self.sample_rate
        bit_samples = int(sr * VIS_BIT_MS / 1000)
        # 10 bits: start (1200 Hz), 7 data bits (1100=1, 1300=0), parity, stop (1200 Hz).
        total_needed = 10 * bit_samples
        if offset + total_needed > len(audio):
            return None, offset
        bits: list[int] = []
        for i in range(10):
            start = offset + i * bit_samples
            seg = audio[start : start + bit_samples].astype(np.float32) / 32768.0
            freq = _estimate_freq(seg, sr)
            # Data bits: 1100 Hz = 1, 1300 Hz = 0 (start/stop/parity use 1200 Hz).
            if abs(freq - 1100) < 75:
                bits.append(1)
            elif abs(freq - 1300) < 75:
                bits.append(0)
            else:
                bits.append(0)  # default to 0 for non-data tones
        # Bits 1-7 are the data (LSB first), bit 8 is parity, bits 0+9 are start/stop.
        code = 0
        for i in range(7):
            code |= (bits[1 + i] << i)
        return code, offset + total_needed

    def _decode_scanline(
        self, audio: np.ndarray, offset: int, mode: SstvMode
    ) -> tuple[np.ndarray | None, int]:
        """Decode one scanline. Returns (pixels, end_offset) or (None, offset).

        The scanline structure varies by mode (Scottie/Martin differ in
        sync placement), but v1 uses a simplified uniform structure:
        sync pulse + porch + R + G + B channels.
        """
        sr = self.sample_rate
        params = _MODE_PARAMS[mode]
        width = int(params["width"])
        pixel_us = float(params["pixel_us"])
        sync_ms = float(params["sync_ms"])
        porch_ms = float(params["porch_ms"])
        # Samples per pixel.
        pixel_samples = max(1, int(sr * pixel_us / 1e6))
        # Sync + porch samples.
        sync_samples = int(sr * sync_ms / 1000)
        porch_samples = int(sr * porch_ms / 1000)
        # Total scanline length: sync + porch + 3 channels × width pixels.
        total = sync_samples + porch_samples + 3 * width * pixel_samples
        if offset + total > len(audio):
            return None, offset
        # Skip sync + porch.
        pos = offset + sync_samples + porch_samples
        # Decode 3 color channels (R, G, B) — each `width` pixels.
        channels: list[np.ndarray] = []
        for _ in range(3):
            chan = np.zeros(width, dtype=np.uint8)
            for x in range(width):
                seg = audio[pos : pos + pixel_samples].astype(np.float32) / 32768.0
                freq = _estimate_freq(seg, sr)
                # Map frequency → intensity (1500 Hz = 0, 2300 Hz = 255).
                intensity = int(np.clip((freq - FREQ_BLACK) / (FREQ_WHITE - FREQ_BLACK) * 255, 0, 255))
                chan[x] = intensity
                pos += pixel_samples
            channels.append(chan)
        # Stack into (width, 3) — R, G, B.
        pixels = np.stack(channels, axis=-1)  # shape (width, 3)
        return pixels, pos

    def _assemble_image(self, mode: SstvMode) -> SstvImage:
        """Assemble the decoded scanlines into a single image."""
        import time
        params = _MODE_PARAMS[mode]
        width = int(params["width"])
        height = int(params["height"])
        # Each scanline is (width, 3); stack into (height, width, 3).
        pixels = np.stack(self._scanlines, axis=0)
        return SstvImage(
            mode=mode,
            width=width,
            height=height,
            pixels=pixels,
            timestamp=time.time(),
        )

    def reset(self) -> None:
        """Clear all state."""
        self._audio_buf.clear()
        self._state = "idle"
        self._mode = None
        self._scanlines = []
        self._images = []

    @property
    def state(self) -> str:
        """Current demodulator state: idle / vis / scanning."""
        return self._state

    @property
    def current_mode(self) -> SstvMode | None:
        """The mode being decoded (None if idle or in VIS detection)."""
        return self._mode

    @property
    def scanline_count(self) -> int:
        """Number of scanlines decoded so far in the current frame."""
        return len(self._scanlines)


def _estimate_freq(samples: np.ndarray, sample_rate: int) -> float:
    """Estimate the dominant frequency of a chunk via zero-crossing count.

    This is a simple, robust frequency estimator for SSTV's narrow-band
    tones. The zero-crossing rate × sample_rate / 2 gives the frequency.
    More accurate than a full FFT for short windows with pure tones.
    """
    if samples.size < 4:
        return 0.0
    # Count zero crossings (sign changes).
    signs = np.sign(samples)
    # Remove zeros (sign returns 0 for exactly 0.0).
    signs = signs[signs != 0]
    if signs.size < 2:
        return 0.0
    crossings = np.sum(np.diff(signs) != 0)
    # Each full cycle has 2 zero crossings.
    freq = crossings * sample_rate / (2.0 * samples.size)
    return float(freq)
