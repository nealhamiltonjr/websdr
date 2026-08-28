"""ITA2 / Baudot code decoder for RTTY.

ITA2 (International Telegraph Alphabet No. 2), also known as Baudot code,
is a 5-bit character encoding used by RTTY. It has 32 codes (2⁵), which
is too few for letters + digits + punctuation, so ITA2 uses **shift
states**: a Letter Shift (code 11111 = 31) switches to the letter set,
a Figure Shift (code 11011 = 27) switches to the figure set.

The 32 codes map to different characters depending on the current shift
state. Codes 0-3 and 27-31 are control codes (null, EOT, CR, LF, shift,
space, etc.); codes 4-26 are printable (different per shift state).

Frame format: 1 start bit (0) + 5 data bits LSB first + 1.42 stop bits (1).
The demodulator hands us the 5 data bits as a code 0-31; this module
tracks the shift state and produces text.

Reference: ITU-R M.476-5 (ITA2 table).
"""

from __future__ import annotations

# ITA2 code table — index 0-31, two columns: LETTERS and FIGURES.
# Source: ITU-R M.476-5, the canonical ITA2 table.
# Codes 0-3 and 27-31 are control codes (same in both shifts except 27/31).
_LETTERS = (
    "\0",   # 00000 — null
    "E",    # 00001
    "\n",   # 00010 — LF
    "A",    # 00011
    " ",    # 00100 — space
    "S",    # 00101
    "I",    # 00110
    "U",    # 00111
    "\r",   # 01000 — CR
    "D",    # 01001
    "R",    # 01010
    "J",    # 01011
    "N",    # 01100
    "F",    # 01101
    "C",    # 01110
    "K",    # 01111
    "T",    # 10000
    "Z",    # 10001
    "L",    # 10010
    "W",    # 10011
    "H",    # 10100
    "Y",    # 10101
    "P",    # 10110
    "Q",    # 10111
    "O",    # 11000
    "B",    # 11001
    "G",    # 11010
    "^",    # 11011 — FIGS shift (internal marker, not emitted as char)
    "M",    # 11100
    "X",    # 11101
    "V",    # 11110
    "*",    # 11111 — LTRS shift (internal marker, not emitted as char)
)

_FIGURES = (
    "\0",   # 00000 — null
    "3",    # 00001
    "\n",   # 00010 — LF
    "-",    # 00011
    " ",    # 00100 — space
    "'",    # 00101
    "8",    # 00110
    "7",    # 00111
    "\r",   # 01000 — CR
    "WRU",  # 01001 — "Who Are You?" (enquiry)
    "4",    # 01010
    "BELL", # 01011 — bell
    ",",    # 01100
    "!",    # 01101
    ":",    # 01110
    "(",    # 01111
    "5",    # 10000
    "+",    # 10001
    ")",    # 10010
    "2",    # 10011
    "#",    # 10100 — sometimes £
    "6",    # 10101
    "0",    # 10110
    "1",    # 10111
    "9",    # 11000
    "?",    # 11001
    "&",    # 11010
    "^",    # 11011 — FIGS shift (internal marker)
    ".",    # 11100
    "/",    # 11101
    ";",    # 11110
    "*",    # 11111 — LTRS shift (internal marker)
)

# Control codes (same in both shifts).
_NULL = 0
_LF = 2
_CR = 8
_SPACE = 4
_LTRS_SHIFT = 31  # 11111
_FIGS_SHIFT = 27  # 11011


class Ita2Decoder:
    """ITA2 / Baudot code decoder with letter/figure shift state.

    Feed 5-bit codes (0-31) via `decode(code)`; get back strings (usually
    0 or 1 character, but control codes like CR/LF produce their respective
    characters, and multi-char sequences like "WRU" or "BELL" are possible).

    The shift state persists across calls — RTTY transmissions switch
    between letters and figures mid-stream.
    """

    def __init__(self) -> None:
        self._letters_mode = True  # default to letters (the idle state)
        self._last_was_cr = False  # for CR+LF → \r\n normalization

    def decode(self, code: int) -> str:
        """Decode one 5-bit ITA2 code (0-31) to a string.

        Returns:
            The decoded character(s), or "" for non-printing codes
            (null, shifts). Control codes (CR, LF, space) produce their
            respective characters. Shift codes change the internal state
            and return "".
        """
        if not 0 <= code <= 31:
            return ""  # invalid code — ignore
        # Handle shift codes first.
        if code == _LTRS_SHIFT:
            self._letters_mode = True
            return ""
        if code == _FIGS_SHIFT:
            self._letters_mode = False
            return ""
        # Handle control codes (same in both shifts).
        if code == _NULL:
            return ""
        if code == _LF:
            self._last_was_cr = False
            return "\n"
        if code == _CR:
            self._last_was_cr = True
            return "\r"
        if code == _SPACE:
            return " "
        # Printable character — look up in the current shift's table.
        table = _LETTERS if self._letters_mode else _FIGURES
        char = table[code]
        # Filter out internal markers (shouldn't appear here, but defensive).
        if char == "^" or char == "*":
            return ""
        return char

    def decode_many(self, codes: list[int]) -> str:
        """Decode a list of 5-bit codes to a string (batch convenience)."""
        return "".join(self.decode(c) for c in codes)

    def reset(self) -> None:
        """Reset to the default (letters) shift state."""
        self._letters_mode = True
        self._last_was_cr = False

    @property
    def in_letters_mode(self) -> bool:
        """True if the decoder is currently in letter-shift mode."""
        return self._letters_mode
