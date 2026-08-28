"""Morse code (CW) decoder protocol — pure-Python state machine.

Implements the bit-level decode after the demodulator: a stream of
on/off keying intervals (dits, dahs, letter gaps, word gaps) becomes
text via the ITU-R M.1677-1 Morse table.

State machine (per the standard):
  - On-interval < 7 units → dit (.) — accumulate into the current char
  - On-interval ≥ 7 units → dah (-) — accumulate into the current char
  - Off-interval 3-7 units → intra-char gap — keep accumulating
  - Off-interval ≥ 7 units but < 7*3 → inter-char gap — flush char
  - Off-interval ≥ 7*3 units → word gap — flush char + space

The "unit" is the dit duration in ms, computed from the receiver's
audio sample rate (default 8000 Hz) and a per-frame adaptive estimate
of the WPM (words per minute). The standard parity is 50 ms dit at
20 WPM (PARIS = 50 dits/word × 60/20 = 60 ms/dit including gaps;
conventionally the dit alone at 20 WPM is ~50 ms).

The Morse table is ITU-R M.1677-1 (the international code) with
prosigns (CT = -.-.- start-of-message, AR = .-.-. end-of-message,
SK = ...-.- end-of-contact, etc.) collapsed into their ASCII
equivalents for simplicity.
"""

from __future__ import annotations

# ITU-R M.1677-1 international Morse code (subset — covers ASCII letters,
# digits, common punctuation, and the most common prosigns). Each entry
# is the dit/dah pattern as a string ('.' dit, '-' dah).
MORSE_TABLE: dict[str, str] = {
    # Letters
    "A": ".-",
    "B": "-...",
    "C": "-.-.",
    "D": "-..",
    "E": ".",
    "F": "..-.",
    "G": "--.",
    "H": "....",
    "I": "..",
    "J": ".---",
    "K": "-.-",
    "L": ".-..",
    "M": "--",
    "N": "-.",
    "O": "---",
    "P": ".--.",
    "Q": "--.-",
    "R": ".-.",
    "S": "...",
    "T": "-",
    "U": "..-",
    "V": "...-",
    "W": ".--",
    "X": "-..-",
    "Y": "-.--",
    "Z": "--..",
    # Digits
    "0": "-----",
    "1": ".----",
    "2": "..---",
    "3": "...--",
    "4": "....-",
    "5": ".....",
    "6": "-....",
    "7": "--...",
    "8": "---..",
    "9": "----.",
    # Punctuation
    ".": ".-.-.-",
    ",": "--..--",
    "?": "..--..",
    "'": ".----.",
    "!": "-.-.--",
    "/": "-..-.",
    "(": "-.--.",
    ")": "-.--.-",
    "&": ".-...",
    ":": "---...",
    ";": "-.-.-.",
    "=": "-...-",
    "+": ".-.-.",
    "-": "-....-",
    "_": "..--.-",
    '"': ".-..-.",
    "$": "...-..-",
    "@": ".--.-.",
}

# Reverse lookup: pattern → character.
MORSE_REVERSE: dict[str, str] = {v: k for k, v in MORSE_TABLE.items()}

# Prosigns (handled as special multi-char patterns).
# These collapse to their ASCII equivalents or control chars.
MORSE_PROSIGNS: dict[str, str] = {
    ".-.-.": "+",  # AR (end of message) — render as '+'
    "-.-.-": "<",  # KA / CT (start of message) — '<'
    "...-.": "=",  # AA (new line) — '='
    "...-.-": "|",  # SK (end of contact / clear) — '|'
    "........": "&",  # HH (error / rubout) — '&'
}


def morse_decode_char(pattern: str) -> str | None:
    """Decode one Morse pattern to a character. Returns None if unknown."""
    if not pattern:
        return None
    # Prosigns first (they're multi-pattern + aren't in the standard table).
    if pattern in MORSE_PROSIGNS:
        return MORSE_PROSIGNS[pattern]
    char = MORSE_REVERSE.get(pattern)
    return char


