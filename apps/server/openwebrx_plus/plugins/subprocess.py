"""Subprocess decoder plumbing — ADR-003's second plugin family.

The contract between the server and ANY external decoder binary
(dump1090, dump978, AIS demods, …), pinned here so C binaries drop in
without server-side changes:

  * spawn      ``argv`` (binary + flags) from a :class:`SubprocessSpec`,
               with ``OWRX_RX_ID`` / ``OWRX_SAMPLE_RATE`` / ``OWRX_CENTER_FREQ``
               / ``OWRX_IQ_FORMAT`` added to the child environment.
  * IQ feed    complex-float32 numpy in → stdin bytes out, converted to the
               child's native layout (``cf32`` / ``cs16`` / ``cu8``).
  * events     one JSON object per line on stdout (NDJSON); each becomes a
               decoder event broadcast over the receiver WebSocket. A line
               ``{"kind": "ready", …}`` is the optional handshake and is
               consumed by the runner, not forwarded.
  * lifecycle  bounded crash-restart with backoff, then a terminal
               ``failed`` state plus a synthetic ``decoder_state`` event;
               teardown closes stdin first (graceful flush), waits, SIGKILLs.

The runner is deliberately single-threaded-asyncio: the stdout reader
task and the session's ``feed_iq`` calls run on the SAME event loop, so
the event deque needs no locking — events parsed between feeds are
drained by the next ``feed()`` (latency: one hub chunk, ~50 ms).

Memory safety on a wedged child: writes go to the asyncio transport
buffer; once it exceeds ``max_buffered_bytes`` further chunks are DROPPED
and counted (``dropped_chunks``) — the same drop-with-counters
backpressure philosophy as the IqHub, never an unbounded queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

import numpy as np
import structlog

from .base import (
    DecoderAttachContext,
    DecoderAttachError,
    DecoderBinaryMissing,
    DecoderPlugin,
)

log = structlog.get_logger(__name__)

IqFormat = Literal["cf32", "cs16", "cu8"]

RunnerState = Literal[
    "idle", "starting", "running", "restarting", "failed", "stopping", "stopped"
]

# readline() budget for one stdout line (aircraft snapshots stay ≪ this).
_STREAM_LIMIT = 4 * 1024 * 1024

# Upper bound on undelivered events between session feeds.
_MAX_EVENT_BACKLOG = 1024

# Grace period between stdin close and SIGKILL during teardown.
_STOP_TIMEOUT = 3.0

# Default transport-buffer ceiling before chunks start dropping.
_DEFAULT_MAX_BUFFERED = 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# IQ format conversion (hub cf32 → child's stdin layout)
# ---------------------------------------------------------------------------


def iq_to_bytes(iq: np.ndarray, fmt: IqFormat) -> bytes:
    """Convert complex-float32 IQ to interleaved stdin bytes.

    Scale conventions match the SDR world: full-scale ±1.0 maps to
    ±32767 (cs16) and 0..255 with mid-tread 127.5 (cu8). Values are
    clipped, never wrapped — a wrapped sample would fabricate a
    full-scale spectral splatter instead of an honest clip.
    """
    iq = np.ascontiguousarray(iq, dtype=np.complex64)
    if fmt == "cf32":
        little_endian: np.ndarray = iq.astype("<c8", copy=False)
        return little_endian.tobytes()
    inter = np.empty(iq.size * 2, dtype=np.float32)
    inter[0::2] = iq.real
    inter[1::2] = iq.imag
    if fmt == "cs16":
        pcm16: np.ndarray = (np.clip(inter, -1.0, 1.0) * 32767.0).astype("<i2")
        return pcm16.tobytes()
    pcm8: np.ndarray = np.clip(inter * 127.5 + 127.5, 0.0, 255.0).astype(np.uint8)
    return pcm8.tobytes()


# ---------------------------------------------------------------------------
# Spec + runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubprocessSpec:
    """Everything the runner needs to drive one external decoder binary."""

    argv: tuple[str, ...]
    #: stdin byte layout the child expects (converted from hub cf32).
    iq_format: IqFormat = "cf32"
    #: wait this many seconds for a ``{"kind": "ready"}`` line before
    #: declaring the attach failed; ``None`` feeds immediately (children
    #: that never print a ready line still work).
    ready_timeout: float | None = None
    #: backoff seconds between crash restarts; length = restart budget.
    restart_backoff: tuple[float, ...] = (0.5, 2.0, 8.0)
    #: drop IQ chunks once the child's transport buffer exceeds this.
    max_buffered_bytes: int = _DEFAULT_MAX_BUFFERED
    #: extra environment variables for the child (merged over os.environ).
    env: dict[str, str] = field(default_factory=dict)


class PluginRunner:
    """Async lifecycle around ONE external decoder process.

    The public surface matches ADR-003's ``PluginRunner`` sketch
    (spawn / feed / events / stop) with the events pull adapted to the
    session's synchronous ``feed_iq`` contract: ``feed()`` returns the
    events parsed since the previous call.
    """

    def __init__(self, spec: SubprocessSpec, *, label: str) -> None:
        self._spec = spec
        self._label = label
        self._state: RunnerState = "idle"
        self._proc: asyncio.subprocess.Process | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lifecycle: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._ready_evt = asyncio.Event()
        self._ready_payload: dict[str, Any] | None = None
        self._events: deque[dict[str, Any]] = deque()
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._restarts = 0
        self._dropped_chunks = 0
        self._parse_errors = 0
        self._event_overflows = 0
        self._context: DecoderAttachContext | None = None
        self._stopping = False

    # -- properties ---------------------------------------------------------

    @property
    def state(self) -> RunnerState:
        return self._state

    @property
    def ready_payload(self) -> dict[str, Any] | None:
        """The child's ``{"kind": "ready", …}`` line, if it sent one."""
        return self._ready_payload

    # -- lifecycle ----------------------------------------------------------

    async def start(self, context: DecoderAttachContext) -> None:
        """Spawn the child and (optionally) await its ready line.

        Raises:
            DecoderBinaryMissing: the argv[0] binary can't be executed.
            DecoderAttachError: no ready line within ``ready_timeout``.
        """
        if self._state != "idle":
            raise RuntimeError(f"{self._label}: runner already started (state={self._state})")
        self._context = context
        try:
            proc = await self._spawn_once()
        except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
            raise DecoderBinaryMissing(
                f"{self._label}: decoder binary not executable: {self._spec.argv[0]!r}"
            ) from exc
        self._state = "running"
        self._lifecycle = asyncio.create_task(
            self._supervise(proc), name=f"plugin-runner-{self._label}"
        )
        if self._spec.ready_timeout is not None:
            try:
                await asyncio.wait_for(self._ready_evt.wait(), self._spec.ready_timeout)
            except TimeoutError:
                await self.stop()
                raise DecoderAttachError(
                    f"{self._label}: decoder did not signal ready within "
                    f"{self._spec.ready_timeout}s (stdout: {self._stderr_tail})"
                ) from None
        log.info(
            "subprocess decoder started",
            decoder=self._label,
            pid=proc.pid,
            iq_format=self._spec.iq_format,
            argv=list(self._spec.argv),
        )

    async def stop(self) -> None:
        """Graceful teardown: stdin EOF → bounded wait → SIGKILL."""
        self._stopping = True
        self._state = "stopping"
        if self._lifecycle is not None:
            self._lifecycle.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._lifecycle
            self._lifecycle = None
        await self._reap()
        self._state = "stopped"
        log.info("subprocess decoder stopped", decoder=self._label, restarts=self._restarts)

    # -- feed + events --------------------------------------------------------

    def feed(self, iq: np.ndarray) -> list[dict[str, Any]]:
        """Write one cf32 IQ chunk to the child; return events since last feed."""
        if iq.size:
            if self._state == "running" and self._writer is not None:
                data = iq_to_bytes(iq, self._spec.iq_format)
                writer = self._writer
                if not writer.is_closing() and (
                    writer.transport.get_write_buffer_size() + len(data)
                    <= self._spec.max_buffered_bytes
                ):
                    try:
                        writer.write(data)
                    except (BrokenPipeError, ConnectionResetError):
                        # Child died mid-write; the supervisor will restart it.
                        self._dropped_chunks += 1
                else:
                    self._dropped_chunks += 1
            else:
                # Restarting / failed / stopping — the chunk has nowhere to go.
                self._dropped_chunks += 1
        events: list[dict[str, Any]] = []
        while self._events:
            events.append(self._events.popleft())
        return events

    def status(self) -> dict[str, Any]:
        """Live counters for the REST decoder-status surface."""
        out: dict[str, Any] = {
            "state": self._state,
            "restarts": self._restarts,
            "dropped_chunks": self._dropped_chunks,
            "parse_errors": self._parse_errors,
        }
        proc = self._proc
        if proc is not None and proc.returncode is None:
            out["pid"] = proc.pid
        if self._event_overflows:
            out["event_overflows"] = self._event_overflows
        if self._ready_payload is not None:
            out["ready"] = dict(self._ready_payload)
        if self._stderr_tail:
            out["stderr_tail"] = list(self._stderr_tail)
        return out

    # -- internals ------------------------------------------------------------

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self._spec.env)
        ctx = self._context
        if ctx is not None:
            env["OWRX_RX_ID"] = ctx.receiver_id
            env["OWRX_SAMPLE_RATE"] = str(ctx.sample_rate)
            env["OWRX_CENTER_FREQ"] = str(ctx.center_freq)
        env["OWRX_IQ_FORMAT"] = self._spec.iq_format
        return env

    async def _spawn_once(self) -> asyncio.subprocess.Process:
        proc = await asyncio.create_subprocess_exec(
            *self._spec.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._child_env(),
            limit=_STREAM_LIMIT,
        )
        self._proc = proc
        self._writer = proc.stdin
        self._ready_evt.clear()  # a new incarnation may send a fresh ready line
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(proc), name=f"plugin-runner-{self._label}-stderr"
        )
        return proc

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """Keep stderr flowing into a bounded tail (bring-up diagnostics)."""
        stderr = proc.stderr
        if stderr is None:
            return
        while True:
            line = await stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self._stderr_tail.append(text)
                log.debug("decoder stderr", decoder=self._label, line=text)

    async def _supervise(self, first_proc: asyncio.subprocess.Process) -> None:
        """Own the child lifecycle: run → (crash → backoff → respawn)* → exit."""
        proc = first_proc
        try:
            while True:
                await self._pump_stdout(proc)
                returncode = await proc.wait()
                if self._stopping:
                    return
                if self._restarts >= len(self._spec.restart_backoff):
                    self._state = "failed"
                    self._push_event(
                        {
                            "kind": "decoder_state",
                            "state": "failed",
                            "reason": f"decoder exited rc={returncode} after "
                            f"{self._restarts} restarts",
                            "restarts": self._restarts,
                        }
                    )
                    log.warning(
                        "subprocess decoder failed permanently",
                        decoder=self._label,
                        returncode=returncode,
                        restarts=self._restarts,
                    )
                    return
                delay = self._spec.restart_backoff[self._restarts]
                self._restarts += 1
                self._state = "restarting"
                self._push_event(
                    {
                        "kind": "decoder_state",
                        "state": "restarting",
                        "attempt": self._restarts,
                        "delay": delay,
                    }
                )
                log.warning(
                    "subprocess decoder crashed — restarting",
                    decoder=self._label,
                    returncode=returncode,
                    attempt=self._restarts,
                    delay=delay,
                )
                await asyncio.sleep(delay)
                proc = await self._spawn_once()
                self._state = "running"
        except asyncio.CancelledError:
            raise

    async def _pump_stdout(self, proc: asyncio.subprocess.Process) -> None:
        """Read NDJSON lines until EOF; parse events + the ready handshake."""
        stdout = proc.stdout
        assert stdout is not None  # PIPE was requested
        while True:
            line = await stdout.readline()
            if not line:
                return  # EOF — child side closed (exit or crash)
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                self._parse_errors += 1
                continue
            if not isinstance(event, dict):
                self._parse_errors += 1
                continue
            if event.get("kind") == "ready":
                self._ready_payload = event
                self._ready_evt.set()
                continue
            self._events.append(event)
            if len(self._events) > _MAX_EVENT_BACKLOG:
                self._events.popleft()
                self._event_overflows += 1

    def _push_event(self, event: dict[str, Any]) -> None:
        self._events.append(event)
        if len(self._events) > _MAX_EVENT_BACKLOG:
            self._events.popleft()
            self._event_overflows += 1

    async def _reap(self) -> None:
        """Close stdin, await exit (bounded), then SIGKILL; cancel helpers."""
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None
        writer, self._writer = self._writer, None
        proc = self._proc
        if writer is not None and not writer.is_closing():
            writer.close()
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), _STOP_TIMEOUT)
            if proc.returncode is None:  # wedged despite stdin EOF
                proc.kill()
                with contextlib.suppress(ProcessLookupError):
                    await proc.wait()
        # Drain any straggler stdout so the pipe doesn't block the child's exit.
        stdout = proc.stdout if proc is not None else None
        if stdout is not None:
            with contextlib.suppress(Exception):
                await stdout.read()


