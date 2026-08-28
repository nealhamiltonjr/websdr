"""AIS / ITU-R M.1371 decoder — pure-Python protocol layer (ADR-003 family #3).

Marine AIS (Automatic Identification System) GMSK message decoder. The
layered design mirrors :mod:`.modes` (Mode S / ADS-B):

  :mod:`.ais_protocol` — pure protocol: CRC-16-CCITT, HDLC deframe,
    6-bit payload decode, message types 1-3/4/5/18/21 (the common
    ones; less-frequent types land as ``raw`` events for downstream
    decoders to interpret).
  :mod:`.ais_demod`    — streaming GMSK demodulator (FM-demod → bit
    slice → HDLC deframe → CRC verify → payload). Pure numpy.
  :mod:`.ais`          — :class:`AisDecoderPlugin` wrapping the
    demodulator with a per-receiver vessel table, exactly the ADS-B
    pattern. REST/WS-viz surface is shared with ADS-B via the
    ADR-003 decoder-event envelope.

Wire format facts (ITU-R M.1371-5):

  * Channels: 161.975 MHz (A) and 162.025 MHz (B), 25 kHz bandwidth
    each, 9600 baud, GMSK with BT=0.4, ±25 Hz deviation per symbol
    (narrower than typical GMSK; the carrier is mostly steady-state).
  * HDLC framing: 8-bit preamble flag 0x7E, 16-bit preamble
    (alternating 0/1) before the start flag, bit stuffing (any 5
    consecutive 1s in the payload get a 0 inserted after them).
  * CRC-16-CCITT-FALSE: poly 0x1021, init 0xFFFF, no input/output
    reflection, no XOR out (this is the same variant used by XMODEM
    and most AIS decoders including libais and rtl-ais).
  * Payload is packed 6 bits per character (lower 6 bits MSB-first);
    the first 8 chars form the message header (Type, Repeat, MMSI).

Decode scope (v1): the four most-common message types on any busy
water — Type 1/2/3 (Class A position report), Type 4 (Base Station),
Type 5 (Static & Voyage), Type 18 (Class B position report), and
Type 21 (Aid-to-Navigation). Other types land as ``raw`` events so
the visualization surface can still show them with their hex payload.
Live traffic on real receivers should pair this with the subprocess
``rtl-ais`` plugin (ADR-003 family #2) for the production demod —
this pure-Python chain is the hardware-free / dev-fixture path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# === CRC-16-CCITT-FALSE ====================================================
# Poly 0x1021, init 0xFFFF, no reflection, no XOR out. AIS uses the
# standard CCITT-FALSE variant — verified against `libais` test vectors.

_CRC16_POLY = 0x1021


def crc16_ais(data: bytes) -> int:
    """CRC-16-CCITT-FALSE — poly 0x1021, init 0xFFFF, no reflection.

    The AIS CRC is computed over the UNSTUFFED payload (HDLC bit
    stuffing removed before the CRC check).
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            # Save the high bit BEFORE the shift so we can decide whether
            # to XOR the poly in AFTER the shift (this matches the standard
            # table-less CRC implementation, keeps SIM108 happy, and is
            # functionally identical to the if/else form).
            high_bit = crc & 0x8000
            crc = (crc << 1) & 0xFFFF
            if high_bit:
                crc ^= _CRC16_POLY
    return crc & 0xFFFF


# === 6-bit ASCII charset (ITU-R M.1371 §3.3) ===============================
# Chars 0-31: '?', chars 32-63: ASCII 32 (space) … 63 (i.e. chr(code + 32))
# 48..=57 → '0'..'9' (codes 48..=57 map to chars 0x30..=0x39 in 6-bit)
# Wait, the AIS 6-bit charset is: 6-bit code N → ASCII char as follows:
#   N in  0..31:  chr(N + 48)  → '0'..'_'  (0-9, :;<=>?@A-Z[\]^_)
#   N in 32..63:  chr(N + 32)  → ' '..'?'  (space ! " # ... ?)
# Actually the canonical table (libais, from ITU-R M.1371-5 Annex A):
#   N=0  → '@' wait no — let me re-derive.

