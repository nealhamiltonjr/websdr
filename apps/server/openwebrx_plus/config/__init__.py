"""Configuration — TOML + environment via pydantic-settings.

The system supports three deployment tiers:
  - tier-dev:   local SQLite + local file storage + console logs (default)
  - tier-prod:  PostgreSQL + TimescaleDB + structured JSON logs
  - tier-fed:   above + federation directory cache

This file loads settings from:
  1. Environment variables (OPENWEBRX_HOST, OPENWEBRX_PORT, etc.)
  2. /etc/openwebrx-plus/config.toml (or path in OPENWEBRX_CONFIG)
  3. ./config/local.toml (developer override, gitignored)
  4. Sensible built-in defaults
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


class DSPSettings(BaseModel):
    """DSP+AI cascade settings. See ADR-002."""

    default_mode: Literal["raw", "classic", "ai", "cascade"] = "classic"
    enable_deepfilternet: bool = False  # requires Rust AI module
    enable_rnnoise_wasm: bool = True  # client-side, no server cost
    enable_demucs: bool = False  # offline only


class FederationSettings(BaseModel):
    enabled: bool = False
    directory_url: str = "https://directory.openwebrx-plus.org/v1"
    cache_ttl_seconds: int = 3600
    max_concurrent_remote_sources: int = 4


class ListingSettings(BaseModel):
    """Self-listing metadata (slice-14, ADR-006 federation polish).

    Operators set these to publish their station to receiverbook.de /
    directory.openwebrx-plus.org. The /api/listing endpoint returns this
    metadata in receiverbook-compatible JSON so the public directory can
    index the receiver. `enabled` defaults to False — self-listing is
    OPT-IN (the privacy-preserving default; the receiver doesn't
    broadcast itself anywhere).
    """

    enabled: bool = False
    id: str = ""  # short slug, e.g. "neal-001"; left blank until set
    name: str = ""  # human-readable, e.g. "Neal's 20m receiver (KH6)"
    url: str = ""  # public-facing ws URL, e.g. "https://sdr.example.com:8073/ws"
    lat: float | None = None  # operator's latitude (decimal degrees)
    lon: float | None = None  # operator's longitude (decimal degrees)
    description: str = ""  # free-form, e.g. "IC-7300 + dipole; HF bands"


class Settings(BaseSettings):
    """Top-level settings."""

    model_config = SettingsConfigDict(
        env_prefix="OPENWEBRX_",
        env_nested_delimiter="__",
        toml_file="config.toml",
        extra="ignore",
    )

    # Network
    host: str = "127.0.0.1"
    port: int = 8073
    log_level: str = "INFO"

    # Deployment tier
    tier: Literal["dev", "prod", "fed"] = "dev"

    # Persistence
    database_url: str = "sqlite:///.openwebrx-plus/db.sqlite"
    recordings_dir: Path = Path(".openwebrx-plus/recordings")

    # Default SDR source for the rx-default session (ADR-004/005). The
    # string is a key into SourceRegistry — validation happens at runtime
    # via the registry, not at config-load time, so external plugins can
    # register sources we don't know about at code-write time.
    # "file" (default) replays default_iq_fixture — the frontend sees
    # realistic signals on hardware-free dev machines. Set to "rtl_sdr"
    # (or airspy/sdrplay/soapy) on hosts with hardware attached.
    default_source_type: str = "file"
    default_iq_fixture: Path = Path("")  # empty → bundled fixtures/iq/hf_20m_evening.cf32
    default_sample_rate: int = 2_400_000  # 2.4 MHz, RTL-SDR default
    default_center_freq: int = 14_205_000  # 20m band, where the FT8 lives

    # Sub-tiers
    dsp: DSPSettings = Field(default_factory=DSPSettings)
    federation: FederationSettings = Field(default_factory=FederationSettings)
    listing: ListingSettings = Field(default_factory=ListingSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Order: env > init > TOML file > defaults."""
        toml_source = TomlConfigSettingsSource(settings_cls)
        return (env_settings, init_settings, toml_source, dotenv_settings, file_secret_settings)


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings from env + TOML + defaults.

    Args:
        config_path: explicit TOML path. If None, falls back to:
            - $OPENWEBRX_CONFIG
            - ./config/local.toml
            - /etc/openwebrx-plus/config.toml
    """
    # For slice-1, we just use env + defaults. Full TOML loader lands later.
    if config_path is not None:
        # Override the toml_file path before instantiation.
        # Pydantic-settings exposes `_toml_file` as an init kwarg but the type
        # stub doesn't reflect it; the `# type: ignore[arg-type,unused-ignore]`
        # silences both the unexpected-kwarg error and the unused-ignore note
        # that appears when the override is in effect.
        return Settings(_toml_file=config_path)  # type: ignore[call-arg,unused-ignore]
    return Settings()


_ = Any  # silence flake8 unused import if F401 runs

