"""REST API — FastAPI app factory.
Endpoints:
    GET  /                  → redirect to /app/ (the frontend)
    GET  /api/health        → liveness probe
    GET  /api/version       → version info
    GET  /api/sources       → list available Source manifests (ADR-004)
    GET  /api/hardware      → probe locally connected SDRs (slice-3)
    GET  /api/directory/kiwi     → public KiwiSDR receivers (ADR-006)
    GET  /api/directory/receiverbook → public OpenWebRX receivers (ADR-006)
    GET  /api/receivers     → list active ReceiverSessions
    POST /api/receivers     → spawn a new ReceiverSession (returns its id)
    DELETE /api/receivers/{receiver_id} → tear down a session

The frontend (apps/web) is served by Vite in dev, by a static file
mount in production. The REST API is for control; all streams go
over WebSocket (see ws.py).
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ..config import Settings
from ..plugins.base import (
    DecoderAlreadyAttached,
    DecoderAttachError,
    DecoderBinaryMissing,
)
from ..plugins.registry import decoder_registry
from ..sessions import (
    create_session,
    destroy_session,
    get_session,
    init_default_sessions,
    list_sessions,
)
from ..sessions.registry import VfoValidationError
from ..sources import SourceRegistry
from ..sources.directory import DirectoryUnavailable, directory_service

log = structlog.get_logger(__name__)


def _list_fixtures() -> list[dict[str, Any]]:
    """Scan the baked IQ fixtures (sync — Path I/O stays out of the loop)."""
    import json as _json
    from pathlib import Path

    fixture_dir = Path(__file__).resolve().parents[2] / "fixtures" / "iq"
    out: list[dict[str, Any]] = []
    if not fixture_dir.is_dir():
        return out
    for meta_path in sorted(fixture_dir.glob("*.meta")):
        try:
            meta = _json.loads(meta_path.read_text())
            data_path = meta_path.with_suffix(".cf32")
            if not data_path.exists():
                continue
            global_meta = meta.get("global", {})
            captures = meta.get("captures") or [{}]
            annotations = meta.get("annotations") or [{}]
            out.append(
                {
                    "name": meta_path.stem,
                    "path": str(data_path),
                    "sample_rate": global_meta.get("core:sample_rate"),
                    "center_freq": captures[0].get("core:frequency"),
                    "description": global_meta.get("core:description"),
                    "label": annotations[0].get("core:label"),
                }
            )
        except (OSError, ValueError, KeyError, IndexError):
            continue  # malformed sidecar — skip, don't hide the rest
    return out


# --- Pydantic models for the REST API ---


class CreateReceiverRequest(BaseModel):
    """Body for POST /api/receivers. All fields optional — defaults apply.

    source_type must match a manifest returned by GET /api/sources.
    Defaults to "simulated" (zero-config, hardware-free). Use "file" for
    IQ replay, "vfo" with source_kwargs={"parent_receiver_id": ...} to
    tap a wideband receiver (ADR-005), a hardware driver type, or a remote
    source: "rtl_tcp" / "kiwi" with source_kwargs={"host": ...}, or
    "openwebrx_remote" with source_kwargs={"url": ...} for any public
    OpenWebRX receiver (ADR-006 federation client — deep-link URLs like
    http://boomerthedog.com:8073/#freq=3570000,mod=lsb,sql=-150 work
    as-is). source_kwargs is passed through to the Source factory (e.g.
    {"file_path": "/path/to/capture.cf32"} for FileSource,
    {"signal_set": "am_band"} for SimulatedSource,
    {"transport": "tcp", "host": ..., "port": ...} for RtlSdrSource,
    {"host": "rx.example.com", "port": 8073} for KiwiSdrSource).
    """

    receiver_id: str | None = None  # auto-generate UUID if not provided
    center_freq: int = 14_205_000  # Hz
    sample_rate: int = 2_400_000  # Hz
    mode: str = "USB"
    source_type: str = "simulated"  # ADR-004: key into SourceRegistry
    source_kwargs: dict[str, Any] = {}  # passed through to Source factory


class ReceiverInfo(BaseModel):
    """One entry in the GET /api/receivers response."""
    receiver_id: str
    center_freq: int
    sample_rate: int
    mode: str
    gain: float | None = None  # dB, None = auto/AGC (slice-4.7)
    dsp_mode: str = "classic"  # ADR-002: raw | classic (ai/cascade pending)
    source: dict[str, Any]


class CreateReceiverResponse(BaseModel):
    receiver_id: str
    center_freq: int
    sample_rate: int
    mode: str


class AttachDecoderRequest(BaseModel):
    """Body for POST /api/receivers/{id}/decoders.

    ``name`` must match a decoder manifest from GET /api/decoders
    (e.g. "adsb" — the bundled Mode S decoder).
    """

    name: str


def create_app(settings: Settings) -> FastAPI:
    """Create the FastAPI app, configured per settings."""
    app = FastAPI(
        title="OpenWebRX+ API",
        version="0.1.0",
        description="Backend orchestration for OpenWebRX+ modernization.",
    )

    # Initialize the default rx-default session eagerly. This is safe because
    # init_default_sessions just creates the session and registers it — the
    # stream task is only started when the WS endpoint calls
    # `await session.start()`. Eager init means REST endpoints (which the
    # frontend uses to list available receivers) see the session immediately.
    init_default_sessions(settings)

    @app.on_event("startup")
    async def _startup() -> None:
        # Belt-and-suspenders: also init on startup in case create_app was
        # called before the settings were final.
        init_default_sessions(settings)
        log.info("default sessions initialized", count=len(list_sessions()))

    # CORS — allow the Vite dev server (port 5173) to call us.
    if settings.tier == "dev":
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/app/")

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/version")
    async def version() -> dict[str, Any]:
        return {
            "version": "0.1.0",
            "tier": settings.tier,
            "default_source": settings.default_source_type,
            "dsp_mode": settings.dsp.default_mode,
            "source_count": len(SourceRegistry.all_manifests()),
        }

    @app.get("/api/sources")
    async def list_sources() -> list[dict[str, Any]]:
        """List available SDR source manifests (ADR-004).

        Returns the union of built-in + discovered manifests. The UI uses
        this to render the source picker when spawning a new receiver.
        """
        return [
            {
                "source_type": m.source_type,
                "label": m.label,
                "sdk": m.sdk,
                "hardware_required": m.hardware_required,
                "default_sample_rate": m.default_sample_rate,
                "sample_rate_range": list(m.sample_rate_range),
                "gain_range": (
                    list(m.gain_range) if m.gain_range is not None else None
                ),
                "supports_bias_tee": m.supports_bias_tee,
                "supports_agc": m.supports_agc,
                "description": m.description,
            }
            for m in SourceRegistry.all_manifests()
        ]

    @app.get("/api/hardware")
    async def list_hardware() -> list[dict[str, Any]]:
        """Probe every driver for connected SDRs (slice-3).

        USB (librtlsdr / libairspy / SDRplay API), rtl_tcp on the default
        host:port, and SoapySDR enumeration. A driver whose SDK is missing
        simply contributes nothing — one broken install must not hide the
        others. The UI uses this to badge sources as "present".
        """
        from ..sources.probe import detect_hardware

        devices = await detect_hardware()
        return [d.to_dict() for d in devices]

    @app.get("/api/fixtures")
    async def list_fixtures() -> list[dict[str, Any]]:
        """Baked IQ fixtures (fixtures/iq/*.meta) — replayable via the
        "file" source. The AddReceiverModal's file form turns these into
        one-click options (e.g. the ADS-B fixture for decoder demos).
        """
        return _list_fixtures()

    # --- Decoder plugins (ADR-003) -----------------------------------------

    @app.get("/api/decoders")
    async def list_decoders() -> list[dict[str, Any]]:
        """Available decoder plugins (name, tap point, event kinds).

        Attach one to a running receiver with
        POST /api/receivers/{id}/decoders {"name": ...}; events stream
        over that receiver's WebSocket as {"type": "decoder", ...} text
        frames.
        """
        return [m.to_dict() for m in decoder_registry.manifests()]

    @app.get("/api/receivers/{receiver_id}/decoders")
    async def list_receiver_decoders(receiver_id: str) -> list[dict[str, Any]]:
        session = get_session(receiver_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"receiver not found: {receiver_id}")
        return session.decoder_status()

    @app.post("/api/receivers/{receiver_id}/decoders", status_code=201)
    async def attach_decoder(
        receiver_id: str, req: AttachDecoderRequest
    ) -> dict[str, Any]:
        session = get_session(receiver_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"receiver not found: {receiver_id}")
        try:
            await session.attach_decoder(req.name)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except DecoderAlreadyAttached as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DecoderBinaryMissing as exc:
            # The plugin is fine; the external binary it drives is not
            # installed/executable — an environment problem, not a bad request.
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except DecoderAttachError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"name": req.name, "attached": True}

    @app.delete("/api/receivers/{receiver_id}/decoders/{decoder_name}", status_code=204)
    async def detach_decoder(receiver_id: str, decoder_name: str) -> None:
        session = get_session(receiver_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"receiver not found: {receiver_id}")
        if not await session.detach_decoder(decoder_name):
            raise HTTPException(
                status_code=404,
                detail=f"decoder not attached: {decoder_name}",
            )

    # --- Remote receiver directories (ADR-006 network sources) ---

    def _directory_response(name: str, receivers: list[Any]) -> dict[str, Any]:
        return {
            "directory": name,
            "count": len(receivers),
            "receivers": [r.to_dict() for r in receivers],
        }

    @app.get("/api/directory/kiwi")
    async def directory_kiwi(refresh: bool = False) -> dict[str, Any]:
        """Public KiwiSDR receivers (rx.kiwisdr.com), TTL-cached.

        Every entry is spawnable right now: POST /api/receivers with
        source_type="kiwi" and source_kwargs={"host": ..., "port": ...}
        parsed from the entry's URL. ?refresh=1 bypasses the cache.
        503 when the directory is unreachable and no stale copy exists
        (this dev box has restricted egress — the graceful path matters).
        """
        try:
            receivers = await directory_service.list_kiwi(refresh=refresh)
        except DirectoryUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return _directory_response("kiwi", receivers)

    @app.get("/api/directory/receiverbook")
    async def directory_receiverbook(refresh: bool = False) -> dict[str, Any]:
        """Public OpenWebRX/OpenWebRX+ receivers (receiverbook.de), TTL-cached.

        Every entry is spawnable: POST /api/receivers with
        source_type="openwebrx_remote" and source_kwargs={"url": <entry url>}
        — the federation client parses host/port (and any deep-link
        freq/mod/sql) from it (ADR-006).
        """
        try:
            receivers = await directory_service.list_receiverbook(refresh=refresh)
        except DirectoryUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return _directory_response("receiverbook", receivers)

    @app.get("/api/receivers", response_model=list[ReceiverInfo])
    async def list_receivers() -> list[ReceiverInfo]:
        return [
            ReceiverInfo(
                receiver_id=s.receiver_id,
                center_freq=s.center_freq,
                sample_rate=s.sample_rate,
                mode=s.mode,
                gain=s.gain,
                dsp_mode=s.dsp_mode,
                source={
                    "type": s.source.info.type,
                    "label": s.source.info.label,
                    "sampleRate": s.source.info.sample_rate,
                },
            )
            for s in list_sessions()
        ]

    @app.post("/api/receivers", response_model=CreateReceiverResponse, status_code=201)
    async def spawn_receiver(req: CreateReceiverRequest) -> CreateReceiverResponse:
        # Validate source_type up front so the error is 400, not 500.
        if SourceRegistry.get_manifest(req.source_type) is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown source_type: {req.source_type!r}. "
                       "See GET /api/sources for available types.",
            )
        try:
            session = create_session(
                receiver_id=req.receiver_id,
                center_freq=req.center_freq,
                sample_rate=req.sample_rate,
                mode=req.mode,
                source_type=req.source_type,
                source_kwargs=req.source_kwargs,
            )
        except VfoValidationError as exc:
            # Rate-incompatible VFO slice — a plain bad request, not a
            # conflict (the id wasn't the problem).
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Start the session's stream task (idempotent if already started).
        await session.start()
        log.info(
            "spawned receiver",
            receiver_id=session.receiver_id,
            freq=session.center_freq,
            mode=session.mode,
        )
        return CreateReceiverResponse(
            receiver_id=session.receiver_id,
            center_freq=session.center_freq,
            sample_rate=session.sample_rate,
            mode=session.mode,
        )

    @app.delete("/api/receivers/{receiver_id}", status_code=204)
    async def destroy_receiver(receiver_id: str) -> None:
        # Don't allow destroying the default session via REST.
        if receiver_id == "rx-default":
            raise HTTPException(
                status_code=403,
                detail="the default session cannot be destroyed",
            )
        destroyed = await destroy_session(receiver_id)
        if not destroyed:
            raise HTTPException(status_code=404, detail=f"receiver not found: {receiver_id}")
        log.info("destroyed receiver", receiver_id=receiver_id)

    # Wire up WebSocket endpoints (see ws.py)
    from .ws import register_websocket_routes

    register_websocket_routes(app)

    # Production: serve the built frontend at /app/
    # (dev: Vite proxies through; we don't mount anything)
    if settings.tier != "dev":
        from pathlib import Path

        from fastapi.staticfiles import StaticFiles

        static_dir = Path(__file__).parent.parent.parent / "static"
        if static_dir.exists():
            app.mount("/app", StaticFiles(directory=static_dir, html=True), name="app")

    return app
