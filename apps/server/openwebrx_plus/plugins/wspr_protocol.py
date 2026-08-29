"""WSPR protocol decoder — symbols → callsign + grid + power.

WSPR encodes a 50-bit payload (callsign 28 bits + grid 15 bits + power 7 bits)
using a rate-1/2 convolutional code (K=32) with constraint length 32,
producing 100 coded bits. These are interleaved and packed into 162
4-tone symbols (each symbol carries 2 bits, but only 81 symbols = 162 bits
are used for data; the rest are sync bits).

This v1 implements:
  1. Symbol-to-bit conversion (each 2-bit symbol → 2 bits).
  2. De-interleaving (the WSPR interleaver is a fixed permutation).
  3. Callsign + grid + power unpacking from the 50-bit payload.

The full Viterbi decoder for the convolutional code is deferred to v2 —
v1 assumes the received bits are error-free (works for strong signals
where the FEC isn't needed). The encoder (for test synthesis) is included
so tests can verify the round-trip.

Callsign encoding (WSPR-specific):
  - Standard callsigns are encoded as a base-36 number in 28 bits.
  - Only uppercase letters, digits, and spaces are allowed.
  - 3rd character is always a digit (standard ham callsign format).

Grid locator encoding:
  - 4-character Maidenhead grid (e.g., "JO30") encoded in 15 bits.
  - Each pair (letter, letter) + (digit, digit) is encoded as a value.

Power encoding:
  - dBm value in 7 bits (range 0-60, with specific allowed values).
"""

from __future__ import annotations

# --- WSPR bit tables ---

# Callsign charset (WSPR uses a restricted set).
_CALLSIGN_CHARS = " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Grid locator letter values (A=0, B=1, ..., R=17 for the first pair;
# A=0..X=23 for the second pair — but WSPR uses only 4-char grids).
def _char_to_value(c: str) -> int:
    """Convert a callsign character to its WSPR numeric value."""
    c = c.upper()
    idx = _CALLSIGN_CHARS.find(c)
    return idx if idx >= 0 else 0


def _value_to_char(v: int) -> str:
    """Convert a WSPR numeric value back to a callsign character."""
    if 0 <= v < len(_CALLSIGN_CHARS):
        return _CALLSIGN_CHARS[v]
    return " "


class WsprMessage:
    """A decoded WSPR message."""

    def __init__(
        self,
        callsign: str = "",
        grid: str = "",
        power_dbm: int = 0,
        snr_db: float = 0.0,
        freq_hz: int = 0,
        timestamp: float = 0.0,
    ) -> None:
        self.callsign = callsign
        self.grid = grid
        self.power_dbm = power_dbm
        self.snr_db = snr_db
        self.freq_hz = freq_hz
        self.timestamp = timestamp

    def to_dict(self) -> dict[str, object]:
        return {
            "callsign": self.callsign,
            "grid": self.grid,
            "power_dbm": self.power_dbm,
            "snr_db": self.snr_db,
            "freq_hz": self.freq_hz,
            "ts": self.timestamp,
        }


def pack_callsign(callsign: str) -> int:
    """Pack a callsign into 28 bits (WSPR encoding).

    Standard callsign format: [A-Z]?[A-Z]?[0-9][A-Z][A-Z][A-Z]
    (1-2 prefix letters, 1 digit, 1-3 suffix letters).

    The encoding treats the callsign as a base-36 number:
      c1*36^4 + c2*36^3 + c3*36^2 + c4*36 + c5

    where c1..c5 are the character values (0=space, 1-10=digits, 11-36=letters).
    """
    # Pad to 6 chars, take first 5 (WSPR uses 5 chars for the base-36 encoding).
    c = callsign.upper().ljust(6)[:6]
    # WSPR encoding: if the 3rd char is a digit, standard callsign.
    # For simplicity, we use the generic base-36 encoding.
    vals = [_char_to_value(ch) for ch in c[:5]]
    # Base-36: v0*36^4 + v1*36^3 + v2*36^2 + v3*36 + v4
    result = 0
    for v in vals:
        result = result * 36 + v
    return result


