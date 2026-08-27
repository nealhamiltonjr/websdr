"""Settings module — re-exports Settings so callers do
    `from openwebrx_plus.config import Settings`.

See __init__.py for the actual definitions.
"""

from . import Settings, load_settings

__all__ = ["Settings", "load_settings"]
