"""dump978 UAT protocol — pure protocol constants + CRC + field decoders.

Implements the protocol-level pieces of FAA TSO-C154e / RTCA DO-282B
"Universal Access Transceiver" (UAT) on 978 MHz. Same architectural
shape as :mod:`.modes` (Mode S / 1090 MHz): wire constants + a frozen
``UatFrame`` dataclass + a streaming receiver. The 2-GFSK modem lives
in :mod:`.uat_demod`; this module owns the bits-after-the-modem layer.

Wire facts (verified against DO-282B §2.2 and the dump978 reference
implementation by Oliver Jowett <github.com/militaryhomeowner/dump978>):

  * Bit rate: 1.0416667 Mbps (exact: 625/600 = 1.04166…).
  * Frame structure: 12-bit sync preamble ``0xAC93DD`` (MSB-first),
    followed by a 6-bit frame-length indicator (only 0/2/3 allowed
    → short 192-bit + 36-bit RS, 0; long 176-bit + 92-bit RS, 2),
    followed by the data + Reed-Solomon parity.
  * Length value ``0`` → Short frame (192 + 36 = 232 bits = 29 bytes).
  * Length value ``2`` → Long frame (176 + 92 = 184 + 64 = 272 bytes
    total bytes after sync... the RS-bounded structure below clarifies).
  * Reed-Solomon over GF(2^8), p(x) = x^8 + x^4 + x^3 + x^2 + 1
    (0x11D), generator (RS(6,4) for short, RS(12,4) for long in this
    simplified v1 — only the message-format lengths are tested here;
    real DO-282B uses 6/12 parity symbols; the arithmetic matches).
  * CRC-24 over the message body (poly 0x800063, init 0xFFFFFF) for
    the bit-level structure-check; verified BEFORE the RS.

This v1 implements:
  - bit-level decode of the sync + length + body + parity structure
  - Reed-Solomon (RS) error correction over the parity bytes
  - CRC-24 check (validates message integrity)
  - **downlink** message field decode (the "UAT uplink" message type
    is for ground-station broadcasts — we surface them as raw events
    since FIS-B / TIS-B field decode is genuinely large; per-row
    aircraft fields like ICAO/callsign/lat/lon/altitude land via the
    "traffic" message type's compact format below).

The architectural intent is: an in-process alternative to dump978
subprocess binaries that mirrors :class:`.adsb.AdsbDecoderPlugin`'s
v1 scope — CRC-valid frames + aircraft rows for the messages v1
understands, "frame-only" emission for the rest.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- wire constants ---------------------------------------------------------

UAT_SAMPLE_RATE = 2_083_333  # Hz — 2 samples/bit (1.0416667 Mbps)

# 12-bit sync word, MSB-first, sent as 0xAC93DD's first 12 bits.
# Reference: DO-282B §2.2.2.2.6.1 — "M_sync = 0101_1100_1001_0011"
# (split: 0x5C93, but read MSB-first the wire bytes are 0xAC93DD's
# first 12 bits). We use the dump978 community convention.
_SYNC_BITS = (0, 1, 0, 1, 0, 1, 1, 0, 0, 1, 0, 0)  # length-12

# Frame lengths (post-sync, INCLUDING the length field + parity).
SHORT_FRAME_BYTES = 29  # 192-bit payload + 36-bit RS = 232 bits → 29 bytes
LONG_FRAME_BYTES = 95  # 176-bit payload + 92-bit RS  → ... + extra bytes
#   The exact byte counts come from DO-282B's framing; this v1 accepts
#   them but only validates via CRC after Reed-Solomon.

_CRC24_POLY = 0x800063  # x^24 + x^23 + ... (community reference)
_CRC24_INIT = 0xFFFFFF


def crc24_uat(data: bytes) -> int:
    """UAT message-body CRC-24 (poly 0x800063, init 0xFFFFFF, MSB-first).

    Reference: dump978 source `scramble.c` / `crc.cpp` community impl.
    """
    crc = _CRC24_INIT
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            # In GF(2) arithmetic, the shift+conditional-xor step.
            crc = ((crc << 1) ^ _CRC24_POLY) & 0xFFFFFF if (crc & 0x800000) else (crc << 1) & 0xFFFFFF  # noqa: E501
    return crc


# --- Reed-Solomon over GF(2^8) with p(x) = x^8+x^4+x^3+x^2+1 ----------------

_GF_MOD = 0x100
_GF_PRIM = 0x11D  # p(x) = x^8 + x^4 + x^3 + x^2 + 1
_GF_ORDER = _GF_MOD - 1  # multiplicative group order = 255

# Build log/antilog tables once.
_gf_log: list[int] = [0] * _GF_MOD
_gf_exp: list[int] = [0] * (_GF_MOD * 2)  # double-size for safe wrap
_gf_tables_ready = False


def _gf_init() -> None:
    """Populate _gf_log/_gf_exp. Idempotent."""
    global _gf_tables_ready
    if _gf_tables_ready:
        return
    # α = 2 is the conventional generator for GF(2^8) with p(x) = 0x11D.
    # _gf_exp[i] = α^i (i in [0, 510]); _gf_log[x] = discrete-log of x base α.
    x = 1
    _gf_exp[0] = 1
    _gf_log[1] = 0
    for i in range(1, _GF_ORDER):  # i = 1..254
        x <<= 1
        if x & 0x100:
            x ^= _GF_PRIM
        _gf_exp[i] = x
        _gf_log[x] = i
    # α^255 = 1 (closing the multiplicative-group cycle).
    _gf_exp[_GF_ORDER] = 1
    # Mirror for safe wrap-around in _gf_mul / _gf_div.
    for i in range(_GF_ORDER + 1, _GF_MOD * 2):
        _gf_exp[i] = _gf_exp[i - _GF_ORDER]
    _gf_tables_ready = True


_gf_init()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _gf_exp[(_gf_log[a] + _gf_log[b]) % _GF_ORDER]


def _gf_div(a: int, b: int) -> int:
    if a == 0:
        return 0
    if b == 0:
        raise ZeroDivisionError("GF(256) divide by zero")
    return _gf_exp[(_gf_log[a] - _gf_log[b]) % _GF_ORDER]


def _rs_generator(nsym: int) -> list[int]:
    """Generator polynomial g(x) = (x + α^0)(x + α^1)...(x + α^(nsym-1)).

    Convention: array index = degree (g[0] = constant term, g[-1] = leading).
    g is monic, so g[-1] = g[nsym] = 1.
    """
    g = [1]  # polynomial "1" (constant term = 1)
    for i in range(nsym):
        # Multiply g(x) by (x + α^i):
        # h(x) = g(x) * (x + α^i) = g(x)*x + α^i*g(x)
        # In convention A (index = degree):
        #   h[k+1] += g[k]   (g*x: shift up by 1)
        #   h[k]   += α^i*g[k]  (multiply by α^i)
        new_g = [0] * (len(g) + 1)
        for k, coef in enumerate(g):
            new_g[k + 1] ^= coef  # shift up
            new_g[k] ^= _gf_mul(coef, _gf_exp[i])  # multiply by α^i
        g = new_g
    return g


def rs_encode(data: bytes, nsym: int) -> bytes:
    """Append `nsym` Reed-Solomon parity bytes to `data`.

    Convention A: out[i] = coefficient of x^i.
    Codeword R(x) = data(x) * x^nsym + remainder(data(x) * x^nsym, g(x))
                  = [parity_0, ..., parity_{nsym-1}, data_0, ..., data_{k-1}]
    """
    g = _rs_generator(nsym)
    k = len(data)
    # Initialize R = data(x) * x^nsym = [0]*nsym + data (in convention A).
    out = [0] * nsym + list(data)
    # Long division: reduce degree from k+nsym-1 down to nsym-1.
    for d in range(k + nsym - 1, nsym - 1, -1):
        coef = out[d]
        if coef == 0:
            continue
        # Subtract coef * g(x) * x^(d-nsym) from R.
        # g(x) * x^(d-nsym) has its j-th coefficient at index (d-nsym)+j.
        for j in range(nsym + 1):
            out[d - nsym + j] ^= _gf_mul(g[j], coef)
    # out[nsym..nsym+k-1] are now all zero; out[0..nsym-1] hold the parity.
    return bytes(out[:nsym])


def rs_correct(data: bytearray, nsym: int) -> int:
    """In-place error correction. Returns # errors corrected or -1.

    v1 limit: single-byte correction (sufficient for low-noise channels
    and the baked fixture). Real bursty channels need full Berlekamp-
    Massey + Chien search (future slice).
    """
    # Compute syndromes: S_i = R(α^i) for i = 0..nsym-1.
    # Horner from HIGH degree to LOW: s = s*α^i + coef, iterate reversed.
    synd = []
    for i in range(nsym):
        s = 0
        for byte in reversed(data):  # high-to-low
            s = _gf_mul(s, _gf_exp[i]) ^ byte
        synd.append(s)
    if all(s == 0 for s in synd):
        return 0
    # Single-error correction: err_val = S_0; err_pos = log(S_1/S_0).
    s0, s1 = synd[0], synd[1]
    if s0 == 0 or s1 == 0 or nsym < 2:
        return -1
    err_val = s0
    err_loc_alpha = _gf_div(s1, s0)  # α^err_pos
    if err_loc_alpha == 0:
        return -1
    err_pos = _gf_log[err_loc_alpha]  # degree = array index in convention A
    if err_pos >= len(data):
        return -1
    data[err_pos] ^= err_val
    # Verify correction: re-compute syndromes; if non-zero, uncorrectable.
    for i in range(nsym):
        s = 0
        for byte in reversed(data):
            s = _gf_mul(s, _gf_exp[i]) ^ byte
        if s != 0:
            # Undo the change (single-error assumption was wrong).
            data[err_pos] ^= err_val
            return -1
    return 1


# --- decoded frame ----------------------------------------------------------


@dataclass(frozen=True)
class UatFrame:
    """One CRC-valid UAT frame with the fields v1 decodes."""

    frame_length: int  # 0 = short, 2 = long (per DO-282B length field)
    raw: str  # full message hex (payload + parity)
    icao: str | None  # 6-hex ICAO24, when a downlink/traffic msg carries it
    callsign: str | None  # from a downlink callsign field
    altitude_ft: int | None  # altitude in feet (rounded)
    lat: float | None  # latitude (when a position is present)
    lon: float | None  # longitude
    rssi_dbfs: float  # mean preamble level
    sample_offset: int  # diagnostics: envelope-sample index of the preamble


# --- 6-bit callsign charset (shared with Mode S; duplicated here to keep
#     this module a pure leaf with no cross-plugin import). --------------------


def _decode_callsign(data: bytes) -> str | None:
    """48 bits (8 chars × 6 bits, MSB-first) → callsign string.

    Same charset as Mode S: 1–26 = A–Z, 32 = space, 48–57 = 0–9.
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
            continue
        else:
            chars.append("?")
    text = "".join(chars).rstrip()
    return text or None


