"""FftChain — pycsdr-backed FFT + log-power + DC-swap pipeline.

Wire topology (all pycsdr blocks, each running in its own AsyncRunner thread)::

    in_buf (COMPLEX_FLOAT) → Fft(fft_size, every_n=fft_size)
        → mid_buf (COMPLEX_FLOAT)
        → LogAveragePower(add_db, fft_size, avg_number)
        → pow_buf (FLOAT)
        → FftSwap(fft_size)
        → out_buf (FLOAT)

The receiver session pushes complex64 IQ bytes via :meth:`feed`. pycsdr
runs the chain on background threads; the ready frames are drained via
:meth:`drain` (non-blocking — returns an empty list when no full frame is
ready yet).
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque
from typing import TYPE_CHECKING

import structlog
from pycsdr.modules import Buffer, Fft, FftSwap, LogAveragePower
from pycsdr.types import Format

from .types import FftFrame

if TYPE_CHECKING:
    from pycsdr.modules import BufferReader

log = structlog.get_logger(__name__)


class FftChain:
    """Push-in / drain-out pycsdr FFT chain.

    Parameters
    ----------
    fft_size
        Number of bins per output frame (power of 2). Default 1024.
    avg_number
        Number of consecutive FFTs averaged into one output frame via
        ``LogAveragePower``. Larger = smoother waterfall but slower update.
        Default 1 (no averaging).
    add_db
        Constant dB offset added by ``LogAveragePower`` before clipping.
        Useful for calibration; downstream clients typically clip to
        ``[min_db, max_db]`` anyway. Default -10.0 to roughly match the
        legacy numpy implementation's dBFS scaling.
    """

    def __init__(
        self,
        *,
        fft_size: int = 1024,
        avg_number: int = 1,
        add_db: float = -10.0,
        center_freq: int = 0,
        sample_rate: int = 0,
        min_db: float = -100.0,
        max_db: float = -20.0,
    ) -> None:
        self.fft_size = fft_size
        self.avg_number = avg_number
        self.add_db = add_db
        self.center_freq = center_freq
        self.sample_rate = sample_rate
        self.min_db = min_db
        self.max_db = max_db

        # Build the pycsdr chain.
        #
        # Buffer sizes are in SAMPLES (not bytes). The input buffer must be
        # comfortably larger than the largest chunk the session will push,
        # because pycsdr's Buffer.write() raises BufferError instead of
        # blocking when the ring is full. 64K complex samples (512 KiB) is
        # generous for the 1024..65536-sample chunks sessions produce.
        in_buf_samples = max(65_536, fft_size * 16)
        self._in_buf = Buffer(Format.COMPLEX_FLOAT, in_buf_samples)
        self._mid_buf = Buffer(Format.COMPLEX_FLOAT, fft_size * 16)
        self._pow_buf = Buffer(Format.FLOAT, fft_size * 8)
        self._out_buf = Buffer(Format.FLOAT, fft_size * 8)
        # Largest single write we will attempt, in bytes (<= quarter ring).
        self._max_write_bytes = (in_buf_samples // 4) * 8
        # Staging for sub-window chunks (see feed()): hold back data until
        # at least 2 * fft_size samples accumulate, so the Fft module's
        # every_n_samples skip logic never discards partial windows.
        self._stage_threshold_bytes = fft_size * 2 * 8
        self._staging = bytearray()

        self._fft = Fft(fft_size, fft_size)
        self._log_power = LogAveragePower(
            add_db=add_db,
            fft_size=fft_size,
            avg_number=avg_number,
        )
        self._fft_swap = FftSwap(fft_size)

        # Wire the chain.
        self._fft.setReader(self._in_buf.getReader())
        self._fft.setWriter(self._mid_buf)
        self._log_power.setReader(self._mid_buf.getReader())
        self._log_power.setWriter(self._pow_buf)
        self._fft_swap.setReader(self._pow_buf.getReader())
        self._fft_swap.setWriter(self._out_buf)

        self._out_reader: BufferReader = self._out_buf.getReader()

        # Reader thread: pycsdr BufferReader.read() blocks until data is
        # available, which would stall the asyncio event loop if called
        # from a coroutine. Run it in a dedicated background thread that
        # pushes ready frames onto a queue.
        self._frames: deque[memoryview] = deque()
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"pycsdr-fft-{fft_size}",
            daemon=True,
        )
        self._reader_thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, iq_bytes: bytes) -> None:
        """Push a chunk of complex64 IQ bytes into the FFT chain.

        ``iq_bytes`` must be a multiple of 8 bytes (sizeof(complex64)); a
        trailing partial sample is dropped.

        Small chunks are STAGED: pycsdr's Fft module drops sub-window data
        through its every_n_samples skip logic when the AsyncRunner runs
        between small writes. We therefore accumulate input until at least
        ``2 * fft_size`` samples are pending, then write the whole batch to
        the ring in one go. Sessions that push fft_size-or-larger chunks
        (the normal case) bypass the staging buffer entirely.
        """
        chunk_len = (len(iq_bytes) // 8) * 8
        if chunk_len == 0:
            return
        data = iq_bytes[:chunk_len] if chunk_len != len(iq_bytes) else iq_bytes

        if len(data) < self._stage_threshold_bytes:
            self._staging += data
            if len(self._staging) < self._stage_threshold_bytes:
                return
            data = bytes(self._staging)
            self._staging.clear()

        offset = 0
        while offset < len(data):
            end = min(offset + self._max_write_bytes, len(data))
            self._write_with_backpressure(data[offset:end])
            offset = end

    def flush(self) -> None:
        """Write any staged sub-window samples into the ring immediately.

        Call when the source is idle/paused and you want the tail of the
        staged data to reach the chain (it may still produce no frame if
        fewer than fft_size samples are pending — that is inherent to the
        pycsdr Fft windowing).
        """
        if self._staging:
            data = bytes(self._staging)
            self._staging.clear()
            offset = 0
            while offset < len(data):
                end = min(offset + self._max_write_bytes, len(data))
                self._write_with_backpressure(data[offset:end])
                offset = end

    def _write_with_backpressure(self, chunk: bytes) -> None:
        """Write one ring-sized slice, retrying while the ring is full."""
        deadline = time.monotonic() + 10.0
        while True:
            try:
                self._in_buf.write(chunk)
                return
            except BufferError:
                if time.monotonic() >= deadline:
                    raise
                # Ring full — AsyncRunner needs a tick to drain it.
                time.sleep(0.005)

    def drain(self) -> list[FftFrame]:
        """Return all ready FFT frames (non-blocking).

        Each frame is a ``FftFrame`` containing a ``memoryview`` of
        ``fft_size * 4`` bytes (float32 dB power per bin).
        """
        out: list[FftFrame] = []
        with self._lock:
            while self._frames:
                out.append(
                    FftFrame(
                        bins=self._frames.popleft(),
                        fft_size=self.fft_size,
                        center_freq=self.center_freq,
                        sample_rate=self.sample_rate,
                        min_db=self.min_db,
                        max_db=self.max_db,
                    )
                )
        return out

    def stop(self) -> None:
        """Stop the reader thread and release pycsdr resources."""
        self._stop_evt.set()
        # Stop the pycsdr modules (their AsyncRunner threads).
        try:
            self._fft.stop()
            self._log_power.stop()
            self._fft_swap.stop()
        except Exception:  # noqa: BLE001
            log.debug("pycsdr stop raised", exc_info=True)
        # Wake the reader thread if it's blocked on read().
        with contextlib.suppress(Exception):
            self._out_reader.stop()
        # Give the reader a tick to unblock; it will exit on the next loop check.
        if self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)

    # ------------------------------------------------------------------
    # Internal reader loop
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        """Background thread that reads frames from pycsdr and queues them.

        pycsdr's ``BufferReader.read()`` returns *everything* currently
        available in the ring — not fft_size-framed chunks. This loop
        re-frames the byte stream: it accumulates incoming bytes in a
        staging buffer and emits one frame per ``fft_size * 4`` bytes,
        carrying any straddling remainder into the next iteration.
        """
        frame_bytes = self.fft_size * 4
        staging = bytearray()
        while not self._stop_evt.is_set():
            try:
                chunk = self._out_reader.read()
            except Exception:  # noqa: BLE001 — exit cleanly on stop
                return
            if not chunk:
                continue
            staging += bytes(chunk)
            # Emit as many complete frames as are ready.
            while len(staging) >= frame_bytes:
                frame = bytes(staging[:frame_bytes])
                del staging[:frame_bytes]
                with self._lock:
                    self._frames.append(memoryview(frame))

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def pending_frame_count(self) -> int:
        """Number of ready frames waiting to be drained."""
        with self._lock:
            return len(self._frames)
