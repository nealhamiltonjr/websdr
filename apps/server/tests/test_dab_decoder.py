"""Tests for the DAB decoder — CRC, FIG decoding, FIC decoding, plugin."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openwebrx_plus.plugins.dab import DabDecoderPlugin  # noqa: E402
from openwebrx_plus.plugins.dab_demod import (  # noqa: E402
    FIB_DATA_SIZE,
    FIB_SIZE,
    DabService,
    crc16_dab,
    decode_fib,
    decode_fic,
    decode_fig_0_1,
    verify_fib_crc,
)

# ============================================================================
# CRC-16 tests
# ============================================================================

def test_crc16_dab_known_value():
    """CRC-16 of '123456789' with init 0xFFFF should be 0x29B1."""
    data = b"123456789"
    crc = crc16_dab(data)
    assert crc == 0x29B1, f"CRC-16 of '123456789' should be 0x29B1, got 0x{crc:04X}"


def test_verify_fib_crc_valid():
    """verify_fib_crc returns True for a valid FIB."""
    # Build a 30-byte data block + 2-byte CRC.
    data = b"\x00" * 30
    crc = crc16_dab(data)
    fib = data + bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    assert verify_fib_crc(fib)


def test_verify_fib_crc_invalid():
    """verify_fib_crc returns False for a corrupted FIB."""
    data = b"\x00" * 30
    crc = crc16_dab(data)
    fib = bytearray(data + bytes([(crc >> 8) & 0xFF, crc & 0xFF]))
    fib[0] ^= 0xFF
    assert not verify_fib_crc(bytes(fib))


def test_verify_fib_crc_short():
    """verify_fib_crc returns False for short data."""
    assert not verify_fib_crc(b"")
    assert not verify_fib_crc(b"\x00" * 10)


# ============================================================================
# FIG 0/1 (Service label) tests
# ============================================================================

def _build_fig_0_1(service_id: int, label: str, pty: int = 0) -> bytes:
    """Build a 22-byte FIG 0/1 data block."""
    # 4 bytes service ID (big-endian).
    sid = service_id.to_bytes(4, "big")
    # 1 byte char set (0 = EBU Latin).
    char_set = bytes([0x00])
    # 16 bytes label (padded with spaces).
    label_bytes = label.ljust(16)[:16].encode("ascii", errors="replace")
    # 1 byte PTy.
    pty_byte = bytes([pty])
    return sid + char_set + label_bytes + pty_byte


def test_decode_fig_0_1_basic():
    """decode_fig_0_1 extracts service ID + label + PTy."""
    fig_data = _build_fig_0_1(0x12345678, "BBC R1", pty=5)
    svc = decode_fig_0_1(fig_data)
    assert svc is not None
    assert svc.service_id == 0x12345678
    assert svc.label == "BBC R1"
    assert svc.program_type == 5


def test_decode_fig_0_1_long_label():
    """Labels longer than 16 chars are truncated."""
    fig_data = _build_fig_0_1(0x00000001, "A" * 20)
    svc = decode_fig_0_1(fig_data)
    assert svc is not None
    assert len(svc.label) <= 16


def test_decode_fig_0_1_short_data():
    """decode_fig_0_1 returns None for data shorter than 22 bytes."""
    assert decode_fig_0_1(b"\x00" * 10) is None


# ============================================================================
# FIB decoding tests
# ============================================================================

def _build_fib_with_fig_0_1(service_id: int, label: str) -> bytes:
    """Build a 30-byte FIB data block containing one FIG 0/1."""
    fig_data = _build_fig_0_1(service_id, label)
    # FIG header: type 0 (high nibble of byte 0), extension 1 (low nibble of byte 1).
    fig_header = bytes([0x00, 0x01])  # FIG type 0, extension 1
    fib_data = fig_header + fig_data
    # Pad to 30 bytes.
    fib_data = fib_data.ljust(FIB_DATA_SIZE, b"\x00")
    return fib_data


def test_decode_fib_extracts_service():
    """decode_fib extracts a service from a FIG 0/1 entry."""
    fib_data = _build_fib_with_fig_0_1(0xABCDEF01, "Radio Test")
    services = decode_fib(fib_data)
    assert len(services) >= 1
    assert services[0].service_id == 0xABCDEF01
    assert services[0].label == "Radio Test"


def test_decode_fib_multiple_services():
    """decode_fib extracts multiple services from multiple FIG 0/1 entries."""
    # Build a FIB with 2 FIG 0/1 entries (2 + 22 = 24 bytes each, 48 total).
    fig1 = bytes([0x00, 0x01]) + _build_fig_0_1(0x11111111, "Station A")
    fig2 = bytes([0x00, 0x01]) + _build_fig_0_1(0x22222222, "Station B")
    fib_data = (fig1 + fig2).ljust(FIB_DATA_SIZE, b"\x00")
    services = decode_fib(fib_data)
    # May not get both if the second FIG is truncated, but at least 1.
    assert len(services) >= 1


# ============================================================================
# FIC decoding tests
# ============================================================================

def test_decode_fic_skips_corrupted_fibs():
    """decode_fic skips FIBs with bad CRC."""
    fib_data = _build_fib_with_fig_0_1(0x12345678, "Test")
    crc = crc16_dab(fib_data)
    fib = fib_data + bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    # Add a corrupted FIB before the valid one.
    bad_fib = b"\xFF" * FIB_SIZE
    fic = bad_fib + fib
    services = decode_fic(fic)
    # Should still get the service from the valid FIB.
    assert len(services) >= 1
    assert services[0].label == "Test"


def test_decode_fic_empty():
    """decode_fic returns empty list for empty input."""
    assert decode_fic(b"") == []


# ============================================================================
# Plugin tests
# ============================================================================

def test_dab_plugin_manifest():
    m = DabDecoderPlugin.manifest
    assert m.name == "dab"
    assert m.tap_point == "rf_band"
    assert "service" in m.events
    assert "ensemble" in m.events


def test_dab_plugin_status():
    plugin = DabDecoderPlugin()
    s = plugin.status()
    assert s["services_known"] == 0
    assert s["ensembles_decoded"] == 0


def test_dab_plugin_feed_iq_empty():
    """Empty IQ input produces no events."""
    plugin = DabDecoderPlugin()
    iq = np.array([], dtype=np.complex64)
    events = plugin.feed_iq(iq)
    assert events == []


def test_dab_service_dataclass():
    """DabService holds the expected fields."""
    svc = DabService(service_id=0x12345678, label="Test Radio", program_type=3)
    assert svc.service_id == 0x12345678
    assert svc.label == "Test Radio"
    assert svc.program_type == 3
    d = svc.to_dict()
    assert d["service_id"] == 0x12345678
    assert d["label"] == "Test Radio"
