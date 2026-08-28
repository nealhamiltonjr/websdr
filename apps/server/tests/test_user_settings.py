"""Tests for the user settings service (slice-5.1).

Covers: defaults, persistence, partial update, reset, default path,
TOML round-trip, validation rejection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openwebrx_plus.config.user_settings import (
    UserSettingsService,
    get_user_settings_service,
    reset_user_settings_service,
)


@pytest.fixture
def settings_path(tmp_path: Path) -> Path:
    """Return a fresh settings path in tmp_path."""
    return tmp_path / "user-settings.toml"


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    """Each test starts with a clean module singleton."""
    reset_user_settings_service()
    yield
    reset_user_settings_service()


def test_defaults_returned_when_no_file(settings_path: Path) -> None:
    service = UserSettingsService(path=settings_path)
    snapshot = service.snapshot()
    assert snapshot["display"]["theme"] == "dark"
    assert snapshot["display"]["waterfall_colormap"] == "turbo"
    assert snapshot["audio"]["master_volume"] == 0.8
    assert snapshot["dsp"]["default_dsp_mode"] == "classic"
    assert snapshot["sources"]["default_source_type"] == "file"


def test_partial_update_persists(settings_path: Path) -> None:
    service = UserSettingsService(path=settings_path)
    updated = service.update_sync({"display": {"theme": "light"}})
    assert updated.display.theme == "light"
    # Reload — should reflect the persisted value.
    service2 = UserSettingsService(path=settings_path)
    assert service2.get().display.theme == "light"
    # Sections not in the patch should be untouched.
    assert service2.get().audio.master_volume == 0.8


def test_partial_update_multiple_sections(settings_path: Path) -> None:
    service = UserSettingsService(path=settings_path)
    service.update_sync(
        {
            "display": {"theme": "light", "waterfall_colormap": "viridis"},
            "audio": {"master_volume": 0.3},
            "dsp": {"default_dsp_mode": "raw"},
        }
    )
    reloaded = UserSettingsService(path=settings_path).get()
    assert reloaded.display.theme == "light"
    assert reloaded.display.waterfall_colormap == "viridis"
    assert reloaded.audio.master_volume == 0.3
    assert reloaded.dsp.default_dsp_mode == "raw"


def test_reset_to_defaults(settings_path: Path) -> None:
    service = UserSettingsService(path=settings_path)
    service.update_sync({"display": {"theme": "light"}, "audio": {"master_volume": 0.1}})
    service.reset_sync()
    fresh = UserSettingsService(path=settings_path).get()
    assert fresh.display.theme == "dark"
    assert fresh.audio.master_volume == 0.8


def test_unknown_field_in_section_ignored(settings_path: Path) -> None:
    service = UserSettingsService(path=settings_path)
    # Unknown field should not crash; pydantic with extra='ignore' drops it.
    service.update_sync({"display": {"theme": "light", "bogus_field": "x"}})
    reloaded = UserSettingsService(path=settings_path).get()
    assert reloaded.display.theme == "light"


def test_unknown_section_ignored(settings_path: Path) -> None:
    service = UserSettingsService(path=settings_path)
    # Unknown section should be dropped silently — no crash, no validation error.
    service.update_sync({"bogus_section": {"x": 1}, "display": {"theme": "light"}})
    reloaded = UserSettingsService(path=settings_path).get()
    assert reloaded.display.theme == "light"


def test_validation_rejects_invalid_enum(settings_path: Path) -> None:
    service = UserSettingsService(path=settings_path)
    with pytest.raises(ValueError):
        service.update_sync({"display": {"theme": "purple"}})


def test_validation_rejects_out_of_range(settings_path: Path) -> None:
    service = UserSettingsService(path=settings_path)
    with pytest.raises(ValueError):
        service.update_sync({"audio": {"master_volume": 2.5}})


def test_malformed_toml_falls_back_to_defaults(settings_path: Path) -> None:
    # Write a malformed TOML — load should silently fall back.
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("not valid toml { = =")
    service = UserSettingsService(path=settings_path)
    assert service.get().display.theme == "dark"


def test_get_singleton_returns_same_instance() -> None:
    a = get_user_settings_service()
    b = get_user_settings_service()
    assert a is b


def test_toml_round_trip_preserves_all_fields(settings_path: Path) -> None:
    service = UserSettingsService(path=settings_path)
    service.update_sync(
        {
            "display": {
                "theme": "light",
                "waterfall_colormap": "jet",
                "spectrum_show_peak_hold": False,
                "spectrum_averaging": "exponential",
                "spectrum_decay_alpha": 0.99,
                "freq_display_unit": "khz",
                "show_passband_overlay": False,
            },
            "audio": {
                "master_volume": 0.5,
                "preferred_output_device": "speakers",
                "default_squelch_db": -50.0,
                "force_mono": True,
            },
            "dsp": {
                "default_dsp_mode": "raw",
                "default_agc_enabled": False,
                "default_low_cut_hz": 300,
                "default_high_cut_hz": 3000,
                "default_notch_enabled": True,
                "default_notch_freq_hz": 1000.0,
                "default_notch_q": 50.0,
                "default_noise_blanker_enabled": True,
                "default_noise_blanker_threshold": 0.8,
            },
            "sources": {
                "default_source_type": "rtl_sdr",
                "default_sample_rate": 2_400_000,
                "default_center_freq": 1090_000_000,
            },
            "decoders": {
                "auto_attach_adsb": True,
                "auto_attach_ais": False,
                "auto_attach_dump978": True,
            },
            "debug": {
                "log_capture_enabled": False,
                "log_ring_capacity": 500,
                "error_ring_capacity": 100,
                "capture_async_exceptions": False,
                "capture_unhandled_exceptions": False,
            },
        }
    )
    reloaded = UserSettingsService(path=settings_path).get()
    assert reloaded.display.theme == "light"
    assert reloaded.display.waterfall_colormap == "jet"
    assert reloaded.display.spectrum_decay_alpha == 0.99
    assert reloaded.audio.preferred_output_device == "speakers"
    assert reloaded.dsp.default_notch_q == 50.0
    assert reloaded.sources.default_center_freq == 1090_000_000
    assert reloaded.decoders.auto_attach_adsb is True
    assert reloaded.debug.log_ring_capacity == 500


def test_default_settings_path_uses_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from openwebrx_plus.config.user_settings import _default_settings_path

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    path = _default_settings_path()
    assert path == tmp_path / "xdg-config" / "openwebrx-plus" / "user-settings.toml"


def test_default_settings_path_falls_back_to_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from openwebrx_plus.config.user_settings import _default_settings_path

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _default_settings_path()
    assert path == tmp_path / ".config" / "openwebrx-plus" / "user-settings.toml"
