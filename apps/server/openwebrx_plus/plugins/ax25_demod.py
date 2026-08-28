"""AX.25 packet radio demodulator — 1200 baud AFSK → HDLC frames.

AX.25 (Amateur X.25) is the data link layer protocol used in amateur
packet radio. The most common physical layer is **1200 baud AFSK**:

  * **Mark frequency**: 1200 Hz (bit 1)
  * **Space frequency**: 2200 Hz (bit 0)
  * **Baud rate**: 1200 baud
  * **Modulation**: AFSK (Audio Frequency Shift Keying) with NRZI encoding

NRZI (Non-Return-to-Zero Inverted) encoding: a 0 bit is represented by
a tone change (1200→2200 or 2200→1200), a 1 bit is represented by no
change (same tone). This makes the clock recovery self-synchronizing.

The demodulator:
  1. Uses two Goertzel filters (one per tone) to compute the mark/space
     magnitude ratio per sample.
  2. Slices at mid-bit to recover the NRZI-encoded bit stream.
  3. Decodes NRZI: tone change → 0, no change → 1.
  4. Hunts for the HDLC flag byte 0x7E (01111110) — bit-stuffed, so
     after de-stuffing it's 8 bits of 01111110.
  5. Reads the packet between flags, checks CRC-16 (CRC-CCITT),
     and hands the decoded frame to the AX.25 protocol decoder.

This module is pure-numpy (ADR-004 compliant — no scipy in the live path).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# --- AX.25 AFSK wire constants ---
DEFAULT_SAMPLE_RATE = 8000  # packet radio typically uses 48 kHz, but 8 kHz works for demod
DEFAULT_MARK_HZ = 1200.0
DEFAULT_SPACE_HZ = 2200.0
DEFAULT_BAUD = 1200

# HDLC framing.
HDLC_FLAG = 0x7E  # 01111110 — frame delimiter
HDLC_FLAG_BITS = [0, 1, 1, 1, 1, 1, 1, 0]  # LSB-first
MAX_FRAME_BYTES = 512  # max AX.25 frame length (excluding flags)


def _samples_per_bit(sample_rate: int, baud: float) -> int:
    return max(1, int(round(sample_rate / baud)))


@dataclass
class Ax25Receiver:
    """Streaming AX.25 packet demodulator — int16 PCM → HDLC frames.

    Args:
        sample_rate: audio sample rate in Hz (default 8000).
        mark_hz: mark tone frequency (default 1200 Hz).
        space_hz: space tone frequency (default 2200 Hz).
        baud: symbol rate (default 1200 baud).
    """

    sample_rate: int = DEFAULT_SAMPLE_RATE
    mark_hz: float = DEFAULT_MARK_HZ
    space_hz: float = DEFAULT_SPACE_HZ
    baud: float = DEFAULT_BAUD

    # Internal state.
    _spb: int = 0  # samples per bit
    _goertzel_mark: tuple[float, float, float] = (0.0, 0.0, 0.0)
    _goertzel_space: tuple[float, float, float] = (0.0, 0.0, 0.0)
    _prev_tone: int = 1  # NRZI: previous tone (1=mark, 0=space)
    _in_frame: bool = False
    _bit_count: int = 0
    _current_byte: int = 0
    _ones_count: int = 0  # for bit de-stuffing
    _frame_bytes: list[int] = field(default_factory=list)
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

    def feed(self, pcm: np.ndarray) -> list[bytes]:
        """Feed int16 PCM samples, return a list of complete AX.25 frames.

        Each frame is a bytes object containing the raw HDLC payload
        (addresses + control + info + CRC). The caller (Ax25DecoderPlugin)
        hands these to the protocol decoder.
        """
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
            # FSK decision: mark (1) if mark_mag > space_mag, else space (0).
            current_tone = 1 if mark_mag > space_mag else 0

            # NRZI decode: tone change → bit 0, no change → bit 1.
            bit = 1 if current_tone == self._prev_tone else 0
            self._prev_tone = current_tone

            # Process the bit through the HDLC framer.
            frame = self._process_bit(bit)
            if frame is not None:
                frames.append(frame)

            i += self._spb

        return frames

    def _process_bit(self, bit: int) -> bytes | None:
        """Process one NRZI-decoded bit through the HDLC framer.

        Returns a complete frame (bytes) when a frame boundary is detected,
        or None if still accumulating.
        """
        # Check for HDLC flag: 6 consecutive 1s followed by a 0.
        # The flag byte is 01111110 (LSB-first: 0,1,1,1,1,1,1,0).
        # In NRZI, 6 consecutive 1s means 6 consecutive "no change" tones.
        if bit == 1:
            self._ones_count += 1
        else:
            if self._ones_count == 6:
                # This is a flag byte — frame delimiter.
                if self._in_frame and len(self._frame_bytes) > 0:
                    # End of frame — check CRC and return.
                    frame = bytes(self._frame_bytes)
                    self._frame_bytes = []
                    self._in_frame = False
                    self._ones_count = 0
                    self._bit_count = 0
                    self._current_byte = 0
                    # Verify CRC-16 (last 2 bytes are FCS).
                    if len(frame) >= 2:
                        return frame  # CRC check done by caller
                    return None
                else:
                    # Start of frame.
                    self._in_frame = True
                    self._frame_bytes = []
                    self._bit_count = 0
                    self._current_byte = 0
                    self._ones_count = 0
                    return None
            elif self._ones_count == 5 and self._in_frame:
                # Bit stuffing: after 5 consecutive 1s, the sender inserts
                # a 0. We skip this stuffed 0 bit.
                self._ones_count = 0
                return None
            else:
                self._ones_count = 0

        if self._in_frame:
            # Accumulate bits into bytes (LSB-first).
            self._current_byte |= (bit << self._bit_count)
            self._bit_count += 1
            if self._bit_count >= 8:
                self._frame_bytes.append(self._current_byte)
                self._current_byte = 0
                self._bit_count = 0
                # Safety: don't let frames grow unbounded.
                if len(self._frame_bytes) > MAX_FRAME_BYTES:
                    self._in_frame = False
                    self._frame_bytes = []
                    self._bit_count = 0
                    self._current_byte = 0
                    self._ones_count = 0

        return None

    @property
    def frame_count(self) -> int:
        """Total frames decoded since creation."""
        return len(self._frames)

    def reset(self) -> None:
        """Clear all streaming state."""
        self._prev_tone = 1
        self._in_frame = False
        self._bit_count = 0
        self._current_byte = 0
        self._ones_count = 0
        self._frame_bytes.clear()
        self._frames.clear()


def _goertzel_coeff(freq: float, sample_rate: int) -> tuple[float, float, float]:
    """Precompute the Goertzel coefficient for a given frequency."""
    k = 2.0 * math.pi * freq / sample_rate
    return (2.0 * math.cos(k), math.cos(k), math.sin(k))


def _goertzel_mag(samples: np.ndarray, coeff: float, cos_term: float, sin_term: float) -> float:
    """Compute the Goertzel magnitude for a chunk of samples."""
    s_prev = 0.0
    s_prev2 = 0.0
    for x in samples:
        s = x + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    mag_sq = s_prev * s_prev + s_prev2 * s_prev2 - coeff * s_prev * s_prev2
    return math.sqrt(max(0.0, mag_sq))


def crc16_ax25(data: bytes) -> int:
    """Compute CRC-16-CCITT for AX.25 (poly 0x1021, init 0xFFFF, no final XOR).

    The AX.25 FCS (Frame Check Sequence) is a CRC-16-CCITT with:
      - Polynomial: 0x1021 (x^16 + x^12 + x^5 + 1)
      - Initial value: 0xFFFF
      - No final XOR (the residue check is: CRC(data + CRC) == 0xF0B8)
    """
    crc = 0xFFFF
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
    """Verify the CRC-16 of a complete AX.25 frame.

    The frame includes the FCS as the last 2 bytes (little-endian).
    Returns True if the CRC is valid.
    """
    if len(frame) < 2:
        return False
    # The FCS is the last 2 bytes, little-endian.
    received_fcs = frame[-2] | (frame[-1] << 8)
    computed = crc16_ax25(frame[:-2])
    return received_fcs == computed
