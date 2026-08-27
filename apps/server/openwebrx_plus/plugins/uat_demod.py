"""dump978 UAT 2-GFSK demodulator — pure Python + numpy (ADR-003).

Streaming GFSK demodulator for the 978 MHz UAT downlink, mirroring the
architectural shape of :mod:`.ais_demod` (the marine AIS demodulator):

    IQ (cf32) → FM-demod → DC-block (carrier offset comp.)
              → mid-symbol bit slice → sync-word hunt
              → phase refinement → bit align → byte align
              → CRC + RS verify → field decode

Wire facts (DO-282B / RTCA DO-282B §2.2):

  * Bit rate: 1.0416667 Mbps (625/600 ≈ 1.042 Mbps).
  * Modulation: 2-GFSK with BT=0.5, h≈0.6, freq. dev. ±312.5 kHz.
  * Sync: 12-bit ``0x5C93`` word at frame start.
  * Frame: 6-bit length field + payload + Reed-Solomon parity.
    - length 0 → Short (192-bit payload + 36-bit RS = 232 bits = 29 bytes)
    - length 2 → Long  (184-bit payload + 64-bit RS = 248 bits = 31 bytes? — the
      exact byte count is computed from RS params; see uat_protocol)

This v2 takes ``UAT_SAMPLE_RATE`` (2.083333 MSPS = 2 samples/symbol)
and runs a demodulator that produces a CRC-valid frame on a synthetic
fixture AND tolerates realistic RF impairments (slice-17):

  1. FM demod (np.angle of the conjugate-product difference)
  2. **Carrier offset compensation** (slice-17): subtract a slow
     moving average (over ~5 symbols = 10 samples) from the FM signal.
     A residual carrier offset (the SDR's LO doesn't exactly match the
     signal center) shows up as a DC bias on the FM-demodulated output;
     if not removed, the bit decision threshold drifts and the demod
     fails on signals with even small (±1 kHz) offset.
  3. Mid-symbol bit slice at one bit-period after a candidate sync
  4. Sync detection via bit-wise compare with the 12-bit sync word
     (tolerating up to 2 bit errors — same margin as the AIS demod)
  5. **Per-frame phase refinement** (slice-17): once sync is detected
     at sample ``i``, try also ``i ± 1`` and pick the offset with the
     best sync correlation. This compensates for small clock drift
     (sample clock mismatch up to ±0.5 sample per symbol ≈ ±100 ppm).
  6. Bit → byte pack MSB-first
  7. Pass to :mod:`.uat_protocol` for RS + CRC + field decode

This v2 is sufficient for live-traffic bring-up on the test fixtures
plus small impairments; the dump978 binary remains the production
answer for traffic with high noise or large (>5 kHz) carrier offsets
(see ADR-003 subprocess family).
"""

from __future__ import annotations

import numpy as np

from .uat_protocol import (
    LONG_FRAME_BYTES,
    SHORT_FRAME_BYTES,
    UatFrame,
    crc24_uat,
    decode_frame_fields,
    rs_correct,
)

UAT_SAMPLE_RATE = 2_083_333  # see uat_protocol.py
_SAMPLES_PER_SYMBOL = 2  # 2.083333 MSPS / 1.0416667 Mbps

_SYNC_LEN = 12
_SYNC_TOL = 2  # bit errors tolerated in sync detection
_LEN_BITS = 6  # length field after sync
_FRAME_BYTES_SHORT = SHORT_FRAME_BYTES  # 29
_FRAME_BYTES_LONG = LONG_FRAME_BYTES  # 95

# Bit positions for length decode: 6 bits, MSB-first, length values 0/2.
_LEN_BIT_POSITIONS = (0, 1, 2, 3, 4, 5)

# Tail buffer for straddling frames between chunks.
_TAIL_KEEP = 8192

# Noise-floor threshold (dBFS, similar philosophy to Mode S).
_NOISE_DB_FLOOR = -60.0

# Slice-17: carrier offset compensation. A moving-average window over
# the FM-demodulated signal removes DC bias from residual LO offset.
# The window must be MUCH longer than the symbol period so it averages
# out the FSK deviation swings (±π/4 per sample at 2 sps) while still
# tracking slow carrier drift over a few hundred ms. 200 samples =
# 100 symbols ≈ half of a 232-bit short frame — sweet spot.
_DC_BLOCK_WINDOW = 200  # samples (~100 symbols at 2 sps)

