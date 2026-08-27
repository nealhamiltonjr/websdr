"""FileSource — replays recorded IQ from disk. Hardware-free dev/test.

Supports:
  - Raw cf32 / .cfile  (interleaved float32 I,Q,I,Q,...  → np.complex64)
  - Raw cs16 / .cs16   (interleaved int16   I,Q,I,Q,...)
  - Raw cu8 / .cu8     (interleaved uint8   I,Q,I,Q,... — RTL-SDR raw format)
  - SigMF-data         (paired with .sigmf-meta JSON; data is cf32)

Loops by default (so the stream never ends mid-test). Can be told to stop at
EOF via stop_at_eof=True.

This is the primary source for hardware-free development, regression tests,
and demos. Pair with a captured IQ file from a real SDR session (e.g.
`rtl_sdr -s 2400000 -f 145000000 -g 40 -n 2400000 - > capture.cf32`) and the
entire pipeline works identically to live SDR.

Slice-1 status: functional. Reads cf32, cs16, cu8. SigMF metadata is read
from the sibling .sigmf-meta file if present (for center_freq + sample_rate
defaults); the data file itself is consumed as raw cf32.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ._hw_common import RealtimePacer
from .base import SourceInfo

# File extensions → numpy dtype + sample interpretation
# NumPy dtype strings use byte-size notation: <f4 = little-endian float32,
# <i2 = little-endian int16, |u1 = single-byte uint8 (no endianness).
_FORMAT_TABLE: dict[str, tuple[np.dtype[Any], str]] = {
    ".cf32": (np.dtype("<f4"), "complex"),
    ".cfile": (np.dtype("<f4"), "complex"),
    ".cs16": (np.dtype("<i2"), "complex"),
    ".cu8": (np.dtype("|u1"), "complex"),
    ".sigmf-data": (np.dtype("<f4"), "complex"),
}


def _detect_format(file_path: Path) -> tuple[np.dtype[Any], str]:
    """Pick the numpy dtype + interpretation from the file extension."""
    ext = file_path.suffix.lower()
    # Special-case .sigmf-data which has a compound extension.
    if file_path.name.lower().endswith(".sigmf-data"):
        return (np.dtype("<f32"), "complex")
    if ext not in _FORMAT_TABLE:
        raise ValueError(
            f"unsupported IQ file format: {ext!r}. "
            f"Supported: {sorted(_FORMAT_TABLE.keys())}"
        )
    return _FORMAT_TABLE[ext]


def _read_sigmf_meta(file_path: Path) -> dict[str, Any] | None:
    """Look for a sibling .sigmf-meta file and parse it. Returns None if absent."""
    meta_path = file_path.with_suffix("")
    # file.sigmf-data → file.sigmf-meta
    if file_path.name.lower().endswith(".sigmf-data"):
        meta_path = file_path.with_suffix(".sigmf-meta")
    else:
        meta_path = file_path.with_suffix(".meta")  # fallback convention
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if isinstance(data, dict):
        return data
    return None


@dataclass
class FileSource:
    """Replay a recorded IQ file. Loops by default.

    Args:
        file_path: path to the IQ file (cf32/cfile/cs16/cu8/sigmf-data).
        loop: if True (default), rewind and replay forever. If False, the
            stream ends at EOF.
        chunk_size: number of complex64 samples per yield. Default 8192.
        center_freq_hint: Hz, used to populate SourceInfo + the FFT wire
            format header. If a sibling .sigmf-meta exists, that value wins.
        sample_rate_hint: Hz, similar to center_freq_hint.
    """

    file_path: Path
    loop: bool = True
    chunk_size: int = 8192
    center_freq_hint: int = 14_205_000
    sample_rate_hint: int = 2_400_000
    realtime: bool = True
    info: SourceInfo = field(default_factory=lambda: SourceInfo(
        type="file",
        label="File source",
        sample_rate=2_400_000,
    ))
    # Runtime digital gain (slice-4.7): dB applied to every replayed chunk.
    # None = unit gain. Plain float assignment — safe to update while the
    # spawn() loop is streaming.
    _runtime_gain_db: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.file_path = Path(self.file_path)
        # ReceiverSession adopts these when present (FileSource ignores the
        # spawn() rate/freq args — the recording IS that rate/freq).
        self.fixed_sample_rate = self.sample_rate_hint
        self.fixed_center_freq = self.center_freq_hint

        # Validate the format first — so callers get a meaningful error for
        # an unsupported extension even if the file doesn't exist.
        try:
            _detect_format(self.file_path)
        except ValueError:
            raise

        if not self.file_path.exists():
            raise FileNotFoundError(f"IQ file not found: {self.file_path}")

        # Try to upgrade hints from a sibling .sigmf-meta file.
        meta = _read_sigmf_meta(self.file_path)
        if meta:
            captures = meta.get("captures", [])
            if captures:
                first_cap = captures[0]
                cf = first_cap.get("core:frequency")
                sr = (
                    first_cap.get("core:sample_rate")
                    or meta.get("global", {}).get("core:sample_rate")
                )
                if cf is not None:
                    self.center_freq_hint = int(cf)
                if sr is not None:
                    self.sample_rate_hint = int(sr)
        self.fixed_sample_rate = self.sample_rate_hint
        self.fixed_center_freq = self.center_freq_hint

        # Populate SourceInfo with the actual values.
        object.__setattr__(
            self,
            "info",
            SourceInfo(
                type="file",
                label=f"File: {self.file_path.name}",
                endpoint=str(self.file_path),
                sample_rate=self.sample_rate_hint,
            ),
        )

    def set_runtime_gain(self, gain_db: float | None) -> bool:
        """Digital gain — scale replayed samples by 10^(dB/20).

        The recording is fixed, so "gain" is a digital scaling of the
        replayed samples (the honest equivalent of IF gain on a captured
        band: signals and noise floor scale together). None resets to unit
        gain.
        """
        self._runtime_gain_db = None if gain_db is None else float(gain_db)
        return True

    async def spawn(
        self,
        center_freq: int,
        sample_rate: int,
        gain: float | None = None,
    ) -> AsyncGenerator[np.ndarray, None]:
        """Stream IQ chunks from the file. Loops forever if self.loop.

        The center_freq and sample_rate arguments from the Source protocol
        are *ignored* — we always emit the file's original sample rate (the
        file was captured at that rate). The wire format will report the
        file's center_freq + sample_rate, not the spawn() args. Expose
        ``fixed_sample_rate`` / ``fixed_center_freq`` so the session adopts
        the real values before building its DSP chains.

        With ``realtime=True`` (default) replay is paced to wall-clock time —
        the waterfall scrolls exactly like live SDR. Tests use
        ``realtime=False`` for speed.

        gain seeds the runtime digital gain (see set_runtime_gain).
        """
        if gain is not None and self._runtime_gain_db is None:
            self._runtime_gain_db = float(gain)
        dtype, _kind = _detect_format(self.file_path)
        file_size_bytes = self.file_path.stat().st_size
        bytes_per_complex = dtype.itemsize * 2  # I + Q
        total_complex_samples = file_size_bytes // bytes_per_complex

        print(
            f"[FileSource] replaying {self.file_path.name} "
            f"({total_complex_samples:,} samples, "
            f"{total_complex_samples / max(1, self.sample_rate_hint):.1f}s @ "
            f"{self.sample_rate_hint:,} Hz, loop={self.loop}, realtime={self.realtime})"
        )
        pacer = RealtimePacer(self.sample_rate_hint, enabled=self.realtime)

        try:
            while True:  # outer loop: reopens file if looping
                # mmap for efficient repeated reads of large files.
                raw = np.memmap(
                    self.file_path,
                    dtype=dtype,
                    mode="r",
                )
                # Interpret the raw buffer as complex64 IQ samples.
                # We need an even number of base samples so the complex view
                # has a whole number of complex elements.
                if dtype == np.dtype("<f4"):  # cf32: float32 interleaved I,Q
                    # View float32 buffer directly as complex64 (2×float32).
                    even_len = raw.size - (raw.size % 2)
                    complex_view = raw[:even_len].view(np.complex64)
                elif dtype == np.dtype("<i2"):  # cs16: int16 interleaved I,Q
                    # Cast int16 → float32, then pair up.
                    even_len = raw.size - (raw.size % 2)
                    floats = np.array(raw[:even_len], dtype=np.float32)
                    complex_view = floats.view(np.complex64) * np.float32(1.0 / 32767.0)
                    complex_view = complex_view.astype(np.complex64)
                elif dtype == np.dtype("|u1"):  # cu8: uint8 interleaved I,Q
                    # RTL-SDR raw format. Center around 127.5, scale to [-1, 1].
                    even_len = raw.size - (raw.size % 2)
                    centered = (
                        raw[:even_len].astype(np.float32) - 127.5
                    ) * np.float32(1.0 / 127.5)
                    complex_view = centered.view(np.complex64)
                else:
                    raise RuntimeError(f"unhandled dtype {dtype}")

                n_total = complex_view.size
                offset = 0
                while offset < n_total:
                    end = min(offset + self.chunk_size, n_total)
                    chunk = np.array(complex_view[offset:end], dtype=np.complex64)
                    # Pad short trailing chunk with zeros so downstream FFT
                    # doesn't break (it expects at least fft_size samples).
                    if chunk.size < self.chunk_size:
                        pad = np.zeros(
                            self.chunk_size - chunk.size, dtype=np.complex64
                        )
                        chunk = np.concatenate([chunk, pad])
                    # Runtime digital gain (slice-4.7).
                    gain_db = self._runtime_gain_db
                    if gain_db is not None and gain_db != 0.0:
                        chunk = chunk * np.float32(10.0 ** (gain_db / 20.0))
                    yield chunk
                    await pacer.pace(chunk.size)
                    offset = end

                # memmap cleanup — just let it GC.
                del raw

                if not self.loop:
                    return
                # Loop: rewind and play again.
        except GeneratorExit:
            return

    async def close(self) -> None:
        return None