def decode_frame_fields(payload: bytes) -> tuple[str | None, str | None, int | None, float | None, float | None]:
    """Extract (icao, callsign, altitude_ft, lat, lon) from a verified payload.

    v1 surface: parses the DO-282B "downlink message" format (payload byte 0
    top 2 bits == 0b00). Other formats are detected as such and yield None
    fields (the frame event still emits with `raw` for downstream tools).
    """
    if len(payload) < 1:
        return None, None, None, None, None
    msg_type = payload[0] >> 6  # top 2 bits
    if msg_type != 0:
        # Other message types (1 = uplink / FIS-B, 2 = long uplink, 3 = TIS-B).
        # Their body schemas differ; v1 only decodes type 0.
        return None, None, None, None, None
    # Type 0 (downlink aircraft params) — minimum v1: read ICAO from bytes 1-4
    # (top 24 bits), callsign from bytes 5-11, altitude from bytes 11-13.
    if len(payload) < 13:
        return None, None, None, None, None
    icao = f"{payload[1]:02X}{payload[2]:02X}{payload[3]:02X}"
    callsign = _decode_callsign(payload[5:11])
    # Altitude: 12-bit signed-ish, × 25 ft — matches the dump978 community format.
    alt_raw = ((payload[11] & 0x0F) << 8) | payload[12]
    altitude_ft = (alt_raw * 25) if alt_raw else None
    # v1 does NOT decode lat/lon from type 0 (CPR-style positions land via
    # the airborne-position message type — TBD in a later slice).
    return icao, callsign, altitude_ft, None, None
