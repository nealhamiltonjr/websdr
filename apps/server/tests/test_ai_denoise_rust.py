"""Tests for the Rust-backed AIDenoiser wrapper (slice-18).

These tests cover the not-built path (the cdylib is not built in the
test environment, since the Rust toolchain isn't installed). When the
cdylib IS available (operators who ran `cargo build --release` in
packages/ai-rust/), the same tests verify the load + version surface.

The numpy AIDenoiser remains the production default — these tests
only verify that:
  - the wrapper module loads cleanly (no import errors)
  - is_available() returns a bool
  - when unavailable, RustAIDenoiser raises RuntimeError on construct
  - when unavailable, rust_version() returns None
  - the wrapper's _find_cdylib() correctly returns None when the
    build dirs don't exist
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def test_module_imports_cleanly() -> None:
    """The wrapper module must import without errors, even when the
    cdylib is not built. The numpy AIDenoiser remains the default."""
    m = importlib.import_module("openwebrx_plus.dsp.ai_denoise_rust")
    assert hasattr(m, "RustAIDenoiser")
    assert hasattr(m, "is_available")
    assert hasattr(m, "rust_version")
    assert hasattr(m, "_find_cdylib")


def test_is_available_returns_bool() -> None:
    """is_available() must return a bool, not a truthy/falsy value.
    The numpy AIDenoiser relies on this for its swap-in check."""
    from openwebrx_plus.dsp.ai_denoise_rust import is_available

    result = is_available()
    assert isinstance(result, bool)


def test_find_cdylib_returns_path_or_none() -> None:
    """The finder must return either a Path or None, not raise."""
    from openwebrx_plus.dsp.ai_denoise_rust import _find_cdylib

    result = _find_cdylib()
    assert result is None or isinstance(result, Path)


def test_rust_version_returns_str_or_none() -> None:
    """When the cdylib is loaded, rust_version returns the version
    string (e.g. 'openwebrx-plus ai 0.1.0 (slice-18 ...)').
    When not loaded, returns None."""
    from openwebrx_plus.dsp.ai_denoise_rust import is_available, rust_version

    if is_available():
        v = rust_version()
        assert v is not None
        assert isinstance(v, str)
        assert "openwebrx-plus" in v
    else:
        assert rust_version() is None


def test_construct_raises_when_unavailable() -> None:
    """If the cdylib isn't loaded, constructing RustAIDenoiser raises
    RuntimeError with an actionable message."""
    from openwebrx_plus.dsp.ai_denoise_rust import (
        AVAILABLE,
        RustAIDenoiser,
    )

    if AVAILABLE:
        pytest.skip(
            "cdylib is loaded — construct path is exercised elsewhere"
        )
    with pytest.raises(RuntimeError, match="not available"):
        RustAIDenoiser(frame_size=480)


def test_construct_works_when_available() -> None:
    """If the cdylib IS loaded, constructing RustAIDenoiser succeeds
    and exposes the frame_size."""
    from openwebrx_plus.dsp.ai_denoise_rust import AVAILABLE, RustAIDenoiser

    if not AVAILABLE:
        pytest.skip("cdylib not built — skip construct test")
    d = RustAIDenoiser(frame_size=480)
    assert d.frame_size == 480
    # The handle must be non-null.
    assert d._handle is not None