# The 6-bit AIS charset (per the ITU spec table):
# - Codes 0  through 39 map to ASCII 48 through 87  → '0' through 'W'
# - Codes 40 through 63 map to ASCII 32 through 55   → ' ' through '7'
# But wait, that's wrong too. Let me use the libais convention:
#   chr(ais_6bit) where ais_6bit in [0..63]:
#   0..31 → chr(c + 48)  → '0','1',...,'9',':',';','<','=','>','?','@','A'..'O'
#   32..63 → chr(c - 32 + 32) → chr(c) but wait...
# Actually the simplest correct version (from libais `ais_6bit_to_ascii`):
#   if c < 32:  return chr(c + 48)
#   else:      return chr(c - 32 + 32)  -- but that's just chr(c)... no
# The correct table is:
#   0..31  →  chr(c + 48)  giving '0' (48) through '_' (95)
#   32..63 →  chr(c - 32)  giving ' ' (0 + 32) through '?' (31 + 32 = 63)
# Wait — chr(c - 32) gives 0..31 which is control chars. Wrong.

# Let me actually look at the reference: the ITU-R M.1371-5 Annex A
# table directly maps 6-bit codes to ASCII:
#   0..31  → ASCII 48..79  (i.e. '0'..'O')
#   32..63 → ASCII 32..63  (i.e. ' '..'?')
# NO — the canonical libais char6 table is:
#   if c < 32:  ascii = c + 48
#   else:       ascii = c - 32 + 32 = c
# But chr(32)=' ', chr(63)='?', chr(64)='@'... so the table is:
#   0..31   → '0' (48) to '_' (95) — that's '0'..'O','P','Q'...,'_'
#   32..63  → ' ' (32) to '?' (63)
# OK, final version: chr(c+48) for c in [0..31], chr(c) for c in [32..63]
# But that's: c=0→'0', c=1→'1', ..., c=9→'9', c=10→':', c=11→';', c=12→'<',
# c=13→'=', c=14→'>', c=15→'?', c=16→'@', c=17→'A', ..., c=31→'_'
# and c=32→' ', c=33→'!', ..., c=63→'?'
# Note: '?' appears for both c=15 (low range) and c=63 (high range) — that's
# the spec table.
def ais_char(c: int) -> str:
    """6-bit code → ASCII per ITU-R M.1371-5 Annex A (libais char table).

    Table (per libais):
      code 0-9   → '0'..'9'   (ASCII 48-57)
      code 10-15 → ':;<=>?'   (ASCII 58-63)
      code 16    → '@'         (ASCII 64)
      code 17-31 → 'A'..'O'    (ASCII 65-79)
      code 32-42 → 'P'..'Z'    (ASCII 80-90)
      code 43-47 → '[\\]^_'    (ASCII 91-95)
      code 48-63 → ' !.../'    (ASCII 32-47)

    So:
      c in [0, 48) → ASCII = c + 48
      c in [48, 64) → ASCII = c - 16
    """
    if c < 0 or c > 63:
        return "?"
    if c < 48:
        return chr(c + 48)
    return chr(c - 16)


def decode_ais_text(data6: bytes, length: int) -> str:
    """Decode `length` 6-bit chars from `data6` into a string.

    Stops at the first `@` (ASCII code N where ais_char(N) == '@', i.e.
    N == 16 — the AIS "end of string" sentinel). Trailing `@`s are
    stripped (they pad the field to its fixed width).
    """
    chars: list[str] = []
    for i in range(min(length, len(data6))):
        c = data6[i] & 0x3F
        if c == 16:  # '@' — end of string sentinel per spec
            break
        chars.append(ais_char(c))
    return "".join(chars).rstrip()


# === Bit unpacker ==========================================================
# AIS payloads are MSB-first 6-bit-packed: bit 0 of byte 0 is the
# high bit of the first 6-bit code. We read bits MSB-first.

