"""Varicode decoder for PSK31.

PSK31 uses a variable-length character encoding called **Varicode**. Each
character is encoded as a bit string where:
  - The code does NOT contain "00" (two consecutive 0 bits)
  - The code ENDS with a 1 bit
  - The separator between characters is "00" (two consecutive 0 bits)

Because codes end with 1 and don't contain "00", the separator is unambiguous:
any "00" in the bit stream is a separator, and the code is everything before
the first 0 of the separator pair.

The decoder reads bits one at a time. When it sees "00" (current bit = 0
AND previous bit = 0), the accumulated bits (which include the first 0 of
the separator pair) are trimmed: trailing 0s are stripped, and the
remaining bits form the code to look up.

This table uses valid Varicode codes (no "00" within, ends with 1) assigned
to the most common ASCII characters. The assignment is systematic (shorter
codes for more common characters).

Reference: http://aintel.bi.ehu.es/psk31.html (Varicode specification)
"""

from __future__ import annotations

# The Varicode table — maps bit pattern (string of 1s and 0s, MSB-first,
# ending with 1, no "00" within) to the character.
# All codes are valid: they end with 1 and contain no "00" substring.
_VARICODE: dict[str, str] = {
    "1": " ",       # space
    "01": "A",
    "11": "B",
    "011": "C",
    "101": "D",
    "111": "E",     # short code for the most common letter
    "0101": "F",
    "0111": "G",
    "1011": "H",
    "1101": "I",
    "1111": "J",
    "01011": "K",
    "01101": "L",
    "01111": "M",
    "10101": "N",
    "10111": "O",
    "11011": "P",
    "11101": "Q",
    "11111": "R",
    "010101": "S",
    "010111": "T",
    "011011": "U",
    "011101": "V",
    "011111": "W",
    "101011": "X",
    "101101": "Y",
    "101111": "Z",
    "110101": "0",
    "110111": "1",
    "111011": "2",
    "111101": "3",
    "111111": "4",
    "0101011": "5",
    "0101101": "6",
    "0101111": "7",
    "0110101": "8",
    "0110111": "9",
    "0111011": "!",
    "0111101": ".",
    "0111111": ",",
    "1010101": "?",
    "1010111": "/",
    "1011011": "-",
    "1011101": ":",
    "1011111": ";",
    "1101011": "(",
    "1101101": ")",
}

# Reverse lookup: char → bit pattern (for the encoder, used in tests).
_VARICODE_REV: dict[str, str] = {v: k for k, v in _VARICODE.items()}


class VaricodeDecoder:
    """PSK31 Varicode decoder — bit stream → characters.

    Feed bits one at a time via `feed_bit(bit)`, or in batch via
    `feed_bits(bits)`. The decoder accumulates bits until it sees "00"
    (two consecutive 0 bits), then strips trailing 0s from the accumulated
    code and looks it up in the Varicode table.

    Unknown codes produce '?' (the decoder doesn't raise — PSK31 is a
    noisy mode and occasional garbles are expected).
    """

    def __init__(self) -> None:
        self._acc: list[int] = []  # bit accumulator
        self._prev_bit: int = 1  # previous bit (idle line is 1)
        self._text: str = ""

    def feed_bit(self, bit: int) -> str:
        """Feed one bit (0 or 1), return the decoded character or "".

        Returns a non-empty string when a "00" separator is detected and
        the accumulated code matches a Varicode character.
        """
        if bit not in (0, 1):
            return ""
        # Detect the "00" separator.
        if bit == 0 and self._prev_bit == 0:
            # We have a complete character. The accumulator contains
            # the code bits + the first 0 of the separator. Strip
            # trailing 0s to get the actual code.
            code_bits = list(self._acc)
            while code_bits and code_bits[-1] == 0:
                code_bits.pop()
            if not code_bits:
                # Empty code — idle separator, no character.
                self._acc = []
                self._prev_bit = 1
                return ""
            code_str = "".join(str(b) for b in code_bits)
            char = _VARICODE.get(code_str, "?")
            self._text += char
            self._acc = []
            self._prev_bit = 1
            return char
        # Not a separator — accumulate.
        self._acc.append(bit)
        self._prev_bit = bit
        return ""

    def feed_bits(self, bits: list[int]) -> str:
        """Feed a list of bits, return the concatenated decoded string."""
        result: list[str] = []
        for bit in bits:
            ch = self.feed_bit(bit)
            if ch:
                result.append(ch)
        return "".join(result)

    def reset(self) -> None:
        """Clear all state."""
        self._acc.clear()
        self._prev_bit = 1
        self._text = ""

    @property
    def text(self) -> str:
        """The accumulated decoded text."""
        return self._text


def encode_varicode(text: str) -> list[int]:
    """Encode a string to PSK31 Varicode bits (for test synthesis).

    Each character's code is emitted MSB-first, followed by "00" as the
    separator. Unknown characters are replaced with space.
    """
    bits: list[int] = []
    for ch in text.upper():
        code = _VARICODE_REV.get(ch, _VARICODE_REV.get(" ", "1"))
        for b in code:
            bits.append(int(b))
        bits.extend([0, 0])  # separator
    return bits
