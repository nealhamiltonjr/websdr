"""ATC (Air Traffic Control) voice activity detector — AM squelch.

ATC communications use AM (Amplitude Modulation) on VHF frequencies
(118.000–136.975 MHz worldwide). This "decoder" isn't a protocol
decoder — it's a voice activity detector that emits events when the
controller or pilot starts/stops talking.

  * **Modulation**: AM (double-sideband, full carrier).
  * **Frequencies**: 118.000–136.975 MHz (25 kHz or 8.33 kHz spacing).
  * **Squelch**: RSSI-based + noise-floor detection.
  * **Voice activity**: Envelope-following with hysteresis.

The detector:
  1. Computes the signal envelope (magnitude of the analytic signal).
  2. Compares against a squelch threshold (configurable, default -40 dBFS).
  3. Emits "voice_start" when the envelope exceeds the threshold for
     >100 ms (debounce).
  4. Emits "voice_end" when the envelope drops below the threshold for
     >500 ms (hang time).
  5. Periodically emits "rssi" events with the current signal strength.

This module is pure-numpy (ADR-004 compliant — no scipy in the live path).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

# --- ATC wire constants ---
DEFAULT_SAMPLE_RATE = 8000
DEFAULT_SQUELCH_DBFS = -40.0  # open squelch above this
DEFAULT_HANG_TIME_S = 0.5  # close squelch after this much silence
DEFAULT_DEBOUNCE_S = 0.1  # require this much signal to open
DEFAULT_RSSI_INTERVAL_S = 1.0  # emit RSSI every this many seconds


@dataclass
class AtcVoiceEvent:
    """One ATC voice activity event."""
    kind: str  # "voice_start" | "voice_end" | "rssi"
    ts: float
    rssi_dbfs: float = 0.0
    frequency_hz: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ts": self.ts,
            "rssi_dbfs": round(self.rssi_dbfs, 1),
            "frequency_hz": self.frequency_hz,
        }


class AtcReceiver:
    """Streaming ATC voice activity detector — int16 PCM → voice events.

    Args:
        sample_rate: audio sample rate in Hz (default 8000).
        squelch_dbfs: squelch open threshold in dBFS (default -40).
        hang_time_s: silence duration before squelch closes (default 0.5 s).
        debounce_s: signal duration before squelch opens (default 0.1 s).
        rssi_interval_s: RSSI report interval (default 1.0 s).
    """

    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        squelch_dbfs: float = DEFAULT_SQUELCH_DBFS,
        hang_time_s: float = DEFAULT_HANG_TIME_S,
        debounce_s: float = DEFAULT_DEBOUNCE_S,
        rssi_interval_s: float = DEFAULT_RSSI_INTERVAL_S,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError(f"sample_rate must be > 0, got {sample_rate}")
        if squelch_dbfs > 0:
            raise ValueError(f"squelch_dbfs must be <= 0, got {squelch_dbfs}")
        self.sample_rate = sample_rate
        self.squelch_dbfs = squelch_dbfs
        self.hang_time_s = hang_time_s
        self.debounce_s = debounce_s
        self.rssi_interval_s = rssi_interval_s
        # State.
        self._is_active: bool = False  # squelch open?
        self._signal_time: float = 0.0  # accumulated signal time
        self._silence_time: float = 0.0  # accumulated silence time
        self._last_rssi: float = -60.0
        self._last_rssi_time: float = 0.0
        self._frequency_hz: int = 0
        self._chunk_count: int = 0

    def feed(self, pcm: np.ndarray, frequency_hz: int = 0) -> list[AtcVoiceEvent]:
        """Feed int16 PCM samples, return voice activity events.

        Args:
            pcm: 1-D int16 numpy array (mono audio).
            frequency_hz: the tuned frequency (for event metadata).
        Returns:
            List of AtcVoiceEvent objects (voice_start, voice_end, rssi).
        """
        if pcm.size == 0:
            return []
        if pcm.dtype != np.int16:
            pcm = pcm.astype(np.int16)
        self._frequency_hz = frequency_hz
        # Compute RSSI (peak amplitude in dBFS).
        peak = float(np.max(np.abs(pcm)))
        rssi_dbfs = 20.0 * math.log10(peak / 32768.0) if peak > 0 else -60.0
        self._last_rssi = rssi_dbfs
        now = time.time()
        events: list[AtcVoiceEvent] = []
        # Squelch logic.
        chunk_duration = pcm.size / self.sample_rate
        above_squelch = rssi_dbfs > self.squelch_dbfs
        if above_squelch:
            self._signal_time += chunk_duration
            self._silence_time = 0.0
            if not self._is_active and self._signal_time >= self.debounce_s:
                self._is_active = True
                events.append(AtcVoiceEvent(
                    kind="voice_start", ts=now, rssi_dbfs=rssi_dbfs,
                    frequency_hz=self._frequency_hz,
                ))
        else:
            self._silence_time += chunk_duration
            self._signal_time = 0.0
            if self._is_active and self._silence_time >= self.hang_time_s:
                self._is_active = False
                events.append(AtcVoiceEvent(
                    kind="voice_end", ts=now, rssi_dbfs=rssi_dbfs,
                    frequency_hz=self._frequency_hz,
                ))
        # Periodic RSSI report.
        if now - self._last_rssi_time >= self.rssi_interval_s:
            self._last_rssi_time = now
            events.append(AtcVoiceEvent(
                kind="rssi", ts=now, rssi_dbfs=rssi_dbfs,
                frequency_hz=self._frequency_hz,
            ))
        self._chunk_count += 1
        return events

    @property
    def is_active(self) -> bool:
        """True when the squelch is open (voice detected)."""
        return self._is_active

    @property
    def last_rssi(self) -> float:
        """The most recent RSSI reading in dBFS."""
        return self._last_rssi

    def reset(self) -> None:
        """Clear all state."""
        self._is_active = False
        self._signal_time = 0.0
        self._silence_time = 0.0
        self._last_rssi = -60.0
        self._last_rssi_time = 0.0
        self._chunk_count = 0
