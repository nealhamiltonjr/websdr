"""Tests for the QSL log (slice-60)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openwebrx_plus.qsl import QslLog, QsoEntry  # noqa: E402


def test_qsl_add_and_list(tmp_path: Path) -> None:
    log = QslLog(path=tmp_path / "qsl.json")
    entry = QsoEntry(id="q1", timestamp=1000, callsign="K1ABC", frequency_hz=14205000, mode="USB")
    log.add(entry)
    entries = log.list()
    assert len(entries) == 1
    assert entries[0].callsign == "K1ABC"


def test_qsl_list_newest_first(tmp_path: Path) -> None:
    log = QslLog(path=tmp_path / "qsl.json")
    for i in range(5):
        log.add(QsoEntry(id=f"q{i}", timestamp=i, callsign=f"CALL{i}", frequency_hz=14000000, mode="CW"))
    entries = log.list()
    assert entries[0].id == "q4"  # newest first
    assert entries[4].id == "q0"


def test_qsl_delete(tmp_path: Path) -> None:
    log = QslLog(path=tmp_path / "qsl.json")
    log.add(QsoEntry(id="q1", timestamp=1, callsign="A", frequency_hz=14e6, mode="CW"))
    assert log.delete("q1") is True
    assert log.count() == 0
    assert log.delete("nonexistent") is False


def test_qsl_persistence(tmp_path: Path) -> None:
    path = tmp_path / "qsl.json"
    log1 = QslLog(path=path)
    log1.add(QsoEntry(id="q1", timestamp=1, callsign="W1AW", frequency_hz=3570000, mode="RTTY"))
    # Reload from disk.
    log2 = QslLog(path=path)
    assert log2.count() == 1
    assert log2.list()[0].callsign == "W1AW"


def test_qsl_max_1000(tmp_path: Path) -> None:
    log = QslLog(path=tmp_path / "qsl.json")
    for i in range(1100):
        log.add(QsoEntry(id=f"q{i}", timestamp=i, callsign="X", frequency_hz=14e6, mode="CW"))
    assert log.count() == 1000


def test_qsl_get_by_id(tmp_path: Path) -> None:
    log = QslLog(path=tmp_path / "qsl.json")
    log.add(QsoEntry(id="q1", timestamp=1, callsign="A", frequency_hz=14e6, mode="CW"))
    entry = log.get("q1")
    assert entry is not None
    assert entry.callsign == "A"
    assert log.get("nonexistent") is None


def test_qsl_clear(tmp_path: Path) -> None:
    log = QslLog(path=tmp_path / "qsl.json")
    log.add(QsoEntry(id="q1", timestamp=1, callsign="A", frequency_hz=14e6, mode="CW"))
    log.clear()
    assert log.count() == 0
