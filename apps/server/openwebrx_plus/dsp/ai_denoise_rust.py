"""Rust-backed AI denoiser — optional swap-in for the numpy AIDenoiser.

Slice-18: the `packages/ai-rust` crate now ships a REAL spectral-
subtraction denoiser (not a stub). The compiled `libowrx_ai.so` (or
`.dylib` / `.dll`) exposes a C ABI matching the numpy AIDenoiser's
``frame_size + process()`` signature.

This module loads the cdylib via ctypes and provides ``RustAIDenoiser``,
a drop-in replacement for the numpy ``AIDenoiser``. If the cdylib
isn't built (e.g., the test environment, or a deployment without the
Rust toolchain), ``RustAIDenoiser.available`` is False — the numpy
AIDenoiser remains the default and the audio path runs unchanged.

Usage from the audio drain loop:

.. code-block:: python

    from openwebrx_plus.dsp.ai_denoise_rust import RustAIDenoiser

    if RustAIDenoiser.available:
        denoiser = RustAIDenoiser(frame_size=480)
    else:
        from openwebrx_plus.dsp.ai_denoise import AIDenoiser
        denoiser = AIDenoiser()

The runtime detection is cached on first import (no per-call ctypes
probing). A failed load is silent — operators who want the Rust impl
must build and install the cdylib themselves; CI builds it via
``cargo build --release`` in the ``packages/ai-rust`` workspace.

Why a separate module instead of inlining into ``ai_denoise.py``:
keeps the numpy impl as the always-available default (zero new
runtime deps), and the Rust impl as an opt-in performance upgrade
that operators explicitly build.
"""

from __future__ import annotations

import contextlib
import ctypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

# Where the cdylib lives when built. cargo emits:
#   packages/ai-rust/target/release/libowrx_ai.so   (Linux)
#   packages/ai-rust/target/release/libowrx_ai.dylib (macOS)
#   packages/ai-rust/target/release/owrx_ai.dll      (Windows)
_REPO_ROOT = Path(__file__).resolve().parents[4]
_BUILD_DIRS = (
    _REPO_ROOT / "packages" / "ai-rust" / "target" / "release",
    _REPO_ROOT / "packages" / "ai-rust" / "target" / "debug",
)
_LIB_NAMES = ("libowrx_ai.so", "libowrx_ai.dylib", "owrx_ai.dll")


def _find_cdylib() -> Path | None:
    """Locate the compiled cdylib on disk. Returns None if not built."""
    for build_dir in _BUILD_DIRS:
        for name in _LIB_NAMES:
            candidate = build_dir / name
            if candidate.is_file():
                return candidate
    return None


def _try_load_cdylib() -> ctypes.CDLL | None:
    """Load the cdylib. Returns None on any failure (silent)."""
    path = _find_cdylib()
    if path is None:
        return None
    try:
        return ctypes.CDLL(str(path))
    except OSError:
        # The lib is built but doesn't load (e.g., missing libc
        # version, arch mismatch). Treat as not-available.
        return None


# Cached at import time so the audio path doesn't probe on every
# frame. Operators who build the cdylib after server start must
# restart the server to pick it up.
_LIB: ctypes.CDLL | None = _try_load_cdylib()
AVAILABLE: bool = _LIB is not None


def _setup_signatures(lib: ctypes.CDLL) -> None:
    """Configure ctypes argument types and return types.

    Called once on import (only if ``_LIB`` is not None).
    """
    # owrx_ai_denoiser_new(frame_size: usize) -> *mut c_void
    lib.owrx_ai_denoiser_new.argtypes = [ctypes.c_size_t]
    lib.owrx_ai_denoiser_new.restype = ctypes.c_void_p
    # owrx_ai_denoiser_free(ptr: *mut c_void) -> void
    lib.owrx_ai_denoiser_free.argtypes = [ctypes.c_void_p]
    lib.owrx_ai_denoiser_free.restype = None
    # owrx_ai_denoiser_reset(ptr: *mut c_void) -> void
    lib.owrx_ai_denoiser_reset.argtypes = [ctypes.c_void_p]
    lib.owrx_ai_denoiser_reset.restype = None
    # owrx_ai_denoise_frame(ptr, samples_ptr, samples_len) -> i32
    lib.owrx_ai_denoise_frame.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_size_t,
    ]
    lib.owrx_ai_denoise_frame.restype = ctypes.c_int
    # owrx_ai_version() -> *const c_char
    lib.owrx_ai_version.argtypes = []
    lib.owrx_ai_version.restype = ctypes.c_void_p  # actually *const c_char


if _LIB is not None:
    with contextlib.suppress(AttributeError, OSError):
        _setup_signatures(_LIB)


