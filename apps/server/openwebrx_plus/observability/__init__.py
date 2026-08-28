"""Observability — structured logging, metrics, tracing, debug capture.

- structlog wired up (slice-1)
- in-process debug log ring buffer + structured error capture (slice-5.1)
  — feeds the in-app Debugger panel via /api/debug/* endpoints.
- Prometheus metrics + OpenTelemetry tracing land in later slices.
"""

from .debug_log import (  # noqa: F401
    DebugLogRingBuffer,
    LogEntry,
    capture_async_exception,
    capture_unhandled_exception,
    enable_debug_capture,
    get_debug_log_buffer,
)
from .logging import configure_logging  # noqa: F401

__all__ = [
    "DebugLogRingBuffer",
    "LogEntry",
    "capture_async_exception",
    "capture_unhandled_exception",
    "configure_logging",
    "enable_debug_capture",
    "get_debug_log_buffer",
]
