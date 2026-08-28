"""FT8 protocol layer — CRC-14 + message unpack (slice-26 v1).

Implements the message-level protocol:

  - CRC-14 over the 77-bit message payload (used to verify decode).
  - Message unpack: 77 bits → (callsign1, callsign2, grid_or_report, type).

Slice-26 v1 simplifications (documented for future improvement):
  - **No LDPC syndrome check**: v1 just verifies CRC-14. Random bits
    have ~1/16384 chance of CRC passing → occasional false positives.
    Honest for v1; actual LDPC error correction lands in v2.
  - **Limited message type coverage**: only i3=0 (standard) is decoded.
    ARRL RTTY RU / Field Day / POTA / contests / WW ROAG (i3=1..5)
    land in v2.
  - **Limited grid field decoding**: standard 4-char Maidenhead grids
    + signal reports (-25..+05 range) + the special markers
    (RRR, RR73, 73). Other grid encodings land in v2.

The 77-bit FT8 message payload layout (per pack77.f90):

  Bit 0-27:  callsign #1 (28-bit packed, base-48 alphabet for 6 chars)
  Bit 28-55: callsign #2 (28-bit)
  Bit 56-70: grid locator OR signal report (15-bit)
  Bit 71-73: i3 type indicator (3-bit)
  Bit 74-76: reserved/padding (3-bit)

The 14-bit CRC is computed over the 77-bit message (CRC-14 with the
FT8 polynomial 0x2757 / 0x6E55 depending on direction — WSJT-X uses
0x2757 reflected).

The full 174-bit LDPC codeword is:
  Bit 0-76:  77-bit message payload
  Bit 77-90: 14-bit CRC (over the 77-bit message)
  Bit 91-173: 83 LDPC parity bits

For v1, the decoder skips the LDPC syndrome check and just verifies
CRC-14. If CRC passes, the 77-bit message is unpacked.
"""

from __future__ import annotations

from dataclasses import dataclass

# FT8 message structure constants.
FT8_PAYLOAD_BITS = 77
FT8_CRC_BITS = 14
FT8_LDPC_PARITY_BITS = 83
FT8_CODEWORD_BITS = FT8_PAYLOAD_BITS + FT8_CRC_BITS + FT8_LDPC_PARITY_BITS  # 174

# FT8 CRC-14 polynomial (per WSJT-X — reflected form, 0x6E55 = 0x2757 reversed).
_CRC14_POLY = 0x2757  # forward direction

# The 40-char FT8 callsign alphabet (per WSJT-X pack77.f90, with the
# space char in position 36 and . / ? at 37/38/39). Each char value 0..39
# maps to one of these. The encoding is base-40 (char1 * 40^5 + char2 * 40^4 + ...).
_FT8_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ ./?"
assert len(_FT8_ALPHABET) == 40


@dataclass
class FT8Message:
    """One decoded FT8 message."""

    callsign1: str  # e.g., "K1ABC"
    callsign2: str  # e.g., "KO51" (could be a callsign too)
    grid_or_report: str  # e.g., "-12" or "KO51" or "RRR" or "73"
    i3_type: int  # 0..5 — only i3=0 fully decoded in v1
    raw_text: str  # the rendered message text, e.g., "K1ABC KO51 -12"
    crc_ok: bool  # True iff the embedded CRC matched the computed


def crc14(bits: list[int] | bytes | bytearray, length_bits: int) -> int:
    """Compute FT8 CRC-14 over the first ``length_bits`` of ``bits``.

    Args:
        bits: iterable of 0/1 values (or a bytes object where each byte is 0/1).
        length_bits: number of bits to consume (must be <= len(bits)*8 if bytes).

    Returns:
        The 14-bit CRC value (0..16383).
    """
    # Initialize the CRC register to 0.
    crc = 0
    for i in range(length_bits):
        # Extract the i-th bit.
        bit = (bits[i] >> 0) & 1 if isinstance(bits, (bytes, bytearray)) else int(bits[i]) & 1
        # XOR with the MSB of the current CRC.
        msb = (crc >> 13) & 1
        crc = ((crc << 1) & 0x3FFF) | bit
        if msb:
            crc ^= _CRC14_POLY
    return crc & 0x3FFF


def unpack_callsign(packed28: int) -> str:
    """Unpack a 28-bit callsign (base-40 alphabet, 5 chars).

    The bit at 0x8000000 (134M) is the non-standard flag; v1 returns
    "<nonstd>" for those. Standard callsigns pack 5 base-40 chars
    (max 40^5 = 102M, fits in 28 bits with the flag bit clear).
    """
    if packed28 >= 0x8000000:
        # Special / non-standard callsign (base-38 alphabet for 5 chars).
        # v1 doesn't decode the base-38 alphabet — just flag as non-standard.
        return "<nonstd>"
    # Decode base-40 → 5 chars.
    chars = []
    val = packed28
    for _ in range(5):
        chars.append(_FT8_ALPHABET[val % 40])
        val //= 40
    chars.reverse()  # MSB first
    s = "".join(chars).rstrip()  # trim trailing spaces (alphabet value 36)
    return s if s else "0"


