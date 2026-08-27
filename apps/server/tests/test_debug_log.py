"""Tests for the debug log ring buffer (slice-5.1).

Covers: add, get_entries (filtering), get_errors, get_stats, clear,
capacity eviction, level normalization, async exception capture,
unhandled exception capture.
"""

from __future__ import annotations

import asyncio

import structlog

from openwebrx_plus.observability import (
    DebugLogRingBuffer,
    capture_async_exception,
    capture_unhandled_exception,
    configure_logging,
    get_debug_log_buffer,
)
from openwebrx_plus.observability.debug_log import LogEntry


def _entry(level: str, message: str = "msg", logger: str = "test", **fields: object) -> LogEntry:
    return LogEntry(
        timestamp="2026-01-01T00:00:00Z",
        level=level,  # type: ignore[arg-type]
        logger=logger,
        message=message,
        fields=dict(fields),
    )


def test_add_and_get_returns_newest_first() -> None:
    buf = DebugLogRingBuffer(capacity=10, error_capacity=10)
    buf.add(_entry("info", "first"))
    buf.add(_entry("info", "second"))
    buf.add(_entry("info", "third"))
    entries = asyncio.run(buf.get_entries(limit=100))
    assert len(entries) == 3
    assert entries[0].message == "third"
    assert entries[2].message == "first"


def test_capacity_eviction_drops_oldest() -> None:
    buf = DebugLogRingBuffer(capacity=3, error_capacity=3)
    for i in range(5):
        buf.add(_entry("info", f"msg-{i}"))
    entries = asyncio.run(buf.get_entries(limit=100))
    assert len(entries) == 3
    # Newest 3 should be msg-2, msg-3, msg-4 (newest-first)
    assert entries[0].message == "msg-4"
    assert entries[-1].message == "msg-2"
    stats = asyncio.run(buf.get_stats())
    assert stats["total_dropped"] == 2


def test_level_filter_only_returns_matching_level() -> None:
    buf = DebugLogRingBuffer(capacity=100, error_capacity=100)
    buf.add(_entry("debug", "dbg"))
    buf.add(_entry("info", "info"))
    buf.add(_entry("warning", "warn"))
    buf.add(_entry("error", "err"))
    only_errors = asyncio.run(buf.get_entries(level="error"))
    assert len(only_errors) == 1
    assert only_errors[0].message == "err"


def test_unknown_level_normalizes_to_info() -> None:
    buf = DebugLogRingBuffer(capacity=10, error_capacity=10)
    entry = LogEntry(
        timestamp="2026-01-01T00:00:00Z",
        level="bogus",  # type: ignore[arg-type]
        logger="test",
        message="msg",
    )
    buf.add(entry)
    entries = asyncio.run(buf.get_entries())
    assert entries[0].level == "info"


def test_logger_substring_filter_case_insensitive() -> None:
    buf = DebugLogRingBuffer(capacity=100, error_capacity=100)
    buf.add(_entry("info", "msg", logger="openwebrx_plus.api.rest"))
    buf.add(_entry("info", "msg", logger="openwebrx_plus.sources.kiwi"))
    entries = asyncio.run(buf.get_entries(logger_substr="API"))
    assert len(entries) == 1
    assert "api" in entries[0].logger


def test_message_substring_filter() -> None:
    buf = DebugLogRingBuffer(capacity=100, error_capacity=100)
    buf.add(_entry("info", "starting receiver"))
    buf.add(_entry("info", "stopping receiver"))
    buf.add(_entry("info", "fixture loaded"))
    entries = asyncio.run(buf.get_entries(message_substr="receiver"))
    assert len(entries) == 2


def test_get_errors_returns_only_warnings_and_above() -> None:
    buf = DebugLogRingBuffer(capacity=100, error_capacity=100)
    buf.add(_entry("debug", "d"))
    buf.add(_entry("info", "i"))
    buf.add(_entry("warning", "w"))
    buf.add(_entry("error", "e"))
    buf.add(_entry("critical", "c"))
    errors = asyncio.run(buf.get_errors())
    assert len(errors) == 3
    levels = [e.level for e in errors]
    assert "warning" in levels
    assert "error" in levels
    assert "critical" in levels


