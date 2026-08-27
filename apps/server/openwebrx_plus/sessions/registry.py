"""SessionRegistry — module-level Map<UUID, ReceiverSession>.

Replaces the slice-1 global `_default_session` singleton. Now the backend
can host N concurrent ReceiverSessions, each addressable by its receiver_id.
The WS handler looks up by id; the REST API creates/lists/deletes sessions.

The registry is intentionally simple — a module-level dict protected by the
GIL. For a multi-process deployment we'd swap to Redis or a shared cache,
but for slice-2 (single-process uvicorn) this is fine.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

from ..config import Settings
from .receiver_session import ReceiverSession, create_default_session

# Module-level state. Don't access directly — use the functions below.
_sessions: dict[str, ReceiverSession] = {}


def init_default_sessions(settings: Settings) -> None:
    """Pre-create the default receiver session.

    Called once on app startup so the frontend's hardcoded `rx-default`
    subscription works out of the box. With the slice-3 defaults this
    replays the baked 20 m IQ fixture (hardware-free dev); configure
    ``default_source_type`` for live hardware.
    """
    if "rx-default" not in _sessions:
        session = create_default_session("rx-default", settings)
        _sessions["rx-default"] = session


def get(receiver_id: str) -> ReceiverSession | None:
    """Look up a session by id. Returns None if not found."""
    return _sessions.get(receiver_id)


def list_sessions() -> list[ReceiverSession]:
    """Return all known sessions, in insertion order."""
    return list(_sessions.values())


def create(
    *,
    receiver_id: str | None = None,
    center_freq: int = 14_205_000,
    sample_rate: int = 2_400_000,
    mode: str = "USB",
    source_type: str = "simulated",
    source_kwargs: dict[str, Any] | None = None,
) -> ReceiverSession:
    """Create a new ReceiverSession and register it.

    Args:
        receiver_id: explicit id, or None to auto-generate a UUID
        center_freq: tuned frequency in Hz
        sample_rate: source sample rate in Hz
        mode: demodulation mode
        source_type: key into SourceRegistry. Default "simulated"
            (always works, zero config — see ADR-004). Hardware types:
            rtl_sdr / airspy / sdrplay / soapy. "file" for IQ replay,
            "vfo" to tap another receiver's wideband stream (ADR-005).
        source_kwargs: optional kwargs passed through to the Source factory
            (e.g. {"file_path": ...} for FileSource, {"transport": "tcp"}
            for RtlSdrSource, {"parent_receiver_id": ...} for VfoTapSource)

    Returns the created session. Raises ValueError if the id is already
    in use. Raises KeyError if source_type is not registered. Raises
    LookupError for invalid VFO parents.
    """
    rid = receiver_id or f"rx-{uuid.uuid4().hex[:12]}"
    if rid in _sessions:
        raise ValueError(f"receiver_id already in use: {rid}")

    if source_type == "vfo":
        _validate_vfo_kwargs(source_kwargs, center_freq, sample_rate)

    # Use the SourceRegistry to instantiate the source. This replaces the
    # slice-2 hardcoded RtlSdrSource() with the plugin-discovery model
    # defined in ADR-004.
    from ..sources import SourceRegistry

    source = SourceRegistry.create(source_type, **(source_kwargs or {}))

    session = ReceiverSession(
        receiver_id=rid,
        source=source,
        center_freq=center_freq,
        sample_rate=sample_rate,
        mode=mode,
    )
    _sessions[rid] = session
    return session


async def destroy(receiver_id: str) -> bool:
    """Tear down a session and remove it from the registry.

    Returns True if a session was destroyed, False if the id was not found.
    """
    session = _sessions.pop(receiver_id, None)
    if session is None:
        return False
    await session.stop()
    return True


class VfoValidationError(ValueError):
    """A VFO spawn request is well-formed but rate-incompatible.

    Subclasses ValueError so existing callers keep catching it; REST maps
    it to 400 (bad request) rather than the id-collision 409.
    """


def _validate_vfo_kwargs(
    source_kwargs: dict[str, Any] | None,
    center_freq: int,
    sample_rate: int,
) -> None:
    """VFO taps need a live parent + a rate-compatible slice: check early so
    POST /api/receivers returns 400 (not a silently-dead 201 that only fails
    later inside the hub pump)."""
    kwargs = source_kwargs or {}
    parent_id = kwargs.get("parent_receiver_id")
    if not parent_id:
        raise LookupError(
            "source_type 'vfo' requires source_kwargs.parent_receiver_id"
        )
    parent = get(str(parent_id))
    if parent is None:
        raise LookupError(f"parent receiver {parent_id!r} not found")
    from ..sources.wideband import get_hub

    if get_hub(str(parent_id)) is None:
        raise LookupError(
            f"parent receiver {parent_id!r} is not streaming — start it "
            "before spawning VFO children"
        )

    # Mirror VfoChain's constraints (sources/wideband.py): the slice must fit
    # in the parent's band and decimate by an integer factor.
    offset_hz = center_freq - parent.center_freq
    if abs(offset_hz) + sample_rate / 2 > parent.sample_rate / 2 + 1:
        raise VfoValidationError(
            f"VFO slice [{offset_hz - sample_rate / 2:.0f}, "
            f"{offset_hz + sample_rate / 2:.0f}] Hz falls outside the "
            f"parent band ±{parent.sample_rate / 2:.0f} Hz"
        )
    if sample_rate <= 0:
        raise VfoValidationError(f"VFO sample_rate must be positive, got {sample_rate}")
    if parent.sample_rate % sample_rate != 0:
        raise VfoValidationError(
            f"parent rate {parent.sample_rate} not divisible by VFO rate "
            f"{sample_rate} (integer decimation required) — pick a divisor "
            f"of {parent.sample_rate} Hz"
        )


def iter_sessions() -> Iterator[ReceiverSession]:
    """Iterator over all sessions."""
    return iter(_sessions.values())
