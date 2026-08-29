"""JT65 protocol decoder — symbols → callsign + grid + signal report.

JT65 encodes a 72-bit payload using Reed-Solomon (63,12) FEC, producing
63 6-bit symbols. The payload structure:

  * **Callsign 1**: 28 bits (packed base-36)
  * **Callsign 2**: 28 bits (packed base-36)
  * **Grid/report**: 15 bits (grid locator or signal report)
  * **Flags**: 1 bit

Total: 72 bits = 12 6-bit symbols. After RS encoding: 63 symbols. With
sync tones (every 4th symbol is a sync reference), the full transmission
is 126 symbols.

This v1 implements:
  1. Symbol extraction (strip sync tones — every 4th symbol).
  2. Callsign + grid unpacking from the 72-bit payload.
  3. The Reed-Solomon FEC is deferred to v2 — v1 assumes error-free symbols.

Callsign encoding uses the same base-36 scheme as WSPR (see wspr_protocol.py).
"""

from __future__ import annotations

from openwebrx_plus.plugins.wspr_protocol import (
    pack_callsign,
    unpack_callsign,
)

# JT65 sync tone positions (every 4th symbol, starting at index 2).
# The sync pattern tells the receiver where the data symbols are.
_SYNC_POSITIONS = {2, 6, 10, 14, 18, 22, 26, 30, 34, 38, 42, 46, 50, 54, 58, 62}


def strip_sync(symbols: list[int]) -> list[int]:
    """Strip the sync tones from a 126-symbol JT65 transmission.

    The sync tones are at positions 2, 6, 10, ... (every 4th, starting at 2).
    The remaining 63 symbols are the RS-coded data.
    """
    return [s for i, s in enumerate(symbols) if i not in _SYNC_POSITIONS]


def symbols_to_bits(symbols: list[int]) -> list[int]:
    """Convert 6-bit symbols to a bit list (MSB-first).

    Each symbol is 6 bits: bit 5 (MSB) down to bit 0 (LSB).
    """
    bits: list[int] = []
    for sym in symbols:
        for i in range(5, -1, -1):
            bits.append((sym >> i) & 1)
    return bits


def bits_to_symbols(bits: list[int]) -> list[int]:
    """Convert a bit list back to 6-bit symbols (inverse of symbols_to_bits)."""
    symbols: list[int] = []
    for i in range(0, len(bits) - 5, 6):
        sym = 0
        for j in range(6):
            sym = (sym << 1) | bits[i + j]
        symbols.append(sym)
    return symbols


def unpack_payload(bits: list[int]) -> tuple[str, str, str]:
    """Unpack the 72-bit JT65 payload into (callsign1, callsign2, grid/report).

    Layout: [callsign1 28 bits][callsign2 28 bits][grid/report 15 bits][flags 1 bit]
    """
    if len(bits) < 72:
        return "", "", ""
    # Extract the three fields.
    c1_bits = bits[:28]
    c2_bits = bits[28:56]
    gr_bits = bits[56:71]
    # Convert bits to integers (MSB-first).
    c1 = _bits_to_int(c1_bits)
    c2 = _bits_to_int(c2_bits)
    gr = _bits_to_int(gr_bits)
    # Unpack callsigns.
    callsign1 = unpack_callsign(c1)
    callsign2 = unpack_callsign(c2)
    # Unpack grid/report.
    grid_report = _unpack_grid_report(gr)
    return callsign1, callsign2, grid_report


def _bits_to_int(bits: list[int]) -> int:
    """Convert a list of bits (MSB-first) to an integer."""
    result = 0
    for b in bits:
        result = (result << 1) | b
    return result


def _unpack_grid_report(code: int) -> str:
    """Unpack a 15-bit grid/report code.

    JT65 grid encoding:
      - If code < 32768: it's a grid locator (same as WSPR: AA00 format).
      - If code >= 32768: it's a signal report (-1 to -30 dB, encoded as
        code - 32768 + 1, giving 1-30 → -1 to -30 dB).
    """
    if code < 32768:
        # Grid locator — reuse WSPR's unpack_grid.
        from openwebrx_plus.plugins.wspr_protocol import unpack_grid
        return unpack_grid(code)
    else:
        # Signal report.
        report = -(code - 32768 + 1)
        return f"{report} dB"


def pack_payload(callsign1: str, callsign2: str, grid_report: str) -> list[int]:
    """Pack a JT65 message into 72 bits (12 6-bit symbols).

    This is the inverse of unpack_payload — used for test synthesis.
    """
    c1 = pack_callsign(callsign1)
    c2 = pack_callsign(callsign2)
    # Encode grid/report.
    if grid_report.endswith("dB"):
        # Signal report: -N dB → code = 32768 + (N - 1)
        try:
            n = int(grid_report.replace("dB", "").strip())
            gr = 32768 + (-n - 1)
        except ValueError:
            gr = 0
    else:
        # Grid locator.
        from openwebrx_plus.plugins.wspr_protocol import pack_grid
        gr = pack_grid(grid_report)
    # Pack into 72 bits: c1 (28) + c2 (28) + gr (15) + flag (1).
    payload = (c1 << 44) | (c2 << 16) | (gr << 1) | 0
    # Convert to bits (MSB-first).
    bits: list[int] = []
    for i in range(71, -1, -1):
        bits.append((payload >> i) & 1)
    return bits


class Jt65Message:
    """A decoded JT65 message."""

    def __init__(
        self,
        callsign1: str = "",
        callsign2: str = "",
        grid_report: str = "",
        snr_db: float = 0.0,
        freq_hz: int = 0,
        timestamp: float = 0.0,
    ) -> None:
        self.callsign1 = callsign1
        self.callsign2 = callsign2
        self.grid_report = grid_report
        self.snr_db = snr_db
        self.freq_hz = freq_hz
        self.timestamp = timestamp

    def to_dict(self) -> dict[str, object]:
        return {
            "callsign1": self.callsign1,
            "callsign2": self.callsign2,
            "grid_report": self.grid_report,
            "snr_db": self.snr_db,
            "freq_hz": self.freq_hz,
            "ts": self.timestamp,
        }

    def __str__(self) -> str:
        return f"{self.callsign1} {self.callsign2} {self.grid_report}"
