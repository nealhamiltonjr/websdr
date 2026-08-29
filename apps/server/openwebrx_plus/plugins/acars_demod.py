"""ACARS demodulator — 1200/2400 baud MSK for aircraft text messaging.

ACARS (Aircraft Communications Addressing and Reporting System) is a
digital datalink used by airlines for short text messages (telemetry,
weather requests, engine reports, etc.). It operates on VHF (131.550 MHz
worldwide, 129.125/130.025/130.450 in the US).

  * **Modulation**: MSK (Minimum Shift Keying), a special case of FSK
    with h=0.5 (phase continuity).
  * **Baud rate**: 1200 baud (older) or 2400 baud (newer, VHF Mode 2).
  * **Mark frequency**: 1200 Hz (bit 1).
  * **Space frequency**: 2400 Hz (bit 0).
  * **Frame format**: HDLC-like with sync bytes + CRC-16.

The demodulator uses two Goertzel filters (1200 Hz + 2400 Hz) and
samples at mid-bit to recover the bit stream, then hunts for the ACARS
sync byte (0xEB 0x90 = the bit-reversed preamble) and reads the frame
up to the CRC-16 check.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

DEFAULT_SAMPLE_RATE = 8000
DEFAULT_MARK_HZ = 1200.0
DEFAULT_SPACE_HZ = 2400.0
DEFAULT_BAUD = 1200

# ACARS sync bytes (the preamble before every frame).
ACARS_SYNC_BYTE_1 = 0xEB
ACARS_SYNC_BYTE_2 = 0x90
# Minimum frame length: sync(2) + SOH(1) + addr(7) + mode(1) + ACK(1) +
# label(2) + block_id(1) + text(N) + CRC(2) + ETX(1) = ~18 bytes minimum.
MIN_FRAME_BYTES = 15
MAX_FRAME_BYTES = 250


def _samples_per_bit(sample_rate: int, baud: float) -> int:
    return max(1, int(round(sample_rate / baud)))


@dataclass
class AcarsReceiver:
    """Streaming ACARS demodulator — int16 PCM → ACARS frames (bytes).

    Args:
        sample_rate: audio sample rate in Hz (default 8000).
        mark_hz: mark tone (default 1200 Hz).
        space_hz: space tone (default 2400 Hz).
        baud: symbol rate (default 1200 baud).
    """

    sample_rate: int = DEFAULT_SAMPLE_RATE
    mark_hz: float = DEFAULT_MARK_HZ
    space_hz: float = DEFAULT_SPACE_HZ
    baud: float = DEFAULT_BAUD

    _spb: int = 0
    _goertzel_mark: tuple[float, float, float] = (0.0, 0.0, 0.0)
    _goertzel_space: tuple[float, float, float] = (0.0, 0.0, 0.0)
    _bit_buf: list[int] = field(default_factory=list)
    _in_frame: bool = False
    _frame_bytes: list[int] = field(default_factory=list)
    _byte_acc: int = 0
    _bit_count: int = 0
    _frames: list[bytes] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError(f"sample_rate must be > 0, got {self.sample_rate}")
        if self.mark_hz <= 0 or self.space_hz <= 0:
            raise ValueError("mark_hz and space_hz must be > 0")
        if self.mark_hz >= self.sample_rate / 2 or self.space_hz >= self.sample_rate / 2:
            raise ValueError(
                f"mark/space must be < sample_rate/2 ({self.sample_rate / 2})"
            )
        if self.baud <= 0:
            raise ValueError(f"baud must be > 0, got {self.baud}")
        self._spb = _samples_per_bit(self.sample_rate, self.baud)
        self._goertzel_mark = _goertzel_coeff(self.mark_hz, self.sample_rate)
        self._goertzel_space = _goertzel_coeff(self.space_hz, self.sample_rate)
        self._etx_pos: int | None = None

    def feed(self, pcm: np.ndarray) -> list[bytes]:
        """Feed int16 PCM samples, return a list of complete ACARS frames."""
        if pcm.size == 0:
            return []
        if pcm.dtype != np.int16:
            pcm = pcm.astype(np.int16)
        audio = pcm.astype(np.float32) / 32768.0
        frames: list[bytes] = []
        i = 0
        while i + self._spb <= audio.size:
            chunk = audio[i : i + self._spb]
            mark_mag = _goertzel_mag(chunk, *self._goertzel_mark)
            space_mag = _goertzel_mag(chunk, *self._goertzel_space)
            bit = 1 if mark_mag > space_mag else 0
            frame = self._process_bit(bit)
            if frame is not None:
                frames.append(frame)
            i += self._spb
        return frames

    def _process_bit(self, bit: int) -> bytes | None:
        """Process one bit through the ACARS frame framer.

        Collects bits MSB-first into bytes. When the 2-byte sync
        (0xEB 0x90) is detected, enters frame mode and accumulates
        bytes until the ETX byte (0x03) or max length.
        """
        # Accumulate bits into bytes (MSB-first).
        self._byte_acc = (self._byte_acc << 1) | bit
        self._bit_count += 1
        if self._bit_count < 8:
            return None
        # We have a full byte.
        byte = self._byte_acc & 0xFF
        self._bit_count = 0
        self._byte_acc = 0
        if not self._in_frame:
            # Look for sync sequence: 0xEB followed by 0x90.
            if len(self._frame_bytes) == 0 and byte == ACARS_SYNC_BYTE_1:
                self._frame_bytes.append(byte)
                return None
            elif len(self._frame_bytes) == 1 and byte == ACARS_SYNC_BYTE_2:
                self._frame_bytes.append(byte)
                self._in_frame = True
                return None
            else:
                # Not a sync match — reset.
                self._frame_bytes = []
                return None
        # We're in a frame — accumulate bytes.
        self._frame_bytes.append(byte)
        # Check for ETX (end of text, 0x03) — end of frame.
        # After ETX, we need 2 more bytes for the CRC.
        if byte == 0x03 and len(self._frame_bytes) >= 5:
            # We found ETX; mark that we need 2 more CRC bytes.
            self._etx_pos = len(self._frame_bytes) - 1
        if self._etx_pos is not None and len(self._frame_bytes) >= self._etx_pos + 3:
            # ETX + 2 CRC bytes collected — frame complete.
            frame = bytes(self._frame_bytes)
            self._frame_bytes = []
            self._in_frame = False
            self._etx_pos = None
            if len(frame) >= MIN_FRAME_BYTES:
                return frame
            return None
        # Safety: don't let frames grow unbounded.
        if len(self._frame_bytes) > MAX_FRAME_BYTES:
            self._in_frame = False
            self._frame_bytes = []
        return None

    @property
    def frame_count(self) -> int:
        return len(self._frames)

    def reset(self) -> None:
        self._bit_buf.clear()
        self._in_frame = False
        self._frame_bytes = []
        self._byte_acc = 0
        self._bit_count = 0
        self._frames = []
        self._etx_pos = None


def _goertzel_coeff(freq: float, sample_rate: int) -> tuple[float, float, float]:
    k = 2.0 * math.pi * freq / sample_rate
    return (2.0 * math.cos(k), math.cos(k), math.sin(k))


def _goertzel_mag(samples: np.ndarray, coeff: float, cos_term: float, sin_term: float) -> float:
    s_prev = 0.0
    s_prev2 = 0.0
    for x in samples:
        s = x + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    mag_sq = s_prev * s_prev + s_prev2 * s_prev2 - coeff * s_prev * s_prev2
    return math.sqrt(max(0.0, mag_sq))


def crc16_acars(data: bytes) -> int:
    """Compute CRC-16 for ACARS (CRC-CCITT, poly 0x1021, init 0x0000).

    ACARS uses CRC-16-CCITT with polynomial 0x1021 and initial value 0x0000
    (no final XOR). The last 2 bytes of the frame are the FCS.
    """
    crc = 0x0000
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def verify_crc(frame: bytes) -> bool:
    """Verify the CRC-16 of an ACARS frame (last 2 bytes are FCS, big-endian)."""
    if len(frame) < 4:
        return False
    received_fcs = (frame[-2] << 8) | frame[-1]
    computed = crc16_acars(frame[:-2])
    return received_fcs == computed
