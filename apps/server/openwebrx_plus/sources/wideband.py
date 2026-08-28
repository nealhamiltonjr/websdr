"""VFO sub-receivers over one wideband capture — ADR-005.

Architecture::

    parent ReceiverSession (owns hardware / file source, wide rate)
        └─ IqHub  (one pump task reading the source, N subscriber queues)
            ├─ parent session's own FFT/audio chains (full span)
            ├─ VfoTapSource #1 ─ pycsdr Shift → FirDecimate → 12 kHz IQ
            │     └─ child ReceiverSession (its own FFT + audio + WS clients)
            └─ VfoTapSource #2 ─ ...

Key contract points (full rationale in ADR/005-vfo-wideband-source.md):

  * The parent source is spawned exactly once, no matter how many VFOs tap
    it. The hub is keyed by the parent's receiver_id and destroyed with it.
  * Every subscriber gets a bounded queue with drop-oldest backpressure —
    real-time streams never block on a slow consumer.
  * A VFO tap is a pure-software DDC (digital down-converter): Shift the
    wanted slice to DC, then FirDecimate to the VFO rate. Both blocks are
    SIMD C++ via pycsdr — the same engine as the audio path (ADR-004).
  * Constraints enforced at spawn: the VFO slice must fit inside the
    parent span, and parent_rate / vfo_rate must be an integer.
  * Hardware-VFO fast path (RSPduo dual tuner) is deliberately NOT here —
    software taps cover every source; per-tuner mapping lands with
    hardware bring-up (ADR-005 §Hardware VFOs).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import structlog
from pycsdr.modules import Buffer, FirDecimate, Shift
from pycsdr.types import Format

from .base import SourceInfo

if TYPE_CHECKING:
    from pycsdr.modules import BufferReader

    from ..sessions.receiver_session import ReceiverSession

log = structlog.get_logger(__name__)

_SENTINEL = None  # queue item meaning "stream ended"

# How many subscribers one hub serves (the parent session + VFO taps).
# CPU budget rule-of-thumb from ADR-005: each VFO costs ~1 parent-rate
# Shift+FirDecimate (~11 Msps measured); 7 taps on a 2.4 MSPS parent is
# comfortably real-time on a NUC-class CPU.
DEFAULT_MAX_SUBSCRIBERS = 8


# ---------------------------------------------------------------------------
# VfoChain — pycsdr Shift → FirDecimate (the DDC)
# ---------------------------------------------------------------------------


class VfoChain:
    """Extract one VFO slice from wideband IQ (pycsdr, push-in / drain-out).

    Wire topology::

        in_buf (COMPLEX_FLOAT) → Shift(rate=offset/input_rate)
            → shifted_buf → FirDecimate(decimation) → out_buf (COMPLEX_FLOAT)

    Mirrors the front half of ``AudioChain`` (see ADR-004 gotchas: ring
    sizing, write-with-backpressure, reader thread).
    """

    def __init__(
        self,
        *,
        input_rate: int,
        output_rate: int,
        offset_hz: float,
        frame_samples: int = 2048,
    ) -> None:
        decimation = round(input_rate / output_rate)
        if decimation < 1:
            raise ValueError(
                f"VFO rate {output_rate} must be <= parent rate {input_rate}"
            )
        if input_rate % output_rate != 0:
            raise ValueError(
                f"parent rate {input_rate} not divisible by VFO rate "
                f"{output_rate} (integer decimation required)"
            )
        if abs(offset_hz) + output_rate / 2 > input_rate / 2 + 1:
            raise ValueError(
                f"VFO slice [{offset_hz - output_rate / 2:.0f}, "
                f"{offset_hz + output_rate / 2:.0f}] Hz falls outside the "
                f"parent span ±{input_rate / 2:.0f} Hz"
            )

        self.input_rate = input_rate
        self.output_rate = output_rate
        self.offset_hz = offset_hz
        self.decimation = decimation
        self.frame_samples = frame_samples

        in_buf_samples = max(65_536, input_rate // 32)
        self._in_buf = Buffer(Format.COMPLEX_FLOAT, in_buf_samples)
        self._shifted_buf = Buffer(Format.COMPLEX_FLOAT, in_buf_samples)
        self._out_buf = Buffer(Format.COMPLEX_FLOAT, max(16_384, output_rate))
        self._max_write_bytes = (in_buf_samples // 4) * 8

        # pycsdr Shift convention (verified empirically — see ADR-004 gotcha
        # #7): Shift(rate) multiplies by exp(+2πi·rate·n), moving the
        # spectrum UP by rate×input_rate. A slice at +offset reaches DC with
        # rate = −offset/input_rate.
        self._shift = Shift(rate=-offset_hz / input_rate)
        self._shift.setReader(self._in_buf.getReader())
        self._shift.setWriter(self._shifted_buf)

        self._fir_decimate = FirDecimate(decimation=decimation, transition=0.05, cutoff=0.47)
        self._fir_decimate.setReader(self._shifted_buf.getReader())
        self._fir_decimate.setWriter(self._out_buf)

        self._out_reader: BufferReader = self._out_buf.getReader()

        self._frames: deque[np.ndarray] = deque()
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"vfo-tap-{int(offset_hz)}",
            daemon=True,
        )
        self._reader_thread.start()

    def feed(self, iq_bytes: bytes) -> None:
        """Push complex64 IQ bytes (at the PARENT rate)."""
        chunk_len = (len(iq_bytes) // 8) * 8
        if chunk_len == 0:
            return
        data = iq_bytes[:chunk_len] if chunk_len != len(iq_bytes) else iq_bytes
        offset = 0
        while offset < len(data):
            end = min(offset + self._max_write_bytes, len(data))
            self._write_with_backpressure(data[offset:end])
            offset = end

    def _write_with_backpressure(self, chunk: bytes) -> None:
        deadline = time.monotonic() + 10.0
        while True:
            try:
                self._in_buf.write(chunk)
                return
            except BufferError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)

    def drain(self) -> list[np.ndarray]:
        """Ready frames: complex64 arrays of frame_samples at the VFO rate."""
        out: list[np.ndarray] = []
        with self._lock:
            while self._frames:
                out.append(self._frames.popleft())
        return out

    def stop(self) -> None:
        self._stop_evt.set()
        for stage in ("_shift", "_fir_decimate"):
            mod = getattr(self, stage, None)
            if mod is None:
                continue
            try:
                mod.stop()
            except Exception:  # noqa: BLE001
                log.debug("vfo chain stage stop raised", stage=stage, exc_info=True)
        with contextlib.suppress(Exception):
            self._out_reader.stop()
        if self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)

    def _reader_loop(self) -> None:
        frame_bytes = self.frame_samples * 8
        staging = bytearray()
        while not self._stop_evt.is_set():
            try:
                chunk = self._out_reader.read()
            except Exception:  # noqa: BLE001 — exits on stop()
                return
            if not chunk:
                continue
            staging += bytes(chunk)
            while len(staging) >= frame_bytes:
                frame = bytes(staging[:frame_bytes])
                del staging[:frame_bytes]
                arr = np.frombuffer(frame, dtype=np.complex64).copy()
                with self._lock:
                    self._frames.append(arr)


# ---------------------------------------------------------------------------
# IqHub — one parent stream, N subscribers
# ---------------------------------------------------------------------------


class IqHub:
    """Fan-out one parent source stream to N bounded subscriber queues.

    The parent source is spawned once by a background pump task; every
    subscriber (the parent session itself, plus VFO taps) gets its own
    ``asyncio.Queue`` fed with the SAME chunk objects (no copies). Queues
    drop-oldest when full and count the loss.
    """

    def __init__(
        self,
        *,
        receiver_id: str,
        source: Any,
        center_freq: int,
        sample_rate: int,
        gain: float | None = None,
        max_subscribers: int = DEFAULT_MAX_SUBSCRIBERS,
        queue_size: int = 32,
    ) -> None:
        self.receiver_id = receiver_id
        self.source = source
        self.center_freq = center_freq
        self.sample_rate = sample_rate
        self.gain = gain
        self.max_subscribers = max_subscribers
        self._queue_size = queue_size
        self._subscribers: dict[int, asyncio.Queue[Any]] = {}
        self._next_sub_id = 0
        self._pump_task: asyncio.Task[None] | None = None
        self.dropped_chunks = 0
        self._stopping = False

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def start(self) -> None:
        """Spawn the pump (idempotent)."""
        if self._pump_task is not None and not self._pump_task.done():
            return
        self._stopping = False
        self._pump_task = asyncio.create_task(self._pump(), name=f"iqhub-{self.receiver_id}")

    async def stop(self) -> None:
        """Cancel the pump, close the source, sentinel all subscribers."""
        self._stopping = True
        if self._pump_task is not None:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump_task
            self._pump_task = None
        with contextlib.suppress(Exception):
            await self.source.close()
        # Sentinels: every subscriber's stream() ends gracefully.
        for q in list(self._subscribers.values()):
            self._offer(q, _SENTINEL)
        self._subscribers.clear()

    def subscribe(self) -> asyncio.Queue[Any]:
        if self.subscriber_count >= self.max_subscribers:
            raise RuntimeError(
                f"hub {self.receiver_id!r} is at its subscriber budget "
                f"({self.max_subscribers}); close a VFO first"
            )
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers[self._next_sub_id] = q
        self._next_sub_id += 1
        return q

    def unsubscribe(self, q: asyncio.Queue[Any]) -> None:
        for sid, sub in list(self._subscribers.items()):
            if sub is q:
                del self._subscribers[sid]
                return

    async def stream(self) -> AsyncIterator[np.ndarray]:
        """Subscribe → yield chunks → unsubscribe. Ends on the sentinel."""
        q = self.subscribe()
        try:
            while True:
                chunk = await q.get()
                if chunk is _SENTINEL:
                    return
                yield chunk
        finally:
            self.unsubscribe(q)

    def _offer(self, q: asyncio.Queue[Any], item: Any) -> None:
        """put_nowait with drop-oldest backpressure; never blocks."""
        while True:
            try:
                q.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    self.dropped_chunks += 1
                except asyncio.QueueEmpty:
                    pass

    async def _pump(self) -> None:
        try:
            async for chunk in self.source.spawn(self.center_freq, self.sample_rate, self.gain):
                if self._stopping:
                    return
                for q in list(self._subscribers.values()):
                    self._offer(q, chunk)
                # Yield to the loop EVERY chunk: unpaced sources (tests,
                # realtime=False) never suspend on their own, and without
                # this the pump would starve its own subscribers.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — pump death must not kill the loop
            log.exception(
                "iq hub pump error",
                receiver_id=self.receiver_id,
                source=getattr(self.source, "info", None),
            )
        finally:
            for q in list(self._subscribers.values()):
                self._offer(q, _SENTINEL)


# ---------------------------------------------------------------------------
# Hub registry
# ---------------------------------------------------------------------------

_hubs: dict[str, IqHub] = {}


def get_hub(receiver_id: str) -> IqHub | None:
    return _hubs.get(receiver_id)


def register_hub(hub: IqHub) -> IqHub:
    """Register an externally-constructed hub (tests, advanced wiring).

    The session path uses get_or_create_hub; this is for callers that build
    an IqHub directly and want VfoTapSources to be able to find it.
    """
    _hubs[hub.receiver_id] = hub
    return hub


def get_or_create_hub(session: ReceiverSession) -> IqHub:
    hub = _hubs.get(session.receiver_id)
    if hub is None:
        hub = IqHub(
            receiver_id=session.receiver_id,
            source=session.source,
            center_freq=session.center_freq,
            sample_rate=session.sample_rate,
            # Pre-start manual gain (slice-4.7) flows into source.spawn();
            # later changes go through Source.set_runtime_gain directly.
            gain=getattr(session, "gain", None),
        )
        _hubs[session.receiver_id] = hub
    return hub


async def destroy_hub(receiver_id: str) -> None:
    hub = _hubs.pop(receiver_id, None)
    if hub is not None:
        await hub.stop()


# ---------------------------------------------------------------------------
# VfoTapSource — a Source that taps a parent hub
# ---------------------------------------------------------------------------


@dataclass
class VfoTapSource:
    """A VFO sub-receiver (ADR-001 Feature #2, ADR-005).

    spawn(center_freq, sample_rate, gain) taps the parent receiver's
    wideband stream and yields complex64 IQ at ``sample_rate``, centered
    on ``center_freq`` — a drop-in Source for a child ReceiverSession.

    Args:
        parent_receiver_id: the parent session whose hub to tap. The parent
            must be STARTED (its hub pumps) before the VFO can stream.
    """

    parent_receiver_id: str
    max_parent_rate: int = 10_000_000
    info: SourceInfo = field(default_factory=lambda: SourceInfo(
        type="vfo",
        label="VFO",
        sample_rate=12_000,
    ))

    async def spawn(
        self,
        center_freq: int,
        sample_rate: int,
        gain: float | None = None,
    ) -> AsyncGenerator[np.ndarray, None]:
        hub = get_hub(self.parent_receiver_id)
        if hub is None:
            raise RuntimeError(
                f"parent receiver {self.parent_receiver_id!r} is not "
                "streaming — start it before spawning VFO children"
            )
        _ = self.max_parent_rate  # reserved for CPU-budget admission control
        offset_hz = float(center_freq - hub.center_freq)
        # VfoChain validates: span containment + integer decimation.
        chain = VfoChain(
            input_rate=hub.sample_rate,
            output_rate=sample_rate,
            offset_hz=offset_hz,
        )
        object.__setattr__(
            self,
            "info",
            SourceInfo(
                type="vfo",
                label=f"VFO {center_freq / 1e6:.6f} MHz ← {self.parent_receiver_id}",
                endpoint=self.parent_receiver_id,
                sample_rate=sample_rate,
            ),
        )
        log.info(
            "VfoTapSource streaming",
            parent=self.parent_receiver_id,
            center_freq=center_freq,
            sample_rate=sample_rate,
            offset_hz=offset_hz,
            decimation=chain.decimation,
        )
        try:
            async for chunk in hub.stream():
                chain.feed(np.ascontiguousarray(chunk, dtype=np.complex64).tobytes())
                for frame in chain.drain():
                    yield frame
        finally:
            chain.stop()

    async def close(self) -> None:
        return None