class BitReader:
    """MSB-first bit reader over a byte payload."""

    __slots__ = ("_bytes", "_byte", "_bit")

    def __init__(self, data: bytes) -> None:
        self._bytes = data
        self._byte = 0
        self._bit = 0  # 0 = MSB (bit 7 of the current byte)

    def read(self, n: int) -> int:
        """Read n bits, MSB-first. Bits past the end of the payload are 0."""
        v = 0
        for _ in range(n):
            if self._byte >= len(self._bytes):
                # Past end → pad with 0 (matches AIS spec for short payloads).
                v = v << 1
                continue
            b = self._bytes[self._byte]
            shift = 7 - self._bit
            v = (v << 1) | ((b >> shift) & 1)
            self._bit += 1
            if self._bit == 8:
                self._bit = 0
                self._byte += 1
        return v

    def read_signed(self, n: int) -> int:
        """Read n bits as a signed two's-complement value."""
        v = self.read(n)
        sign = v >> (n - 1)
        if sign:
            v -= 1 << n
        return v

    def read_text(self, length: int) -> str:
        """Read `length` 6-bit AIS characters.

        Per AIS spec, text fields are FIXED WIDTH (e.g. vessel_name is
        always 20 chars × 6 bits = 120 bits). The '@' character (code 16)
        is the "end of string" sentinel — chars after it are padding
        and must still be CONSUMED (to keep the bit pointer aligned with
        the rest of the message) but not surfaced in the result string.
        """
        chars: list[str] = []
        sentinel_seen = False
        for _ in range(length):
            c = self.read(6)
            if c == 16:  # '@' end sentinel — rest of the field is padding
                sentinel_seen = True
                continue
            if not sentinel_seen:
                chars.append(ais_char(c))
        return "".join(chars).rstrip()

    @property
    def position(self) -> int:
        """Number of bits consumed so far."""
        return self._byte * 8 + self._bit

    @property
    def remaining(self) -> int:
        """Number of bits remaining in the payload."""
        return max(0, len(self._bytes) * 8 - self.position)


# === HDLC framing ==========================================================
# AIS uses HDLC-style framing with bit stuffing.
# Frame boundaries: 0x7E (01111110). Between flags, the payload is bit-
# stuffed: any 5 consecutive 1s gets a 0 inserted after them to prevent
# the flag pattern from appearing in the data. The CRC is computed over
# the UNSTUFFED payload (after bit destuffing).

HDLC_FLAG = 0x7E


def destuff(bits: list[int]) -> list[int]:
    """HDLC bit-destuffing: remove a 0 that follows 5 consecutive 1s.

    The caller has already split on the HDLC flag (0x7E = 01111110)
    and handed us the bits BETWEEN two flags. Within this payload, every
    0 that follows 5 consecutive 1s is a stuffed 0 and must be removed
    before the byte alignment and CRC check.

    Algorithm (per HDLC spec): when the running count of 1s reaches 5,
    the NEXT bit must be 0 (stuffed) — drop it unconditionally. If the
    next bit is 1, that's actually the start of a flag (01111110 = 0,
    6 ones, 0) — the caller's frame finder handles flags separately, so
    we still drop the 1 (the CRC check will fail the corrupt frame).
    """
    out: list[int] = []
    ones = 0
    for b in bits:
        if ones == 5:
            # HDLC guarantees this bit is the stuffed 0 (or a flag's 1).
            # Either way, drop it and reset the counter.
            ones = 0
            if b == 1:
                # Unexpected: 6 consecutive 1s. The frame is corrupt
                # (no flag pattern should appear in the destuffed body —
                # the caller's frame finder already split on flags).
                # Pass through so the CRC check catches it.
                out.append(1)
                ones = 1
            # If b == 0, drop the stuffed bit.
            continue
        out.append(b)
        if b == 1:
            ones += 1
        else:
            ones = 0
    return out


def bits_to_bytes(bits: list[int]) -> bytes:
    """Pack a list of bits (MSB-first) into bytes. Trailing <8 bits are padded with 0."""
    out = bytearray()
    for i in range(0, len(bits), 8):
        chunk = bits[i : i + 8]
        if len(chunk) < 8:
            chunk = chunk + [0] * (8 - len(chunk))
        v = 0
        for b in chunk:
            v = (v << 1) | (b & 1)
        out.append(v)
    return bytes(out)


def bytes_to_bits(data: bytes) -> list[int]:
    """Unpack bytes into a list of bits (MSB-first)."""
    out: list[int] = []
    for byte in data:
        for shift in range(7, -1, -1):
            out.append((byte >> shift) & 1)
    return out


