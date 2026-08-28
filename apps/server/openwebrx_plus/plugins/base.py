"""Decoder plugin base — the ADR-003 v1 contract, now real.

Two plugin families share this module:

  - ``DecoderPlugin`` (in-process): pure-Python demod/decode running inside
    the server process, tapping IQ or audio directly (numpy in → event
    dicts out). The bundled ADS-B / Mode S decoder is the first of these.
    Zero subprocess overhead, trivially testable, the right home for
    protocols whose demod fits numpy comfortably.

  - ``SubprocessDecoder`` (external binary): dump1090, dump978, AIS, DAB,
    ACARS — fed IQ over stdin, emitting NDJSON on stdout, exactly the
    upstream OpenWebRX+ plugin convention (ADR-003). Shipped in slice-4.9:
    :mod:`openwebrx_plus.plugins.subprocess` (``PluginRunner`` + the
    ``SubprocessDecoderPlugin`` adapter) and the bundled ``dump1090``
    plugin (``plugins/dump1090.py``), verified against a
    protocol-faithful fake binary in tests.

Both families register in :mod:`openwebrx_plus.plugins.registry` and are
attached to ReceiverSessions by name via REST → session.attach_decoder().
Events fan out to every WS subscriber as JSON text frames:

    {"type": "decoder", "decoder": "adsb", "receiverId": "rx-…",
     "event": {"kind": "frame", …}}

Attach-time validation failures raise ``DecoderAttachError`` subclasses
so REST can map them to precise status codes (409 for duplicates, 400
for capability mismatches).
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

import numpy as np

TapPoint = Literal["rf_band", "audio_band"]


@dataclass(frozen=True)
class DecoderAttachContext:
    """Receiver facts handed to a plugin at attach time.

    Subprocess plugins forward these to their child via ``OWRX_*``
    environment variables; in-process plugins can ignore them.
    """

    receiver_id: str
    sample_rate: int
    center_freq: int


class DecoderAttachError(ValueError):
    """A decoder attach request is well-formed but the receiver can't host it."""


class DecoderAlreadyAttached(DecoderAttachError):
    """The receiver already runs a decoder of this name."""


class DecoderBinaryMissing(DecoderAttachError):
    """A subprocess decoder's binary could not be executed.

    Defined here (not in :mod:`.subprocess`) so REST can import every
    decoder error from one place; ``.subprocess`` re-exports it.
    """


class DecoderManifest:
    """Static description of a decoder plugin (name, tap point, events)."""

    def __init__(
        self,
        *,
        name: str,
        version: str,
        label: str,
        tap_point: TapPoint,
        description: str,
        required_sample_rate: int | None = None,
        events: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.version = version
        self.label = label
        self.tap_point = tap_point
        self.description = description
        self.required_sample_rate = required_sample_rate
        self.events = events

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "label": self.label,
            "tap_point": self.tap_point,
            "description": self.description,
            "required_sample_rate": self.required_sample_rate,
            "events": list(self.events),
        }


class DecoderPlugin(ABC):
    """In-process decoder plugin contract (ADR-003 v1, native variant).

    Subclasses set ``manifest`` and implement the feed for their tap
    point. Feeds are synchronous numpy work — cheap enough at SDR rates
    to run inline in the session's decoder task (the ADS-B demodulator
    processes a second of 2 MSPS IQ in ~15 ms).
    """

    manifest: ClassVar[DecoderManifest]

    def feed_iq(self, iq: np.ndarray) -> list[dict[str, Any]]:
        """Consume one complex-float32 IQ chunk; return decoded events."""
        return []

    def feed_audio(self, pcm: np.ndarray, sample_rate: int) -> list[dict[str, Any]]:
        """Consume one int16 mono audio chunk; return decoded events."""
        return []

    async def on_attach(self, context: DecoderAttachContext) -> None:  # noqa: B027
        """Async post-instantiation hook (called before the feed task starts).

        In-process plugins rarely need it; subprocess plugins spawn their
        child here so attach-time failures surface as ``DecoderAttachError``
        with a precise REST status code.
        """

    def stop(self) -> None:  # noqa: B027 — optional hook, not abstract
        """Release resources (final state flushes go through feed paths)."""

    async def astop(self) -> None:
        """Async teardown — awaited by session.detach_decoder().

        Default: the sync ``stop()`` hook. Subprocess plugins override to
        await child-process exit (bounded, then SIGKILL) so tests and REST
        observe deterministic teardown.
        """
        self.stop()

    def status(self) -> dict[str, Any]:
        """Live counters for GET /api/receivers/{id}/decoders."""
        return {}
