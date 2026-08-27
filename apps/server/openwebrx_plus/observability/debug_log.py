"""Debug log ring buffer + structured error capture.

This module provides in-process capture of all log events (so the in-app
debugger panel can show them) plus a dedicated ring buffer for warnings
and errors (so the debugger panel can highlight problem events).

Design notes:

- The ring buffer is **process-local**. Multi-worker deployments would
  need a shared sink (e.g., Redis list) — see ADR for that future work.
  For our single-process uvicorn tier (dev/prod), this is sufficient.

- We attach the capture as a structlog **processor** so every log event
  that goes through structlog is mirrored into the ring buffer. The
  processor is a no-op until :func:`enable_debug_capture` is called,
  which :func:`configure_logging` does by default with a sane capacity.

- Thread safety: the buffer is single-threaded-asyncio by design (one
  event loop, one writer). We use :class:`asyncio.Lock` only to make
  concurrent reads from REST handlers safe against a writer mid-flight;
  we do NOT lock on individual ``add`` calls because the event loop is
  single-threaded and structlog processors run synchronously.

- Capacity defaults are sized for a typical debug session: 1000 events
  in the all-logs buffer, 200 in the errors-only buffer. These can be
  tuned via :class:`Settings.DebugSettings` (TODO slice-5.2).

This module is the backend half of the in-app debugger panel. The
frontend half lives in ``apps/web/src/components/DebugPanel.tsx`` and
polls ``GET /api/debug/logs`` and ``GET /api/debug/errors``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

LogLevel = Literal["debug", "info", "warning", "error", "critical"]
"""Canonical log level names (structlog uses these lowercase strings)."""

_LEVEL_RANK: dict[str, int] = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
    "critical": 50,
}


@dataclass(slots=True)
class LogEntry:
    """One captured log event."""

    timestamp: str  # ISO-8601 UTC
    level: LogLevel
    logger: str  # structlog logger name (usually __name__)
    message: str  # event string (structlog's ``event`` field)
    fields: dict[str, Any] = field(default_factory=dict)  # structured kwargs

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "logger": self.logger,
            "message": self.message,
            "fields": self.fields,
        }


class DebugLogRingBuffer:
    """In-memory ring buffer of recent log events.

    Capacity-bounded; oldest entries drop off the front when full.
    Lookups by level and substring are O(N) but N is small (default 1000).
    """

    def __init__(self, capacity: int = 1000, error_capacity: int = 200) -> None:
        self._all: deque[LogEntry] = deque(maxlen=capacity)
        self._errors: deque[LogEntry] = deque(maxlen=error_capacity)
        self._counts: dict[str, int] = dict.fromkeys(_LEVEL_RANK, 0)
        self._total_dropped: int = 0  # entries that fell off the front
        self._lock = asyncio.Lock()  # guards reads against concurrent writes

    def add(self, entry: LogEntry) -> None:
        """Add an entry to the buffer. Synchronous (called from a structlog
        processor, which runs on the same thread as the logger call).

        Note: ``deque(maxlen=N)`` silently drops the oldest item when
        full, so this method is O(1) and never raises on overflow.
        """
        if entry.level not in _LEVEL_RANK:
            # Treat unknown levels as info — structlog defaults to info.
            entry = LogEntry(
                timestamp=entry.timestamp,
                level="info",
                logger=entry.logger,
                message=entry.message,
                fields=entry.fields,
            )
        before_all = len(self._all)
        self._all.append(entry)
        if len(self._all) < before_all + 1:
            # deque dropped an entry to make room
            self._total_dropped += 1
        if _LEVEL_RANK[entry.level] >= _LEVEL_RANK["warning"]:
            self._errors.append(entry)
        self._counts[entry.level] = self._counts.get(entry.level, 0) + 1

    async def get_entries(
        self,
        level: str | None = None,
        logger_substr: str | None = None,
        message_substr: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[LogEntry]:
        """Return entries, newest-first, filtered by the given criteria."""
        async with self._lock:
            # deque iteration is oldest-first; we want newest-first.
            entries = list(reversed(self._all))
        if level is not None:
            level_lower = level.lower()
            entries = [e for e in entries if e.level == level_lower]
        if logger_substr is not None:
            logger_lower = logger_substr.lower()
            entries = [e for e in entries if logger_lower in e.logger.lower()]
        if message_substr is not None:
            msg_lower = message_substr.lower()
            entries = [e for e in entries if msg_lower in e.message.lower()]
        return entries[offset : offset + limit]

    async def get_errors(self, limit: int = 100, offset: int = 0) -> list[LogEntry]:
        """Return warnings+errors, newest-first."""
        async with self._lock:
            entries = list(reversed(self._errors))
        return entries[offset : offset + limit]

    async def get_stats(self) -> dict[str, Any]:
        """Return counts by level + total dropped count."""
        async with self._lock:
            counts = dict(self._counts)
            return {
                "counts_by_level": counts,
                "total_captured": sum(counts.values()),
                "total_dropped": self._total_dropped,
                "all_capacity": self._all.maxlen,
                "errors_capacity": self._errors.maxlen,
                "all_current": len(self._all),
                "errors_current": len(self._errors),
            }

    async def clear(self) -> None:
        """Drop all entries and reset counters."""
        async with self._lock:
            self._all.clear()
            self._errors.clear()
            for k in list(self._counts.keys()):
                self._counts[k] = 0
            self._total_dropped = 0


# Module-level singletons (one ring buffer per process).
_all_buffer = DebugLogRingBuffer(capacity=1000, error_capacity=200)


def get_debug_log_buffer() -> DebugLogRingBuffer:
    """Return the process-wide debug log ring buffer singleton."""
    return _all_buffer


def _structlog_capture_processor(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> Mapping[str, Any]:
    """structlog processor that mirrors each event into the ring buffer.

    Plugged into the structlog processor chain by :func:`configure_logging`
    AFTER all formatters run — so the event_dict shape is final. We pull
    out the canonical fields and store the rest as ``fields``.
    """
    level = str(event_dict.pop("level", "info")).lower()
    if level == "warn":  # structlog normalizes to "warning" but be safe
        level = "warning"
    timestamp = str(event_dict.pop("timestamp", ""))
    if not timestamp:
        timestamp = datetime.now(UTC).isoformat()
    logger_name = str(event_dict.pop("logger", ""))
    if not logger_name:
        logger_name = str(event_dict.pop("module", "") or __name__)
    message = str(event_dict.pop("event", ""))
    # Everything left in event_dict is structured fields.
    fields = {k: v for k, v in event_dict.items() if not k.startswith("_")}
    entry = LogEntry(
        timestamp=timestamp,
        level=level if level in _LEVEL_RANK else "info",  # type: ignore[arg-type]
        logger=logger_name,
        message=message,
        fields=fields,
    )
    _all_buffer.add(entry)
    # Re-build the event_dict for downstream processors (we popped fields).
    event_dict["level"] = level
    event_dict["timestamp"] = timestamp
    event_dict["logger"] = logger_name
    event_dict["event"] = message
    for k, v in fields.items():
        event_dict.setdefault(k, v)
    return event_dict


def enable_debug_capture() -> None:
    """Re-configure structlog to include the capture processor.

    Called automatically by :func:`configure_logging`. Exposed for
    tests that want to enable capture after a manual structlog reset.

    .. deprecated:: slice-5.1
       The capture processor is now wired directly in
       :func:`configure_logging`. This function is kept as a no-op for
       backwards compatibility with any caller that referenced it.
    """
    # No-op — the processor chain is built at configure_logging time and
    # includes the capture processor when ``debug_capture=True``.
    return None


def capture_unhandled_exception(exc_type: type, exc: BaseException, tb: Any) -> None:
    """sys.excepthook handler — captures unhandled crashes into the buffer.

    Wire this up at app startup:
        import sys
        sys.excepthook = capture_unhandled_exception
    """
    import traceback as _tb

    tb_text = "".join(_tb.format_exception(exc_type, exc, tb))
    entry = LogEntry(
        timestamp=datetime.now(UTC).isoformat(),
        level="critical",
        logger="excepthook",
        message=f"unhandled {exc_type.__name__}: {exc}",
        fields={
            "exception_type": exc_type.__name__,
            "exception": str(exc),
            "traceback": tb_text,
        },
    )
    _all_buffer.add(entry)
    # Also print to stderr so the crash is visible in the server log too.
    import sys as _sys

    print(tb_text, file=_sys.stderr)


def capture_async_exception(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """asyncio loop exception handler — captures async crash context.

    Wire up at app startup:
        asyncio.get_event_loop().set_exception_handler(capture_async_exception)
    """
    exc = context.get("exception")
    message = context.get("message", "async exception")
    entry = LogEntry(
        timestamp=datetime.now(UTC).isoformat(),
        level="error",
        logger="asyncio",
        message=str(message),
        fields={
            "exception_type": type(exc).__name__ if exc else "Unknown",
            "exception": str(exc) if exc else "",
            "context": {k: str(v) for k, v in context.items() if k != "exception"},
        },
    )
    _all_buffer.add(entry)
    # Also log via the default handler so the message reaches stdout.
    loop.default_exception_handler(context)


# Re-export for tests and external callers.
__all__ = [
    "DebugLogRingBuffer",
    "LogEntry",
    "LogLevel",
    "capture_async_exception",
    "capture_unhandled_exception",
    "enable_debug_capture",
    "get_debug_log_buffer",
]
