"""FT8 audio-band decoder plugin (ADR-003 in-process plugin family #6).

Slice-28 v2 (2026-08-28): closes the v1 LDPC simplification. Ships
real LDPC (174, 91) encoding via the WSJT-X generator matrix +
H-matrix syndrome check (rejects garbage before CRC, eliminating
the v1 ~1/16384 false-positive rate) + a soft-decision sum-product
belief-propagation decoder (available via the new ``decode_slot_soft``
API; not yet wired into the main decode flow — that lands in v2.1
along with the soft FSK demodulator).

Slice-26 v1 (2026-08-28): closed the slice-21 manifest stub. Shipped
FSK demodulator + symbol extraction + CRC-14 verify + standard
message unpack.

Wire format (this module → frontend):

  * ``{"kind": "message", "ts": float, "mode": "FT8",
      "text": "K1ABC KO51 -12", "callsign": "K1ABC",
      "grid_locator": "KO51", "snr_db": -12, ...}`` per decoded message.
  * ``{"kind": "messages", "ts": float, "messages": [...]}`` snapshot
    of the most recent N messages (default 50; a ring buffer).

This mirrors the ADS-B / AIS pattern (per-message "frame" / per-table
"snapshot" events) but with a free-form ``text`` field for arbitrary
audio-band digital modes (FT8 / FT4 / WSPR / JT65 / JT9 / PSK31 /
RTTY — all feed the same DigiMessageListViz).

Slice-28 v2 simplifications (documented for future improvement):
  - **No Costas loop / symbol timing recovery**: assumes the symbol
    boundaries are aligned to 0.16 s boundaries from the start of the
    15-second slot. Real FT8 has ±0.5 symbol timing offset + Doppler;
    a Costas-loop correction lands in v2.1.
  - **Sum-product LDPC decoder available but not wired**: the
    :mod:`.ft8_ldpc` module ships ``decode_ldpc(soft_llrs)`` (min-sum
    BP, ~3 dB SNR improvement vs hard-decision); the plugin's main
    decode path still uses hard-decision + syndrome + CRC. Wiring
    requires a soft FSK demodulator that emits per-tone magnitudes
    (current ``detect_symbols`` returns hard argmax). Lands in v2.1.
  - **Limited message type coverage**: only i3=0 (standard callsign +
    grid/report) is fully decoded. ARRL RTTY RU / Field Day / POTA /
    contests / WW ROAG (i3=1..5) land in v3.
  - **Limited grid field decoding**: standard 4-char Maidenhead grids
    + signal reports (-25..+24 range) + the special markers
    (RRR, RR73, 73). Other grid encodings land in v3.
  - **5-char callsigns only**: 6-char callsigns (extended WSJT-X
    alphabet with CQ/QRZ/DE tokens, standard vs non-standard, hash
    table lookups) land in v3.

The implementation is split across four modules:
  - :mod:`.ft8` (this file) — the DecoderPlugin wrapper, manifest, event
    surface, and the audio chunk → 15-second slot buffer.
  - :mod:`.ft8_demod` — FSK Goertzel detection + symbol → bit extraction.
  - :mod:`.ft8_protocol` — CRC-14 + message unpack (callsign / grid /
    report decoding).
  - :mod:`.ft8_ldpc` — LDPC (174, 91) codec: real parity encoder,
    syndrome check, soft-decision sum-product decoder.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, ClassVar

import numpy as np

from .base import DecoderManifest, DecoderPlugin
from .ft8_demod import (
    FT8_SLOT_SAMPLES,
    detect_symbols,
    symbols_to_bits,
)
from .ft8_ldpc import (
    LDPCDecodeResult,
    decode_ldpc,
    is_valid_codeword,
)
from .ft8_protocol import (
    FT8_CRC_BITS,
    FT8_LDPC_PARITY_BITS,
    FT8_PAYLOAD_BITS,
    add_crc,
    add_ldpc_parity,
    pack_message,
    unpack_message,
    verify_crc,
)
from .registry import DecoderRegistry

# FT8 spec constants (WSJT-X standard).
FT8_SAMPLE_RATE = 12_000  # 12 kHz audio band
FT8_TONE_SPACING_HZ = 6.25  # 8-FSK, 6.25 Hz between tones
FT8_BIT_RATE_BAUD = 6.25  # 6.25 baud (one symbol per 0.16 s)
FT8_SLOT_SECONDS = 15  # 15-second slot alignment (UTC-locked)

# Number of recent messages retained in the snapshot ring buffer.
MAX_MESSAGES = 50


@DecoderRegistry.register
class FT8DecoderPlugin(DecoderPlugin):
    """FT8 / audio-band digital-mode decoder (slice-26 v1).

    Registered in DecoderRegistry; advertised in GET /api/decoders.
    feed_audio buffers incoming PCM into 15-second slots, runs the FSK
    demodulator + CRC-14 verify + message unpack, and emits "message"
    events per decoded frame. The "messages" snapshot event is emitted
    at the end of each slot.
    """

    manifest: ClassVar[DecoderManifest] = DecoderManifest(
        name="ft8",
        version="0.3.0",  # bumped: 0.1.0 (slice-21 stub) → 0.2.0 (slice-26 v1) → 0.3.0 (slice-28 v2 LDPC)
        label="FT8 (audio-band digi modes)",
        tap_point="rf_band",
        description=(
            "FT8 / FT4 / WSPR / JT65 / JT9 / PSK31 / RTTY decoder "
            "(ADR-003 in-process plugin family #6). Slice-28 v2 status: "
            "FSK demod + real LDPC (174,91) parity + syndrome check "
            "+ CRC-14 + standard message unpack. Sum-product BP decoder "
            "available via ft8_ldpc.decode_ldpc (not yet wired into "
            "main decode flow — that needs soft FSK demod, lands v2.1). "
            "Tap point: rf_band (operates on the audio-band IQ, not raw "
            "RF). Defaults: 12 kHz audio rate, 6.25 Hz tone spacing, "
            "15-second slot alignment."
        ),
        required_sample_rate=FT8_SAMPLE_RATE,
        events=("message", "messages"),
    )

    def __init__(self) -> None:
        self._audio_buffer: np.ndarray = np.zeros(0, dtype=np.float32)
        self._slot_count: int = 0
        self._messages_decoded: int = 0
        self._crc_failures: int = 0
        self._syndrome_failures: int = 0  # slice-28 v2: garbage rejected by LDPC syndrome
        self._recent_messages: deque[dict[str, Any]] = deque(maxlen=MAX_MESSAGES)

    def feed_iq(self, iq: np.ndarray) -> list[dict[str, Any]]:
        """Consume one complex-float32 IQ chunk; return decoded events.

        FT8 is an audio-band mode (operates on the demod channel, not the
        raw IQ). feed_iq accepts the IQ but routes it through the same
        demod path as feed_audio (taking the REAL PART as audio — the
        operator normally attaches FT8 to a demod-channel receiver that
        produces complex baseband audio).
        """
        # For FT8, the IQ input is typically already channelized audio
        # (the receiver session's AudioChain demod output). Take the real
        # part as the audio (the demod-channel IQ has the audio envelope
        # in its real part; the imag part is the Hilbert transform).
        if iq.dtype == np.complex64:
            audio = np.real(iq).astype(np.float32)
        else:
            audio = np.asarray(iq, dtype=np.float32)
        return self._feed_audio_internal(audio, FT8_SAMPLE_RATE)

    def feed_audio(self, pcm: np.ndarray, sample_rate: int) -> list[dict[str, Any]]:
        """Audio-band path — PCM int16 samples."""
        if sample_rate != FT8_SAMPLE_RATE:
            # The contract requires 12 kHz audio. Caller should resample
            # before attaching FT8; we accept and skip if not.
            return []
        audio = pcm.astype(np.float32) / 32768.0
        return self._feed_audio_internal(audio, sample_rate)

    def _feed_audio_internal(self, audio: np.ndarray, sample_rate: int) -> list[dict[str, Any]]:
        """Buffer audio into 15-second slots, demod each, emit events."""
        self._audio_buffer = np.concatenate([self._audio_buffer, audio])
        events: list[dict[str, Any]] = []
        # Process complete 15-second slots.
        while len(self._audio_buffer) >= FT8_SLOT_SAMPLES:
            slot_audio = self._audio_buffer[:FT8_SLOT_SAMPLES]
            self._audio_buffer = self._audio_buffer[FT8_SLOT_SAMPLES:]
            slot_events = self._process_slot(slot_audio, sample_rate)
            events.extend(slot_events)
        return events

    def _process_slot(self, slot_audio: np.ndarray, sample_rate: int) -> list[dict[str, Any]]:
        """Demodulate one 15-second FT8 slot → events.

        Slice-28 v2 decode path:

          1. FSK Goertzel symbol detection (hard-decision argmax of 8 tones).
          2. Extract 174-bit LDPC codeword from 58 data symbols.
          3. **Syndrome check** (v2 new): reject if the codeword is not a
             valid LDPC codeword. This eliminates the v1 false-positive
             failure mode where random bits passed CRC at ~1/16384.
          4. All-zero degenerate-case skip (silence produces all-zero bits
             which trivially satisfy both syndrome and CRC).
          5. CRC-14 verify over the 77-bit message + 14-bit embedded CRC.
          6. Unpack the 77-bit message → callsigns + grid/report.
          7. Emit "message" + "messages" snapshot events.

        Sum-product LDPC error correction (slice-28 v2 also ships
        ``ft8_ldpc.decode_ldpc``) is not wired into this path yet; it
        needs soft FSK magnitudes (current ``detect_symbols`` returns
        hard argmax). Lands in v2.1 along with the soft demodulator.
        """
        self._slot_count += 1
        symbols = detect_symbols(slot_audio, sample_rate)
        if len(symbols) == 0:
            return []
        bits = symbols_to_bits(symbols)
        if len(bits) != FT8_PAYLOAD_BITS + FT8_CRC_BITS + FT8_LDPC_PARITY_BITS:
            return []
        # The full 174-bit LDPC codeword (91 systematic + 83 parity).
        codeword = list(bits.tolist())
        # v2: syndrome check BEFORE CRC. Rejects garbage decodes that
        # would otherwise pass CRC at ~1/16384.
        if not is_valid_codeword(codeword):
            self._syndrome_failures += 1
            return []
        systematic_bits = codeword[: FT8_PAYLOAD_BITS + FT8_CRC_BITS]
        # All-zero systematic → no signal (silence). Both syndrome and
        # CRC trivially pass for all-zero (crc14(zeros) = 0, embedded
        # CRC bits are also zero, all-zero codeword is a valid LDPC
        # codeword). Skip — this is the "no signal" degenerate case.
        if all(b == 0 for b in systematic_bits):
            return []
        crc_ok, message_bits = verify_crc(systematic_bits)
        if not crc_ok:
            self._crc_failures += 1
            return []
        # CRC OK — unpack the message.
        try:
            msg = unpack_message(message_bits)
        except Exception:  # noqa: BLE001
            # Defensive: don't let a bad unpack crash the plugin.
            self._crc_failures += 1
            return []
        msg.crc_ok = True
        self._messages_decoded += 1
        ts = time.time()
        event: dict[str, Any] = {
            "kind": "message",
            "ts": ts,
            "mode": "FT8",
            "text": msg.raw_text,
            "callsign": msg.callsign1,
            "grid_locator": msg.grid_or_report if msg.i3_type == 0 else None,
            "snr_db": None,  # v1 doesn't compute SNR — that needs the
                              # Goertzel magnitude ratio vs noise floor,
                              # lands in v2.
            "audio_offset_hz": None,
            "slot_utc": self._slot_count,
            "slot_index": self._slot_count,
            "i3_type": msg.i3_type,
            "crc_ok": True,
        }
        self._recent_messages.append(event)
        # Emit a snapshot event alongside the message.
        snapshot: dict[str, Any] = {
            "kind": "messages",
            "ts": ts,
            "messages": list(self._recent_messages),
            "slot_index": self._slot_count,
        }
        return [event, snapshot]

    def status(self) -> dict[str, Any]:
        """Live counters for GET /api/receivers/{id}/decoders."""
        return {
            "messages_decoded": self._messages_decoded,
            "crc_failures": self._crc_failures,
            "syndrome_failures": self._syndrome_failures,  # v2 new
            "slot_count": self._slot_count,
            "stub": False,
            "version": "0.3.0",
            "v2_simplifications": (
                "no_costas_loop | sum_product_ldpc_not_wired | "
                "i3_only_0 | simplified_grid_encoding | 5char_callsigns"
            ),
            "note": (
                "slice-28 v2: real LDPC (174,91) parity + syndrome check "
                "BEFORE CRC. Sum-product BP decoder available via "
                "ft8_ldpc.decode_ldpc (not yet wired into main decode flow — "
                "needs soft FSK demod, lands v2.1). Costas loop / symbol "
                "timing recovery, i3!=0 message types, 6-char callsigns "
                "are deferred to v3."
            ),
        }

    def stop(self) -> None:
        """Release streaming state."""
        self._audio_buffer = np.zeros(0, dtype=np.float32)
        self._slot_count = 0
        self._messages_decoded = 0
        self._crc_failures = 0
        self._syndrome_failures = 0
        self._recent_messages.clear()

    # ------------------------------------------------------------------
    # Slice-28 v2: optional soft-decode API (wired in v2.1)
    # ------------------------------------------------------------------

    @staticmethod
    def decode_slot_with_ldpc(soft_llrs: list[float], max_iter: int = 20) -> LDPCDecodeResult:
        """Soft-decision LDPC decode of a 174-LLR FT8 slot.

        Convenience wrapper around :func:`ft8_ldpc.decode_ldpc` for
        callers that have per-symbol soft FSK magnitudes (the soft FSK
        demodulator that produces 8-magnitude vectors per symbol period
        lands in v2.1; this method exposes the LDPC decoder to plugins
        / E2E tests today so the soft-demod wiring is a single-line change
        when v2.1 lands).

        Args:
            soft_llrs: 174 log-likelihood ratios (one per codeword bit,
                MSB-first). Positive → bit more likely 0; negative → bit
                more likely 1.
            max_iter: BP iteration cap (default 20).

        Returns:
            LDPCDecodeResult with ``systematic_bits`` (91 list of 0/1) on
            success or ``None`` on failure.
        """
        return decode_ldpc(soft_llrs, max_iter=max_iter)


# ============================================================================
# Test helpers (also exported for use by tests/test_ft8_decoder.py)
# ============================================================================

def synthesize_audio(
    callsign1: str,
    callsign2: str,
    grid_or_report: str,
    i3: int = 0,
    sample_rate: int = FT8_SAMPLE_RATE,
) -> np.ndarray:
    """Synthesize FT8 audio for a known message (for test frames).

    Pipeline: message → 77 bits → +CRC → 91 bits → +LDPC parity (real,
    via the WSJT-X (174,91) generator matrix) → 174-bit valid LDPC
    codeword → symbols → audio cosine.

    v2 (slice-28): the LDPC parity is now REAL (was zero-padded in v1).
    The synthesized audio, when demodulated hard-decision and run
    through syndrome + CRC, decodes cleanly. The sum-product LDPC
    decoder (:meth:`FT8DecoderPlugin.decode_slot_with_ldpc`) also
    recovers the systematic bits from the soft-LLR form.
    """
    from .ft8_demod import bits_to_symbols, symbols_to_audio
    message_bits = pack_message(callsign1, callsign2, grid_or_report, i3)
    systematic = add_crc(message_bits)
    codeword = add_ldpc_parity(systematic)
    symbols = bits_to_symbols(np.array(codeword, dtype=np.uint8))
    return symbols_to_audio(symbols, sample_rate)
