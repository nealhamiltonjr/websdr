"""FAX (Facsimile) demodulator — FM weather image decoder.

Weather FAX transmits images via FM modulation:

  * **Black frequency**: 1500 Hz (0% intensity)
  * **White frequency**: 2300 Hz (100% intensity)
  * **Start tone**: 300 Hz, 5% amplitude for 5 seconds
  * **Phase sync (phasing)**: alternating black/white lines
  * **Stop tone**: 450 Hz for 5 seconds
  * **IOC (Index of Cooperation)**: 576 (standard) or 288 (low-res)
  * **LPM (Lines Per Minute)**: 120 (standard) or 60/90/240 (other modes)

The demodulator:
  1. Detects the start tone (300 Hz for 5 s).
  2. Reads image lines using FM demodulation (frequency → intensity).
  3. Detects the stop tone (450 Hz for 5 s).
  4. Emits a complete image when the stop tone is received.

This module reuses the SSTV frequency-to-pixel mapping (1500 Hz = black,
2300 Hz = white) but with a different line structure (FAX has no VIS code;
mode is detected from the start/stop tones + line timing).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# --- FAX wire constants ---
DEFAULT_SAMPLE_RATE = 8000
FREQ_BLACK = 1500.0
FREQ_WHITE = 2300.0
FREQ_START = 300.0  # start tone
FREQ_STOP = 450.0   # stop tone

DEFAULT_IOC = 576  # Index of Cooperation (standard)
DEFAULT_LPM = 120  # Lines Per Minute (standard)
DEFAULT_LINE_WIDTH = 800  # pixels per line (for IOC 576)


@dataclass
class FaxImage:
    """One decoded FAX image."""
    width: int
    height: int
    # Grayscale pixel data, shape (height, width), uint8.
    pixels: np.ndarray
    timestamp: float = 0.0


class FaxReceiver:
    """Streaming FAX demodulator — int16 PCM → FaxImage frames.

    Args:
        sample_rate: audio sample rate in Hz (default 8000).
        ioc: Index of Cooperation (default 576).
        lpm: Lines Per Minute (default 120).
        line_width: pixels per line (default 800 for IOC 576).
    """

    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        ioc: int = DEFAULT_IOC,
        lpm: int = DEFAULT_LPM,
        line_width: int = DEFAULT_LINE_WIDTH,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError(f"sample_rate must be > 0, got {sample_rate}")
        if lpm <= 0:
            raise ValueError(f"lpm must be > 0, got {lpm}")
        self.sample_rate = sample_rate
        self.ioc = ioc
        self.lpm = lpm
        self.line_width = line_width
        # Derived timing.
        self._line_duration_s = 60.0 / lpm  # seconds per line
        self._samples_per_line = int(sample_rate * self._line_duration_s)
        self._samples_per_pixel = max(1, self._samples_per_line // line_width)
        # State.
        self._state: str = "idle"  # idle | receiving | done
        self._scanlines: list[np.ndarray] = []
        self._audio_buf: list[np.ndarray] = []
        self._start_detect_samples = int(sample_rate * 0.5)  # 500ms window

    def feed(self, pcm: np.ndarray) -> list[FaxImage]:
        """Feed int16 PCM samples, return a list of completed FaxImage frames."""
        if pcm.size == 0:
            return []
        if pcm.dtype != np.int16:
            pcm = pcm.astype(np.int16)
        self._audio_buf.append(pcm)
        # Process when we have at least one line's worth of audio.
        total = sum(len(c) for c in self._audio_buf)
        if total < self._samples_per_line:
            return []
        audio = np.concatenate(self._audio_buf)
        self._audio_buf.clear()
        images: list[FaxImage] = []
        offset = 0
        while offset < len(audio):
            if self._state == "idle":
                # Look for the start tone (300 Hz).
                start_end = self._detect_start_tone(audio, offset)
                if start_end is None:
                    # Keep the last 500ms for the next call.
                    keep = min(self._start_detect_samples, len(audio) - offset)
                    if keep > 0:
                        self._audio_buf.append(audio[len(audio) - keep:])
                    break
                offset = start_end
                self._state = "receiving"
                self._scanlines = []
            elif self._state == "receiving":
                # Check for stop tone.
                if self._detect_stop_tone(audio, offset):
                    # Complete image.
                    if len(self._scanlines) > 0:
                        img = self._assemble_image()
                        images.append(img)
                    self._state = "idle"
                    self._scanlines = []
                    offset += self._start_detect_samples
                    continue
                # Decode one scanline.
                if offset + self._samples_per_line > len(audio):
                    self._audio_buf.append(audio[offset:])
                    break
                scanline = self._decode_scanline(audio[offset : offset + self._samples_per_line])
                self._scanlines.append(scanline)
                offset += self._samples_per_line
        return images

    def _detect_start_tone(self, audio: np.ndarray, offset: int) -> int | None:
        """Detect the 300 Hz start tone. Returns end offset or None."""
        if offset + self._start_detect_samples > len(audio):
            return None
        seg = audio[offset : offset + self._start_detect_samples].astype(np.float32) / 32768.0
        freq = _estimate_freq(seg, self.sample_rate)
        if abs(freq - FREQ_START) < 100:
            return offset + self._start_detect_samples
        return None

    def _detect_stop_tone(self, audio: np.ndarray, offset: int) -> bool:
        """Check if the current position has a 450 Hz stop tone."""
        if offset + self._start_detect_samples > len(audio):
            return False
        seg = audio[offset : offset + self._start_detect_samples].astype(np.float32) / 32768.0
        freq = _estimate_freq(seg, self.sample_rate)
        return abs(freq - FREQ_STOP) < 100

    def _decode_scanline(self, audio: np.ndarray) -> np.ndarray:
        """Decode one scanline to a 1-D pixel array (grayscale)."""
        sr = self.sample_rate
        spp = self._samples_per_pixel
        pixels = np.zeros(self.line_width, dtype=np.uint8)
        for x in range(self.line_width):
            start = x * spp
            end = min(start + spp, len(audio))
            if start >= len(audio):
                break
            seg = audio[start:end].astype(np.float32) / 32768.0
            freq = _estimate_freq(seg, sr)
            # Map frequency → intensity (1500 Hz = 0 black, 2300 Hz = 255 white).
            intensity = int(np.clip((freq - FREQ_BLACK) / (FREQ_WHITE - FREQ_BLACK) * 255, 0, 255))
            pixels[x] = intensity
        return pixels

    def _assemble_image(self) -> FaxImage:
        import time
        pixels = np.stack(self._scanlines, axis=0)
        return FaxImage(
            width=self.line_width,
            height=len(self._scanlines),
            pixels=pixels,
            timestamp=time.time(),
        )

    def reset(self) -> None:
        self._state = "idle"
        self._scanlines = []
        self._audio_buf = []

    @property
    def state(self) -> str:
        return self._state

    @property
    def scanline_count(self) -> int:
        return len(self._scanlines)


def _estimate_freq(samples: np.ndarray, sample_rate: int) -> float:
    """Estimate dominant frequency via zero-crossing count."""
    if samples.size < 4:
        return 0.0
    signs = np.sign(samples)
    signs = signs[signs != 0]
    if signs.size < 2:
        return 0.0
    crossings = np.sum(np.diff(signs) != 0)
    freq = crossings * sample_rate / (2.0 * samples.size)
    return float(freq)
