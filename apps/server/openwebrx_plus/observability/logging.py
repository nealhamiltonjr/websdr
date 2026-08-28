"""Structured logging — structlog setup.

Usage:
    from openwebrx_plus.observability import configure_logging
    configure_logging("DEBUG")
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Mapping, MutableMapping
from typing import Any

import structlog


def configure_logging(level: str = "INFO", debug_capture: bool = True) -> None:
    """Configure structlog for the OpenWebRX+ backend.

    Slice-1: console renderer with timestamps. Slice-2 will switch to
    JSON output in prod, add trace IDs, and ship to a sink.

    Slice-5.1: optionally wires the debug log ring buffer into the
    processor chain so every log event is mirrored into the in-app
    debugger panel. Disable via debug_capture=False for tests that
    want a clean structlog chain.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    # structlog's processor type is complex; declare it as the structlog
    # processor callable shape so mypy doesn't infer a narrow union.
    ProcessorCallable = Callable[
        [Any, str, MutableMapping[str, Any]],
        "Mapping[str, Any] | str | bytes | bytearray | tuple[Any, ...]",
    ]
    processors: list[ProcessorCallable] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if debug_capture:
        # Insert the capture processor just before the renderer so we
        # see the fully-formed event without interfering with rendering.
        from .debug_log import _structlog_capture_processor

        processors.append(_structlog_capture_processor)
    processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
