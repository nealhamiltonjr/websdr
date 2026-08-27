"""Mode S / ADS-B demodulator + decoder — pure Python + numpy (ADR-003).

Tap point: complex float32 IQ at exactly 2 MSPS (2 samples per PPM
half-bit, the classic RTL-SDR 1090 MHz rate). The demodulator is a
streaming envelope detector:

    IQ (cf32) → |·| envelope → preamble match → 1 Mbps PPM bit slice
              → frame bytes → CRC-24 verification → field decode

Wire format facts (verified against the baked fixture
``fixtures/iq/adsb_1090.cf32`` and the dump1090 test vector
``8D4840D6202CC371C32CE0`` → CRC ``0x576098``):

  * Preamble: 0.5 µs pulses at 0.0/1.0/2.5/3.5/4.5 µs → half-µs sample
    slots 0, 2, 5, 7, 9 HIGH; slots 1, 3, 4, 6, 8, 10 LOW. Data starts
    at slot 16 (8.0 µs).
  * PPM: each bit occupies 1 µs = 2 slots. Bit "1" pulses the FIRST
    half, bit "0" pulses the SECOND half.
  * Frame length: 56 bits (DF 0–15, e.g. DF11 all-call) or 112 bits
    (DF 16–31, e.g. DF17 ADS-B extended squitter).
  * Parity: CRC-24 (poly 0xFFF409, init 0, MSB-first) over the whole
    frame is zero for "data parity" frames. DF11 may alternatively use
    "address parity": crc24(frame) == ICAO24.

Decode scope (v1): DF11 (ICAO + CA), DF17/DF18 (ICAO, callsign TC=4,
altitude TC 0–4). Position (CPR) and velocity (TC 19) land with the
dump1090 subprocess integration — see ADR-003's hardware bring-up plan.

Altitude caveat: the baked fixture encodes the 11-bit altitude field as
plain binary × 25 ft (documented in scripts/generate_iq_fixtures.py —
"approximated as binary, enough for decoders to read a stable field").
This decoder reads it back the same way so the fixture round-trips.
Real-world Mode S uses Gilliam/Gray coding with a Q bit; that decode
belongs to the dump1090 plugin, which is the ADR-003 v1 answer for
live 1090 MHz traffic.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# --- wire constants ---------------------------------------------------------

MODE_S_SAMPLE_RATE = 2_000_000  # Hz — 2 samples per PPM half-bit

_PREAMBLE_HIGH = (0, 2, 5, 7, 9)  # half-µs slots, pulse centers
_PREAMBLE_LOW = (1, 3, 4, 6, 8, 10)
_PREAMBLE_SPAN = 11  # slots scanned by the matcher
_DATA_START = 16  # first data bit's first slot

_SHORT_BITS = 56  # DF 0–15
_LONG_BITS = 112  # DF 16–31
_MAX_FRAME_SLOTS = _DATA_START + _LONG_BITS  # 240 half-µs samples

# Retained tail between feeds: anything shorter than a max frame can
# still be completed by the next chunk.
_TAIL_KEEP = _MAX_FRAME_SLOTS + 64

# Adaptive threshold: noise = 25th percentile of the envelope, HIGH =
# noise × multiplier. The baked fixture's weakest frames (0.12 amplitude
# vs 0.02 σ noise) sit ~3× above this; live receivers should tune here.
_NOISE_PERCENTILE = 25.0
_THRESHOLD_MULTIPLIER = 3.5
_MIN_THRESHOLD = 1e-6


def crc24_mode_s(data: bytes) -> int:
    """Mode S parity: poly 0xFFF409, init 0, MSB-first, no reflection."""
    crc = 0
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0xFFF409
    return crc & 0xFFFFFF


# --- decoded frame ----------------------------------------------------------


@dataclass(frozen=True)
class ModeSFrame:
    """One CRC-verified Mode S frame with the fields v1 decodes."""

    df: int  # downlink format 0–31
    icao: str | None  # 6-hex ICAO24 ("4D22AA") when the format carries it
    raw: str  # full frame hex, data + parity
    callsign: str | None  # DF17/18 TC 1–4
    altitude_ft: int | None  # DF17/18 TC 0–4 (fixture binary encoding)
    parity: str  # "data" | "address"
    rssi_dbfs: float  # mean preamble pulse level, dBFS
    sample_offset: int  # envelope-sample index of the preamble (diagnostics)


# --- 6-bit callsign charset -------------------------------------------------

def _decode_callsign(data: bytes) -> str | None:
    """48 bits (8 chars × 6 bits, MSB-first) → callsign string.

    Charset (ICAO Annex 10): 1–26 = A–Z, 32 = space, 48–57 = 0–9,
    0 = no information. Unknown codes render as '?'.
    """
    acc = int.from_bytes(data, "big")
    chars: list[str] = []
    for shift in range(42, -1, -6):
        v = (acc >> shift) & 0x3F
        if 1 <= v <= 26:
            chars.append(chr(64 + v))
        elif v == 32:
            chars.append(" ")
        elif 48 <= v <= 57:
            chars.append(chr(v))
        elif v == 0:
            continue  # no information
        else:
            chars.append("?")  # reserved code point
    text = "".join(chars).rstrip()
    return text or None


def _decode_altitude(me: bytes) -> int | None:
    """11-bit altitude × 25 ft — mirrors the fixture encoder exactly.

    The field lives in ME[0] bits 0–2 + ME[1] bits 0–7. (Gilliam/Q-bit
    decode for live traffic is deferred to the dump1090 plugin.)
    """
    if len(me) < 2:
        return None
    ac = ((me[0] & 0x07) << 8) | me[1]
    return ac * 25


def decode_frame_fields(frame: bytes) -> tuple[str | None, str | None, int | None]:
    """Extract (icao, callsign, altitude_ft) from a verified frame."""
    df = frame[0] >> 3
    icao: str | None = None
    callsign: str | None = None
    altitude_ft: int | None = None
    if df in (11, 17, 18):
        icao = frame[1:4].hex().upper()
    if df in (17, 18) and len(frame) >= 11:
        me = frame[4:11]
        tc = me[0] >> 3
        if 1 <= tc <= 4:
            callsign = _decode_callsign(me[1:7])
        elif tc == 0:
            # TC 0 = surveillance altitude (no position). TC 1–4 are
            # identification frames — their ME is the callsign, decoding
            # "altitude" from it would be garbage. Real Gilliam decode for
            # TC 5–18 (airborne position) belongs to the dump1090 plugin.
            altitude_ft = _decode_altitude(me)
    return icao, callsign, altitude_ft


# --- streaming demodulator ----------------------------------------------------


class ModeSReceiver:
    """Streaming Mode S PPM demodulator + frame decoder.

    Feed complex IQ chunks in any size (frames may straddle chunk
    boundaries — a tail buffer carries partial frames across feeds).
    Only accepts exactly ``MODE_S_SAMPLE_RATE``; other rates need a DDC
    in front (see ADR-005 VfoChain) and fail fast here.
    """

    def __init__(self, sample_rate: int = MODE_S_SAMPLE_RATE) -> None:
        if int(sample_rate) != MODE_S_SAMPLE_RATE:
            raise ValueError(
                f"Mode S demodulator requires {MODE_S_SAMPLE_RATE} S/s "
                f"(2 samples per PPM half-bit), got {sample_rate}"
            )
        self._env = np.zeros(0, dtype=np.float32)
        self._search = 0  # scan cursor into _env
        self._stream_offset = 0  # samples trimmed so far (diagnostics)
        self.frames = 0  # CRC-valid frames decoded
        self.crc_failures = 0  # preambles whose frame failed parity

    # -- public API ---------------------------------------------------------

    def feed(self, iq: np.ndarray) -> list[ModeSFrame]:
        """Demod one IQ chunk; returns frames completed by this chunk."""
        env = np.abs(iq).astype(np.float32, copy=False)
        if env.size == 0:
            return []
        self._env = (
            np.concatenate((self._env, env)) if self._env.size else env.copy()
        )
        frames = self._scan()
        self._trim()
        return frames

    # -- internals ----------------------------------------------------------

    def _scan(self) -> list[ModeSFrame]:
        env = self._env
        n = env.size
        out: list[ModeSFrame] = []
        if n < _DATA_START + 10:
            self._search = min(self._search, n)
            return out

        noise = float(np.percentile(env, _NOISE_PERCENTILE))
        threshold = max(noise * _THRESHOLD_MULTIPLIER, _MIN_THRESHOLD)

        # Candidate pre-filter, fully vectorized: position i survives iff
        # all five preamble pulse slots are above threshold. (A pure-Python
        # walk over 2 MSPS would be the pipeline bottleneck; this is one
        # boolean array + four shifted ANDs.) Low-slot checks run per
        # candidate — there are only a handful even at busy-airport rates.
        lo = self._search
        hi = n - _PREAMBLE_SPAN
        if hi <= lo:
            return out
        high = env > threshold
        pulse = high.copy()
        for shift in (2, 5, 7, 9):
            pulse[:-shift] &= high[shift:]
        candidates = np.flatnonzero(pulse[lo:hi]) + lo

        i = lo
        search_end = hi  # normal completion: everything up to hi examined
        for cand in candidates.tolist():
            if cand < i:
                continue  # candidate sits inside the frame just decoded
            i = cand
            if not self._preamble_low_ok(env, i, threshold):
                continue
            if i + _DATA_START + 10 > n:
                search_end = i  # not even DF is fully arrived — wait
                break
            df = self._read_bits(env, i, 0, 5)
            total_bits = _LONG_BITS if df >= 16 else _SHORT_BITS
            frame_end = i + _DATA_START + total_bits * 2
            if frame_end > n:
                search_end = i  # partial frame — keep the cursor, wait
                break
            frame = self._read_frame(env, i, total_bits)
            decoded = self._verify(frame, env, i)
            if decoded is not None:
                out.append(decoded)
                self.frames += 1
            else:
                self.crc_failures += 1
            i = frame_end
        self._search = search_end
        return out

    def _preamble_low_ok(self, env: np.ndarray, i: int, threshold: float) -> bool:
        """Quiet-slot check, RELATIVE to the pulse level.

        An absolute noise-threshold check rejects real preambles whenever a
        single noise spike lands in a quiet slot (the baked fixture loses
        ~15% of frames that way — Rayleigh σ≈0.014 spikes past a 3.5σ
        threshold often enough to matter). Quiet slots only need to be
        well below the pulses, not below the noise floor.
        """
        mean_pulse = float(np.mean(env[[i + slot for slot in _PREAMBLE_HIGH]]))
        ceiling = max(mean_pulse * 0.5, threshold)
        return all(env[i + slot] <= ceiling for slot in _PREAMBLE_LOW)

    def _read_bits(self, env: np.ndarray, i: int, bit_offset: int, count: int) -> int:
        """PPM-slice `count` bits starting at bit `bit_offset`, MSB-first."""
        acc = 0
        base = i + _DATA_START
        for k in range(count):
            s0 = env[base + 2 * (bit_offset + k)]
            s1 = env[base + 2 * (bit_offset + k) + 1]
            acc = (acc << 1) | (1 if s0 > s1 else 0)
        return acc

    def _read_frame(self, env: np.ndarray, i: int, total_bits: int) -> bytes:
        acc = 0
        base = i + _DATA_START
        for k in range(total_bits):
            s0 = env[base + 2 * k]
            s1 = env[base + 2 * k + 1]
            acc = (acc << 1) | (1 if s0 > s1 else 0)
        return acc.to_bytes(total_bits // 8, "big")

    def _verify(self, frame: bytes, env: np.ndarray, i: int) -> ModeSFrame | None:
        crc = crc24_mode_s(frame)
        df = frame[0] >> 3
        parity = "data"
        if crc != 0:
            # DF11 address/parity variant: PI = CRC(data) ⊕ ICAO. By CRC
            # linearity the whole-frame residue then equals crc24(ICAO
            # bytes) — leading zeros pass through the init-0 register
            # unchanged, so only the 3 appended ICAO bytes contribute.
            if df == 11 and crc == crc24_mode_s(frame[1:4]):
                parity = "address"
            else:
                return None
        icao, callsign, altitude_ft = decode_frame_fields(frame)
        rssi = float(np.mean(env[[i + slot for slot in _PREAMBLE_HIGH]]))
        rssi_dbfs = 20.0 * float(np.log10(max(rssi, 1e-9)))
        return ModeSFrame(
            df=df,
            icao=icao,
            raw=frame.hex().upper(),
            callsign=callsign,
            altitude_ft=altitude_ft,
            parity=parity,
            rssi_dbfs=rssi_dbfs,
            sample_offset=self._stream_offset + i,
        )

    def _trim(self) -> None:
        """Drop consumed history; keep a tail so straddling frames survive."""
        n = self._env.size
        keep_from = min(self._search, max(0, n - _TAIL_KEEP))
        if keep_from > 0:
            self._env = self._env[keep_from:].copy()
            self._search -= keep_from
            self._stream_offset += keep_from