@dataclass
class RustAIDenoiser:
    """Rust-backed spectral-subtraction denoiser (slice-18).

    Drop-in for ``AIDenoiser`` from ``ai_denoise.py``. Same
    ``frame_size + process()`` signature; the difference is the
    denoising runs in native Rust code (no Python overhead per
    frame).

    Construct only if ``RustAIDenoiser.available`` is True; otherwise
    fall back to the numpy ``AIDenoiser``.
    """

    frame_size: int = 480
    _handle: Any = None  # ctypes pointer to the Denoiser struct

    def __post_init__(self) -> None:
        if not AVAILABLE or _LIB is None:
            raise RuntimeError(
                "RustAIDenoiser not available — the libowrx_ai cdylib "
                "is not built. Run `cargo build --release` in "
                "packages/ai-rust/, or use the numpy AIDenoiser."
            )
        self._handle = _LIB.owrx_ai_denoiser_new(self.frame_size)
        if not self._handle:
            raise RuntimeError(
                f"owrx_ai_denoiser_new(frame_size={self.frame_size}) "
                "returned null — invalid config?"
            )

    def __del__(self) -> None:  # noqa: D401
        if getattr(self, "_handle", None) and _LIB is not None:
            with contextlib.suppress(AttributeError, OSError):
                _LIB.owrx_ai_denoiser_free(self._handle)
            self._handle = None

    def feed(self, samples: np.ndarray) -> np.ndarray:
        """Denoise a chunk of int16 PCM.

        Mirrors ``AIDenoiser.feed()`` — accepts arbitrary-length int16,
        returns int16 of the same length. Internally buffers samples
        until a full frame is available, then calls the Rust impl.

        Args:
            samples: 1-D int16 numpy array (any length ≥ 0).
        Returns:
            Denoised int16 numpy array. Length = max(0, fed - frame_size
            + hop_size) per call's accumulation.
        """
        if _LIB is None:  # pragma: no cover — available-checked at construct
            return samples
        if samples.size == 0:
            return np.zeros(0, dtype=np.int16)
        if samples.dtype != np.int16:
            samples = samples.astype(np.int16)
        # Convert to float32 for the Rust impl (mirrors numpy AIDenoiser).
        f32 = samples.astype(np.float32) / 32768.0
        # Buffer between calls (the Rust impl is frame-based; we feed
        # one frame at a time, accumulating output).
        if not hasattr(self, "_buf"):
            self._buf = np.zeros(0, dtype=np.float32)
        self._buf = np.concatenate([self._buf, f32])
        out_chunks: list[np.ndarray] = []
        while self._buf.size >= self.frame_size:
            frame = self._buf[: self.frame_size].copy()
            self._buf = self._buf[self.frame_size :]
            # The Rust impl processes in place and writes the first
            # hop_size samples of `frame` with the denoised output.
            # Default hop_size = frame_size / 2.
            hop = self.frame_size // 2
            frame_c = np.ascontiguousarray(frame, dtype=np.float32)
            rc = _LIB.owrx_ai_denoise_frame(
                self._handle,
                frame_c.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                frame_c.size,
            )
            if rc != 0:
                # Wrong frame size or null ptr — shouldn't happen with
                # our setup; emit silence on failure (don't break audio).
                out_chunks.append(np.zeros(hop, dtype=np.float32))
            else:
                out_chunks.append(frame_c[:hop].copy())
        if not out_chunks:
            return np.zeros(0, dtype=np.int16)
        out = np.concatenate(out_chunks)
        # Convert back to int16.
        out_int16 = np.clip(out * 32768.0, -32768, 32767).astype(np.int16)
        return out_int16

    def drain(self) -> np.ndarray:
        """Flush any buffered samples (zero-pad to a final frame)."""
        if _LIB is None:  # pragma: no cover
            return np.zeros(0, dtype=np.int16)
        if not hasattr(self, "_buf") or self._buf.size == 0:
            return np.zeros(0, dtype=np.int16)
        pad = np.zeros(self.frame_size - self._buf.size, dtype=np.float32)
        final = np.concatenate([self._buf, pad])
        self._buf = np.zeros(0, dtype=np.float32)
        final_c = np.ascontiguousarray(final, dtype=np.float32)
        rc = _LIB.owrx_ai_denoise_frame(
            self._handle,
            final_c.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            final_c.size,
        )
        if rc != 0:
            return np.zeros(0, dtype=np.int16)
        hop = self.frame_size // 2
        out = final_c[:hop]
        result: np.ndarray = np.clip(out * 32768.0, -32768, 32767).astype(np.int16)
        return result

    def reset(self) -> None:
        """Clear streaming state (noise floor + overlap buffers)."""
        if self._handle and _LIB is not None:
            _LIB.owrx_ai_denoiser_reset(self._handle)
        self._buf = np.zeros(0, dtype=np.float32)


def is_available() -> bool:
    """True iff the Rust cdylib was loaded at import time.

    The numpy AIDenoiser remains the default; this function lets the
    audio path decide at startup which impl to instantiate.
    """
    return AVAILABLE


def rust_version() -> str | None:
    """Return the Rust impl's version string (e.g.,
    'openwebrx-plus ai 0.1.0 (slice-18 spectral-subtraction)').

    Returns None if the cdylib isn't loaded.
    """
    if _LIB is None:
        return None
    with contextlib.suppress(AttributeError, OSError):
        ptr = _LIB.owrx_ai_version()
        if ptr:
            raw = ctypes.cast(ptr, ctypes.c_char_p).value
            if raw is not None:
                return raw.decode("utf-8", errors="replace")
    return None
