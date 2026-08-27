"""User runtime settings — mutable preferences persisted to a TOML file.

Unlike the top-level :class:`openwebrx_plus.config.Settings` (which is
deployment-level config loaded at startup and treated as immutable), user
settings are runtime-mutable preferences the user can change via the
in-app Settings panel without a server restart.

Persistence: a TOML file at ``$XDG_CONFIG_HOME/openwebrx-plus/user-settings.toml``
(or ``~/.config/openwebrx-plus/user-settings.toml`` on systems without
XDG). The file is created lazily on first change; if it doesn't exist,
defaults are returned.

Concurrency: a single asyncio.Lock guards writes so concurrent REST
calls don't clobber each other. The Settings instance is shared
module-level singleton.
"""

from __future__ import annotations

import asyncio
import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

# ----------------------------------------------------------------------------
# Pydantic models (one per concern; flat fields — the UI renders the sections)
# ----------------------------------------------------------------------------


class DisplaySettings(BaseModel):
    """Display preferences — visual rendering choices."""

    theme: Literal["dark", "light", "system"] = "dark"
    waterfall_colormap: Literal["viridis", "turbo", "jet", "grayscale"] = "turbo"
    spectrum_show_peak_hold: bool = True
    spectrum_averaging: Literal["none", "linear", "exponential"] = "linear"
    spectrum_decay_alpha: float = Field(0.95, ge=0.0, le=1.0)
    freq_display_unit: Literal["hz", "khz", "mhz"] = "mhz"
    show_passband_overlay: bool = True


class AudioSettings(BaseModel):
    """Audio output preferences."""

    master_volume: float = Field(0.8, ge=0.0, le=1.0)
    # Default output device — empty string = system default
    preferred_output_device: str = ""
    # Squelch threshold in dBFS (default: open)
    default_squelch_db: float = Field(-150.0, ge=-150.0, le=0.0)
    # Mono / stereo rendering
    force_mono: bool = False


class DSPSettings(BaseModel):
    """DSP defaults for new ReceiverSessions."""

    default_dsp_mode: Literal["raw", "classic"] = "classic"
    # ai/cascade gated behind AI_DSP_MODES_AVAILABLE (see receiver_session.py)
    default_agc_enabled: bool = True
    # Default audio low/high cut in Hz (passband). Mode-specific defaults
    # are applied by AudioChain when these are None; the user setting
    # overrides per-mode defaults if non-None.
    default_low_cut_hz: int | None = None
    default_high_cut_hz: int | None = None
    # Notch filter defaults (slice-5.2)
    default_notch_enabled: bool = False
    default_notch_freq_hz: float = 0.0
    default_notch_q: float = Field(30.0, ge=1.0, le=200.0)
    # Noise blanker defaults (slice-5.2)
    default_noise_blanker_enabled: bool = False
    default_noise_blanker_threshold: float = Field(0.7, ge=0.0, le=1.0)


class SourcesSettings(BaseModel):
    """Source-related defaults."""

    default_source_type: str = "file"  # key into SourceRegistry
    default_sample_rate: int = 2_400_000
    default_center_freq: int = 14_205_000  # 20m band


class DecoderSettings(BaseModel):
    """Decoder plugin defaults."""

    # Auto-attach ADS-B decoder when a 1090 MHz receiver spawns
    auto_attach_adsb: bool = False
    # Auto-attach AIS decoder when a 162 MHz receiver spawns (slice-5.5)
    auto_attach_ais: bool = False
    # Auto-attach dump978 UAT when a 978 MHz receiver spawns (slice-5.5)
    auto_attach_dump978: bool = False


class DebugSettings(BaseModel):
    """Debug / observability preferences."""

    # If True, the in-app debugger captures all log events
    log_capture_enabled: bool = True
    # Capacity for the all-events ring buffer
    log_ring_capacity: int = Field(1000, ge=100, le=10_000)
    # Capacity for the errors-only ring buffer
    error_ring_capacity: int = Field(200, ge=50, le=2000)
    # If True, capture unhandled asyncio loop exceptions
    capture_async_exceptions: bool = True
    # If True, capture unhandled threading excepthook crashes
    capture_unhandled_exceptions: bool = True


