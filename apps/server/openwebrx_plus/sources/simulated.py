"""SimulatedSource — generates multi-signal synthetic IQ. Hardware-free dev/test.

This is the workhorse source for hardware-free development and demos. Unlike
the slice-1 RtlSdrSource stub (which just emits a single complex tone), the
SimulatedSource generates a richer RF scene:

  - Gaussian noise floor (so the waterfall has background, not just black)
  - N narrowband carriers at user-set offsets, with per-carrier modulation:
      - CW (Morse-like keying)
      - AM (carrier × (1 + m × audio))
      - FM (carrier phase modulated by audio)
      - SSB-AM (carrier with one sideband, audio-shaped envelope)
  - Optional pulsed signals (for ADS-B / FT8 fixture-like simulations)

Default "signal_set" presets:
  - "default"      — a few carriers + noise (good for general FFT/waterfall dev)
  - "am_band"      — 5 AM carriers in the AM broadcast band, simulating local stations
  - "ham_band"     — 3 SSB-ish carriers in the 20m band, simulating hams
  - "ads_b"        — pulsed 1200 µs PPM-style bursts at baseband (1090 MHz sim)
  - "ft8_dry_run"  — 8 narrow carriers simulating FT8 pile-up

The frequency offsets are relative to the Source's center_freq. The
SimulatedSource sets self.info.sample_rate = sample_rate so the wire format
reports correctly.

Slice-1 status: functional. Sufficient for slice-1/2/3 frontend dev without
any SDR hardware present.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from ._hw_common import RealtimePacer
from .base import SourceInfo

SignalSet = Literal[
    "default",
    "am_band",
    "ham_band",
    "ads_b",
    "ft8_dry_run",
]


@dataclass(frozen=True)
class _Carrier:
    """One synthesized carrier in the simulated RF scene."""

    offset_hz: float           # offset from center_freq
    amplitude: float           # 0..1
    kind: Literal["cw", "am", "fm", "ssb"] = "cw"
    audio_freq: float = 440.0  # Hz; for am/fm/ssb, the modulating tone
    morse_pattern: tuple[bool, ...] = ()  # for cw: on/off at the chunk rate
    pulse_period_s: float = 0.0  # for pulsed (ads_b-like)
    pulse_duration_s: float = 0.0


# Each preset is a tuple of _Carrier specs. Offsets are kept inside ±500 kHz
# so they fit in a 2.4 MSPS source window; simulators with wider offsets
# are possible but unusual.
_PRESETS: dict[str, tuple[_Carrier, ...]] = {
    "default": (
        _Carrier(offset_hz=0.0, amplitude=0.6, kind="cw"),
        _Carrier(offset_hz=12_500.0, amplitude=0.4, kind="am", audio_freq=440.0),
        _Carrier(offset_hz=-37_500.0, amplitude=0.5, kind="fm", audio_freq=220.0),
        _Carrier(offset_hz=87_300.0, amplitude=0.3, kind="ssb", audio_freq=800.0),
    ),
    "am_band": (
        _Carrier(offset_hz=0.0, amplitude=0.7, kind="am", audio_freq=350.0),
        _Carrier(offset_hz=50_000.0, amplitude=0.6, kind="am", audio_freq=520.0),
        _Carrier(offset_hz=100_000.0, amplitude=0.65, kind="am", audio_freq=680.0),
        _Carrier(offset_hz=150_000.0, amplitude=0.5, kind="am", audio_freq=290.0),
        _Carrier(offset_hz=-50_000.0, amplitude=0.4, kind="am", audio_freq=880.0),
    ),
    "ham_band": (
        _Carrier(offset_hz=0.0, amplitude=0.55, kind="ssb", audio_freq=750.0),
        _Carrier(offset_hz=25_000.0, amplitude=0.4, kind="ssb", audio_freq=1100.0),
        _Carrier(offset_hz=-30_000.0, amplitude=0.6, kind="cw"),
    ),
    "ads_b": (
        # Single 1200 µs pulse at baseband, repeating every ~1 s (ADS-B-like
        # squitter rate). Used to test the AircraftMapViz + ADS-B plugin
        # before they're wired to real IQ.
        _Carrier(
            offset_hz=0.0,
            amplitude=0.95,
            kind="cw",
            pulse_period_s=1.0,
            pulse_duration_s=120e-6,
        ),
    ),
    "ft8_dry_run": tuple(
        _Carrier(
            offset_hz=float(i * 6.25 - 50.0),  # 6.25 Hz FT8 spacing, 8 carriers
            amplitude=0.35,
            kind="cw",  # FT8 is more complex, but a CW carrier approximates the spectral look
        )
        for i in range(8)
    ),
}


def _hann_envelope(n: int, period: float, duration: float, sample_rate: int) -> np.ndarray:
    """Return a Tukey-like window of length n: 0 outside [t0, t0+duration], 1 in the middle."""
    # period and duration in seconds; returns 0..1 envelope per sample.
    t = np.arange(n, dtype=np.float32) / sample_rate
    # Modulo the period to get the position in the cycle.
    t_phase = t % period
    # 1 inside [0, duration], 0 outside, with cosine-tapered edges.
    inside = (t_phase < duration).astype(np.float32)
    # Edge taper: 128 samples cosine edge.
    edge = min(128, max(1, int(0.25 * duration * sample_rate)))
    edge_envelope = np.ones(n, dtype=np.float32)
    for i, tv in enumerate(t_phase):
        if tv < edge / sample_rate:
            edge_envelope[i] = 0.5 * (1 - np.cos(np.pi * tv * sample_rate / edge))
        elif tv > duration - edge / sample_rate and tv < duration:
            edge_envelope[i] = 0.5 * (1 - np.cos(np.pi * (duration - tv) * sample_rate / edge))
        elif tv >= duration:
            edge_envelope[i] = 0.0
    return inside * edge_envelope


@dataclass
class SimulatedSource:
    """Generate synthetic multi-signal IQ. Hardware-free.

    Args:
        signal_set: which preset to use ("default", "am_band", "ham_band",
            "ads_b", "ft8_dry_run"). See _PRESETS above.
        noise_floor: RMS amplitude of the background Gaussian noise. 0..1.
        sample_rate: output sample rate in Hz.
        chunk_size: complex64 samples per yield.
        seed: RNG seed for reproducible noise.
    """

    signal_set: SignalSet = "default"
    noise_floor: float = 0.05
    sample_rate: int = 2_400_000
    chunk_size: int = 8192
    seed: int = 42
    realtime: bool = True
    info: SourceInfo = field(default_factory=lambda: SourceInfo(
        type="simulated",
        label="Simulated source",
        sample_rate=2_400_000,
    ))
    # Runtime digital gain (slice-4.7): dB applied to every yielded chunk.
    # None = unit gain (no manual gain). Plain float assignment — safe to
    # update while the spawn() loop is streaming.
    _runtime_gain_db: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.signal_set not in _PRESETS:
            raise ValueError(
                f"unknown signal_set {self.signal_set!r}; "
                f"valid: {sorted(_PRESETS.keys())}"
            )
        # Sync info.sample_rate to the actual sample_rate.
        object.__setattr__(
            self,
            "info",
            SourceInfo(
                type="simulated",
                label=f"Simulated ({self.signal_set})",
                sample_rate=self.sample_rate,
            ),
        )
        self._rng = np.random.default_rng(self.seed)
        # Sample-continuous time counter for phase coherence across chunks.
        self._t0 = 0.0

    def set_runtime_gain(self, gain_db: float | None) -> bool:
        """Digital gain — scale the synthetic scene by 10^(dB/20).

        There is no hardware behind this source, so "gain" is a digital
        scaling of the output samples (signals + noise floor together,
        exactly what an IF gain stage does to a captured band). None resets
        to unit gain.
        """
        self._runtime_gain_db = None if gain_db is None else float(gain_db)
        return True

    async def spawn(
        self,
        center_freq: int,
        sample_rate: int,
        gain: float | None = None,
    ) -> AsyncGenerator[np.ndarray, None]:
        """Generate synthetic IQ chunks forever.

        center_freq is informational only (the SimulatedSource is baseband;
        its carriers are offsets from whatever center_freq you ask for).

        sample_rate from spawn() overrides self.sample_rate if different.
        gain is ignored (no real hardware).

        With ``realtime=True`` (default) output is wall-clock paced — the
        scene behaves like a live SDR for the FFT/audio chains and WS
        subscribers (ADR-004 gotcha #2). Tests use ``realtime=False``.
        """
        if sample_rate != self.sample_rate:
            # Honor whatever the ReceiverSession asks for.
            self.sample_rate = sample_rate
        # Spawn-time gain seeds the runtime digital gain (hub passes the
        # session's gain through) unless a runtime gain was already set.
        if gain is not None and self._runtime_gain_db is None:
            self._runtime_gain_db = float(gain)

        pacer = RealtimePacer(sample_rate, enabled=self.realtime)

        carriers = _PRESETS[self.signal_set]
        # Per-carrier phase accumulators (so the signal is sample-continuous
        # across chunk boundaries).
        phase_accum = np.zeros(len(carriers), dtype=np.float64)
        # Phase accumulator for the modulating audio too.
        audio_phase_accum = np.zeros(len(carriers), dtype=np.float64)

        # Noise RNG per-spawn (so multiple spawns of the same source produce
        # different noise — important for the noise floor to look natural).
        rng = np.random.default_rng(self.seed)

        print(
            f"[SimulatedSource] signal_set={self.signal_set!r} "
            f"{len(carriers)} carriers, sample_rate={sample_rate:,} Hz, "
            f"noise_floor={self.noise_floor}"
        )

        try:
            while True:
                n = self.chunk_size
                t = self._t0 + np.arange(n, dtype=np.float32) / sample_rate
                # Accumulator: complex64 output.
                out = np.zeros(n, dtype=np.complex64)

                for i, c in enumerate(carriers):
                    # Phase advance for this carrier at this chunk.
                    carrier_phase_inc = 2 * np.pi * c.offset_hz / sample_rate
                    phase = phase_accum[i] + (
                        np.arange(n, dtype=np.float32) * carrier_phase_inc
                    )

                    if c.kind == "cw":
                        # amp may be a scalar (constant carrier) or a
                        # per-sample envelope (pulsed carriers like ADS-B).
                        amp: float | np.ndarray = c.amplitude
                        # Pulsed? (e.g. ADS-B)
                        if c.pulse_period_s > 0 and c.pulse_duration_s > 0:
                            amp = amp * _hann_envelope(
                                n, c.pulse_period_s, c.pulse_duration_s, sample_rate
                            )
                        carrier = amp * np.exp(1j * phase.astype(np.float32))
                    elif c.kind == "am":
                        # AM: carrier × (1 + m × audio)
                        audio_phase_inc = 2 * np.pi * c.audio_freq / sample_rate
                        audio_phase = audio_phase_accum[i] + (
                            np.arange(n, dtype=np.float32) * audio_phase_inc
                        )
                        audio = np.sin(audio_phase.astype(np.float32))
                        m = 0.8  # 80% modulation
                        envelope = (1.0 + m * audio) * c.amplitude
                        carrier = envelope * np.exp(1j * phase.astype(np.float32))
                    elif c.kind == "fm":
                        # FM: phase modulated by integral of audio
                        audio_phase_inc = 2 * np.pi * c.audio_freq / sample_rate
                        audio_phase = audio_phase_accum[i] + (
                            np.arange(n, dtype=np.float32) * audio_phase_inc
                        )
                        audio = np.sin(audio_phase.astype(np.float32))
                        # Frequency deviation: 3 kHz
                        dev_hz = 3000.0
                        # Phase deviation = 2π × dev × ∫ audio dt
                        phase_mod = (
                            2 * np.pi * dev_hz * audio / sample_rate
                        )
                        # Accumulate phase_mod into phase_accum via cumulative sum
                        modulated_phase = phase + np.cumsum(phase_mod)
                        carrier = c.amplitude * np.exp(
                            1j * modulated_phase.astype(np.float32)
                        )
                        # Update phase_accum to the last phase value.
                        phase_accum[i] = float(modulated_phase[-1] % (2 * np.pi))
                        out += carrier
                        audio_phase_accum[i] = float(
                            (audio_phase[-1]) % (2 * np.pi)
                        )
                        continue  # skip the shared phase_accum update below
                    elif c.kind == "ssb":
                        # SSB: simulated as carrier × audio-shaped envelope
                        # (real SSB has no carrier; this is an approximation
                        # sufficient for the waterfall to look alive.)
                        audio_phase_inc = 2 * np.pi * c.audio_freq / sample_rate
                        audio_phase = audio_phase_accum[i] + (
                            np.arange(n, dtype=np.float32) * audio_phase_inc
                        )
                        audio = np.sin(audio_phase.astype(np.float32))
                        carrier = c.amplitude * audio * np.exp(
                            1j * phase.astype(np.float32)
                        )
                        audio_phase_accum[i] = float(
                            (audio_phase[-1]) % (2 * np.pi)
                        )
                    else:
                        # Unknown kind — skip.
                        continue

                    out += carrier
                    # phase_accum for non-fm kinds (fm already `continue`d above
                    # with its own phase update).
                    phase_accum[i] = float(phase[-1] % (2 * np.pi))

                # Add Gaussian noise floor.
                if self.noise_floor > 0:
                    noise_re = rng.standard_normal(n).astype(np.float32)
                    noise_im = rng.standard_normal(n).astype(np.float32)
                    out += (
                        self.noise_floor
                        / np.sqrt(2.0)
                        * (noise_re + 1j * noise_im)
                    )

                # Advance the global time counter.
                self._t0 = float(t[-1]) + 1.0 / sample_rate

                # Runtime digital gain (slice-4.7) — applied last so it
                # covers carriers AND the noise floor.
                gain_db = self._runtime_gain_db
                if gain_db is not None and gain_db != 0.0:
                    out *= np.float32(10.0 ** (gain_db / 20.0))

                yield out.astype(np.complex64)
                await pacer.pace(n)
        except GeneratorExit:
            return

    async def close(self) -> None:
        return None
