"""ACARS protocol decoder — frames → aircraft messages.

ACARS frame format (after the 0xEB 0x90 sync bytes):
  * SOH (0x01): Start of heading
  * Address: 7 ASCII chars (aircraft registration, e.g., "N123AB")
  * Mode: 1 char (2=VHF Mode 2, etc.)
  * ACK: 1 char (ACK/NACK + message label)
  * Label: 2 chars (message type, e.g., "H1" for meteorological data)
  * Block ID: 1 char (sequence number or '#')
  * Text: N chars (the message payload, ASCII)
  * ETX (0x03): End of text (frame delimiter)
  * CRC: 2 bytes (CRC-16-CCITT, big-endian)

This module parses the fixed fields and extracts the aircraft address,
message label, and text payload.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AcarsMessage:
    """A decoded ACARS message."""
    address: str = ""        # aircraft registration (7 chars)
    mode: str = ""           # mode character (e.g., "2")
    ack: str = ""            # ACK/NACK character
    label: str = ""          # message label (2 chars, e.g., "H1")
    block_id: str = ""       # block sequence ID
    text: str = ""           # message payload (ASCII)
    raw_hex: str = ""        # raw frame bytes as hex (for diagnostics)

    def to_dict(self) -> dict[str, object]:
        return {
            "address": self.address,
            "mode": self.mode,
            "ack": self.ack,
            "label": self.label,
            "block_id": self.block_id,
            "text": self.text,
            "raw_hex": self.raw_hex,
        }


def decode_frame(frame: bytes) -> AcarsMessage | None:
    """Parse a raw ACARS frame (with sync + CRC) into an AcarsMessage.

    Frame layout: [0xEB][0x90][SOH][addr×7][mode][ack][label×2][block][text…][ETX][CRC×2]

    Returns None if the frame is too short or malformed.
    """
    if len(frame) < MIN_FRAME_BYTES:
        return None
    # Strip sync (2 bytes) + SOH (1 byte).
    offset = 3
    if offset >= len(frame):
        return None
    # The byte at offset should be SOH (0x01), but some implementations
    # don't include it. Check + skip if present.
    if frame[offset] == 0x01:
        offset += 1
    # Address: 7 ASCII chars.
    if offset + 7 > len(frame):
        return None
    address = frame[offset : offset + 7].decode("ascii", errors="replace")
    offset += 7
    # Mode: 1 char.
    if offset >= len(frame):
        return None
    mode = chr(frame[offset])
    offset += 1
    # ACK: 1 char.
    if offset >= len(frame):
        return None
    ack = chr(frame[offset])
    offset += 1
    # Label: 2 chars.
    if offset + 2 > len(frame):
        return None
    label = frame[offset : offset + 2].decode("ascii", errors="replace")
    offset += 2
    # Block ID: 1 char.
    if offset >= len(frame):
        return None
    block_id = chr(frame[offset])
    offset += 1
    # Text: everything up to ETX (0x03) or CRC.
    text_end = len(frame) - 2  # exclude CRC (2 bytes)
    if offset >= text_end:
        text = ""
    else:
        text_bytes = frame[offset:text_end]
        # Strip trailing ETX if present.
        if text_bytes and text_bytes[-1] == 0x03:
            text_bytes = text_bytes[:-1]
        text = text_bytes.decode("ascii", errors="replace")
    return AcarsMessage(
        address=address.strip(),
        mode=mode,
        ack=ack,
        label=label,
        block_id=block_id,
        text=text,
        raw_hex=frame.hex(),
    )


# Minimum frame bytes (imported from demod module for validation).
MIN_FRAME_BYTES = 15