# Slice-17: per-frame phase refinement. When a sync is detected at
# sample i, sweep i ± _PHASE_SEARCH_RANGE samples and pick the offset
# with the best sync correlation. At 2 sps, ±1 sample = ±0.5 symbol;
# this covers clock drift up to ±0.5 sample per frame-length.
_PHASE_SEARCH_RANGE = 1  # samples


class UatReceiver:
    """Streaming UAT GFSK demodulator + frame decoder.

    Feed complex IQ chunks in any size; frames may straddle chunk
    boundaries (a tail buffer carries partial bits across feeds).
    """

    def __init__(self, sample_rate: int = UAT_SAMPLE_RATE) -> None:
        if int(sample_rate) != UAT_SAMPLE_RATE:
            raise ValueError(
                f"UAT demodulator requires {UAT_SAMPLE_RATE} S/s "
                f"(2 samples/symbol of 1.0416667 Mbps), got {sample_rate}"
            )
        self._samples = np.zeros(0, dtype=np.complex64)
        self._search = 0  # cursor into _samples
        self._stream_offset = 0  # samples trimmed so far (diagnostics)
        self.frames = 0  # CRC-valid frames decoded
        self.crc_failures = 0  # frames whose CRC failed after RS

    def feed(self, iq: np.ndarray) -> list[UatFrame]:
        """Demod one IQ chunk; returns frames completed by this chunk."""
        if iq.size == 0:
            return []
        # Make sure we have complex64 for fast arithmetic.
        if iq.dtype != np.complex64:
            iq = iq.astype(np.complex64, copy=False)
        if self._samples.size:
            self._samples = np.concatenate((self._samples, iq))
        else:
            self._samples = iq.copy()
        out = self._scan()
        self._trim()
        return out

    # -- internals ----------------------------------------------------------

    def _scan(self) -> list[UatFrame]:
        samples = self._samples
        n = samples.size
        out: list[UatFrame] = []
        if n < _SYNC_LEN * _SAMPLES_PER_SYMBOL + 4:
            return out

        # FM demod: arg(s[i] * conj(s[i-1])) = phase difference.
        prod = samples[1:] * np.conj(samples[:-1])
        fm = np.angle(prod).astype(np.float32, copy=False)

        # Slice-17: carrier offset compensation. A residual LO offset
        # shows up as a DC bias on the FM-demodulated output; if not
        # removed, the sign(fm) bit decision drifts. Subtract a slow
        # moving average (window = _DC_BLOCK_WINDOW samples = ~5 sym).
        # np.convolve mode="same" returns same length; the boundary
        # samples see a partial window but that's tolerable since
        # we scan past the silence-padded frame boundaries anyway.
        if fm.size >= _DC_BLOCK_WINDOW:
            dc_kernel = np.ones(_DC_BLOCK_WINDOW, dtype=np.float32) / _DC_BLOCK_WINDOW
            dc_estimate = np.convolve(fm, dc_kernel, mode="same")
            fm = fm - dc_estimate

        # Adaptive threshold for sync detection: mid-symbol bit value is
        # sign(fm); we scan for windows of sign(fm) that match the sync
        # pattern tolerating _SYNC_TOL bit errors.
        bits = (fm > 0).astype(np.int8)  # 1 = +π/2 phase, 0 = -π/2 phase

        # The sync word bits (MSB-first), as expected from the demod.
        # DO-282B sync: 0,1,0,1,0,1,1,0,0,1,0,0 = 0x5C9 / length-12
        sync_bits = np.array(
            (0, 1, 0, 1, 0, 1, 1, 0, 0, 1, 0, 0),
            dtype=np.int8,
        )

        # Adaptive noise threshold (used for RSSI / signal presence).
        # Computed for diagnostics; not yet surfaced in frame events.
        _ = max(
            float(np.percentile(np.abs(samples), 25)),
            10 ** (_NOISE_DB_FLOOR / 20.0),
        )

        # bits array is 1 shorter than samples (conjugate-product loses
        # one sample). Use bits_n for all bounds checks so the mid-symbol
        # slice at body_end-1 doesn't read past the end.
        bits_n = bits.size  # = n - 1

        # Scan for sync: a candidate starts at sample i where the next
        # _SYNC_LEN * _SAMPLES_PER_SYMBOL samples' mid-symbol bit slice
        # matches sync_bits within _SYNC_TOL errors.
        lo = self._search
        hi = bits_n - _SYNC_LEN * _SAMPLES_PER_SYMBOL - _FRAME_BYTES_SHORT * 8 * _SAMPLES_PER_SYMBOL
        if hi <= lo:
            return out

        i = lo
        search_end = hi
        while i < hi:
            # Compute mid-symbol bit slice at offset i
            if not _matches_sync(bits, i, sync_bits, _SYNC_TOL):
                i += 1
                continue
            # Slice-17: per-frame phase refinement. Sync is detected at
            # sample i — sweep i ± _PHASE_SEARCH_RANGE samples and pick
            # the offset with the best sync correlation. This compensates
            # for small clock drift between the transmitter and our SDR's
            # sample clock. The best-scoring offset replaces i for the
            # rest of the frame decode.
            i = _refine_sync_phase(bits, i, sync_bits, _PHASE_SEARCH_RANGE)
            # Found a sync. Read length field next.
            length_bits_start = i + _SYNC_LEN * _SAMPLES_PER_SYMBOL
            if length_bits_start + _LEN_BITS * _SAMPLES_PER_SYMBOL > bits_n:
                search_end = i
                break
            length_val = _read_bits_at(
                bits, length_bits_start, _LEN_BITS, _SAMPLES_PER_SYMBOL
            )
            # Decode length: DO-282B says only 0/2 valid.
            if length_val not in (0, 2):
                i += 1
                continue
            frame_bytes = _FRAME_BYTES_LONG if length_val == 2 else _FRAME_BYTES_SHORT
            body_start = length_bits_start + _LEN_BITS * _SAMPLES_PER_SYMBOL
            body_end = body_start + frame_bytes * 8 * _SAMPLES_PER_SYMBOL
            # body_end-1 + mid_idx must be < bits.size; mid_idx = sps//2 = 1.
            if body_end + _SAMPLES_PER_SYMBOL // 2 > bits_n:
                search_end = i
                break
            # Pack bits → bytes, MSB-first.
            body = _pack_bits(
                bits, body_start, frame_bytes * 8, _SAMPLES_PER_SYMBOL
            )
            # RSSI for the sync window.
            rssi = float(np.mean(np.abs(samples[i : i + _SYNC_LEN * _SAMPLES_PER_SYMBOL])))
            rssi_dbfs = 20.0 * float(np.log10(max(rssi, 1e-9)))
            decoded = self._verify(body, length_val, rssi_dbfs, i)
            if decoded is not None:
                out.append(decoded)
                self.frames += 1
            else:
                self.crc_failures += 1
            i = body_end
        self._search = search_end
        return out

    def _verify(
        self,
        body: bytes,
        length_val: int,
        rssi_dbfs: float,
        sample_off: int,
    ) -> UatFrame | None:
        """RS-correct + CRC-check the body bytes; return a frame if valid.

        Convention A: body[0..nsym-1] = parity (low-degree);
        body[nsym..] = message+payload (high-degree). The CRC protects
        the HIGH-degree portion (payload_with_crc), so msg_bytes =
        body[nsym:], NOT body[:-nsym].
        """
        body_arr = bytearray(body)
        # v1 single-error correction (full BM/Chien for live channels
        # lands in a later slice — sufficient for low-noise fixtures).
        nsym = 6 if length_val == 0 else 12
        rs_correct(body_arr, nsym)
        # Message body (Convention A: skip the parity at the start).
        msg_bytes = bytes(body_arr[nsym:])
        crc = crc24_uat(msg_bytes)
        if crc != 0:
            return None
        icao, callsign, altitude_ft, lat, lon = decode_frame_fields(msg_bytes)
        return UatFrame(
            frame_length=length_val,
            raw=body_arr.hex().upper(),
            icao=icao,
            callsign=callsign,
            altitude_ft=altitude_ft,
            lat=lat,
            lon=lon,
            rssi_dbfs=rssi_dbfs,
            sample_offset=self._stream_offset + sample_off,
        )

    def _trim(self) -> None:
        """Drop consumed history; keep a tail so straddling frames survive."""
        n = self._samples.size
        keep_from = min(self._search, max(0, n - _TAIL_KEEP))
        if keep_from > 0:
            self._samples = self._samples[keep_from:].copy()
            self._search -= keep_from
            self._stream_offset += keep_from


