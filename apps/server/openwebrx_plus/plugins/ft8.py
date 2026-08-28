"""FT8 audio-band decoder plugin (ADR-003 in-process plugin family #6).

Slice-26 v1 (2026-08-28): closes the slice-21 manifest stub. Ships
the actual FSK demodulator + symbol extraction + CRC-14 verify +
standard message unpack. LDPC syndrome check is deferred to v2 (CRC
alone catches most garbage; occasional false positives are honest
for v1).

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

Slice-26 v1 simplifications (documented for future improvement):
  - **No Costas loop / symbol timing recovery**: assumes the symbol
    boundaries are aligned to 0.16 s boundaries from the start of the
    15-second slot. Real FT8 has ±0.5 symbol timing offset + Doppler;
    a Costas-loop correction lands in v2.
  - **No LDPC syndrome check**: v1 just verifies CRC-14. Random bits
    have ~1/16384 chance of CRC passing → occasional false positives.
    Honest for v1; the actual LDPC error correction lands in v2
    (sum-product decoder on the published H matrix).
  - **Limited message type coverage**: only i3=0 (standard callsign +
    grid/report) is fully decoded. ARRL RTTY RU / Field Day / POTA /
    contests / WW ROAG (i3=1..5) land in v2.
  - **Limited grid field decoding**: standard 4-char Maidenhead grids
    + signal reports (-50..+50 range) + the special markers
    (RRR, RR73, 73). Other grid encodings land in v2.

The implementation is split across three modules:
  - :mod:`.ft8` (this file) — the DecoderPlugin wrapper, manifest, event
    surface, and the audio chunk → 15-second slot buffer.
  - :mod:`.ft8_demod` — FSK Goertzel detection + symbol → bit extraction.
  - :mod:`.ft8_protocol` — CRC-14 + message unpack (callsign / grid /
    report decoding).
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
        version="0.2.0",  # bumped from 0.1.0 (slice-21 stub → slice-26 v1)
        label="FT8 (audio-band digi modes)",
        tap_point="rf_band",
        description=(
            "FT8 / FT4 / WSPR / JT65 / JT9 / PSK31 / RTTY decoder "
            "(ADR-003 in-process plugin family #6). Slice-26 v1 status: "
            "FSK demod + CRC-14 + standard message unpack SHIPPED; LDPC "
            "syndrome check + Costas loop / symbol timing recovery "
            "deferred to v2 (CRC alone catches most garbage). Tap point: "
            "rf_band (operates on the audio-band IQ, not raw RF). "
            "Defaults: 12 kHz audio rate, 6.25 Hz tone spacing, "
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
        """Demodulate one 15-second FT8 slot → events."""
        self._slot_count += 1
        symbols = detect_symbols(slot_audio, sample_rate)
        if len(symbols) == 0:
            return []
        bits = symbols_to_bits(symbols)
        if len(bits) != FT8_PAYLOAD_BITS + FT8_CRC_BITS + FT8_LDPC_PARITY_BITS:
            return []
        # v1: skip the LDPC syndrome check. Just verify CRC.
        systematic_bits = list(bits[: FT8_PAYLOAD_BITS + FT8_CRC_BITS].tolist())
        # All-zero systematic bits → no signal (silence). CRC happens to
        # pass for all-zero because crc14(zeros) = 0 and the embedded
        # CRC bits are also zero. Skip — this is the "no signal" degenerate
        # case that would otherwise produce spurious "00000 00000 ..."
        # decodes.
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
            "slot_count": self._slot_count,
            "stub": False,  # v1 is no longer a stub
            "version": "0.2.0",
            "v1_simplifications": (
                "no_ldpc_syndrome_check | no_costas_loop | "
                "i3_only_0 | simplified_grid_encoding"
            ),
            "note": (
                "slice-26 v1: FSK demod + CRC-14 + standard message unpack. "
                "LDPC syndrome check, Costas loop, symbol timing recovery, "
                "and i3!=0 message types are deferred to v2."
            ),
        }

    def stop(self) -> None:
        """Release streaming state."""
        self._audio_buffer = np.zeros(0, dtype=np.float32)
        self._slot_count = 0
        self._messages_decoded = 0
        self._crc_failures = 0
        self._recent_messages.clear()


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

    Pipeline: message → 77 bits → +CRC → 91 bits → +parity (zero pad v1)
    → 174 bits → symbols → audio cosine.

    v1: the LDPC parity is zero-padded (the decoder doesn't check it).
    """
    from .ft8_demod import bits_to_symbols, symbols_to_audio
    message_bits = pack_message(callsign1, callsign2, grid_or_report, i3)
    systematic = add_crc(message_bits)
    codeword = add_ldpc_parity(systematic)
    symbols = bits_to_symbols(np.array(codeword, dtype=np.uint8))
    return symbols_to_audio(symbols, sample_rate)
