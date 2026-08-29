"""Tests for QoS layer (slice-63)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openwebrx_plus.qos import qos_label, qos_tier, rank_sources  # noqa: E402


def test_local_hardware_tier_0():
    assert qos_tier("rtl_sdr") == 0
    assert qos_tier("airspy") == 0
    assert qos_tier("sdrplay") == 0
    assert qos_tier("soapy") == 0


def test_free_public_tier_2():
    assert qos_tier("kiwi") == 2
    assert qos_tier("spyserver") == 2
    assert qos_tier("openwebrx_remote") == 2


def test_unknown_tier_3():
    assert qos_tier("nonexistent") == 3


def test_qos_labels():
    assert qos_label(0) == "local"
    assert qos_label(1) == "managed"
    assert qos_label(2) == "free public"
    assert qos_label(3) == "unknown"


def test_rank_sources_best_first():
    sources = ["kiwi", "rtl_sdr", "openwebrx_remote", "airspy"]
    ranked = rank_sources(sources)
    # Local hardware (tier 0) should come first.
    assert ranked[0]["source_type"] in ("rtl_sdr", "airspy")
    assert ranked[0]["tier"] == 0
    # Free public (tier 2) should come last.
    assert ranked[-1]["source_type"] in ("kiwi", "openwebrx_remote")
    assert ranked[-1]["tier"] == 2


def test_rank_sources_empty():
    assert rank_sources([]) == []