def pack_callsign(callsign: str) -> int:
    """Pack a callsign to 28 bits (base-40 alphabet, 5 chars).

    Raises ValueError if the callsign contains chars outside the FT8 alphabet
    or is longer than 5 chars. Short callsigns are padded with trailing
    spaces (alphabet value 36). v1 supports up to 5-char callsigns;
    6-char callsigns (which use a special 6-char alphabet in WSJT-X)
    land in v2.
    """
    if len(callsign) > 5:
        raise ValueError(
            f"callsign too long (max 5 chars in v1 — 6-char callsigns land "
            f"in v2): {callsign!r}"
        )
    val = 0
    for ch in callsign:
        idx = _FT8_ALPHABET.find(ch)
        if idx < 0:
            raise ValueError(
                f"callsign {callsign!r} contains char {ch!r} not in FT8 alphabet "
                f"({_FT8_ALPHABET})"
            )
        val = val * 40 + idx
    # Pad with trailing spaces (alphabet value 36) for short callsigns.
    for _ in range(5 - len(callsign)):
        val = val * 40 + 36  # space
    if val >= 0x8000000:
        raise ValueError(f"callsign {callsign!r} packs to {val:#x} (>= 0x8000000)")
    return val


def unpack_grid_or_report(grid15: int) -> str:
    """Unpack the 15-bit grid/report field.

    Encoding (v1 simplified — see pack77.f90 for the full spec):
      - 0: "..." (no grid)
      - 0x7FFD: "RRR"
      - 0x7FFE: "RR73"
      - 0x7FFF: "73"
      - 1..50: signal report (1 → -25, 26 → 0, 50 → +24)
      - 0x8000..0x807FFC: 4-char Maidenhead grid (lower 14 bits split
        into chars_idx*100 + digits → 2 letters + 2 digits)
    """
    # Special markers.
    if grid15 == 0:
        return "..."
    if grid15 == 0x7FFF:
        return "73"
    if grid15 == 0x7FFE:
        return "RR73"
    if grid15 == 0x7FFD:
        return "RRR"
    # Signal reports: 1 → -25, 50 → +24 (50 values, fits in 6 bits).
    if 1 <= grid15 <= 50:
        report = grid15 - 26  # 1-26=-25, 50-26=24
        return f"{report:+03d}"
    # 4-char Maidenhead grid (e.g., KO51).
    if grid15 >= 0x8000:
        half = grid15 & 0x7FFF
        chars_idx = half // 100
        digits = half % 100
        c1 = chars_idx // 18
        c2 = chars_idx % 18
        if 0 <= c1 < 18 and 0 <= c2 < 18:
            return chr(ord("A") + c1) + chr(ord("A") + c2) + f"{digits:02d}"
        return f"?{grid15}"
    return f"?{grid15}"


def pack_grid_or_report(s: str) -> int:
    """Inverse of unpack_grid_or_report — for synthetic test frames.

    v1 supports standard forms only (4-char grid, signal report -25..+24,
    and the special markers RRR / RR73 / 73 / ...). Other forms raise
    ValueError.
    """
    if s == "...":
        return 0
    if s == "73":
        return 0x7FFF
    if s == "RR73":
        return 0x7FFE
    if s == "RRR":
        return 0x7FFD
    # 4-char grid (e.g., KO51).
    if len(s) == 4 and s[0].isalpha() and s[1].isalpha() and s[2:4].isdigit():
        c1 = ord(s[0]) - ord("A")
        c2 = ord(s[1]) - ord("A")
        digits = int(s[2:4])
        if 0 <= c1 < 18 and 0 <= c2 < 18 and 0 <= digits < 100:
            chars_idx = c1 * 18 + c2
            return 0x8000 + chars_idx * 100 + digits
    # Signal report (e.g., -25, +05). v1 supports -25..+24.
    if (s.startswith("-") or s.startswith("+")) and s[1:].isdigit():
        report = int(s)
        if -25 <= report <= 24:
            return report + 26  # -25 → 1, +24 → 50
    raise ValueError(
        f"cannot pack grid/report {s!r} — v1 only supports standard forms "
        f"(4-char grid AA00-RR99, signal report -25..+24, "
        f"RRR/RR73/73/...)"
    )


def bits_to_int(bits: list[int] | bytes | bytearray, length: int) -> int:
    """Convert the first ``length`` bits (MSB first) to an int."""
    val = 0
    for i in range(length):
        bit = (bits[i] >> 0) & 1 if isinstance(bits, (bytes, bytearray)) else int(bits[i]) & 1
        val = (val << 1) | bit
    return val


def int_to_bits(val: int, length: int) -> list[int]:
    """Convert an int to ``length`` bits (MSB first)."""
    bits = []
    for i in range(length - 1, -1, -1):
        bits.append((val >> i) & 1)
    return bits


