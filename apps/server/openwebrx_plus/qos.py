"""QoS (Quality of Service) layer — source priority (slice-63).

Prioritizes sources: local SDR > paid federation > free public.
When a user requests a receiver, the QoS layer can rank available
sources by quality tier and recommend the best one.

Tiers:
  0 (highest): local hardware (rtl_sdr, airspy, sdrplay, soapy)
  1: paid federation (not yet implemented — future)
  2: free public (kiwi, spyserver, openwebrx_remote, sdrangel)

The QoS layer is a simple lookup table — no complex scheduling (v1).
"""

from __future__ import annotations

from typing import Any

# Source type → QoS tier (lower = higher priority).
_QOS_TIERS: dict[str, int] = {
    # Tier 0: local hardware.
    "rtl_sdr": 0,
    "airspy": 0,
    "sdrplay": 0,
    "soapy": 0,
    # Tier 0: local replay / simulated.
    "file": 0,
    "simulated": 0,
    # Tier 0: VFO (local DSP).
    "vfo": 0,
    # Tier 2: free public remotes.
    "kiwi": 2,
    "spyserver": 2,
    "openwebrx_remote": 2,
    "sdrangel": 2,
    "rtl_tcp": 1,  # could be local or remote — assume tier 1 (paid/managed).
}


def qos_tier(source_type: str) -> int:
    """Get the QoS tier for a source type (lower = higher priority)."""
    return _QOS_TIERS.get(source_type, 3)  # unknown = lowest priority


def qos_label(tier: int) -> str:
    """Human-readable label for a QoS tier."""
    if tier == 0:
        return "local"
    if tier == 1:
        return "managed"
    if tier == 2:
        return "free public"
    return "unknown"


def rank_sources(source_types: list[str]) -> list[dict[str, Any]]:
    """Rank sources by QoS tier (best first).

    Returns a list of dicts: {source_type, tier, label}.
    """
    ranked = sorted(source_types, key=qos_tier)
    return [
        {"source_type": s, "tier": qos_tier(s), "label": qos_label(qos_tier(s))}
        for s in ranked
    ]
