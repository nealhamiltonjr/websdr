"""AX.25 protocol decoder — HDLC frames → addresses + control + info.

AX.25 (Amateur X.25) is the data link layer protocol for amateur packet
radio. A frame consists of:

  * **Flag**: 0x7E (handled by the demodulator, not in the decoded frame)
  * **Address field**: 14-70 bytes (destination + source + optional digipeaters)
    - Each callsign is 7 bytes: 6 ASCII chars (shifted left 1) + SSID byte
    - The LSB of the last byte of each address is the "address extension"
      bit (0 = more addresses follow, 1 = last address)
  * **Control field**: 1-2 bytes (frame type: I/S/U)
  * **Info field**: 0-256 bytes (payload, for I-frames)
  * **FCS**: 2 bytes CRC-16-CCITT
  * **Flag**: 0x7E

This module parses the address field, control field, and info field
from a raw HDLC frame (with the FCS already verified by the demodulator).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Ax25Address:
    """One AX.25 address (callsign + SSID)."""
    callsign: str
    ssid: int

    def to_dict(self) -> dict[str, Any]:
        return {"callsign": self.callsign, "ssid": self.ssid}

    def __str__(self) -> str:
        if self.ssid == 0:
            return self.callsign
        return f"{self.callsign}-{self.ssid}"


@dataclass
class Ax25Frame:
    """A decoded AX.25 frame."""
    destination: Ax25Address
    source: Ax25Address
    digipeaters: list[Ax25Address] = field(default_factory=list)
    control: int = 0
    info: bytes = b""
    frame_type: str = ""  # "I" (information), "S" (supervisory), "U" (unnumbered)

    def to_dict(self) -> dict[str, Any]:
        return {
            "destination": self.destination.to_dict(),
            "source": self.source.to_dict(),
            "digipeaters": [d.to_dict() for d in self.digipeaters],
            "control": self.control,
            "info_hex": self.info.hex(),
            "info_text": self.info.decode("ascii", errors="replace"),
            "frame_type": self.frame_type,
        }


def decode_callsign(addr_bytes: bytes) -> Ax25Address:
    """Decode a 7-byte AX.25 address into callsign + SSID.

    The first 6 bytes are ASCII characters, each shifted left by 1 bit
    (the LSB is reserved). The 7th byte contains the SSID in bits 4-7
    and the address-extension bit in bit 0.
    """
    if len(addr_bytes) < 7:
        return Ax25Address(callsign="", ssid=0)
    # Decode callsign: each byte >> 1 gives the ASCII char.
    chars: list[str] = []
    for b in addr_bytes[:6]:
        ch = chr(b >> 1)
        if ch == " ":
            break
        chars.append(ch)
    callsign = "".join(chars)
    # SSID: bits 4-7 of the 7th byte (after >> 1).
    ssid_byte = addr_bytes[6]
    ssid = (ssid_byte >> 1) & 0x0F
    return Ax25Address(callsign=callsign, ssid=ssid)


def encode_callsign(addr: Ax25Address) -> bytes:
    """Encode a callsign + SSID into 7 AX.25 address bytes."""
    # Pad callsign to 6 chars with spaces.
    cs = addr.callsign.upper().ljust(6)[:6]
    result = bytearray()
    for ch in cs:
        result.append(ord(ch) << 1)  # shift left 1
    # SSID byte: (ssid << 1) | 0x60 (reserved bits) | extension bit (0 here)
    ssid_byte = (addr.ssid << 1) | 0x60
    result.append(ssid_byte)
    return bytes(result)


def decode_frame(frame: bytes) -> Ax25Frame | None:
    """Decode a raw AX.25 HDLC frame (with FCS already stripped or verified).

    Args:
        frame: The raw frame bytes (addresses + control + info + FCS).
               The FCS (last 2 bytes) is stripped if present.

    Returns:
        Ax25Frame if the frame is valid, None if parsing fails.
    """
    if len(frame) < 16:  # min: 14 address bytes + 1 control + 2 FCS... actually 14+1 = 15
        return None

    # Strip FCS if present (last 2 bytes).
    payload = frame[:-2] if len(frame) >= 2 else frame

    # Parse address field: 14 bytes minimum (destination + source).
    if len(payload) < 14:
        return None

    destination = decode_callsign(payload[0:7])
    source = decode_callsign(payload[7:14])

    # Check address extension bits to find digipeaters.
    offset = 14
    digipeaters: list[Ax25Address] = []

    # Check if the source address has the extension bit set.
    if not (payload[13] & 0x01):
        # More addresses follow — parse digipeaters (7 bytes each).
        while offset + 7 <= len(payload):
            digi = decode_callsign(payload[offset : offset + 7])
            digipeaters.append(digi)
            offset += 7
            if payload[offset - 1] & 0x01:
                break  # last address
            if len(digipeaters) > 8:  # safety limit
                break

    # Control field.
    if offset >= len(payload):
        return None
    control = payload[offset]
    offset += 1

    # Determine frame type.
    frame_type = _frame_type(control)

    # Info field (for I-frames and some U-frames).
    info = payload[offset:]

    return Ax25Frame(
        destination=destination,
        source=source,
        digipeaters=digipeaters,
        control=control,
        info=info,
        frame_type=frame_type,
    )


def _frame_type(control: int) -> str:
    """Determine the AX.25 frame type from the control byte."""
    if control & 0x01 == 0:
        return "I"  # Information frame
    if control & 0x03 == 0x01:
        return "S"  # Supervisory frame
    return "U"  # Unnumbered frame


def encode_frame(
    destination: Ax25Address,
    source: Ax25Address,
    info: bytes = b"",
    control: int = 0x00,  # I-frame
    digipeaters: list[Ax25Address] | None = None,
) -> bytes:
    """Encode an AX.25 frame (without FCS — caller must add it).

    Returns the frame payload (addresses + control + info) without the
    FCS or flag bytes. The caller should append a CRC-16 before sending.
    """
    result = bytearray()
    # Destination address (extension bit = 0, unless no source follows).
    result.extend(encode_callsign(destination))
    # Source address (extension bit = 0 if digipeaters follow, 1 if not).
    if digipeaters:
        result.extend(encode_callsign(source))
    else:
        # Set extension bit on source (last address).
        src_bytes = bytearray(encode_callsign(source))
        src_bytes[6] |= 0x01
        result.extend(src_bytes)
    # Digipeater addresses.
    if digipeaters:
        for i, digi in enumerate(digipeaters):
            digi_bytes = bytearray(encode_callsign(digi))
            if i == len(digipeaters) - 1:
                digi_bytes[6] |= 0x01  # last address
            result.extend(digi_bytes)
    # Control field.
    result.append(control)
    # Info field.
    result.extend(info)
    return bytes(result)