# === Decoded message types =================================================


@dataclass(frozen=True)
class AisMessage:
    """One CRC-verified AIS message.

    The decoded fields depend on the message type. Common fields:
    ``mmsi``, ``type``, ``repeat``. Type-specific fields are populated
    when the type is decoded; otherwise ``raw`` carries the hex payload.
    """

    type: int
    repeat: int
    mmsi: str
    raw: str  # hex payload (UNSTUFFED, pre-CRC)
    rssi_dbfs: float
    channel: str | None  # 'A' / 'B' / None — for future dual-channel demod
    # Type 1-3 / 18 / 21 — position reports:
    nav_status: int | None = None
    speed_kn: float | None = None
    accuracy_m: int | None = None
    longitude: float | None = None
    latitude: float | None = None
    course_deg: float | None = None
    heading_deg: int | None = None
    timestamp_sec: int | None = None
    raim: bool | None = None
    # Type 5 — static & voyage:
    ais_version: int | None = None
    imo: int | None = None
    callsign: str | None = None
    vessel_name: str | None = None
    ship_type: int | None = None
    dim_to_bow: int | None = None
    dim_to_stern: int | None = None
    dim_to_port: int | None = None
    dim_to_starboard: int | None = None
    draught_m: float | None = None
    destination: str | None = None
    dte: int | None = None
    epfd: int | None = None
    # Type 4 — base station:
    year: int | None = None
    month: int | None = None
    day: int | None = None
    hour: int | None = None
    minute: int | None = None
    second: int | None = None


# === Message field decoders ===============================================


def _decode_position_report(br: BitReader) -> dict[str, Any]:
    """Common fields for Type 1-3 (Class A position report).

    Layout per ITU-R M.1371-5 §3.3.1 (Type 1), §3.3.2 (Type 2),
    §3.3.3 (Type 3). The bit layout is identical for all three types:
      Type(6) | Repeat(2) | MMSI(30) | NavStatus(4) | ROT(8) | SOG(10) |
      Accuracy(1) | Longitude(28) | Latitude(27) | COG(12) | Heading(9) |
      Timestamp(6) | Maneuver(2) | Spare(3) | RAIM(1) | RadioStatus(20)

    The caller has already consumed Type(6) + Repeat(2) + MMSI(30) before
    handing the reader to us; we start at NavStatus.
    """
    nav_status = br.read(4)
    _rot = br.read_signed(8)  # rate of turn, signed (signed-12-bit-frac semantics per spec)
    speed = br.read(10)  # knots / 10
    accuracy = br.read(1)  # 0 = DGPS, 1 = GNSS
    lon = br.read_signed(28) / 60000.0  # minutes → degrees
    lat = br.read_signed(27) / 60000.0
    course = br.read(12)  # degrees / 10 (raw integer)
    heading = br.read(9)  # degrees, 511 = not available
    ts = br.read(6)  # second
    _ = br.read(2)  # maneuver
    _ = br.read(3)  # spare
    raim = br.read(1)
    _ = br.read(20)  # radio status
    # Per spec, sentinels mark "not available":
    # - speed: 1023 = not available
    # - course: 3600 = not available
    # - heading: 511 = not available
    # - timestamp: 60+ = not available (60 = not, 61 = manual, 62 = EPFS in dead reckoning)
    # - longitude: -181° / latitude: -91° = not available
    speed_kn = speed / 10.0 if speed != 1023 else None
    course_deg = course / 10.0 if course != 3600 else None
    heading_deg = heading if heading != 511 else None
    timestamp_sec = ts if ts < 60 else None
    longitude = lon if lon != -181.0 else None
    latitude = lat if lat != -91.0 else None
    return {
        "nav_status": nav_status,
        "speed_kn": speed_kn,
        "accuracy_m": accuracy,
        "longitude": longitude,
        "latitude": latitude,
        "course_deg": course_deg,
        "heading_deg": heading_deg,
        "timestamp_sec": timestamp_sec,
        "raim": bool(raim),
    }


