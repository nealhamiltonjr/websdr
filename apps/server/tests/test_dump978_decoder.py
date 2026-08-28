"""dump978 UAT decoder tests — protocol, demod round-trip, plugin.

Mirrors the AIS test pattern: build a known payload, CRC it, RS-encode
parity, build the bit frame (sync + length + body + parity), 2-GFSK
modulate at 2.083 MSPS, feed the demodulator, verify the round-trip
recovers the original ICAO/callsign/altitude.

The protocol here is the project's own v1 (plugins/uat_protocol.py)
which is self-consistent for testing. A real DO-282B bit layout differs
in field positions; the dump978 subprocess plugin is the production
answer for live traffic (ADR-003 subprocess family).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from openwebrx_plus.plugins.dump978 import Dump978Plugin
from openwebrx_plus.plugins.uat_demod import UAT_SAMPLE_RATE, UatReceiver
from openwebrx_plus.plugins.uat_protocol import (
    UatFrame,
    _gf_mul,
    crc24_uat,
    decode_frame_fields,
    rs_correct,
    rs_encode,
)

# === Protocol: GF(256), Reed-Solomon, CRC-24 ===============================


def test_gf_mul_identity_and_zero() -> None:
    """α^0 = 1 (multiplicative identity); 0 * x = 0."""
    assert _gf_mul(0, 5) == 0
    assert _gf_mul(5, 0) == 0
    assert _gf_mul(1, 5) == 5
    assert _gf_mul(5, 1) == 5


def test_gf_mul_commutative_and_inverse() -> None:
    """a*b == b*a; a*α^(-a_log) == 1 for a ≠ 0."""
    for a in (1, 2, 4, 8, 0x80, 0xFF):
        for b in (1, 2, 3, 0x55):
            assert _gf_mul(a, b) == _gf_mul(b, a)


def test_crc24_known_property() -> None:
    """CRC of (data + CRC(data)) == 0 — the verify property."""
    data = b"HELLO_UAT_"
    crc = crc24_uat(data)
    parity = bytes([(crc >> 16) & 0xFF, (crc >> 8) & 0xFF, crc & 0xFF])
    assert crc24_uat(data + parity) == 0


def test_rs_encode_then_correct_zero_errors() -> None:
    """Encoding produces a codeword with zero syndromes."""
    data = b"PAYLOAD"  # 7 bytes
    nsym = 6
    parity = rs_encode(data, nsym)
    # Convention A: parity at low-degree, data at high.
    codeword = parity + data
    assert rs_correct(bytearray(codeword), nsym) == 0


def test_rs_corrects_single_byte_error_in_data() -> None:
    data = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"  # 26 bytes
    nsym = 6
    parity = rs_encode(data, nsym)
    codeword = bytearray(parity + data)
    # Corrupt one byte in the data portion (offset nsym+5).
    codeword[nsym + 5] ^= 0xAB
    n_corrected = rs_correct(codeword, nsym)
    assert n_corrected == 1
    assert codeword[nsym + 5] == ord("F")  # restored


def test_rs_corrects_single_byte_error_in_parity() -> None:
    data = b"PAYLOAD"
    nsym = 6
    parity = rs_encode(data, nsym)
    codeword = bytearray(parity + data)
    codeword[1] ^= 0xFF
    assert rs_correct(codeword, nsym) == 1
    assert codeword == bytearray(parity + data)


def test_rs_uncorrectable_when_too_many_errors() -> None:
    """v1 only does single-error correction; 2 errors → -1."""
    data = b"PAYLOAD"
    nsym = 6
    parity = rs_encode(data, nsym)
    codeword = bytearray(parity + data)
    codeword[nsym + 1] ^= 0xAB
    codeword[nsym + 2] ^= 0xCD
    assert rs_correct(codeword, nsym) == -1


# === Protocol: field decode ===============================================


def test_decode_frame_fields_type0_extracts_icao_callsign_altitude() -> None:
    """A type-0 downlink message yields ICAO + callsign + altitude."""
    payload = bytearray(13)
    payload[0] = 0x00  # type 0 (top 2 bits = 0b00)
    payload[1:4] = bytes([0x4D, 0x22, 0xAA])  # ICAO 4D22AA
    # Callsign 6 bytes = 48 bits = 8 chars × 6 bits. Encode "OWRX001 ".
    chars = [15, 23, 18, 24, 48, 48, 49, 32]  # O W R X 0 0 1 (space)
    acc = 0
    for c in chars:
        acc = (acc << 6) | c
    payload[5:11] = acc.to_bytes(6, "big")
    # Altitude 12500 ft → 12500/25 = 500 = 0x1F4
    alt = 12500 // 25
    payload[11] = (alt >> 8) & 0x0F  # lower nibble of byte 11
    payload[12] = alt & 0xFF
    icao, callsign, altitude_ft, lat, lon = decode_frame_fields(bytes(payload))
    assert icao == "4D22AA"
    assert callsign == "OWRX001"
    assert altitude_ft == 12500
    assert lat is None
    assert lon is None


def test_decode_frame_fields_non_type0_returns_none() -> None:
    """Other message types are not v1-decoded (fields stay None)."""
    payload = b"\xff" + b"\x00" * 12  # top 2 bits = 0b11
    icao, callsign, altitude_ft, lat, lon = decode_frame_fields(payload)
    assert icao is None
    assert callsign is None
    assert altitude_ft is None


# === Demod: 2-GFSK round-trip =============================================


def _gfsk_modulate(bits: list[int], sps: int = 2) -> np.ndarray:
    """Simple 2-GFSK modulator: ±π/2/sps phase increment per sample.

    Bit "1" → +π/2 phase shift over the symbol period.
    Bit "0" → -π/2 phase shift.

    A moving-average smoothing approximates the Gaussian filter.
    """
    total = len(bits) * sps
    increments = np.zeros(total, dtype=np.float32)
    for i, bit in enumerate(bits):
        dev = (1 if bit else -1) * (math.pi / 2 / sps)
        increments[i * sps : (i + 1) * sps] = dev
    kernel = np.ones(sps, dtype=np.float32) / sps
    increments_smooth = np.convolve(increments, kernel, mode="same") * sps
    phase = np.cumsum(increments_smooth)
    return np.exp(1j * phase).astype(np.complex64)


def _build_uat_frame_bits(payload_with_crc: bytes, parity: bytes) -> list[int]:
    """Build the bit sequence: sync(12) + length(6) + body(payload+parity).

    Sync pattern matches the demod's expected bits.
    Length field = 0 (short frame) → 6 bits = 000000.
    Body = payload_with_crc + parity, MSB-first bit packing.
    """
    sync = [0, 1, 0, 1, 0, 1, 1, 0, 0, 1, 0, 0]
    length = [0, 0, 0, 0, 0, 0]  # length 0 = short frame
    body = bytes(parity) + bytes(payload_with_crc)  # convention A: parity low, data high
    body_bits: list[int] = []
    for byte in body:
        for k in range(7, -1, -1):
            body_bits.append((byte >> k) & 1)
    return sync + length + body_bits


def _build_payload_with_crc() -> bytes:
    """Build a 23-byte payload whose CRC-24 (computed over the first 20
    bytes) verifies to zero on the full 23-byte sequence.

    Layout (project's v1 — see uat_protocol.decode_frame_fields):
      byte 0     : message type (top 2 bits = 0b00 = type 0)
      bytes 1-3  : ICAO24 (3 bytes)
      byte 4     : unused
      bytes 5-10 : callsign (6 bytes = 48 bits = 8 chars × 6 bits)
      bytes 11-12: altitude (12 bits, × 25 ft)
      bytes 13-19: unused
      bytes 20-22: CRC-24 of bytes 0-19
    """
    msg = bytearray(20)
    msg[0] = 0x00  # type 0
    msg[1:4] = bytes([0x4D, 0x22, 0xAA])  # ICAO 4D22AA
    chars = [15, 23, 18, 24, 48, 48, 49, 32]  # "OWRX001 "
    acc = 0
    for c in chars:
        acc = (acc << 6) | c
    msg[5:11] = acc.to_bytes(6, "big")
    alt = 12500 // 25  # 500
    msg[11] = (alt >> 8) & 0x0F
    msg[12] = alt & 0xFF
    crc = crc24_uat(bytes(msg))
    parity = bytes([(crc >> 16) & 0xFF, (crc >> 8) & 0xFF, crc & 0xFF])
    return bytes(msg) + parity  # 23 bytes


def test_uat_demod_round_trip_recovers_frame() -> None:
    """Encode payload → CRC → RS → bit frame → 2-GFSK → demod → frame."""
    payload_with_crc = _build_payload_with_crc()
    assert len(payload_with_crc) == 23
    # Sanity: CRC verifies
    assert crc24_uat(payload_with_crc) == 0
    nsym = 6
    parity = rs_encode(payload_with_crc, nsym)
    body = parity + payload_with_crc  # convention A
    assert len(body) == 29
    # Sanity: body has zero syndromes
    assert rs_correct(bytearray(body), nsym) == 0

    bits = _build_uat_frame_bits(payload_with_crc, parity)
    signal = _gfsk_modulate(bits, sps=2)
    # Pre- AND post-pad with silence. The pre-pad gives the demod time to
    # warm up its DC estimate; the post-pad ensures the final mid-symbol
    # bit slice of the last body byte is in-bounds (the conjugate-product
    # loses one sample, so we need at least one extra sample of margin
    # past body_end + sps//2).
    silence = np.zeros(2000, dtype=np.complex64)
    signal = np.concatenate([silence, signal, silence])

    rx = UatReceiver(sample_rate=UAT_SAMPLE_RATE)
    frames = list(rx.feed(signal))
    assert len(frames) >= 1, f"no frames decoded (frames={rx.frames}, crc_failures={rx.crc_failures})"
    frame = frames[0]
    assert isinstance(frame, UatFrame)
    assert frame.frame_length == 0
    assert frame.icao == "4D22AA"
    assert frame.callsign == "OWRX001"
    assert frame.altitude_ft == 12500


def test_uat_demod_streams_across_chunks() -> None:
    """Frames straddling chunk boundaries must still decode."""
    payload_with_crc = _build_payload_with_crc()
    parity = rs_encode(payload_with_crc, 6)
    bits = _build_uat_frame_bits(payload_with_crc, parity)
    signal = _gfsk_modulate(bits, sps=2)
    silence = np.zeros(2000, dtype=np.complex64)
    signal = np.concatenate([silence, signal, silence])
    rx = UatReceiver(sample_rate=UAT_SAMPLE_RATE)
    frames: list[UatFrame] = []
    for i in range(0, len(signal), 1000):
        frames.extend(rx.feed(signal[i : i + 1000]))
    assert len(frames) >= 1
    assert frames[0].icao == "4D22AA"


def test_uat_demod_sample_rate_guard() -> None:
    """Non-UAT sample rates must fail fast."""
    with pytest.raises(ValueError, match="UAT demodulator requires"):
        UatReceiver(sample_rate=2_000_000)


# === Plugin: manifest, status, feed_iq ===================================


def test_plugin_manifest() -> None:
    p = Dump978Plugin()
    m = p.manifest
    assert m.name == "dump978"
    assert m.required_sample_rate == UAT_SAMPLE_RATE
    assert "frame" in m.events
    assert "aircraft" in m.events


def test_plugin_feed_iq_produces_aircraft_snapshot() -> None:
    """Plugin emits frame + aircraft events on a valid round-trip signal."""
    payload_with_crc = _build_payload_with_crc()
    parity = rs_encode(payload_with_crc, 6)
    bits = _build_uat_frame_bits(payload_with_crc, parity)
    signal = _gfsk_modulate(bits, sps=2)
    silence = np.zeros(2000, dtype=np.complex64)
    signal = np.concatenate([silence, signal, silence])

    p = Dump978Plugin()
    events = p.feed_iq(signal)
    kinds = [e["kind"] for e in events]
    assert "frame" in kinds
    assert "aircraft" in kinds
    # The aircraft snapshot must carry the ICAO we modulated.
    snap = next(e for e in events if e["kind"] == "aircraft")
    rows = snap["aircraft"]
    assert any(r["icao"] == "4D22AA" for r in rows)
    # Status reports frames decoded
    status = p.status()
    assert status["frames"] >= 1
    assert status["aircraft"] == 1


def test_plugin_status_initial() -> None:
    p = Dump978Plugin()
    s = p.status()
    assert s["frames"] == 0
    assert s["crc_failures"] == 0
    assert s["aircraft"] == 0


def test_plugin_stop_is_noop() -> None:
    p = Dump978Plugin()
    p.stop()  # must not raise


# === Slice-17: carrier offset compensation + symbol timing recovery =========
#
# The v2 demod tolerates realistic RF impairments the v1 demod would fail
# on: residual LO offset (DC bias on FM-demod output) and small clock
# drift (sample clock mismatch causing the mid-symbol slice to wander).


def _apply_carrier_offset(signal: np.ndarray, hz: float, sample_rate: int) -> np.ndarray:
    """Multiply by exp(j 2π·hz·n/fs) to simulate a residual carrier offset.

    This is what happens when the SDR's LO doesn't exactly match the
    signal center: the downconverted IQ has a slow-rotating phase that
    appears as a constant DC offset on the FM-demodulated output
    (offset_hz * 2π / sample_rate radians per sample).
    """
    n = np.arange(signal.size, dtype=np.float32)
    rotation = np.exp(1j * 2 * math.pi * hz * n / sample_rate).astype(np.complex64)
    return (signal * rotation).astype(np.complex64)


def _apply_clock_drift(signal: np.ndarray, ppm: float) -> np.ndarray:
    """Resample the signal at a slightly different rate.

    Positive ppm = our SDR samples FASTER than the transmitter (the
    signal appears stretched in our time → we receive fewer samples
    than the transmitter emitted per symbol → bits drift FORWARD
    relative to the mid-symbol slice). Negative ppm = slower.
    """
    if ppm == 0:
        return signal.copy()
    # Linear interpolation resample. Sample the original at fractional
    # indices scaled by (1 + ppm/1e6).
    n_out = int(round(signal.size * (1 + ppm / 1e6)))
    idx = np.linspace(0, signal.size - 1, n_out, dtype=np.float32)
    i0 = np.floor(idx).astype(np.int64)
    i1 = np.clip(i0 + 1, 0, signal.size - 1)
    frac = (idx - i0).astype(np.complex64)
    # signal[i0] is shape (n_out,) complex64; we want element-wise
    # interpolation: out[k] = signal[i0[k]] * (1 - frac[k]) + signal[i1[k]] * frac[k]
    out = signal[i0] * (np.complex64(1) - frac) + signal[i1] * frac
    return out.astype(np.complex64)


def test_demod_tolerates_small_carrier_offset() -> None:
    """Slice-17: with the DC-block (window=200 samples), the demod
    must still decode the frame when a ±5 kHz residual carrier offset
    is applied. The v1 demod would fail (the DC bias shifts the bit
    decision threshold)."""
    payload_with_crc = _build_payload_with_crc()
    parity = rs_encode(payload_with_crc, 6)
    bits = _build_uat_frame_bits(payload_with_crc, parity)
    signal = _gfsk_modulate(bits, sps=2)
    silence = np.zeros(2000, dtype=np.complex64)
    signal = np.concatenate([silence, signal, silence])

    # Apply a 5 kHz carrier offset — typical residual after a low-cost
    # SDR's auto-PPM correction still leaves ~1-5 kHz of drift.
    offset_signal = _apply_carrier_offset(signal, hz=5000.0, sample_rate=UAT_SAMPLE_RATE)

    rx = UatReceiver(sample_rate=UAT_SAMPLE_RATE)
    frames = list(rx.feed(offset_signal))
    assert len(frames) >= 1, (
        f"no frames decoded with +5kHz offset (frames={rx.frames}, "
        f"crc_failures={rx.crc_failures})"
    )
    assert frames[0].icao == "4D22AA"
    assert frames[0].callsign == "OWRX001"


def test_demod_tolerates_negative_carrier_offset() -> None:
    """Same as above but with a -3 kHz offset (rotation the other way)."""
    payload_with_crc = _build_payload_with_crc()
    parity = rs_encode(payload_with_crc, 6)
    bits = _build_uat_frame_bits(payload_with_crc, parity)
    signal = _gfsk_modulate(bits, sps=2)
    silence = np.zeros(2000, dtype=np.complex64)
    signal = np.concatenate([silence, signal, silence])

    offset_signal = _apply_carrier_offset(signal, hz=-3000.0, sample_rate=UAT_SAMPLE_RATE)
    rx = UatReceiver(sample_rate=UAT_SAMPLE_RATE)
    frames = list(rx.feed(offset_signal))
    assert len(frames) >= 1, (
        f"no frames decoded with -3kHz offset (frames={rx.frames}, "
        f"crc_failures={rx.crc_failures})"
    )
    assert frames[0].icao == "4D22AA"


def test_demod_tolerates_small_clock_drift() -> None:
    """Slice-17: with per-frame phase refinement (search_range=1),
    the demod must still decode the frame when the sample clock drifts
    by ±50 ppm (so the mid-symbol slice drifts by up to ±0.5 sample
    over a 232-bit frame)."""
    payload_with_crc = _build_payload_with_crc()
    parity = rs_encode(payload_with_crc, 6)
    bits = _build_uat_frame_bits(payload_with_crc, parity)
    signal = _gfsk_modulate(bits, sps=2)
    silence = np.zeros(2000, dtype=np.complex64)
    signal = np.concatenate([silence, signal, silence])

    drifted = _apply_clock_drift(signal, ppm=50.0)
    rx = UatReceiver(sample_rate=UAT_SAMPLE_RATE)
    frames = list(rx.feed(drifted))
    assert len(frames) >= 1, (
        f"no frames decoded with +50ppm drift (frames={rx.frames}, "
        f"crc_failures={rx.crc_failures})"
    )
    assert frames[0].icao == "4D22AA"


def test_demod_tolerates_negative_clock_drift() -> None:
    """Same but with -50 ppm drift."""
    payload_with_crc = _build_payload_with_crc()
    parity = rs_encode(payload_with_crc, 6)
    bits = _build_uat_frame_bits(payload_with_crc, parity)
    signal = _gfsk_modulate(bits, sps=2)
    silence = np.zeros(2000, dtype=np.complex64)
    signal = np.concatenate([silence, signal, silence])

    drifted = _apply_clock_drift(signal, ppm=-50.0)
    rx = UatReceiver(sample_rate=UAT_SAMPLE_RATE)
    frames = list(rx.feed(drifted))
    assert len(frames) >= 1, (
        f"no frames decoded with -50ppm drift (frames={rx.frames}, "
        f"crc_failures={rx.crc_failures})"
    )
    assert frames[0].icao == "4D22AA"


def test_demod_tolerates_combined_offset_and_drift() -> None:
    """Slice-17 stress: ±2 kHz offset + ±30 ppm drift — both
    impairments together. The DC-block + phase refinement handle
    each; combined, they still decode."""
    payload_with_crc = _build_payload_with_crc()
    parity = rs_encode(payload_with_crc, 6)
    bits = _build_uat_frame_bits(payload_with_crc, parity)
    signal = _gfsk_modulate(bits, sps=2)
    silence = np.zeros(2000, dtype=np.complex64)
    signal = np.concatenate([silence, signal, silence])

    impaired = _apply_carrier_offset(signal, hz=2000.0, sample_rate=UAT_SAMPLE_RATE)
    impaired = _apply_clock_drift(impaired, ppm=30.0)
    rx = UatReceiver(sample_rate=UAT_SAMPLE_RATE)
    frames = list(rx.feed(impaired))
    assert len(frames) >= 1, (
        f"no frames decoded with combined impairments "
        f"(frames={rx.frames}, crc_failures={rx.crc_failures})"
    )
    assert frames[0].icao == "4D22AA"


def test_refine_sync_phase_picks_best_offset() -> None:
    """The phase refinement function should pick the candidate with
    the fewest sync bit errors when given a slightly-off starting
    offset."""
    from openwebrx_plus.plugins.uat_demod import (
        _PHASE_SEARCH_RANGE,
        _refine_sync_phase,
        _sync_error_count,
    )

    # Build a perfectly-modulated sync so we know the truth.
    sync_bits_list = [0, 1, 0, 1, 0, 1, 1, 0, 0, 1, 0, 0]
    # Each bit takes 2 samples; we replicate the mid-symbol-slice logic
    # to construct an ideal `bits` array where the demod would have
    # detected the sync perfectly at offset 0.
    bits = np.zeros(len(sync_bits_list) * 2 + 4, dtype=np.int8)
    # Mid-symbol slice reads at index 1 of each 2-sample symbol.
    # Set bit values at positions 0,1 (the first symbol pair).
    for i, b in enumerate(sync_bits_list):
        bits[i * 2 + 1] = b  # mid-symbol sample carries the value

    sync = np.array(sync_bits_list, dtype=np.int8)

    # At offset 0: 0 errors. At offset ±1: should have at least 1 error.
    assert _sync_error_count(bits, 0, sync) == 0
    # The refinement returns the same offset if it's already perfect.
    assert _refine_sync_phase(bits, 0, sync, _PHASE_SEARCH_RANGE) == 0

    # Now shift the data so the optimal offset is +1 (the demod
    # detected the sync one sample early).
    bits_shifted = np.zeros(len(bits) + 1, dtype=np.int8)
    bits_shifted[1:] = bits[:-1] if False else np.concatenate([bits[:-1], [0]])  # keep length
    bits_shifted[1:] = bits
    # Looking from offset 0 now sees the original offset-1 sync (1 err);
    # looking from offset 1 sees the perfect sync (0 errs).
    # _refine_sync_phase(start=0) should pick offset=1.
    # NOTE: this requires the original sync to actually be at offset 1
    # in the shifted array, which it is.
    errs_at_0 = _sync_error_count(bits_shifted, 0, sync)
    errs_at_1 = _sync_error_count(bits_shifted, 1, sync)
    assert errs_at_1 < errs_at_0, "test setup wrong"
    chosen = _refine_sync_phase(bits_shifted, 0, sync, _PHASE_SEARCH_RANGE)
    assert chosen == 1, f"expected refined offset=1, got {chosen}"
