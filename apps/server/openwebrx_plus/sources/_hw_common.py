"""Shared plumbing for hardware source drivers.

Three pieces every hardware backend needs:

  * :class:`AsyncIqBridge` — pushes IQ blocks from a *driver callback thread*
    (USB callbacks, cffi/ctypes stream callbacks) into asyncio consumers.
    Bounded, drop-oldest: real-time sources must never block on a slow
    consumer, they drop the oldest queued block and count the loss.
  * :class:`RealtimePacer` — paces non-hardware sources (file replay,
    synthetic generators) so they emit samples at wall-clock real-time
    rate. Keeps pycsdr ring buffers and downstream subscribers honest
    (ADR-004 gotcha #2: rings overwrite slow consumers).
  * cu8/cs16 → complex64 converters — every driver normalizes to complex64
    at the source boundary so the rest of the system never sees a raw
    integer format (Source protocol contract).

numpy-only on purpose: this is live-path code (ADR-004 forbids scipy here).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections import deque
from collections.abc import AsyncIterator

import numpy as np

# ---------------------------------------------------------------------------
# Format converters (raw SDR byte orders → complex64)
# ---------------------------------------------------------------------------

# librtlsdr centers cu8 around 127.5 (0..255 → -1..+1). We use 127.4 like
# upstream csdr's u8 conversion, which compensates the documented +0.5 DC
# offset of the RTL2832U ADC.
_U8_CENTER = np.float32(127.4)
_U8_SCALE = np.float32(1.0 / 127.4)


def cu8_to_cf32(raw: np.ndarray) -> np.ndarray:
    """Interleaved uint8 I/Q (RTL-SDR native) → complex64 in [-1, 1]."""
    even = raw[: raw.size - (raw.size % 2)]
    floats = (even.astype(np.float32) - _U8_CENTER) * _U8_SCALE
    return floats.view(np.complex64)


def cs16_to_cf32(raw: np.ndarray) -> np.ndarray:
    """Interleaved int16 I/Q (Airspy / SDRplay native) → complex64."""
    even = raw[: raw.size - (raw.size % 2)]
    return (even.astype(np.float32) * np.float32(1.0 / 32768.0)).view(np.complex64)


# ---------------------------------------------------------------------------
# AsyncIqBridge — driver callback thread → asyncio consumer
# ---------------------------------------------------------------------------


class AsyncIqBridge:
    """Bounded queue bridging a hardware callback thread into asyncio.

    ``push()`` is called from the driver's stream callback (any thread);
    ``stream()`` is consumed from the event loop. When the queue is full
    the OLDEST block is dropped (real-time policy: stale IQ is worthless)
    and :attr:`dropped_blocks` counts the loss for observability.

    Typical wiring::

        bridge = AsyncIqBridge()
        bridge.bind()                     # inside async context, before start
        driver_start_rx(callback=lambda raw: bridge.push(convert(raw)))
        async for chunk in bridge.stream():
            ...
        bridge.close()                    # ends stream() after draining
    """

    def __init__(self, max_blocks: int = 32) -> None:
        if max_blocks < 1:
            raise ValueError("max_blocks must be >= 1")
        self._q: deque[np.ndarray] = deque()
        self._lock = threading.Lock()
        self._evt = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._max_blocks = max_blocks
        self.dropped_blocks = 0
        self.pushed_blocks = 0

    def bind(self) -> None:
        """Capture the running loop. Call once from async context."""
        self._loop = asyncio.get_running_loop()

    def push(self, arr: np.ndarray) -> None:
        """Thread-safe: enqueue one block of samples (any dtype)."""
        with self._lock:
            if self._closed:
                return
            if len(self._q) >= self._max_blocks:
                self._q.popleft()
                self.dropped_blocks += 1
            self._q.append(arr)
            self.pushed_blocks += 1
        self._wake()

    def close(self) -> None:
        """End the stream (after draining queued blocks). Thread-safe."""
        with self._lock:
            self._closed = True
        self._wake()

    def _wake(self) -> None:
        loop = self._loop
        if loop is None:
            return
        # Loop already shut down (process teardown) — nothing to wake.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._evt.set)

    async def stream(self) -> AsyncIterator[np.ndarray]:
        """Yield blocks until closed and drained."""
        while True:
            if not self._pending():
                if self._is_closed():
                    return
                self._evt.clear()
                # Re-check after clear to avoid a lost-wakeup race with push().
                if not self._pending() and not self._is_closed():
                    await self._evt.wait()
                continue
            yield self._pop()

    # -- small helpers keep the lock discipline in one place --
    def _pending(self) -> bool:
        with self._lock:
            return bool(self._q)

    def _is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def _pop(self) -> np.ndarray:
        with self._lock:
            return self._q.popleft()


# ---------------------------------------------------------------------------
# RealtimePacer — wall-clock pacing for file/synthetic sources
# ---------------------------------------------------------------------------


class RealtimePacer:
    """Pace a synthetic/file source to wall-clock real-time.

    Feels like hardware to everything downstream: pycsdr rings, hub
    subscribers, WS clients. Use::

        pacer = RealtimePacer(sample_rate)          # in spawn()
        for chunk in ...:
            yield chunk
            await pacer.pace(chunk.size)            # sleeps the remainder
    """

    def __init__(self, sample_rate: int, enabled: bool = True) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be > 0")
        self._rate = float(sample_rate)
        self._enabled = enabled
        self._t0: float | None = None
        self._samples = 0

    async def pace(self, n_samples: int) -> None:
        """Advance the schedule by n_samples, sleeping the remainder."""
        if not self._enabled:
            return
        self._samples += n_samples
        if self._t0 is None:
            self._t0 = time.monotonic()
            return
        target = self._samples / self._rate
        remaining = target - (time.monotonic() - self._t0)
        if remaining > 0:
            await asyncio.sleep(remaining)
