"""API module — REST + WebSocket endpoints."""

from .rest import create_app
from .ws import register_websocket_routes

__all__ = ["create_app", "register_websocket_routes"]