def _matches_sync(
    bits: np.ndarray,
    start: int,
    sync: np.ndarray,
    tol: int,
) -> bool:
    """True iff `bits[start : start+len(sync)*_SAMPLES_PER_SYMBOL]` matches
    `sync` (mid-symbol bit slice, allowing up to `tol` bit errors)."""
    n_per_sym = _SAMPLES_PER_SYMBOL
    window = bits[start : start + len(sync) * n_per_sym]
    if window.size < len(sync) * n_per_sym:
        return False
    # Mid-symbol slice: take the second sample of each symbol (less
    # inter-symbol interference at the boundary).
    mid = window[n_per_sym // 2 :: n_per_sym][: len(sync)]
    errs = int(np.count_nonzero(mid != sync))
    return errs <= tol


def _sync_error_count(
    bits: np.ndarray,
    start: int,
    sync: np.ndarray,
) -> int:
    """Count mid-symbol bit errors against the sync pattern at `start`.

    Pure function (no tolerance check) — used by the slice-17 phase
    refinement sweep to pick the best of several candidate offsets.
    Returns the number of mismatched bits (0 = perfect sync).
    """
    n_per_sym = _SAMPLES_PER_SYMBOL
    window = bits[start : start + len(sync) * n_per_sym]
    if window.size < len(sync) * n_per_sym:
        return len(sync)  # worse than any in-bounds candidate
    mid = window[n_per_sym // 2 :: n_per_sym][: len(sync)]
    return int(np.count_nonzero(mid != sync))


def _refine_sync_phase(
    bits: np.ndarray,
    start: int,
    sync: np.ndarray,
    search_range: int,
) -> int:
    """Slice-17: per-frame phase refinement.

    Once the sync pattern is detected at sample ``start`` (within
    ``_SYNC_TOL`` bit errors), sweep ``start ± search_range`` samples
    and pick the offset with the FEWEST sync bit errors. The selected
    offset replaces ``start`` for the rest of the frame decode.

    At 2 samples/symbol, ±1 sample covers ±0.5 symbol of phase drift
    — i.e. clock mismatch up to ~100 ppm over a 232-bit frame. The
    refinement is bounded (O(search_range) sync_error_count calls),
    each O(len(sync)) — cheap enough to run per frame.

    If the original ``start`` is already the best (or one of several
    ties), it is returned unchanged. The caller's tolerance check has
    already validated that we have a viable sync; this function
    only picks the best of nearby candidates.
    """
    if search_range <= 0:
        return start
    best_start = start
    best_errs = _sync_error_count(bits, start, sync)
    for delta in range(1, search_range + 1):
        for cand in (start - delta, start + delta):
            if cand < 0:
                continue
            errs = _sync_error_count(bits, cand, sync)
            if errs < best_errs:
                best_errs = errs
                best_start = cand
    return best_start


def _read_bits_at(
    bits: np.ndarray,
    start: int,
    count: int,
    n_per_sym: int,
) -> int:
    """Pack `count` mid-symbol bits into an int, MSB-first."""
    acc = 0
    mid_idx = n_per_sym // 2
    for k in range(count):
        b = bits[start + k * n_per_sym + mid_idx]
        acc = (acc << 1) | int(b)
    return acc


def _pack_bits(
    bits: np.ndarray,
    start: int,
    count: int,
    n_per_sym: int,
) -> bytes:
    """Pack `count` mid-symbol bits into bytes, MSB-first."""
    out = bytearray((count + 7) // 8)
    mid_idx = n_per_sym // 2
    acc = 0
    for k in range(count):
        b = bits[start + k * n_per_sym + mid_idx]
        acc = (acc << 1) | int(b)
        if (k + 1) % 8 == 0:
            out[k // 8] = acc & 0xFF
            acc = 0
    # Final partial byte (if count % 8 != 0): left-pad with zeros.
    if count % 8 != 0:
        out[count // 8] = (acc << (8 - (count % 8))) & 0xFF
    return bytes(out)
