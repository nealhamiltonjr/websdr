"""Entrypoint for the OpenWebRX+ backend.

Usage:
    uv run openwebrx-plus                 # run with defaults
    OPENWEBRX_PORT=8073 uv run openwebrx-plus

For development:
    uv run python -m openwebrx_plus  # or just `make dev-server`
"""

from __future__ import annotations

import structlog

from .api.rest import create_app
from .config.settings import Settings

log = structlog.get_logger(__name__)


def main() -> None:
    settings = Settings()
    log.info("openwebrx-plus starting", version="0.1.0", settings=settings.model_dump())
    app = create_app(settings)
    # Defer uvicorn import to keep startup fast in test environments
    import uvicorn

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,  # structlog handles this
    )


if __name__ == "__main__":
    main()