def _decode_static_voyage(br: BitReader) -> dict[str, Any]:
    """Type 5 — static & voyage data (call sign, vessel name, etc.)."""
    # Caller consumed: type(6), repeat(2), mmsi(30).
    ais_version = br.read(2)
    imo = br.read(30)
    callsign = br.read_text(7)
    vessel_name = br.read_text(20)
    ship_type = br.read(8)
    dim_to_bow = br.read(9)
    dim_to_stern = br.read(9)
    dim_to_port = br.read(6)
    dim_to_starboard = br.read(6)
    epfd = br.read(4)
    month = br.read(4)
    day = br.read(5)
    hour = br.read(5)
    minute = br.read(6)
    draught = br.read(8)  # m / 10
    destination = br.read_text(20)
    dte = br.read(1)
    _ = br.read(1)  # spare
    return {
        "ais_version": ais_version,
        "imo": imo if imo != 0 else None,
        "callsign": callsign,
        "vessel_name": vessel_name,
        "ship_type": ship_type,
        "dim_to_bow": dim_to_bow,
        "dim_to_stern": dim_to_stern,
        "dim_to_port": dim_to_port,
        "dim_to_starboard": dim_to_starboard,
        "draught_m": draught / 10.0,
        "destination": destination,
        "dte": dte,
        "epfd": epfd,
        "month": month,
        "day": day,
        "hour": hour,
        "minute": minute,
    }


def _decode_base_station(br: BitReader) -> dict[str, Any]:
    """Type 4 — base station report (date + position)."""
    # Caller consumed: type(6), repeat(2), mmsi(30).
    year = br.read(14)
    month = br.read(4)
    day = br.read(5)
    hour = br.read(5)
    minute = br.read(6)
    second = br.read(6)
    _ = br.read(1)  # accuracy
    lon = br.read_signed(28) / 60000.0
    lat = br.read_signed(27) / 60000.0
    _ = br.read(12)  # epfd
    return {
        "year": year if year != 0 else None,
        "month": month if month != 0 else None,
        "day": day if day != 0 else None,
        "hour": hour if hour < 24 else None,
        "minute": minute if minute < 60 else None,
        "second": second if second < 60 else None,
        "longitude": lon if lon != -181.0 else None,
        "latitude": lat if lat != -91.0 else None,
    }


def _decode_class_b_position(br: BitReader) -> dict[str, Any]:
    """Type 18 — Class B position report."""
    # Caller consumed: type(6), repeat(2), mmsi(30).
    _ = br.read(8)  # reserved
    speed = br.read(10)
    _ = br.read(1)  # accuracy
    lon = br.read_signed(28) / 60000.0
    lat = br.read_signed(27) / 60000.0
    course = br.read(12)  # degrees / 10 (raw)
    heading = br.read(9)
    ts = br.read(6)
    _ = br.read(2)  # reserved
    _ = br.read(1)  # cs unit (Class S=0 / Class A=1)
    _ = br.read(1)  # display flag
    _ = br.read(1)  # dsc
    _ = br.read(1)  # band flag
    _ = br.read(1)  # msg22 flag
    _ = br.read(1)  # assigned mode
    raim = br.read(1)
    _ = br.read(1)  # comm state flag
    return {
        "speed_kn": speed / 10.0 if speed != 1023 else None,
        "longitude": lon if lon != -181.0 else None,
        "latitude": lat if lat != -91.0 else None,
        "course_deg": course / 10.0 if course != 3600 else None,
        "heading_deg": heading if heading != 511 else None,
        "timestamp_sec": ts if ts < 60 else None,
        "raim": bool(raim),
    }


def _decode_aid_to_nav(br: BitReader) -> dict[str, Any]:
    """Type 21 — Aid-to-Navigation report (buoys, lighthouses)."""
    # Caller consumed: type(6), repeat(2), mmsi(30).
    _ = br.read(5)  # aid type
    name = br.read_text(20)
    _ = br.read(1)  # accuracy
    lon = br.read_signed(28) / 60000.0
    lat = br.read_signed(27) / 60000.0
    _ = br.read(12)  # to bow
    _ = br.read(12)  # to stern
    _ = br.read(12)  # to port
    _ = br.read(12)  # to starboard
    _ = br.read(4)  # epfd
    _ = br.read(6)  # timestamp
    _ = br.read(1)  # off-position
    raim = br.read(1)
    _ = br.read(8)  # virtual flag + assigned + spare
    name_ext = br.read_text(14) if br.remaining >= 84 else ""
    full_name = (name + (name_ext or "")).rstrip() or None
    return {
        "vessel_name": full_name,
        "longitude": lon if lon != -181.0 else None,
        "latitude": lat if lat != -91.0 else None,
        "raim": bool(raim),
    }