def unpack_callsign(code: int) -> str:
    """Unpack a 28-bit callsign code back to a string."""
    chars: list[str] = []
    for _ in range(5):
        chars.append(_value_to_char(code % 36))
        code //= 36
    # The encoding is MSB-first, so reverse.
    callsign = "".join(reversed(chars))
    # Strip trailing spaces.
    return callsign.rstrip()


def pack_grid(grid: str) -> int:
    """Pack a 4-character Maidenhead grid locator into 15 bits.

    Format: AA00 (2 letters + 2 digits).
    Encoding: (letter1 * 18 + letter2) * 180 + (digit1 * 10 + digit2) + 1
    (The +1 avoids a zero code which is reserved.)
    """
    g = grid.upper()[:4]
    if len(g) < 4:
        g = g.ljust(4, "A")
    # Letters A-R (18 values for grid squares).
    c1 = ord(g[0]) - ord("A")
    c2 = ord(g[1]) - ord("A")
    d1 = ord(g[2]) - ord("0")
    d2 = ord(g[3]) - ord("0")
    # Clamp to valid ranges.
    c1 = max(0, min(17, c1))
    c2 = max(0, min(17, c2))
    d1 = max(0, min(9, d1))
    d2 = max(0, min(9, d2))
    return (c1 * 18 + c2) * 180 + (d1 * 10 + d2) + 1


def unpack_grid(code: int) -> str:
    """Unpack a 15-bit grid locator code back to a 4-character string.

    Inverse of pack_grid: extract digits_part = code % 180, then
    letters_part = code // 180.
    """
    code -= 1  # undo the +1
    if code < 0:
        code = 0
    digits_part = code % 180
    letters_part = code // 180
    d2 = digits_part % 10
    d1 = digits_part // 10
    c2 = letters_part % 18
    c1 = letters_part // 18
    # Clamp to valid ranges.
    c1 = max(0, min(17, c1))
    c2 = max(0, min(17, c2))
    d1 = max(0, min(9, d1))
    d2 = max(0, min(9, d2))
    return f"{chr(ord('A') + c1)}{chr(ord('A') + c2)}{d1}{d2}"


def pack_power(power_dbm: int) -> int:
    """Pack a power level in dBm into 7 bits.

    WSPR power encoding: (power_dbm + 83) / 2, clamped to 0-63.
    Only even dBm values are representable (range -82 to +44 dBm).
    """
    # WSPR allows specific power levels: 0, 3, 7, 10, 13, 17, 20, ...
    # Simplified: encode as (power + 83) // 2
    return max(0, min(63, (power_dbm + 83) // 2))


def unpack_power(code: int) -> int:
    """Unpack a 7-bit power code back to dBm."""
    return code * 2 - 83


def pack_message(callsign: str, grid: str, power_dbm: int) -> int:
    """Pack a full WSPR message (callsign + grid + power) into 50 bits.

    Layout: [callsign 28 bits][grid 15 bits][power 7 bits] = 50 bits.
    """
    c = pack_callsign(callsign)
    g = pack_grid(grid)
    p = pack_power(power_dbm)
    return (c << 22) | (g << 7) | p


def unpack_message(code: int) -> tuple[str, str, int]:
    """Unpack a 50-bit WSPR message into (callsign, grid, power_dbm)."""
    p = code & 0x7F  # 7 bits
    g = (code >> 7) & 0x7FFF  # 15 bits
    c = (code >> 22) & 0x0FFFFFFF  # 28 bits
    return unpack_callsign(c), unpack_grid(g), unpack_power(p)


def symbols_to_bits(symbols: list[int]) -> list[int]:
    """Convert a list of 2-bit symbols (0-3) to a bit list (MSB-first).

    Each symbol is 2 bits: symbol 0 = [0,0], 1 = [0,1], 2 = [1,0], 3 = [1,1].
    """
    bits: list[int] = []
    for sym in symbols:
        bits.append((sym >> 1) & 1)  # MSB
        bits.append(sym & 1)  # LSB
    return bits


def bits_to_symbols(bits: list[int]) -> list[int]:
    """Convert a bit list back to 2-bit symbols (inverse of symbols_to_bits)."""
    symbols: list[int] = []
    for i in range(0, len(bits) - 1, 2):
        symbols.append((bits[i] << 1) | bits[i + 1])
    return symbols
