"""WebSocket endpoints — one per ReceiverSession.

Client protocol (client → server):
    text: {"type": "subscribe", "receiverId": "rx-default"}
    text: {"type": "control", "receiverId": "rx-default", "command": "setFrequency", "value": 14205000}
    text: {"type": "unsubscribe", "receiverId": "rx-default"}

Server protocol (server → client):
    binary: <FFT frame header + Float32Array bins>
    binary: <Audio frame header + Int16Array PCM>
    text:   {"type": "metadata", ...}
    text:   {"type": "decoder", "decoder": "adsb", "receiverId": ..., "event": {...}}

The WS handler uses the SessionRegistry to look up the ReceiverSession by
the receiver_id path segment. If the session doesn't exist, returns 404.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ..sessions import get_session, init_default_sessions

log = structlog.get_logger(__name__)


def register_websocket_routes(app: FastAPI) -> None:
    @app.websocket("/ws/{receiver_id}")
    async def ws_endpoint(websocket: WebSocket, receiver_id: str) -> None:
        # Look up the session in the registry. If it doesn't exist, close
        # the connection with a 4401 policy code so the client can handle it.
        init_default_modules()
        session = get_session(receiver_id)
        if session is None:
            await websocket.accept()
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "error",
                        "receiverId": receiver_id,
                        "message": f"receiver not found: {receiver_id}",
                        "code": "RECEIVER_NOT_FOUND",
                    }
                )
            )
            await websocket.close(code=4401)
            return

        await websocket.accept()
        log.info("ws connected", receiver_id=receiver_id, client=websocket.client)

        await session.start()
        q = session.subscribe()

        async def pump_to_client() -> None:
            try:
                while True:
                    frame_bytes: bytes | str = await q.get()
                    # Binary frames are FFT (WRFO) / audio (AUDI) — the
                    # SharedWorker routes them by magic. Strings are JSON:
                    # metadata (every frame) or decoder events (ADR-003).
                    if isinstance(frame_bytes, str):
                        await websocket.send_text(frame_bytes)
                        continue
                    await websocket.send_bytes(frame_bytes)

                    # Send JSON metadata alongside (every frame, so popouts
                    # that join late can sync quickly). display_frequency
                    # follows remote-VFO tuning for federation sessions.
                    # gain/dspMode echo the session's control state
                    # (slice-4.7); gainRange/supportsAgc advertise the
                    # source's capabilities so the UI can shape the knob.
                    gain_range, supports_agc = session.gain_capabilities()
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "metadata",
                                "receiverId": receiver_id,
                                "frequency": session.display_frequency,
                                "mode": session.mode,
                                "gain": session.gain,
                                "dspMode": session.dsp_mode,
                                "source": {
                                    "type": session.source.info.type,
                                    "label": session.source.info.label,
                                    "sampleRate": session.source.info.sample_rate,
                                    "gainRange": (
                                        list(gain_range) if gain_range is not None else None
                                    ),
                                    "supportsAgc": supports_agc,
                                },
                            }
                        )
                    )
            except WebSocketDisconnect:
                pass
            except Exception as exc:
                log.exception("ws pump error", receiver_id=receiver_id, error=str(exc))

        async def listen_to_client() -> None:
            try:
                while True:
                    msg = await websocket.receive_text()
                    try:
                        data: dict[str, Any] = json.loads(msg)
                    except json.JSONDecodeError:
                        await websocket.send_text(
                            json.dumps({"type": "error", "message": "invalid JSON"})
                        )
                        continue
                    cmd = data.get("command")
                    if cmd == "setFrequency":
                        new_freq = int(data.get("value", 0))
                        if new_freq > 0:
                            # Display sessions forward the tune to the remote
                            # demodulator (ADR-006); IQ sessions move the
                            # session center (legacy behavior).
                            await session.set_frequency(new_freq)
                    elif cmd == "setMode":
                        new_mode = str(data.get("value", ""))
                        if new_mode:
                            await session.set_mode(new_mode)
                    elif cmd == "setGain":
                        # "auto" (or null) → AGC / unit gain; a number → dB.
                        raw_gain = data.get("value")
                        if raw_gain is None or raw_gain == "auto":
                            gain_value: float | None = None
                        else:
                            try:
                                gain_value = float(raw_gain)
                            except (TypeError, ValueError):
                                await websocket.send_text(
                                    json.dumps(
                                        {
                                            "type": "error",
                                            "command": "setGain",
                                            "receiverId": receiver_id,
                                            "message": f"invalid gain value: {raw_gain!r}",
                                        }
                                    )
                                )
                                continue
                        applied, reason = await session.set_gain(gain_value)
                        if not applied:
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "type": "error",
                                        "command": "setGain",
                                        "receiverId": receiver_id,
                                        "message": reason,
                                    }
                                )
                            )
                    elif cmd == "setDSPMode":
                        dsp_value = str(data.get("value", ""))
                        applied, reason = await session.set_dsp_mode(dsp_value)
                        if not applied:
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "type": "error",
                                        "command": "setDSPMode",
                                        "receiverId": receiver_id,
                                        "message": reason,
                                    }
                                )
                            )
                    else:
                        log.warning(
                            "unknown control command",
                            receiver_id=receiver_id,
                            command=cmd,
                        )
            except WebSocketDisconnect:
                pass

        try:
            await asyncio.gather(pump_to_client(), listen_to_client())
        except WebSocketDisconnect:
            pass
        finally:
            session.unsubscribe(q)
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()
            log.info("ws disconnected", receiver_id=receiver_id)


# Defer the default session init until the first WS connection. This avoids
# spawning the stream task before the asyncio loop is ready.
_default_modules_initialized = False


def init_default_modules() -> None:
    global _default_modules_initialized
    if _default_modules_initialized:
        return
    # Pre-create rx-default so a fresh boot has it ready.
    # We pass dummy settings — the defaults baked into Settings() match the
    # actual config.
    from ..config import Settings

    init_default_sessions(Settings())
    _default_modules_initialized = True
