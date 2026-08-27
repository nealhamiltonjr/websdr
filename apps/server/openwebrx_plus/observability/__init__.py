"""Observability — structured logging, metrics, tracing.

Slice-1 status: structlog is wired up. Metrics (Prometheus) and tracing
(OpenTelemetry) land in slice-2 / slice-3.
"""

from .logging import configure_logging  # noqa: F401
