"""QSL log — amateur radio contact logging (slice-60).

Provides a simple QSL (contact confirmation) log with REST API +
JSON persistence. Operators can log contacts (QSOs) with callsign,
frequency, mode, signal report, and timestamp.

The log is stored as a JSON file at `~/.config/openwebrx-plus/qsl-log.json`
(same pattern as user_settings.py). A ring buffer of the last 1000 QSOs
is kept; older entries are archived.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

_MAX_QSOS = 1000


class QsoEntry(BaseModel):
    """One QSO (contact) entry."""
    id: str
    timestamp: float
    callsign: str
    frequency_hz: int
    mode: str
    signal_report: str = ""
    notes: str = ""


class QslLog:
    """QSL log with JSON file persistence."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            config_dir = Path.home() / ".config" / "openwebrx-plus"
            config_dir.mkdir(parents=True, exist_ok=True)
            path = config_dir / "qsl-log.json"
        self._path = path
        self._entries: list[QsoEntry] = []
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            self._entries = [QsoEntry(**e) for e in data.get("qsos", [])]
        except Exception:
            self._entries = []

    def _save(self) -> None:
        data = {"qsos": [e.model_dump() for e in self._entries[-_MAX_QSOS:]]}
        self._path.write_text(json.dumps(data, indent=2))

    def add(self, entry: QsoEntry) -> QsoEntry:
        """Add a QSO to the log."""
        self._entries.append(entry)
        if len(self._entries) > _MAX_QSOS:
            self._entries = self._entries[-_MAX_QSOS:]
        self._save()
        return entry

    def list(self, limit: int = 100, offset: int = 0) -> list[QsoEntry]:
        """List QSOs, newest first."""
        return list(reversed(self._entries))[offset : offset + limit]

    def get(self, qso_id: str) -> QsoEntry | None:
        """Get a specific QSO by ID."""
        for e in self._entries:
            if e.id == qso_id:
                return e
        return None

    def delete(self, qso_id: str) -> bool:
        """Delete a QSO by ID. Returns True if found + deleted."""
        for i, e in enumerate(self._entries):
            if e.id == qso_id:
                self._entries.pop(i)
                self._save()
                return True
        return False

    def count(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        """Clear all QSOs."""
        self._entries.clear()
        self._save()