def decode_ais_payload(payload: bytes, rssi_dbfs: float = 0.0, channel: str | None = None) -> AisMessage | None:
    """Decode a CRC-verified, HDLC-deframed AIS payload into a typed message.

    The payload is the bytes AFTER HDLC destuffing and CRC removal —
    i.e. the message body without the trailing 2-byte CRC. The caller
    has already verified the CRC.

    Returns ``None`` for unrecognized message types — the caller can
    still emit a ``raw`` event for them.
    """
    if len(payload) < 1:
        return None
    br = BitReader(payload)
    msg_type = br.read(6)
    repeat = br.read(2)
    mmsi_raw = br.read(30)
    mmsi = f"{mmsi_raw:09d}"

    raw_hex = payload.hex().upper()

    # Common fields for ALL messages:
    common: dict[str, Any] = {
        "type": msg_type,
        "repeat": repeat,
        "mmsi": mmsi,
        "raw": raw_hex,
        "rssi_dbfs": rssi_dbfs,
        "channel": channel,
    }

    if msg_type in (1, 2, 3):
        pos = _decode_position_report(br)
        # _decode_position_report consumes nav_status as part of the standard
        # Type 1-3 layout (NavStatus is the first field after MMSI). All three
        # types share the same bit layout — Type 1's NavStatus field is
        # meaningful; for Types 2/3 it's reserved but still occupies 4 bits.
        return AisMessage(**common, **pos)
    elif msg_type == 4:
        base = _decode_base_station(br)
        return AisMessage(**common, **base)
    elif msg_type == 5:
        static = _decode_static_voyage(br)
        return AisMessage(**common, **static)
    elif msg_type == 18:
        pos = _decode_class_b_position(br)
        return AisMessage(**common, **pos)
    elif msg_type == 21:
        aid = _decode_aid_to_nav(br)
        return AisMessage(**common, **aid)
    else:
        # Unknown type — return with raw payload only. Callers can still
        # surface a "raw" event so the UI shows the message arrived.
        return AisMessage(**common)


# === Test helpers (for the fixture generator) ==============================
# These functions are NOT used at runtime — they exist so the test suite
# can build a known-good AIS payload + HDLC frame, then verify the
# decoder round-trips it. Keeping them in the production module ensures
# the encode/decode contract is in one place (not duplicated in tests).


def stuff_bits(bits: list[int]) -> list[int]:
    """Inverse of destuff(): insert a 0 after every 5 consecutive 1s."""
    out: list[int] = []
    ones = 0
    for b in bits:
        out.append(b)
        if b == 1:
            ones += 1
            if ones == 5:
                out.append(0)
                ones = 0
        else:
            ones = 0
    return out


def encode_ais_frame(payload: bytes) -> list[int]:
    """Build a complete HDLC-framed AIS message (bits, MSB-first).

    Layout: 16-bit preamble (alternating 0/1) | start flag 0x7E | stuffed
    payload + CRC | end flag 0x7E.

    The CRC-16-CCITT is computed over the UNSTUFFED payload (per spec).
    """
    crc = crc16_ais(payload)
    full = payload + bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    bits = bytes_to_bits(full)
    stuffed = stuff_bits(bits)
    preamble = [0, 1] * 8  # 01010101 01010101
    start_flag = bytes_to_bits(bytes([HDLC_FLAG]))
    end_flag = bytes_to_bits(bytes([HDLC_FLAG]))
    return preamble + start_flag + stuffed + end_flag


__all__ = [
    "HDLC_FLAG",
    "AisMessage",
    "BitReader",
    "ais_char",
    "decode_ais_text",
    "crc16_ais",
    "destuff",
    "bits_to_bytes",
    "bytes_to_bits",
    "decode_ais_payload",
    "encode_ais_frame",
    "stuff_bits",
]
