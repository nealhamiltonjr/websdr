"""Olivia character decoder — symbols → ASCII text.

Olivia uses a custom encoding: 7-bit ASCII characters are interleaved
across multiple symbols with FEC (Golay code in the full spec). This v1
implements a simplified decoder that:
  1. Accumulates symbols (each symbol = 5 bits for Olivia 32).
  2. Assembles 7-bit characters from the accumulated bits.
  3. Maps to ASCII.

The full Olivia FEC (Golay (23,12) + interleaving) is deferred to v2 —
v1 works for strong signals where bit errors are rare.
"""

from __future__ import annotations


class OliviaDecoder:
    """Olivia character decoder — symbols → ASCII text.

    Feed symbols (0-31 for Olivia 32) via `feed_symbol()`. The decoder
    accumulates bits (each symbol contributes 5 bits, MSB-first) and
    assembles 7-bit ASCII characters.

    Olivia's actual encoding interleaves bits across multiple characters
    with FEC. This v1 skips the interleaving + FEC and decodes directly,
    which works for strong signals but won't recover weak-signal garbles.
    """

    def __init__(self, bits_per_symbol: int = 5, bits_per_char: int = 7) -> None:
        self._bits_per_symbol = bits_per_symbol
        self._bits_per_char = bits_per_char
        self._bit_buffer: list[int] = []
        self._text = ""

    def feed_symbol(self, symbol: int) -> str:
        """Feed one symbol, return the decoded character or "".

        Returns a non-empty string when enough bits accumulate for a
        complete character.
        """
        if symbol < 0 or symbol >= (1 << self._bits_per_symbol):
            return ""
        # Extract bits MSB-first.
        for i in range(self._bits_per_symbol - 1, -1, -1):
            self._bit_buffer.append((symbol >> i) & 1)
        # Assemble characters while we have enough bits.
        result = ""
        while len(self._bit_buffer) >= self._bits_per_char:
            char_bits = self._bit_buffer[: self._bits_per_char]
            self._bit_buffer = self._bit_buffer[self._bits_per_char :]
            # MSB-first 7-bit ASCII.
            code = 0
            for b in char_bits:
                code = (code << 1) | b
            # Only accept printable ASCII (32-126) + common control chars.
            if 32 <= code <= 126:
                result += chr(code)
            elif code == 10:  # LF
                result += "\n"
            elif code == 13:  # CR
                result += "\r"
            # Non-printable codes are silently dropped (FEC would catch
            # these in the full spec; v1 just discards them).
        if result:
            self._text += result
        return result

    def feed_symbols(self, symbols: list[int]) -> str:
        """Feed a list of symbols, return the concatenated decoded string."""
        return "".join(self.feed_symbol(s) for s in symbols)

    def reset(self) -> None:
        """Clear all state."""
        self._bit_buffer.clear()
        self._text = ""

    @property
    def text(self) -> str:
        """The accumulated decoded text."""
        return self._text
