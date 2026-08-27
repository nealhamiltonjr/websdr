"""FT8 decoder plugin tests — manifest + status + stub feed paths.

Slice-21 ships the manifest scaffolding + wire-format types in
shared-types (DigiMessageEvent, DigiMessageListEvent); the actual FSK
demodulator + LDPC decoder lands in a future slice. These tests
verify the contract surface so the frontend DigiMessageListViz can
rely on it.
"""

from __future__ import annotations

import numpy as np

from openwebrx_plus.plugins.ft8 import (
    FT8_BIT_RATE_BAUD,
    FT8_SAMPLE_RATE,
    FT8_SLOT_SECONDS,
    FT8_TONE_SPACING_HZ,
    FT8DecoderPlugin,
)


def test_plugin_manifest_fields() -> None:
    p = FT8DecoderPlugin()
    m = p.manifest
    assert m.name == "ft8"
    assert m.version == "0.1.0"
    assert m.tap_point == "rf_band"
    assert m.required_sample_rate == FT8_SAMPLE_RATE
    assert "message" in m.events
    assert "messages" in m.events
    assert "FT8" in m.label
    # The description must clearly state the slice-21 stub status.
    assert "slice-21" in m.description.lower()
    assert "scaffold" in m.description.lower()


def test_plugin_status_reports_stub_state() -> None:
    """status() reports zeros + a 'stub: True' flag so the UI can
    surface the 'not yet implemented' state honestly."""
    p = FT8DecoderPlugin()
    s = p.status()
    assert s["messages_decoded"] == 0
    assert s["crc_failures"] == 0
    assert s["slot_count"] == 0
    assert s["stub"] is True
    assert "not implemented" in s["note"].lower()


def test_plugin_feed_iq_returns_empty() -> None:
    """Stub: feed_iq accepts IQ but returns no events."""
    p = FT8DecoderPlugin()
    # 1000 complex samples at FT8_SAMPLE_RATE — about 1/12 second.
    iq = np.zeros(1000, dtype=np.complex64)
    events = p.feed_iq(iq)
    assert events == []


def test_plugin_feed_iq_accepts_arbitrary_chunk_sizes() -> None:
    """feed_iq must handle any chunk size (mirrors other plugins'
    streaming contract — chunks may straddle calls)."""
    p = FT8DecoderPlugin()
    # Empty, tiny, mid, large.
    assert p.feed_iq(np.zeros(0, dtype=np.complex64)) == []
    assert p.feed_iq(np.zeros(1, dtype=np.complex64)) == []
    assert p.feed_iq(np.zeros(64, dtype=np.complex64)) == []
    assert p.feed_iq(np.zeros(4096, dtype=np.complex64)) == []


def test_plugin_feed_audio_stub_returns_empty() -> None:
    """The audio-band path is also stubbed — the manifest declares a
    required_sample_rate, but no actual demod runs yet."""
    p = FT8DecoderPlugin()
    pcm = np.zeros(480, dtype=np.int16)
    events = p.feed_audio(pcm, FT8_SAMPLE_RATE)
    assert events == []


def test_plugin_stop_is_noop() -> None:
    """stop() must not raise — even with no streaming state."""
    p = FT8DecoderPlugin()
    p.stop()  # must not raise


def test_ft8_constants_match_protocol_spec() -> None:
    """FT8 protocol constants — these are the values the future
    demodulator will use; document them in code so the manifest
    entry's required_sample_rate is traceable."""
    # WSJT-X standard: 12 kHz audio band, 6.25 Hz tone spacing,
    # 6.25 baud, 15-second slots.
    assert FT8_SAMPLE_RATE == 12_000
    assert FT8_TONE_SPACING_HZ == 6.25
    assert FT8_BIT_RATE_BAUD == 6.25
    assert FT8_SLOT_SECONDS == 15


def test_plugin_is_registered_in_decoder_registry() -> None:
    """The FT8 plugin must be registered in the DecoderRegistry so
    GET /api/decoders lists it and the attach endpoint accepts it."""
    from openwebrx_plus.plugins.registry import DecoderRegistry
    names = [d.name for d in DecoderRegistry.manifests()]
    assert "ft8" in names