# Adaptive WPM estimation constants.
# Standard Morse PARIS = 50 dit-equivalents per word at 12 WPM → dit at 20 WPM = 50 ms.
# Formula: dit_ms = 1200 / wpm.
DEFAULT_WPM = 20.0
DIT_MS_AT_20WPM = 60.0  # actually 1200/20 = 60 ms (PARIS reference)


def wpm_to_dit_ms(wpm: float) -> float:
    """Convert words-per-minute to dit duration in milliseconds.

    Standard: PARIS has 50 dits-equivalent per word, so 1 word = 60s/wpm,
    dit = 1200/wpm ms.
    """
    return 1200.0 / max(5.0, min(80.0, wpm))


def dit_ms_to_wpm(dit_ms: float) -> float:
    """Inverse: dit duration in ms → WPM estimate."""
    if dit_ms <= 0:
        return DEFAULT_WPM
    return 1200.0 / dit_ms


class MorseDecoder:
    """Streaming Morse decoder state machine.

    Feed it on/off intervals (in ms); emit decoded text characters as
    they complete. The state machine adapts the WPM estimate from
    observed dit/dah ratios.
    """

    def __init__(self, wpm_estimate: float = DEFAULT_WPM) -> None:
        self.wpm = wpm_estimate
        self._current_pattern: list[str] = []  # accumulating dit/dah chars
        self._decoded: list[str] = []

    def feed_intervals(self, intervals: list[tuple[bool, float]]) -> str:
        """Process a batch of (is_on, duration_ms) intervals.

        Returns the decoded text produced by this batch (may be empty).
        Each character is appended to the internal decoded buffer too.
        """
        out: list[str] = []
        dit_ms = wpm_to_dit_ms(self.wpm)

        for is_on, duration in intervals:
            # Adapt WPM estimate from on-intervals (a run of dits/dahs).
            if is_on:
                # Classify as dit (< 2 dits) or dah (≥ 2 dits).
                if duration < dit_ms * 1.5:
                    self._current_pattern.append(".")
                    self._adapt_wpm(duration, is_dit=True)
                else:
                    self._current_pattern.append("-")
                    # Dah is 3 dits; use this as a sanity-check on WPM.
                    self._adapt_wpm(duration / 3.0, is_dit=True)
            else:
                # Off-interval: classify gap.
                if duration < dit_ms * 2.0:
                    # Intra-char gap — keep accumulating.
                    continue
                if duration < dit_ms * 5.0:
                    # Inter-char gap — flush current char.
                    char = morse_decode_char("".join(self._current_pattern))
                    if char is not None:
                        out.append(char)
                        self._decoded.append(char)
                    self._current_pattern = []
                else:
                    # Word gap — flush char + space.
                    char = morse_decode_char("".join(self._current_pattern))
                    if char is not None:
                        out.append(char)
                        self._decoded.append(char)
                    self._current_pattern = []
                    out.append(" ")
                    self._decoded.append(" ")
        return "".join(out)

    def flush(self) -> str:
        """Flush any pending char (call on stream end / decoder pause)."""
        if not self._current_pattern:
            return ""
        char = morse_decode_char("".join(self._current_pattern))
        self._current_pattern = []
        if char is None:
            return ""
        self._decoded.append(char)
        return char

    @property
    def text(self) -> str:
        """All decoded text so far (read-only)."""
        return "".join(self._decoded)

    def reset(self) -> None:
        """Drop all state (mode switch / source change)."""
        self._current_pattern = []
        self._decoded = []

    def _adapt_wpm(self, observed_dit_ms: float, is_dit: bool) -> None:
        """EMA update of the WPM estimate from an observed dit duration.

        Conservative 5% adaptation rate so a single noisy interval can't
        derail the estimate.
        """
        if observed_dit_ms <= 0:
            return
        observed_wpm = dit_ms_to_wpm(observed_dit_ms)
        if 5.0 <= observed_wpm <= 80.0:
            self.wpm = 0.95 * self.wpm + 0.05 * observed_wpm