def test_get_stats_counts_by_level() -> None:
    buf = DebugLogRingBuffer(capacity=100, error_capacity=100)
    buf.add(_entry("info", "a"))
    buf.add(_entry("info", "b"))
    buf.add(_entry("warning", "c"))
    buf.add(_entry("error", "d"))
    stats = asyncio.run(buf.get_stats())
    assert stats["counts_by_level"]["info"] == 2
    assert stats["counts_by_level"]["warning"] == 1
    assert stats["counts_by_level"]["error"] == 1
    assert stats["total_captured"] == 4
    assert stats["all_capacity"] == 100


def test_clear_resets_all_state() -> None:
    buf = DebugLogRingBuffer(capacity=100, error_capacity=100)
    for i in range(10):
        buf.add(_entry("info", f"msg-{i}"))
    asyncio.run(buf.clear())
    entries = asyncio.run(buf.get_entries())
    assert len(entries) == 0
    stats = asyncio.run(buf.get_stats())
    assert stats["total_captured"] == 0
    assert stats["total_dropped"] == 0


def test_offset_and_limit_pagination() -> None:
    buf = DebugLogRingBuffer(capacity=100, error_capacity=100)
    for i in range(10):
        buf.add(_entry("info", f"msg-{i:02d}"))
    page1 = asyncio.run(buf.get_entries(limit=3, offset=0))
    page2 = asyncio.run(buf.get_entries(limit=3, offset=3))
    assert [e.message for e in page1] == ["msg-09", "msg-08", "msg-07"]
    assert [e.message for e in page2] == ["msg-06", "msg-05", "msg-04"]


def test_structlog_capture_processor_records_to_ring_buffer() -> None:
    """When the capture processor is wired (default via configure_logging),
    every structlog event should land in the ring buffer."""
    configure_logging("INFO", debug_capture=True)
    test_log = structlog.get_logger("test.capture")
    test_log.info("hello", key="value")
    test_log.warning("oops", reason="bad")
    buf = get_debug_log_buffer()
    asyncio.run(buf.clear())
    # Re-log after clear so we have a known state.
    test_log.info("post-clear-event")
    test_log.error("post-clear-error")
    entries = asyncio.run(buf.get_entries())
    messages = [e.message for e in entries]
    assert "post-clear-event" in messages
    assert "post-clear-error" in messages
    errors = asyncio.run(buf.get_errors())
    assert any(e.message == "post-clear-error" for e in errors)


def test_capture_unhandled_exception_records_critical() -> None:
    buf = get_debug_log_buffer()
    asyncio.run(buf.clear())
    try:
        raise RuntimeError("synthetic crash")
    except RuntimeError as exc:
        capture_unhandled_exception(RuntimeError, exc, exc.__traceback__)
    errors = asyncio.run(buf.get_errors())
    assert any(
        "unhandled" in e.message and "RuntimeError" in e.message for e in errors
    )
    assert any(e.level == "critical" for e in errors)


def test_capture_async_exception_records_error() -> None:
    buf = get_debug_log_buffer()
    asyncio.run(buf.clear())
    # Python 3.12: get_event_loop() raises without a running loop; create one
    # explicitly. We just need a loop-shaped object for the handler signature.
    loop = asyncio.new_event_loop()
    try:
        try:
            raise ValueError("async boom")
        except ValueError as exc:
            context = {
                "message": "task crashed",
                "exception": exc,
            }
            capture_async_exception(loop, context)
    finally:
        loop.close()
    errors = asyncio.run(buf.get_errors())
    assert any("task crashed" in e.message for e in errors)
    assert any(e.level == "error" for e in errors)


def test_to_dict_round_trip_shape() -> None:
    entry = LogEntry(
        timestamp="2026-01-01T00:00:00Z",
        level="info",
        logger="test",
        message="hello",
        fields={"key": "value", "count": 42},
    )
    d = entry.to_dict()
    assert d["timestamp"] == "2026-01-01T00:00:00Z"
    assert d["level"] == "info"
    assert d["logger"] == "test"
    assert d["message"] == "hello"
    assert d["fields"]["key"] == "value"
    assert d["fields"]["count"] == 42
