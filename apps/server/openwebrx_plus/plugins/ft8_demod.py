"""FT8 FSK demodulator — slice-26 v1.

Implements the audio-band FSK tone detection + symbol timing for FT8.
The demodulator:

  1. Buffers incoming audio samples into 0.16-second symbol periods
     (FT8_SAMPLE_RATE * 0.16 = 1920 samples per symbol at 12 kHz).
  2. For each symbol period, computes the Goertzel magnitude at each
     of the 8 FT8 tones (offset 0..7 * 6.25 Hz from the baseline
     tone, typically 1500 Hz for FT8).
  3. Picks the strongest tone = the 3-bit symbol value.
  4. Collects 79 symbols per 15-second slot → 237 bits.
  5. Extracts the 174-bit LDPC codeword (per the symbol position layout).

The output is a list of demodulated bits per detected 15-second slot.

Slice-26 v1 simplifications (documented for future improvement):
  - **No Costas loop / symbol timing recovery**: assumes the symbol
    boundaries are aligned to 0.16 s boundaries from the start of the
    15-second slot. Real FT8 has ±0.5 symbol timing offset + Doppler;
    a Costas-loop correction lands in v2.
  - **No LDPC syndrome check**: v1 just verifies CRC-14. This means
    random audio bits have a ~1/16384 chance of CRC passing on
    garbage — operators see occasional false positives. Honest for v1;
    the actual LDPC error correction lands in v2 (sum-product decoder
    on the published H matrix).

The FT8 frame structure (per WSJT-X pack77.f90):

  - 79 symbols total in a 15-second slot
  - Costas arrays at symbol positions 0-6, 36-42, 72-78 (3x 7 symbols
    each, used for sync — skipped in the data extraction)
  - Data symbols at positions 7-35 (29 symbols) + 43-71 (29 symbols)
    = 58 data symbols × 3 bits = 174 bits = the LDPC codeword

  The 174-bit LDPC codeword is:
    - 91 systematic bits (77 message + 14 CRC)
    - 83 parity bits

  CRC-14 is computed over the 77-bit message, then concatenated with
  the 83 LDPC parity bits to form the 91+83 = 174-bit codeword.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# Audio band rate (WSJT-X standard). 12 kHz audio, 6.25 baud → 1920 samples/symbol.
FT8_SYMBOL_SAMPLES = 1920  # FT8_SAMPLE_RATE / FT8_BIT_RATE_BAUD = 12000 / 6.25
FT8_SYMBOLS_PER_SLOT = 79
FT8_SLOT_SAMPLES = FT8_SYMBOL_SAMPLES * FT8_SYMBOLS_PER_SLOT  # 151_680 samples ≈ 12.64 s
# The slot is 15 seconds total; the extra ~2.36 s is the guard interval +
# 0.5 s Costas sync at start (offset within the slot varies — v1 assumes
# symbol 0 starts at sample 0 of the slot).

# Symbol positions (per WSJT-X ft8b.f90). Costas arrays at [0,7) and [36,43)
# and [72,79); data at [7,36) ∪ [43,72).
COSTAS_POSITIONS = (
    list(range(0, 7))    # first Costas array
    + list(range(36, 43))  # middle Costas array
    + list(range(72, 79))  # last Costas array
)
DATA_POSITIONS = [i for i in range(FT8_SYMBOLS_PER_SLOT) if i not in COSTAS_POSITIONS]
# 58 data positions × 3 bits = 174 bits LDPC codeword
# Set form for O(1) "in" lookup (used by detect_symbols_soft).
_DATA_POSITIONS_SET: set[int] = set(DATA_POSITIONS)

# The baseline FT8 tone (Hz). WSJT-X uses 1500 Hz as the FT8 audio center.
# Tone k = baseline + k * FT8_TONE_SPACING_HZ for k = 0..7.
FT8_BASELINE_TONE_HZ = 1500.0


@dataclass
class FT8Slot:
    """One demodulated 15-second FT8 slot."""

    symbols: np.ndarray  # int8, shape (79,) — values 0..7
    bits: np.ndarray  # uint8, shape (174,) — extracted LDPC codeword bits
    slot_index: int  # 0, 1, 2, ... (per the plugin's slot counter)
    sample_offset: int  # absolute offset within the audio stream


def goertzel_magnitude(samples: np.ndarray, freq_hz: float, sample_rate: int) -> float:
    """Compute the Goertzel magnitude at one frequency.

    The Goertzel algorithm is more efficient than a full FFT when you only
    need a small number of frequencies — exactly the FT8 case (8 tones).
    """
    n = len(samples)
    if n == 0:
        return 0.0
    # Normalized frequency coefficient.
    k = int(0.5 + n * freq_hz / sample_rate)
    w = 2.0 * math.pi * k / n
    coeff_re = math.cos(w)
    # Goertzel recurrence: s[n] = samples[n] + 2*coeff_re*s[n-1] - s[n-2]
    s_prev = 0.0
    s_prev2 = 0.0
    for sample in samples:
        s = float(sample) + 2.0 * coeff_re * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    # Final magnitude: sqrt(s_prev^2 + s_prev2^2 - 2*coeff_re*s_prev*s_prev2)
    mag_sq = s_prev * s_prev + s_prev2 * s_prev2 - 2.0 * coeff_re * s_prev * s_prev2
    return mag_sq if mag_sq > 0 else 0.0


def detect_symbols(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """Detect FT8 symbols in one 15-second audio slot.

    Returns an int8 array of length 79 with values 0..7 (the FT8 tone index
    for each symbol period). The audio must be exactly FT8_SLOT_SAMPLES long
    (or be sliced to that length internally — extra samples are ignored).

    Args:
        audio: float32 PCM samples, mono, length >= FT8_SLOT_SAMPLES.
        sample_rate: must match FT8_SAMPLE_RATE (12 kHz).

    Raises:
        ValueError if sample_rate != FT8_SAMPLE_RATE.
    """
    if sample_rate != 12_000:
        raise ValueError(
            f"FT8 demod requires 12 kHz audio, got {sample_rate}. "
            "Set the receiver's audio sample rate to 12000 or resample before feeding."
        )
    n = min(len(audio), FT8_SLOT_SAMPLES)
    if n < FT8_SLOT_SAMPLES:
        # Not enough for a full slot — return empty (caller should buffer).
        return np.zeros(0, dtype=np.int8)
    symbols = np.zeros(FT8_SYMBOLS_PER_SLOT, dtype=np.int8)
    for i in range(FT8_SYMBOLS_PER_SLOT):
        start = i * FT8_SYMBOL_SAMPLES
        end = start + FT8_SYMBOL_SAMPLES
        chunk = audio[start:end]
        # Compute Goertzel magnitude at each of the 8 FT8 tones.
        mags = np.zeros(8, dtype=np.float32)
        for k in range(8):
            freq = FT8_BASELINE_TONE_HZ + k * 6.25
            mags[k] = goertzel_magnitude(chunk, freq, sample_rate)
        # Pick the strongest tone.
        symbols[i] = int(np.argmax(mags))
    return symbols


# Bit pattern for each FT8 tone (tone value 0..7 → 3-bit pattern MSB-first).
_TONE_BIT_PATTERNS: list[tuple[int, int, int]] = [
    (0, 0, 0),  # tone 0
    (0, 0, 1),  # tone 1
    (0, 1, 0),  # tone 2
    (0, 1, 1),  # tone 3
    (1, 0, 0),  # tone 4
    (1, 0, 1),  # tone 5
    (1, 1, 0),  # tone 6
    (1, 1, 1),  # tone 7
]


def detect_symbols_soft(
    audio: np.ndarray, sample_rate: int
) -> tuple[np.ndarray, list[float]]:
    """Detect FT8 symbols with soft (LLR) output — slice-29 v2.1.

    Returns both the hard symbol decisions (same as :func:`detect_symbols`)
    AND a list of 174 per-bit log-likelihood ratios for the LDPC
    sum-product decoder. The hard decisions are the v1 path (retained
    for backward compatibility + as the fallback when LDPC fails);
    the soft LLRs feed :func:`ft8_ldpc.decode_ldpc` for the v2.1
    soft-decision path that gives ~3 dB SNR improvement.

    LLR derivation (one-of-8 symbol → per-bit LLR):

      For each symbol period, we have 8 tone magnitudes m[0..7].
      Tone k encodes 3 bits (MSB, mid, LSB) per _TONE_BIT_PATTERNS.
      The soft "probability" of tone k is proportional to m[k]
      (treat magnitude as a non-normalized likelihood).

      For bit position p (0=MSB, 1, 2=LSB):
        P(bit p = 1) = sum over k where _TONE_BIT_PATTERNS[k][p] == 1 of m[k]
        P(bit p = 0) = sum over k where _TONE_BIT_PATTERNS[k][p] == 0 of m[k]

      LLR(p) = log(P(bit p = 1) / P(bit p = 0))

      Sign convention: positive LLR → bit is more likely 0 (P(0) > P(1));
      negative LLR → bit is more likely 1. This matches the LDPC decoder's
      convention. The magnitude is the log-ratio of the two probabilities
      (higher = more confident).

      We use magnitude (not magnitude-squared) as the soft weight — this
      is the standard non-coherent FSK soft-decision derivation. Adding a
      tiny epsilon (1e-12) prevents log(0) when one side is zero.

      Costas sync symbols (21 of 79) are NOT data — they don't contribute
      LLRs. Only the 58 data positions produce LLRs (3 bits each → 174
      total). The LLR list order matches the LDPC codeword bit order
      (DATA_POSITIONS × 3 bits MSB-first per symbol) — see
      :func:`symbols_to_bits` for the matching convention.

    Args:
        audio: float32 PCM, mono, length >= FT8_SLOT_SAMPLES.
        sample_rate: must be 12_000.

    Returns:
        (hard_symbols, soft_llrs) where:
          - hard_symbols is np.int8, length 79 (same as detect_symbols).
          - soft_llrs is a list of 174 floats (one per LDPC codeword bit,
            MSB-first per symbol, in DATA_POSITIONS order).

    Raises:
        ValueError if sample_rate != FT8_SAMPLE_RATE or audio is too short.
    """
    if sample_rate != 12_000:
        raise ValueError(
            f"FT8 demod requires 12 kHz audio, got {sample_rate}."
        )
    n = min(len(audio), FT8_SLOT_SAMPLES)
    if n < FT8_SLOT_SAMPLES:
        return np.zeros(0, dtype=np.int8), []

    hard_symbols = np.zeros(FT8_SYMBOLS_PER_SLOT, dtype=np.int8)
    soft_llrs: list[float] = []
    epsilon = 1e-12

    for i in range(FT8_SYMBOLS_PER_SLOT):
        start = i * FT8_SYMBOL_SAMPLES
        end = start + FT8_SYMBOL_SAMPLES
        chunk = audio[start:end]
        mags = np.zeros(8, dtype=np.float64)
        for k in range(8):
            freq = FT8_BASELINE_TONE_HZ + k * 6.25
            mags[k] = float(goertzel_magnitude(chunk, freq, sample_rate))
        # Hard decision: argmax tone.
        hard_symbols[i] = int(np.argmax(mags))
        # Soft LLRs only for data symbols (skip Costas positions — they
        # don't contribute to the LDPC codeword).
        if i in _DATA_POSITIONS_SET:
            for p in range(3):  # 3 bit positions: MSB, mid, LSB
                p_bit1 = sum(
                    mags[k] for k in range(8) if _TONE_BIT_PATTERNS[k][p] == 1
                )
                p_bit0 = sum(
                    mags[k] for k in range(8) if _TONE_BIT_PATTERNS[k][p] == 0
                )
                # LLR = log(P(bit=1) / P(bit=0)). Positive → bit likely 0
                # (P(0) > P(1)); negative → bit likely 1. Matches the
                # LDPC decoder's hard-decision convention (bit = 1 if LLR<0).
                llr = float(np.log((p_bit1 + epsilon) / (p_bit0 + epsilon)))
                # Note: our LDPC decoder expects "positive LLR → bit likely 0"
                # which means LLR = log(P(0)/P(1)) = -log(P(1)/P(0)).
                # So we negate the formula above.
                soft_llrs.append(-llr)
    return hard_symbols, soft_llrs


def symbols_to_bits(symbols: np.ndarray) -> np.ndarray:
    """Extract the 174-bit LDPC codeword from 79 symbols.

    The 79 symbols contain 3 Costas arrays (21 symbols total — sync only,
    not data) and 58 data symbols (58 × 3 bits = 174 bits).
    Each data symbol's value (0..7) unpacks to 3 bits (MSB first).
    """
    if len(symbols) != FT8_SYMBOLS_PER_SLOT:
        return np.zeros(0, dtype=np.uint8)
    bits = np.zeros(174, dtype=np.uint8)
    bit_idx = 0
    for pos in DATA_POSITIONS:
        sym_val = int(symbols[pos]) & 0x7
        bits[bit_idx] = (sym_val >> 2) & 1  # MSB
        bits[bit_idx + 1] = (sym_val >> 1) & 1
        bits[bit_idx + 2] = sym_val & 1  # LSB
        bit_idx += 3
    return bits


def bits_to_symbols(bits: np.ndarray) -> np.ndarray:
    """Encode 174 LDPC bits → 58 data symbols (for synthetic test frames).

    Inverse of :func:`symbols_to_bits`. The 21 Costas sync symbols are
    set to the standard FT8 Costas sequence (3, 1, 4, 0, 6, 5, 2) repeated
    at each of the 3 Costas positions.
    """
    if len(bits) != 174:
        raise ValueError(f"bits must be 174 long, got {len(bits)}")
    symbols = np.zeros(FT8_SYMBOLS_PER_SLOT, dtype=np.int8)
    # Standard FT8 Costas sequence (from WSJT-X ft8b.f90).
    costas = [3, 1, 4, 0, 6, 5, 2]
    for i, pos in enumerate(COSTAS_POSITIONS):
        symbols[pos] = costas[i % len(costas)]
    # Pack 3 bits per data symbol.
    bit_idx = 0
    for pos in DATA_POSITIONS:
        sym_val = (int(bits[bit_idx]) << 2) | (int(bits[bit_idx + 1]) << 1) | int(bits[bit_idx + 2])
        symbols[pos] = sym_val
        bit_idx += 3
    return symbols


def symbols_to_audio(symbols: np.ndarray, sample_rate: int = 12_000) -> np.ndarray:
    """Synthesize FT8 audio from symbols (for test frames).

    Generates a cosine wave at the tone frequency for each symbol period.
    Real FT8 uses GFSK with 4-sample Raised-Cosine shaping; v1 uses pure
    cosine (sufficient for the Goertzel demodulator to lock on).
    """
    if len(symbols) != FT8_SYMBOLS_PER_SLOT:
        raise ValueError(f"symbols must be 79 long, got {len(symbols)}")
    n = FT8_SYMBOLS_PER_SLOT * FT8_SYMBOL_SAMPLES
    t = np.arange(n, dtype=np.float32) / sample_rate
    # Phase-continuous cosine: track phase across symbols to avoid jumps.
    phase = 0.0
    audio = np.zeros(n, dtype=np.float32)
    for i, sym_val in enumerate(symbols):
        freq = FT8_BASELINE_TONE_HZ + (int(sym_val) & 0x7) * 6.25
        # Phase advance for one symbol period at this frequency.
        chunk_start = i * FT8_SYMBOL_SAMPLES
        chunk_end = chunk_start + FT8_SYMBOL_SAMPLES
        chunk_t = t[chunk_start:chunk_end]
        audio[chunk_start:chunk_end] = np.cos(2.0 * np.pi * freq * chunk_t + phase)
        # Update phase for continuity: phase += 2*pi*freq*symbol_duration
        phase += 2.0 * np.pi * freq * (FT8_SYMBOL_SAMPLES / sample_rate)
        phase = math.fmod(phase, 2.0 * math.pi)
    return audio
