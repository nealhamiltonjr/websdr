"""API module — REST + WebSocket endpoints."""

from .rest import create_app
from .settings_debug import register_settings_and_debug_routes
from .ws import register_websocket_routes

__all__ = [
    "create_app",
    "register_settings_and_debug_routes",
    "register_websocket_routes",
]
