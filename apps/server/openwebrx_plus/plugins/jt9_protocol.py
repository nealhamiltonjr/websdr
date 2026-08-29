"""JT9 protocol decoder — symbols → callsign + grid + signal report.

JT9 uses the same 72-bit payload structure as JT65 (callsign1 28 bits +
callsign2 28 bits + grid/report 15 bits + flag 1 bit), but with only 9
tones (4 bits per symbol after the sync tone is stripped). The FEC and
interleaving differ from JT65 but the payload layout is the same.

This v1 reuses the JT65 protocol decoder's payload unpacking. The
symbol-to-bit conversion uses 4 bits per symbol (tones 0-8 map to 4-bit
values 0-8).
"""

from __future__ import annotations

from openwebrx_plus.plugins.jt65_protocol import unpack_payload as jt65_unpack_payload


def symbols_to_bits(symbols: list[int]) -> list[int]:
    """Convert 4-bit symbols (0-8) to a bit list (MSB-first).

    Each symbol is treated as a 4-bit value. Tones 0-8 map directly to
    values 0-8 (so only the lower 4 bits are used; values 9-15 never appear).
    """
    bits: list[int] = []
    for sym in symbols:
        for i in range(3, -1, -1):
            bits.append((sym >> i) & 1)
    return bits


def bits_to_symbols(bits: list[int]) -> list[int]:
    """Convert a bit list back to 4-bit symbols."""
    symbols: list[int] = []
    for i in range(0, len(bits) - 3, 4):
        sym = 0
        for j in range(4):
            sym = (sym << 1) | bits[i + j]
        symbols.append(sym)
    return symbols


def unpack_payload(symbols: list[int]) -> tuple[str, str, str]:
    """Unpack JT9 symbols into (callsign1, callsign2, grid/report).

    Reuses the JT65 payload layout (72 bits: 28+28+15+1).
    """
    bits = symbols_to_bits(symbols)
    if len(bits) < 72:
        return "", "", ""
    return jt65_unpack_payload(bits[:72])
