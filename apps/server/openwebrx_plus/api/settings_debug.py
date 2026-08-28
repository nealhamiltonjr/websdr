"""REST endpoints for user settings and the in-app debugger.

Endpoints:
    GET  /api/settings                    → return current user settings
    PUT  /api/settings                    → partial update (deep-merge per section)
    POST /api/settings/reset              → reset to defaults
    GET  /api/debug/logs                  → recent log entries (filterable)
    GET  /api/debug/errors                → recent warnings+errors
    GET  /api/debug/stats                 → counts by level + buffer stats
    POST /api/debug/clear                 → drop all entries
    GET  /api/debug/export                → download all entries as JSON

The settings module persists to $XDG_CONFIG_HOME/openwebrx-plus/user-settings.toml
(or ~/.config/openwebrx-plus/user-settings.toml). See config/user_settings.py.

The debug module reads from the in-process ring buffer populated by the
structlog capture processor. See observability/debug_log.py.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ..config.user_settings import (
    get_user_settings_service,
)
from ..observability import get_debug_log_buffer

log = structlog.get_logger(__name__)


# --- Pydantic models for the settings endpoints ---


class DisplaySettingsPatch(BaseModel):
    theme: str | None = None
    waterfall_colormap: str | None = None
    spectrum_show_peak_hold: bool | None = None
    spectrum_averaging: str | None = None
    spectrum_decay_alpha: float | None = None
    freq_display_unit: str | None = None
    show_passband_overlay: bool | None = None


class AudioSettingsPatch(BaseModel):
    master_volume: float | None = None
    preferred_output_device: str | None = None
    default_squelch_db: float | None = None
    force_mono: bool | None = None


class DSPSettingsPatch(BaseModel):
    default_dsp_mode: str | None = None
    default_agc_enabled: bool | None = None
    default_low_cut_hz: int | None = None
    default_high_cut_hz: int | None = None
    default_notch_enabled: bool | None = None
    default_notch_freq_hz: float | None = None
    default_notch_q: float | None = None
    default_noise_blanker_enabled: bool | None = None
    default_noise_blanker_threshold: float | None = None


class SourcesSettingsPatch(BaseModel):
    default_source_type: str | None = None
    default_sample_rate: int | None = None
    default_center_freq: int | None = None


class DecoderSettingsPatch(BaseModel):
    auto_attach_adsb: bool | None = None
    auto_attach_ais: bool | None = None
    auto_attach_dump978: bool | None = None


class DebugSettingsPatch(BaseModel):
    log_capture_enabled: bool | None = None
    log_ring_capacity: int | None = None
    error_ring_capacity: int | None = None
    capture_async_exceptions: bool | None = None
    capture_unhandled_exceptions: bool | None = None


class UserSettingsPatch(BaseModel):
    """Body for PUT /api/settings. All sections optional — only present
    sections are mutated. Within a section, only present fields change."""

    display: DisplaySettingsPatch | None = None
    audio: AudioSettingsPatch | None = None
    dsp: DSPSettingsPatch | None = None
    sources: SourcesSettingsPatch | None = None
    decoders: DecoderSettingsPatch | None = None
    debug: DebugSettingsPatch | None = None


def register_settings_and_debug_routes(app: FastAPI) -> None:
    """Wire the settings + debug endpoints onto a FastAPI app."""

    # --- Settings -------------------------------------------------------------

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        """Return the current user settings (with the file's effective values,
        not just defaults — useful for the Settings panel to display)."""
        service = get_user_settings_service()
        return service.snapshot()

    @app.put("/api/settings")
    async def update_settings(patch: UserSettingsPatch) -> dict[str, Any]:
        """Partial-update user settings. Only sections present in the body are
        mutated; sections omitted are preserved as-is. Within a section,
        only present fields change.

        Validation: pydantic will reject invalid enum values with a 422.
        Range constraints (e.g. master_volume between 0 and 1) also
        surface as 422 with a clear message.
        """
        service = get_user_settings_service()
        # Convert the patch model to a dict that drops None values per section.
        patch_dict: dict[str, dict[str, Any]] = {}
        for section_name, section_patch in patch.model_dump(exclude_none=True).items():
            if isinstance(section_patch, dict):
                patch_dict[section_name] = section_patch
        if not patch_dict:
            # Empty patch — return current settings unchanged.
            return service.snapshot()
        try:
            updated = await service.update(patch_dict)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        log.info(
            "user settings updated",
            sections=list(patch_dict.keys()),
        )
        return updated.model_dump()

    @app.post("/api/settings/reset")
    async def reset_settings() -> dict[str, Any]:
        """Reset user settings to defaults and persist."""
        service = get_user_settings_service()
        await service.reset()
        log.info("user settings reset to defaults")
        return service.snapshot()

    # --- Debug ----------------------------------------------------------------

    @app.get("/api/debug/logs")
    async def get_debug_logs(
        level: str | None = Query(
            None, description="Filter by level (debug/info/warning/error/critical)."
        ),
        logger: str | None = Query(
            None, description="Filter by substring in logger name."
        ),
        message: str | None = Query(
            None, description="Filter by substring in message text."
        ),
        limit: int = Query(
            200, ge=1, le=2000, description="Maximum entries to return."
        ),
        offset: int = Query(0, ge=0, description="Skip the first N entries."),
    ) -> dict[str, Any]:
        """Return recent log entries, newest-first, filterable.

        The buffer is in-process and bounded (default 1000 entries). If
        you need more, bump ``log_ring_capacity`` in the user settings
        and the buffer will resize on next process start.
        """
        buf = get_debug_log_buffer()
        entries = await buf.get_entries(
            level=level,
            logger_substr=logger,
            message_substr=message,
            limit=limit,
            offset=offset,
        )
        stats = await buf.get_stats()
        return {
            "entries": [e.to_dict() for e in entries],
            "count": len(entries),
            "stats": stats,
            "filters": {
                "level": level,
                "logger": logger,
                "message": message,
                "limit": limit,
                "offset": offset,
            },
        }

    @app.get("/api/debug/errors")
    async def get_debug_errors(
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        """Return warnings+errors, newest-first. Used by the Debugger panel's
        error-only view."""
        buf = get_debug_log_buffer()
        entries = await buf.get_errors(limit=limit, offset=offset)
        stats = await buf.get_stats()
        return {
            "entries": [e.to_dict() for e in entries],
            "count": len(entries),
            "stats": stats,
        }

    @app.get("/api/debug/stats")
    async def get_debug_stats() -> dict[str, Any]:
        """Return counts by level + buffer capacities. Useful for a small
        dashboard widget in the Debugger panel."""
        buf = get_debug_log_buffer()
        return await buf.get_stats()

    @app.post("/api/debug/clear")
    async def clear_debug_logs() -> dict[str, str]:
        """Drop all entries from the ring buffer."""
        buf = get_debug_log_buffer()
        await buf.clear()
        log.info("debug log buffer cleared")
        return {"status": "cleared"}

    @app.get("/api/debug/export")
    async def export_debug_logs() -> PlainTextResponse:
        """Export all captured log entries as newline-delimited JSON.

        Format: one JSON object per line, ordered newest-first (matches
        the in-buffer order). Suitable for piping into ``jq`` or loading
        into another log analysis tool.
        """
        buf = get_debug_log_buffer()
        entries = await buf.get_entries(limit=10_000, offset=0)
        lines = [json.dumps(e.to_dict()) for e in entries]
        body = "\n".join(lines)
        return PlainTextResponse(
            content=body,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": "attachment; filename=openwebrx-plus-logs.ndjson"},
        )


__all__ = ["register_settings_and_debug_routes", "UserSettingsPatch"]
