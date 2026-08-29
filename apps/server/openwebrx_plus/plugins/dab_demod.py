"""DAB (Digital Audio Broadcasting) FIC decoder — service information.

DAB is a digital radio standard used in Europe and parts of Asia. It
uses OFDM with DQPSK modulation, carrying multiple audio services in a
multiplex. The FIC (Fast Information Channel) carries service labels,
frequencies, and program type information.

A full DAB decoder requires:
  1. OFDM demodulation (1536 subcarriers in Mode I).
  2. DQPSK symbol extraction.
  3. FIC decoding (FIB blocks with CRC-16).
  4. MSC (Main Service Channel) decoding for audio.

This v1 implements the FIC service label extraction layer — it takes
decoded FIB bytes (from a future OFDM front-end) and extracts service
labels. This is the building block for a DAB service list viz.

FIC structure:
  * FIB (Fast Information Block): 256 bytes, 30 bytes payload + CRC.
  * FIG (Fast Information Group): type + extension, variable length.
  * FIG 0/1: Service label (16 chars + program type).
  * FIG 0/2: Service component (ASNo + subchannel ID).
  * FIG 0/21: Service label (new format with character set).

This module is pure-numpy (ADR-004 compliant — no scipy in the live path).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# DAB FIC constants.
FIB_SIZE = 32  # 30 bytes data + 2 bytes CRC
FIB_DATA_SIZE = 30
FIB_CRC_SIZE = 2


@dataclass
class DabService:
    """A DAB service (radio station)."""
    service_id: int  # 32-bit SId
    label: str = ""
    program_type: int = 0
    subchannel_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "label": self.label,
            "program_type": self.program_type,
            "subchannel_id": self.subchannel_id,
        }


@dataclass
class DabEnsemble:
    """A DAB ensemble (multiplex)."""
    ensemble_id: int = 0
    label: str = ""
    services: list[DabService] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ensemble_id": self.ensemble_id,
            "label": self.label,
            "services": [s.to_dict() for s in self.services],
        }


def crc16_dab(data: bytes) -> int:
    """Compute CRC-16 for DAB FIB (poly 0x1021, init 0xFFFF).

    DAB uses CRC-16-CCITT with polynomial 0x1021, initial value 0xFFFF.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def verify_fib_crc(fib: bytes) -> bool:
    """Verify the CRC-16 of a 32-byte FIB (last 2 bytes are CRC)."""
    if len(fib) < FIB_SIZE:
        return False
    received_crc = (fib[FIB_DATA_SIZE] << 8) | fib[FIB_DATA_SIZE + 1]
    computed = crc16_dab(fib[:FIB_DATA_SIZE])
    return received_crc == computed


def decode_fig_0_1(data: bytes) -> DabService | None:
    """Decode FIG 0/1 (Service label).

    Format: [SId 4 bytes][char set 1 byte (low 4 bits)][label 16 bytes][PTy 1 byte].

    The label is 16 ASCII characters, padded with spaces. The character
    set field (bits 0-3 of byte 4) is 0 for EBU Latin.
    """
    if len(data) < 22:  # 4 + 1 + 16 + 1 = 22
        return None
    service_id = (data[0] << 24) | (data[1] << 16) | (data[2] << 8) | data[3]
    # Character set is in the low 4 bits of byte 4.
    # Label starts at byte 5 (after the char set byte).
    label_bytes = data[5:21]
    # EBU Latin character set — replace high-bit chars with '?'.
    label = "".join(
        chr(b) if 32 <= b < 127 else "?"
        for b in label_bytes
    ).rstrip()
    # Program type is the last byte.
    pty = data[21] if len(data) > 21 else 0
    return DabService(
        service_id=service_id,
        label=label,
        program_type=pty,
    )


def decode_fib(fib: bytes) -> list[DabService]:
    """Decode all FIGs in a 30-byte FIB data block.

    Returns a list of DabService objects (from FIG 0/1 entries).
    """
    services: list[DabService] = []
    offset = 0
    while offset < len(fib):
        # FIG header: 1 byte (type in bits 4-7, length in bits 0-3 of next byte).
        if offset + 1 >= len(fib):
            break
        fig_type = (fib[offset] >> 4) & 0x0F
        # The length is determined by the FIG extension + data — for FIG 0/1
        # the length is fixed at 22 bytes. For other FIGs, we skip based on
        # the remaining data.
        if fig_type == 0:
            # FIG Type 0 — check the extension.
            if offset + 2 >= len(fib):
                break
            extension = fib[offset + 1] & 0x0F
            if extension == 1:
                # FIG 0/1: Service label (22 bytes after the 2-byte header).
                fig_data = fib[offset + 2 : offset + 2 + 22]
                if len(fig_data) >= 22:
                    service = decode_fig_0_1(fig_data)
                    if service is not None:
                        services.append(service)
                offset += 2 + 22
            else:
                # Unknown FIG 0 extension — skip 2 header bytes + guess length.
                # In a real implementation we'd parse the FIG length field.
                offset += 4  # safe skip
        else:
            # Unknown FIG type — skip.
            offset += 4
    return services


def decode_fic(fic_bytes: bytes) -> list[DabService]:
    """Decode a FIC (Fast Information Channel) data block.

    The FIC consists of multiple FIBs (Fast Information Blocks), each
    32 bytes (30 data + 2 CRC). This function splits the FIC into FIBs,
    verifies CRC, and decodes the services from each valid FIB.
    """
    services: list[DabService] = []
    offset = 0
    while offset + FIB_SIZE <= len(fic_bytes):
        fib = fic_bytes[offset : offset + FIB_SIZE]
        offset += FIB_SIZE
        if not verify_fib_crc(fib):
            continue  # skip corrupted FIBs
        fib_services = decode_fib(fib[:FIB_DATA_SIZE])
        services.extend(fib_services)
    return services
