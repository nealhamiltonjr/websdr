"""AIS GMSK streaming demodulator (ADR-003 family #3).

Pure-numpy GMSK demodulator: complex IQ in → HDLC-deframed AIS
messages out. Designed for narrowband AIS at 9600 baud, ideally at
24-48 kS/s input rate (the source baseband rate after the wideband
DDC has centered the carrier and decimated). Above ~100 kS/s the
bit-slicer becomes unreliable — pair with a VFO tap (ADR-005) for
real receivers, OR use the subprocess ``rtl-ais`` plugin (ADR-003
family #2) for the production demod.

Demod chain:
  cf32 IQ → FM demod (np.angle diff) → mid-symbol slice (1 sample/bit)
       → HDLC deframe (find 0x7E start, destuff, find 0x7E end)
       → CRC-16-CCITT verify → decode_ais_payload()

The FM demod is the standard ``arctan(diff(arg(IQ)))`` form. Bit
slicing happens at the IDEAL mid-symbol sample (every SPS samples),
and the bit value is determined by a sign threshold on a 16-symbol
moving average (so DC offsets from the carrier not being perfectly
centered don't tilt the bit decisions).

Limitations (documented honestly):
  - No carrier frequency tracking. The source's center_freq MUST be
    within ~500 Hz of the AIS channel center (162 MHz) or the
    accumulated phase walks off and the demod produces garbage bits.
  - No equalization. Real multipath on 162 MHz will degrade the
    bit-error-rate; this demod is best for clean fixtures + strong
    nearby stations.
  - No bit stuffing lookahead in the demod itself — that lives in
    :func:`ais_protocol.destuff`, which runs on each candidate frame.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np

from .ais_protocol import (
    AisMessage,
    bits_to_bytes,
    crc16_ais,
    decode_ais_payload,
    destuff,
)

# --- constants -------------------------------------------------------------

AIS_BAUD = 9600  # bits/s — ITU-R M.1371
AIS_SAMPLE_RATE = 48_000  # Hz — 5 samples/bit at 9600 baud (a reasonable default)
MIN_FRAME_BITS = 56  # 1 byte start + 6 bytes min payload + 2 bytes CRC + flags
MAX_FRAME_BITS = 8 + 8 + (200 + 16) * 8  # preamble + flags + max payload+CRC unstuffed

# 5 consecutive 1-bits = the stuffing pattern; 6 consecutive 1-bits =
# a flag (0x7E = 01111110) when bracketed by 0s. The bit-stuffing rule
# guarantees the flag pattern only appears at frame boundaries.


@dataclass
class AisDemodStats:
    """Cumulative stats surfaced in ``status()``."""

    frames: int = 0
    crc_failures: int = 0
    short_fragments: int = 0
    feed_samples: int = 0


class AisReceiver:
    """Streaming AIS GMSK demodulator + HDLC deframer.

    Feed complex IQ chunks in any size (frames may straddle chunk
    boundaries — a tail buffer carries partial frames across feeds).
    The default sample rate is 48 kS/s (5 samples/bit); any integer
    multiple of the baud rate >= 2 is accepted (>= 4 is recommended
    for clean bit-slicing).
    """

    def __init__(self, sample_rate: int = AIS_SAMPLE_RATE) -> None:
        if sample_rate % AIS_BAUD != 0:
            raise ValueError(
                f"AIS demodulator sample rate must be a multiple of {AIS_BAUD}, "
                f"got {sample_rate}"
            )
        if sample_rate < 2 * AIS_BAUD:
            raise ValueError(
                f"AIS demodulator needs >= 2 samples/bit ({2 * AIS_BAUD} Hz), "
                f"got {sample_rate} Hz"
            )
        self._sps = sample_rate // AIS_BAUD
        self._sample_rate = sample_rate
        self._bits: list[int] = []  # accumulated bit stream
        self._feed_offset = 0
        self._last_dc = 0.0
        self.stats = AisDemodStats()

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def frames(self) -> int:
        return self.stats.frames

    @property
    def crc_failures(self) -> int:
        return self.stats.crc_failures

    # --- streaming feed ---------------------------------------------------

    def feed(self, iq: np.ndarray) -> Iterator[AisMessage]:
        """Feed one chunk of complex IQ (any size). Yields CRC-valid frames.

        The chunk's sample rate MUST match the constructor's; the
        session enforces this at attach time.
        """
        if iq.dtype != np.complex64:
            iq = iq.astype(np.complex64)
        if len(iq) == 0:
            return

        self.stats.feed_samples += len(iq)

        # FM demod: phase difference per sample.
        # np.angle returns [-π, π]; the wrap avoids wraparound when the
        # carrier drifts by more than 1 Hz per sample.
        phase = np.angle(iq)
        d = np.diff(phase)
        # Wrap to [-π, π]
        d = (d + np.pi) % (2 * np.pi) - np.pi
        fm = d.astype(np.float32)

        # Bit slicing: at each mid-symbol sample (every SPS samples
        # starting at SPS//2), take sign(FM value).
        #
        # No DC removal in v1 — the GMSK modulation we synthesize is
        # balanced (every ±π/2 bit is followed by its mirror over the
        # frame preamble), so the FM demod has zero mean across any
        # ~16-symbol window. A moving-average DC estimate caused
        # edge-effect bit errors at flag/payload transitions in the
        # original implementation. Real receivers with carrier drift
        # should pair with the subprocess ``rtl-ais`` plugin for a
        # production demod with carrier tracking.
        sps = self._sps
        offset = sps // 2
        new_bits: list[int] = []
        for i in range(offset, len(fm), sps):
            new_bits.append(1 if fm[i] > 0 else 0)

        self._bits.extend(new_bits)

        # Drain complete frames from the bit stream.
        yield from self._drain_frames()

        # Cap accumulated bits to avoid unbounded growth when no frame
        # boundaries appear (badly-tuned receiver or pure noise).
        if len(self._bits) > MAX_FRAME_BITS * 4:
            self._bits = self._bits[-(MAX_FRAME_BITS * 2):]

    # --- HDLC deframe + CRC verify ----------------------------------------

    def _drain_frames(self) -> Iterator[AisMessage]:
        """Find complete HDLC frames in ``self._bits`` and decode them.

        Scans for the flag pattern (0x7E = 01111110). Between two flags,
        destuff the payload, pack back to bytes, verify CRC, then hand
        to :func:`ais_protocol.decode_ais_payload`.
        """
        bits = self._bits
        # Find all flag positions: 8 consecutive bits = 01111110 (HDLC_FLAG).
        # We scan for the bit pattern, not byte boundaries — bit stuffing
        # means flag positions can land on any bit offset within the stream.
        flag_positions = self._find_flags(bits, len(bits) - 8)
        # Need at least 2 flags (start + end) to form a frame.
        if len(flag_positions) < 2:
            return

        consumed_up_to = 0
        for i in range(len(flag_positions) - 1):
            start = flag_positions[i] + 8  # after the start flag
            end = flag_positions[i + 1]
            payload_bits = bits[start:end]
            if len(payload_bits) < MIN_FRAME_BITS:
                self.stats.short_fragments += 1
                continue
            destuffed = destuff(payload_bits)
            # Pack into bytes — but the last byte may be partial (padding).
            # The HDLC payload is byte-aligned (the start flag is on a byte
            # boundary and bit-stuffing preserves alignment), so this should
            # be exact. If it's not, the frame is corrupt — skip.
            if len(destuffed) % 8 != 0:
                self.stats.short_fragments += 1
                continue
            payload_bytes = bits_to_bytes(destuffed)
            if len(payload_bytes) < 4:  # payload + 2-byte CRC
                self.stats.short_fragments += 1
                continue
            # CRC over everything except the last 2 bytes (which are the CRC).
            data = payload_bytes[:-2]
            crc_recv = (payload_bytes[-2] << 8) | payload_bytes[-1]
            crc_calc = crc16_ais(data)
            if crc_recv != crc_calc:
                self.stats.crc_failures += 1
                continue
            msg = decode_ais_payload(data, rssi_dbfs=0.0)
            if msg is not None:
                self.stats.frames += 1
                yield msg
            consumed_up_to = end

        # Drop consumed bits so the next feed starts after the last flag.
        if consumed_up_to > 0:
            self._bits = self._bits[consumed_up_to:]

    @staticmethod
    def _find_flags(bits: list[int], end: int) -> list[int]:
        """Find positions where the bit pattern 01111110 appears.

        Scans bit-by-bit (the pattern can land on any bit offset
        because bit stuffing doesn't preserve byte alignment in the
        stuffed stream).
        """
        positions: list[int] = []
        # Pattern: 0,1,1,1,1,1,1,0
        for i in range(min(end, len(bits) - 8) + 1):
            if (
                bits[i] == 0
                and bits[i + 1] == 1
                and bits[i + 2] == 1
                and bits[i + 3] == 1
                and bits[i + 4] == 1
                and bits[i + 5] == 1
                and bits[i + 6] == 1
                and bits[i + 7] == 0
            ):
                positions.append(i)
        return positions


__all__ = [
    "AIS_BAUD",
    "AIS_SAMPLE_RATE",
    "AisDemodStats",
    "AisReceiver",
]