class UserSettings(BaseModel):
    """Top-level user settings model.

    All fields have defaults — a fresh install with no persisted TOML
    file returns the defaults. REST callers can do partial updates:
    only the fields present in the PUT body are mutated; everything else
    is preserved.
    """

    display: DisplaySettings = Field(default_factory=DisplaySettings)  # type: ignore[arg-type]
    audio: AudioSettings = Field(default_factory=AudioSettings)  # type: ignore[arg-type]
    dsp: DSPSettings = Field(default_factory=DSPSettings)  # type: ignore[arg-type]
    sources: SourcesSettings = Field(default_factory=SourcesSettings)
    decoders: DecoderSettings = Field(default_factory=DecoderSettings)
    debug: DebugSettings = Field(default_factory=DebugSettings)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------
# Settings service — load / save / mutate
# ----------------------------------------------------------------------------


def _default_settings_path() -> Path:
    """Resolve the user settings TOML path per XDG conventions.

    Honors $XDG_CONFIG_HOME if set, else falls back to ~/.config. The
    directory is created lazily on save, never on load.
    """
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        return Path(xdg_config) / "openwebrx-plus" / "user-settings.toml"
    return Path.home() / ".config" / "openwebrx-plus" / "user-settings.toml"


class UserSettingsService:
    """Singleton service for loading, persisting, and updating user settings."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_settings_path()
        self._lock = asyncio.Lock()
        self._settings: UserSettings = self._load_sync()

    # --- internal ---

    def _load_sync(self) -> UserSettings:
        """Load settings from TOML. Returns defaults if file is missing.

        Tomllib (Python 3.11+) is the reader; we don't need a writer
        here because we use tomli_w for round-trip preservation.
        """
        if not self._path.exists():
            return UserSettings()
        try:
            with self._path.open("rb") as f:
                data = tomllib.load(f)
            # Pydantic will reject unknown fields by default; we ignore
            # extras so older TOML files with removed keys still load.
            return UserSettings.model_validate(data)
        except (tomllib.TOMLDecodeError, OSError, ValueError):
            # Malformed file — fall back to defaults. The next save
            # overwrites the bad file.
            return UserSettings()

    def _save_sync(self) -> None:
        """Persist current settings to TOML."""
        import tomli_w  # local import — only needed when we write

        self._path.parent.mkdir(parents=True, exist_ok=True)
        # exclude_none=True so Optional fields (e.g. default_low_cut_hz=None)
        # are dropped from the TOML rather than crashing the serializer.
        data = self._settings.model_dump(exclude_none=True)
        with self._path.open("wb") as f:
            tomli_w.dump(data, f)

    # --- public API ---

    def get(self) -> UserSettings:
        """Return current settings (no copy — caller should treat as read-only)."""
        return self._settings

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable snapshot of current settings."""
        return self._settings.model_dump()

    async def update(self, patch: dict[str, Any]) -> UserSettings:
        """Apply a partial update (deep-merge per section) and persist.

        Example patch:
            {"display": {"theme": "light"}, "audio": {"master_volume": 0.5}}
        Sections not present in the patch are left unchanged. Unknown
        fields within a section are silently ignored (extra='ignore').
        """
        async with self._lock:
            current = self._settings.model_dump()
            for section, fields in patch.items():
                if section not in current:
                    continue
                if not isinstance(fields, dict):
                    continue
                for k, v in fields.items():
                    current[section][k] = v
            self._settings = UserSettings.model_validate(current)
            self._save_sync()
            return self._settings

    async def reset(self) -> UserSettings:
        """Reset to defaults and persist."""
        async with self._lock:
            self._settings = UserSettings()
            self._save_sync()
            return self._settings

    def update_sync(self, patch: dict[str, Any]) -> UserSettings:
        """Synchronous version of update — for tests and any caller that
        doesn't want to await. Acquires the lock synchronously."""
        import asyncio

        return asyncio.run(self.update(patch))

    def reset_sync(self) -> UserSettings:
        """Synchronous reset — for tests that don't want to await."""
        self._settings = UserSettings()
        self._save_sync()
        return self._settings


# Module-level singleton.
_service: UserSettingsService | None = None


def get_user_settings_service() -> UserSettingsService:
    """Return the process-wide UserSettingsService singleton."""
    global _service
    if _service is None:
        _service = UserSettingsService()
    return _service


def reset_user_settings_service() -> None:
    """Reset the singleton — used by tests to start each test with a
    clean settings file in a tmp_path."""
    global _service
    _service = None


__all__ = [
    "AudioSettings",
    "DSPSettings",
    "DebugSettings",
    "DecoderSettings",
    "DisplaySettings",
    "SourcesSettings",
    "UserSettings",
    "UserSettingsService",
    "get_user_settings_service",
    "reset_user_settings_service",
]