# ---------------------------------------------------------------------------
# Adapter: subprocess plugin as an in-process DecoderPlugin
# ---------------------------------------------------------------------------


class SubprocessDecoderPlugin(DecoderPlugin):
    """Bridge one external binary into the session's DecoderPlugin contract.

    Subclasses set ``manifest`` + ``spec`` (both ClassVars). The session
    calls ``on_attach`` → runner spawn; every hub chunk flows through
    ``feed_iq`` → stdin; stdout NDJSON returns as events on the NEXT
    feed cycle; ``astop`` awaits teardown.
    """

    spec: ClassVar[SubprocessSpec]

    def __init__(self, spec_override: SubprocessSpec | None = None) -> None:
        self._spec = spec_override if spec_override is not None else type(self).spec
        self._runner = PluginRunner(self._spec, label=self.manifest.name)

    # -- DecoderPlugin contract ------------------------------------------------

    async def on_attach(self, context: DecoderAttachContext) -> None:
        try:
            await self._runner.start(context)
        except (DecoderBinaryMissing, DecoderAttachError):
            raise
        except Exception as exc:  # defensive: spawn raised something exotic
            raise DecoderAttachError(
                f"{self.manifest.name}: failed to start decoder subprocess: {exc}"
            ) from exc

    def feed_iq(self, iq: np.ndarray) -> list[dict[str, Any]]:
        return self._runner.feed(iq)

    def stop(self) -> None:
        # Sync contract cannot await child exit; astop() does. If stop()
        # is called directly (legacy path), still run the FULL teardown —
        # scheduled on the loop that owns the runner.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop → no spawned child either
        loop.create_task(self._runner.stop(), name=f"plugin-runner-{self.manifest.name}-stop")

    async def astop(self) -> None:
        await self._runner.stop()

    def status(self) -> dict[str, Any]:
        return self._runner.status()
