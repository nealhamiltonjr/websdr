"""IQ recording — opt-in recording of raw IQ to disk (slice-61).

Provides REST endpoints to start/stop recording the raw IQ stream from
a receiver to a file. Recordings are saved as complex-float32 (.cf32)
files in the configured recordings_dir (default: .openwebrx-plus/recordings/).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class Recording:
    """One IQ recording."""
    receiver_id: str
    path: Path
    center_freq: int
    sample_rate: int
    started_at: float
    stopped_at: float | None = None
    bytes_written: int = 0


class IqRecorder:
    """Manages IQ recordings for multiple receivers."""

    def __init__(self, recordings_dir: Path) -> None:
        self._dir = recordings_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._active: dict[str, Recording] = {}
        self._file_handles: dict[str, Any] = {}

    def start(
        self,
        receiver_id: str,
        center_freq: int,
        sample_rate: int,
    ) -> Recording:
        """Start recording IQ from the given receiver."""
        if receiver_id in self._active:
            raise ValueError(f"receiver {receiver_id} is already recording")
        filename = f"{receiver_id}_{int(time.time())}.cf32"
        path = self._dir / filename
        rec = Recording(
            receiver_id=receiver_id,
            path=path,
            center_freq=center_freq,
            sample_rate=sample_rate,
            started_at=time.time(),
        )
        self._active[receiver_id] = rec
        self._file_handles[receiver_id] = open(path, "wb")  # noqa: SIM115
        return rec

    def write(self, receiver_id: str, iq: np.ndarray) -> None:
        """Write IQ samples to the recording file."""
        if receiver_id not in self._active:
            return
        fh = self._file_handles[receiver_id]
        # Convert complex64 to bytes.
        data = np.ascontiguousarray(iq, dtype=np.complex64).tobytes()
        fh.write(data)
        self._active[receiver_id].bytes_written += len(data)

    def stop(self, receiver_id: str) -> Recording | None:
        """Stop recording. Returns the completed Recording or None."""
        if receiver_id not in self._active:
            return None
        rec = self._active.pop(receiver_id)
        fh = self._file_handles.pop(receiver_id)
        fh.close()
        rec.stopped_at = time.time()
        return rec

    @property
    def active_recordings(self) -> list[str]:
        """List of receiver IDs currently recording."""
        return list(self._active.keys())

    def is_recording(self, receiver_id: str) -> bool:
        return receiver_id in self._active

    def list_recordings(self) -> list[dict[str, Any]]:
        """List all recording files in the recordings dir."""
        recordings: list[dict[str, Any]] = []
        for p in sorted(self._dir.glob("*.cf32")):
            recordings.append({
                "filename": p.name,
                "path": str(p),
                "size_bytes": p.stat().st_size,
                "modified": p.stat().st_mtime,
            })
        return recordings

    def delete_recording(self, filename: str) -> bool:
        """Delete a recording file by filename."""
        path = self._dir / filename
        if not path.exists() or path.suffix != ".cf32":
            return False
        path.unlink()
        return True
