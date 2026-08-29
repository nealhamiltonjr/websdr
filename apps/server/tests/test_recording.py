"""Tests for IQ recording (slice-61)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openwebrx_plus.recording import IqRecorder  # noqa: E402


def test_start_and_stop(tmp_path: Path) -> None:
    rec = IqRecorder(tmp_path)
    r = rec.start("rx-1", 14_200_000, 240_000)
    assert r.receiver_id == "rx-1"
    assert r.path.exists()
    assert rec.is_recording("rx-1")
    stopped = rec.stop("rx-1")
    assert stopped is not None
    assert stopped.stopped_at is not None
    assert not rec.is_recording("rx-1")


def test_write_data(tmp_path: Path) -> None:
    rec = IqRecorder(tmp_path)
    rec.start("rx-1", 14e6, 240_000)
    iq = np.array([1+1j, 2+2j, 3+3j], dtype=np.complex64)
    rec.write("rx-1", iq)
    stopped = rec.stop("rx-1")
    assert stopped is not None
    assert stopped.bytes_written == iq.nbytes
    # Verify the file contains the data.
    data = stopped.path.read_bytes()
    assert len(data) == iq.nbytes


def test_start_twice_raises(tmp_path: Path) -> None:
    rec = IqRecorder(tmp_path)
    rec.start("rx-1", 14e6, 240_000)
    with pytest.raises(ValueError, match="already recording"):
        rec.start("rx-1", 14e6, 240_000)


def test_stop_not_recording(tmp_path: Path) -> None:
    rec = IqRecorder(tmp_path)
    assert rec.stop("rx-1") is None


def test_list_recordings(tmp_path: Path) -> None:
    rec = IqRecorder(tmp_path)
    rec.start("rx-1", 14e6, 240_000)
    rec.write("rx-1", np.zeros(10, dtype=np.complex64))
    rec.stop("rx-1")
    recordings = rec.list_recordings()
    assert len(recordings) == 1
    assert recordings[0]["filename"].endswith(".cf32")
    assert recordings[0]["size_bytes"] > 0


def test_delete_recording(tmp_path: Path) -> None:
    rec = IqRecorder(tmp_path)
    rec.start("rx-1", 14e6, 240_000)
    rec.stop("rx-1")
    recordings = rec.list_recordings()
    filename = recordings[0]["filename"]
    assert rec.delete_recording(filename) is True
    assert len(rec.list_recordings()) == 0
    assert rec.delete_recording("nonexistent.cf32") is False


def test_creates_recordings_dir(tmp_path: Path) -> None:
    """The recordings dir is created if it doesn't exist."""
    d = tmp_path / "subdir" / "recordings"
    IqRecorder(d)
    assert d.exists()