def unpack_message(message_bits: list[int]) -> FT8Message:
    """Unpack the 77-bit FT8 message into a structured FT8Message.

    Args:
        message_bits: 77 bits (MSB first), as a list of 0/1 ints.

    Returns:
        FT8Message with callsign1, callsign2, grid_or_report, i3_type, raw_text.

    Note: this does NOT verify CRC — the caller should compute CRC-14
    over these 77 bits and compare to the embedded CRC before trusting
    the unpacked values.
    """
    if len(message_bits) != FT8_PAYLOAD_BITS:
        raise ValueError(
            f"message must be {FT8_PAYLOAD_BITS} bits, got {len(message_bits)}"
        )
    cs1 = bits_to_int(message_bits[0:28], 28)
    cs2 = bits_to_int(message_bits[28:56], 28)
    grid = bits_to_int(message_bits[56:71], 15)
    i3 = bits_to_int(message_bits[71:74], 3)
    # Bits 74-76 reserved (3-bit).
    callsign1 = unpack_callsign(cs1)
    callsign2 = unpack_callsign(cs2)
    grid_str = unpack_grid_or_report(grid)
    # Render the message text — standard format is "CALL1 CALL2 GRID" or
    # "CALL1 CALL2 R-NN" or "CALL1 CALL2 73".
    if i3 == 0:
        raw_text = f"{callsign1} {callsign2} {grid_str}"
    else:
        raw_text = f"{callsign1} {callsign2} {grid_str} [i3={i3}]"
    return FT8Message(
        callsign1=callsign1,
        callsign2=callsign2,
        grid_or_report=grid_str,
        i3_type=i3,
        raw_text=raw_text,
        crc_ok=False,  # caller sets this after CRC verify
    )


def pack_message(callsign1: str, callsign2: str, grid_or_report: str, i3: int = 0) -> list[int]:
    """Pack a standard FT8 message into 77 bits (for synthetic test frames).

    Inverse of :func:`unpack_message`.
    """
    cs1 = pack_callsign(callsign1)
    cs2 = pack_callsign(callsign2)
    grid = pack_grid_or_report(grid_or_report)
    bits = (
        int_to_bits(cs1, 28)
        + int_to_bits(cs2, 28)
        + int_to_bits(grid, 15)
        + int_to_bits(i3, 3)
        + [0, 0, 0]  # reserved
    )
    assert len(bits) == FT8_PAYLOAD_BITS
    return bits


def verify_crc(codeword_bits: list[int]) -> tuple[bool, list[int]]:
    """Verify the 14-bit CRC at the end of the 91-bit systematic codeword.

    The codeword is [77 message bits | 14 CRC bits]. The CRC is computed
    over the 77 message bits using the FT8 polynomial, then compared to
    the embedded 14 bits.

    Args:
        codeword_bits: 91 bits (77 message + 14 CRC), MSB first.

    Returns:
        (crc_ok, message_bits) where crc_ok is True iff the embedded
        CRC matches the computed CRC, and message_bits is the first 77
        bits (for unpacking).
    """
    if len(codeword_bits) != FT8_PAYLOAD_BITS + FT8_CRC_BITS:
        raise ValueError(
            f"codeword must be {FT8_PAYLOAD_BITS + FT8_CRC_BITS} bits, "
            f"got {len(codeword_bits)}"
        )
    message_bits = list(codeword_bits[:FT8_PAYLOAD_BITS])
    embedded_crc = bits_to_int(codeword_bits[FT8_PAYLOAD_BITS:FT8_PAYLOAD_BITS + FT8_CRC_BITS], FT8_CRC_BITS)
    computed_crc = crc14(message_bits, FT8_PAYLOAD_BITS)
    return computed_crc == embedded_crc, message_bits


def add_crc(message_bits: list[int]) -> list[int]:
    """Append the 14-bit CRC to a 77-bit message → 91-bit systematic codeword.

    For synthetic test frames (the inverse of verify_crc).
    """
    if len(message_bits) != FT8_PAYLOAD_BITS:
        raise ValueError(f"message must be {FT8_PAYLOAD_BITS} bits, got {len(message_bits)}")
    crc = crc14(message_bits, FT8_PAYLOAD_BITS)
    return list(message_bits) + int_to_bits(crc, FT8_CRC_BITS)


def add_ldpc_parity(systematic_bits: list[int]) -> list[int]:
    """Append 83 LDPC parity bits → 174-bit LDPC codeword (synthetic test frames).

    v1: just pads with zeros (the parity is unused — we skip the LDPC
    syndrome check in v1; if a real LDPC decoder lands in v2, this stub
    will be replaced with the actual parity computation using the
    published H matrix).
    """
    if len(systematic_bits) != FT8_PAYLOAD_BITS + FT8_CRC_BITS:
        raise ValueError(
            f"systematic must be {FT8_PAYLOAD_BITS + FT8_CRC_BITS} bits, "
            f"got {len(systematic_bits)}"
        )
    return list(systematic_bits) + [0] * FT8_LDPC_PARITY_BITS
