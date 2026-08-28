"""AudioChain — pycsdr-backed per-mode audio demodulation pipeline.

Wire topology (mode-dependent)::

    AM/NFM/WFM:
        in_buf (COMPLEX_FLOAT)
            → Shift(rate=offset/sample_rate)        # tune to channel center
            → Bandpass(low, high, transition)       # channel filter
            → demod (AmDemod | FmDemod | PhaseDemod)
            → [Squelch]                             # slice-5.2 (optional)
            → [DcBlock | WfmDeemphasis | NfmDeemphasis]  # classic only
            → [Agc | Gain]                          # slice-5.2 (optional)
            → AudioResampler(in_rate, out_rate)      # → 8 kHz
            → [Limit(maxAmplitude=1.0)]              # classic only (when AGC off)
            → Convert(FLOAT → SHORT)                  # → int16 PCM
            → out_buf (SHORT)

    USB/LSB/CW:
        in_buf (COMPLEX_FLOAT)
            → Shift(rate=offset/sample_rate)
            → Bandpass(low, high, transition)        # SSB filter ~2.7 kHz
            → RealPart()                              # collapse complex → real
            → [Squelch]                              # slice-5.2 (optional)
            → [Agc | Gain]                           # slice-5.2 (optional)
            → AudioResampler(in_rate, out_rate)
            → Convert(FLOAT → SHORT)
            → out_buf (SHORT)

DSP modes (ADR-002, slice-4.7):
    conditioning=True  ("classic") — demodulated audio is conditioned:
        DC-block after AM/NFM demod, 50 µs de-emphasis after WFM demod,
        and a ±1.0 soft limiter before the int16 convert.
    conditioning=False ("raw") — the demodulator output goes straight to
        the resampler + convert. What you hear is what the demod saw: no
        DC removal, no de-emphasis, no limiting (may clip harshly on
        overdriven signals — that's the point). For SSB/CW the two modes
        are structurally identical (RealPart is the demodulator, not a
        conditioning stage).

Slice-5.2 fine-grained controls (DSPParams):
    The chain accepts an optional dsp_params struct that wires in
    additional stages when their corresponding fields are set. See
    `dsp/types.py:DSPParams` for the field-by-field contract.
    - low_cut_hz / high_cut_hz override the mode profile's bandpass cuts
    - agc_enabled inserts an Agc block (replaces the soft Limit when on)
    - squelch_db inserts a Squelch block after the demod
    - dc_block_enabled=False skips the DcBlock stage (overrides classic)
    - deemphasis_enabled inserts NfmDeemphasis on NFM (WFM keeps it)
    - manual_gain_db inserts a Gain block before the resampler
    - notch_* and noise_blanker_* are accepted but no-op until slice-5.3
      (pycsdr has no native Notch / Nb block; queued for custom impl)

The receiver session pushes complex64 IQ bytes via :meth:`feed`; ready
audio frames are drained via :meth:`drain` (non-blocking).

Per-mode defaults (Hz, relative to center after Shift):

    USB:  +150 .. +2850  (lower side suppressed)
    LSB:  -2850 .. -150  (upper side suppressed)
    CW:   +600 .. +900   (300 Hz wide, offset so tone lands at ~750 Hz)
    AM:   -5000 .. +5000 (10 kHz wide)
    NFM:  -6000 .. +6000 (12.5 kHz channel, voice)
    WFM:  -100000 .. +100000 (broadcast FM 200 kHz)
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import structlog
from pycsdr.modules import (
    Agc,
    AmDemod,
    AudioResampler,
    Bandpass,
    Buffer,
    Convert,
    DcBlock,
    FirDecimate,
    FmDemod,
    Gain,
    Limit,
    NfmDeemphasis,
    RealPart,
    Shift,
    Squelch,
    WfmDeemphasis,
)
from pycsdr.types import Format

from .types import AudioFrame, DSPParams

if TYPE_CHECKING:
    from pycsdr.modules import BufferReader

log = structlog.get_logger(__name__)

DemodMode = Literal["AM", "NFM", "WFM", "USB", "LSB", "CW"]


@dataclass(frozen=True, slots=True)
class _ModeProfile:
    """Per-mode chain layout.

    ``channel_rate`` is the post-decimation rate the demodulator and
    bandpass run at. Decimating the wideband IQ down to the channel rate
    FIRST is critical: it makes the bandpass cheap and keeps the audio
    resampler's ratio small (libsamplerate's sinc converters are slow at
    high ratios — 30:1 runs at ~80 Ksps, 6:1 at Msps rates).
    """

    demod: str  # "AM" | "NFM" | "WFM" | "SSB" | "CW"
    channel_rate: int  # Hz, post-FirDecimate rate the demod runs at
    low_cut: float  # Hz, relative to channel center (post-shift)
    high_cut: float
    transition: float = 0.05  # fraction of nyquist for filter rolloff
    deemphasis: bool = False
    bandpass_use_fft: bool = False  # FIR bandpass at channel rate is cheap


_MODE_PROFILES: dict[str, _ModeProfile] = {
    "USB": _ModeProfile(demod="SSB", channel_rate=12_000, low_cut=150.0, high_cut=2850.0),
    "LSB": _ModeProfile(demod="SSB", channel_rate=12_000, low_cut=-2850.0, high_cut=-150.0),
    "CW": _ModeProfile(demod="SSB", channel_rate=6_000, low_cut=600.0, high_cut=900.0),
    "AM": _ModeProfile(demod="AM", channel_rate=48_000, low_cut=-5000.0, high_cut=5000.0),
    "NFM": _ModeProfile(demod="NFM", channel_rate=48_000, low_cut=-6000.0, high_cut=6000.0),
    "WFM": _ModeProfile(
        demod="WFM",
        channel_rate=240_000,
        low_cut=-100_000.0,
        high_cut=100_000.0,
        deemphasis=True,
    ),
}


class AudioChain:
    """Push-in / drain-out pycsdr audio demodulation chain.

    Parameters
    ----------
    mode
        Demodulator mode: AM | NFM | WFM | USB | LSB | CW.
    input_rate
        Source sample rate (Hz). Must match the rate of the IQ bytes
        pushed into :meth:`feed`.
    output_rate
        Audio output sample rate (Hz). Default 8000 — matches the legacy
        ws wire format. 12000 / 16000 / 24000 / 48000 are also supported
        if the frontend is upgraded.
    channel_offset_hz
        Offset of the channel center from the SDR's center frequency, in
        Hz. The Shift block moves the channel to DC so the demod can run
        at baseband. Default 0 (channel already at center).
    """

    def __init__(
        self,
        *,
        mode: DemodMode = "USB",
        input_rate: int = 2_400_000,
        output_rate: int = 8000,
        channel_offset_hz: float = 0.0,
        conditioning: bool = True,
        dsp_params: DSPParams | None = None,
    ) -> None:
        """Build the per-mode demodulation chain.

        Parameters
        ----------
        mode
            Demodulator mode: AM | NFM | WFM | USB | LSB | CW.
        input_rate
            Source sample rate (Hz). Must match the rate of the IQ bytes
            pushed into :meth:`feed`.
        output_rate
            Audio output sample rate (Hz). Default 8000 — matches the legacy
            ws wire format. 12000 / 16000 / 24000 / 48000 are also supported
            if the frontend is upgraded.
        channel_offset_hz
            Offset of the channel center from the SDR's center frequency, in
            Hz. The Shift block moves the channel to DC so the demod can run
            at baseband. Default 0 (channel already at center).
        conditioning
            ADR-002 DSP mode mapping: True ("classic") inserts the
            conditioning stages (DcBlock / WfmDeemphasis / Limit); False
            ("raw") wires the demodulator output straight through. See the
            module docstring.
        dsp_params
            Slice-5.2 fine-grained controls. When provided, the chain
            conditionally inserts Agc / Squelch / Gain / NfmDeemphasis
            blocks based on the corresponding fields. The struct is
            immutable; modify by rebuilding the chain with a new struct.
        """
        if mode not in _MODE_PROFILES:
            raise ValueError(f"unsupported mode {mode!r}")
        if input_rate <= 0 or output_rate <= 0:
            raise ValueError("input_rate and output_rate must be > 0")

        self.mode = mode
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.channel_offset_hz = channel_offset_hz
        self.conditioning = conditioning
        self.dsp_params = dsp_params or DSPParams()
        profile = _MODE_PROFILES[mode]

        # Decimation: input_rate → channel_rate. This is the single most
        # important architectural choice in the chain (mirrors upstream
        # OpenWebRX's csdr recipe): decimate wideband IQ down to the
        # channel rate BEFORE demodulating, so the bandpass runs on
        # narrowband data and the audio resampler sees a small ratio.
        decimation = max(1, round(input_rate / profile.channel_rate))
        self.decimation = decimation
        # Actual post-decimation rate (input_rate may not divide evenly).
        self.channel_rate = input_rate / decimation

        # Shift rate: pycsdr Shift(rate) moves the spectrum UP by rate×fs
        # (mixer exp(+2πi·rate·n) — ADR-004 gotcha #7), so a channel at
        # +offset reaches DC with rate = −offset/input_rate.
        shift_rate = -channel_offset_hz / input_rate

        # Bandpass cuts, normalized against the CHANNEL rate (post-decimation).
        # Slice-5.2: DSPParams overrides the mode profile's defaults.
        manual_low = self.dsp_params.low_cut_hz
        manual_high = self.dsp_params.high_cut_hz
        low_cut = manual_low if manual_low is not None else profile.low_cut
        high_cut = manual_high if manual_high is not None else profile.high_cut
        low = low_cut / self.channel_rate
        high = high_cut / self.channel_rate

        # Buffers. Sizes are in SAMPLES (not bytes).
        #
        # IMPORTANT: pycsdr Ringbuffer.writeable() returns a CONSTANT
        # (size - 1) — the ring does NOT track reader positions, and a
        # writer overwrites data a slow consumer hasn't read. A write only
        # fails when a single chunk is >= ring size. All buffers must
        # therefore be large enough to absorb the largest burst the
        # session pushes (typically one source chunk) with headroom, so
        # that the ~11 Msps processing stages keep up without drops.
        in_buf_samples = max(65_536, input_rate // 32)
        self._in_buf = Buffer(Format.COMPLEX_FLOAT, in_buf_samples)
        # Mid buffers carry complex (post-shift, post-decimation) until demod.
        # Same generous sizing as the input ring — producers burst at the
        # chunk rate and consumers drain in parallel threads.
        mid_samples = in_buf_samples
        self._shifted_buf = Buffer(Format.COMPLEX_FLOAT, mid_samples)
        self._decimated_buf = Buffer(Format.COMPLEX_FLOAT, mid_samples)
        self._filtered_buf = Buffer(Format.COMPLEX_FLOAT, mid_samples)
        self._demod_buf = Buffer(Format.FLOAT, mid_samples)
        self._deemph_buf = Buffer(Format.FLOAT, mid_samples)
        # Audio-rate buffers: several seconds' worth each (post-resampler
        # data is tiny compared to IQ).
        self._resampled_buf = Buffer(Format.FLOAT, output_rate * 4)
        self._out_buf = Buffer(Format.SHORT, output_rate * 4)
        # Largest single write we will attempt, in bytes (<= quarter ring).
        self._max_write_bytes = (in_buf_samples // 4) * 8

        # Build the chain stages.
        #
        #   in_buf → Shift → shifted_buf → FirDecimate → decimated_buf
        #         → Bandpass → filtered_buf → demod → demod_buf
        #         → [DcBlock | WfmDeemphasis] → deemph_buf
        #         → AudioResampler(channel_rate → output_rate) → resampled_buf
        #         → Limit → limited_buf → Convert(F→S) → out_buf
        self._shift = Shift(rate=shift_rate)
        self._shift.setReader(self._in_buf.getReader())
        self._shift.setWriter(self._shifted_buf)

        # FirDecimate: anti-alias low-pass + decimation in one block.
        #
        # pycsdr semantics (from libcsdr source):
        #   * `cutoff` is normalized to the OUTPUT (channel) Nyquist —
        #     FirDecimate internally divides it by `decimation` to get the
        #     input-normalized cutoff. 0.5 = channel_rate/2.
        #   * `transition` is the normalized transition width at the INPUT
        #     rate; the FIR tap count is 4/transition. We derive it from the
        #     channel bandwidth (25% of half-bandwidth) but clamp the tap
        #     count to [81, 2051] to bound CPU per output sample.
        channel_half_bw = max(abs(profile.low_cut), abs(profile.high_cut))
        fir_cutoff = min(0.47, (channel_half_bw * 1.15) / (self.channel_rate / 2))
        transition_hz = channel_half_bw * 0.25
        fir_transition = min(
            0.05,
            max(4.0 / 2051.0, transition_hz / (input_rate / 2)),
        )
        self._fir_decimate = FirDecimate(
            decimation=decimation,
            transition=fir_transition,
            cutoff=fir_cutoff,
        )
        self._fir_decimate.setReader(self._shifted_buf.getReader())
        self._fir_decimate.setWriter(self._decimated_buf)

        self._bandpass = Bandpass(
            low_cut=low,
            high_cut=high,
            transition=profile.transition,
            use_fft=profile.bandpass_use_fft,
        )
        self._bandpass.setReader(self._decimated_buf.getReader())
        self._bandpass.setWriter(self._filtered_buf)

        # Stage 0.5: optional Squelch (slice-5.2). Inserted BETWEEN the
        # bandpass and the demodulator, on the COMPLEX IQ stream — pycsdr's
        # Squelch measures IQ power and zeros out the complex output when
        # below threshold. (FLOAT output would be rejected; the block only
        # accepts COMPLEX_FLOAT.) The threshold is an integer dBFS value;
        # we cast from the float the user provides.
        post_filtered_buf = self._filtered_buf
        if self.dsp_params.squelch_db is not None:
            self._squelch_buf = Buffer(Format.COMPLEX_FLOAT, mid_samples)
            self._squelch = Squelch(
                int(self.dsp_params.squelch_db),
                int(self.channel_rate),
            )
            self._squelch.setReader(post_filtered_buf.getReader())
            self._squelch.setWriter(self._squelch_buf)
            post_filtered_buf = self._squelch_buf

        # Demod stage — always consumes complex from post_filtered_buf.
        # ``demod_out_buf`` is where the demod result lands for the
        # resampler: with conditioning=True an intermediate stage
        # (DcBlock / WfmDeemphasis / NfmDeemphasis) is inserted in between
        # (ADR-002). Slice-5.2: DSPParams.dc_block_enabled / deemphasis_enabled
        # can override the conditioning defaults; Agc + Gain are inserted
        # conditionally based on the same struct.
        # Buffers for optional stages (allocated lazily so unused stages
        # don't waste ring memory).
        self._agc_buf: Buffer | None = None
        self._gain_buf: Buffer | None = None
        self._nfm_deemph_buf: Buffer | None = None
        self._agc: Agc | None = None
        self._gain: Gain | None = None
        self._nfm_deemph: NfmDeemphasis | None = None

        # Stage 1: demodulator → demod_buf
        if profile.demod == "AM":
            self._demod = AmDemod()
            self._demod.setReader(post_filtered_buf.getReader())
            self._demod.setWriter(self._demod_buf)
            post_demod_buf = self._demod_buf
        elif profile.demod == "NFM" or profile.demod == "WFM":
            self._demod = FmDemod()
            self._demod.setReader(post_filtered_buf.getReader())
            self._demod.setWriter(self._demod_buf)
            post_demod_buf = self._demod_buf
        elif profile.demod == "SSB":
            # SSB/CW: collapse to real part, no DC block needed.
            self._real_part = RealPart()
            self._real_part.setReader(post_filtered_buf.getReader())
            self._real_part.setWriter(self._demod_buf)
            post_demod_buf = self._demod_buf
        else:  # pragma: no cover — defensive
            raise AssertionError(f"unhandled demod {profile.demod!r}")

        # Stage 3: conditioning (classic only). dc_block_enabled and
        # deemphasis_enabled override the profile defaults when set.
        do_dc_block = (
            conditioning
            and profile.demod in ("AM", "NFM")
            and (self.dsp_params.dc_block_enabled is not False)
        )
        do_wfm_deemph = (
            conditioning
            and profile.demod == "WFM"
            and (self.dsp_params.deemphasis_enabled is not False)
        )
        do_nfm_deemph = (
            conditioning
            and profile.demod == "NFM"
            and (self.dsp_params.deemphasis_enabled is True)
        )
        if do_dc_block:
            self._dc_block = DcBlock()
            self._dc_block.setReader(post_demod_buf.getReader())
            self._dc_block.setWriter(self._deemph_buf)
            post_demod_buf = self._deemph_buf
        if do_wfm_deemph:
            self._deemph = WfmDeemphasis(int(self.channel_rate), 50e-6)
            self._deemph.setReader(post_demod_buf.getReader())
            self._deemph.setWriter(self._deemph_buf)
            post_demod_buf = self._deemph_buf
        if do_nfm_deemph:
            # NFM has its own de-emphasis block (slice-5.2) — rarely needed
            # for voice channels but useful for data modes.
            self._nfm_deemph_buf = Buffer(Format.FLOAT, mid_samples)
            self._nfm_deemph = NfmDeemphasis(int(self.channel_rate))
            self._nfm_deemph.setReader(post_demod_buf.getReader())
            self._nfm_deemph.setWriter(self._nfm_deemph_buf)
            post_demod_buf = self._nfm_deemph_buf

        demod_out_buf = post_demod_buf

        # Stage 4: optional Agc or Gain (slice-5.2). AGC replaces the soft
        # Limit when active (Agc handles its own limiting). Manual Gain is
        # applied before the resampler (linear gain, not dB).
        # pycsdr signatures: Agc(Format), Gain(Format, gain_float).
        if self.dsp_params.agc_enabled is True:
            self._agc_buf = Buffer(Format.FLOAT, mid_samples)
            self._agc = Agc(Format.FLOAT)
            self._agc.setReader(demod_out_buf.getReader())
            self._agc.setWriter(self._agc_buf)
            demod_out_buf = self._agc_buf
        elif self.dsp_params.manual_gain_db is not None:
            # Convert dB → linear and apply a fixed gain.
            linear_gain = 10 ** (self.dsp_params.manual_gain_db / 20.0)
            self._gain_buf = Buffer(Format.FLOAT, mid_samples)
            self._gain = Gain(Format.FLOAT, linear_gain)
            self._gain.setReader(demod_out_buf.getReader())
            self._gain.setWriter(self._gain_buf)
            demod_out_buf = self._gain_buf

        # Resample channel_rate → output_rate (small ratio by design).
        self._resampler = AudioResampler(
            inputRate=int(self.channel_rate),
            outputRate=output_rate,
        )
        self._resampler.setReader(demod_out_buf.getReader())
        self._resampler.setWriter(self._resampled_buf)

        # Final audio path: Limit + Convert FLOAT → SHORT. The limiter is a
        # conditioning stage — raw mode converts straight from the
        # resampler (overdriven audio then clips at the int16 convert,
        # which is exactly the "raw" contract). Slice-5.2: when AGC is
        # active, the Agc block already handles limiting, so the soft
        # Limit is skipped (would be a no-op anyway).
        skip_limit = self.dsp_params.agc_enabled is True
        if conditioning and not skip_limit:
            self._limited_buf = Buffer(Format.FLOAT, output_rate * 4)
            self._limit = Limit(maxAmplitude=1.0)
            self._limit.setReader(self._resampled_buf.getReader())
            self._limit.setWriter(self._limited_buf)
            self._convert = Convert(Format.FLOAT, Format.SHORT)
            self._convert.setReader(self._limited_buf.getReader())
            self._convert.setWriter(self._out_buf)
        else:
            self._limited_buf = Buffer(Format.FLOAT, output_rate * 4)
            self._convert = Convert(Format.FLOAT, Format.SHORT)
            self._convert.setReader(self._resampled_buf.getReader())
            self._convert.setWriter(self._out_buf)

        self._out_reader: BufferReader = self._out_buf.getReader()

        # Output queue for the reader thread.
        self._frames: deque[memoryview] = deque()
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"pycsdr-audio-{mode}",
            daemon=True,
        )
        self._reader_thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, iq_bytes: bytes) -> None:
        """Push complex64 IQ bytes into the chain.

        Large chunks are split into ring-sized slices; if the ring is full
        (consumer running behind), this method blocks briefly and retries.
        """
        chunk_len = (len(iq_bytes) // 8) * 8
        if chunk_len == 0:
            return
        data = iq_bytes[:chunk_len] if chunk_len != len(iq_bytes) else iq_bytes
        offset = 0
        while offset < len(data):
            end = min(offset + self._max_write_bytes, len(data))
            self._write_with_backpressure(data[offset:end])
            offset = end

    def _write_with_backpressure(self, chunk: bytes) -> None:
        """Write one ring-sized slice, retrying while the ring is full."""
        deadline = time.monotonic() + 10.0
        while True:
            try:
                self._in_buf.write(chunk)
                return
            except BufferError:
                if time.monotonic() >= deadline:
                    raise
                # Ring full — AsyncRunner needs a tick to drain it.
                time.sleep(0.005)

    def drain(self) -> list[AudioFrame]:
        """Return all ready audio frames (non-blocking)."""
        out: list[AudioFrame] = []
        with self._lock:
            while self._frames:
                mv = self._frames.popleft()
                # int16 = 2 bytes per sample
                frame_count = len(mv) // 2
                out.append(
                    AudioFrame(
                        pcm=mv,
                        sample_rate=self.output_rate,
                        frame_count=frame_count,
                    )
                )
        return out

    def stop(self) -> None:
        """Stop the reader thread and release pycsdr resources."""
        self._stop_evt.set()
        # Stop the pycsdr modules (their AsyncRunner threads).
        for stage in (
            "_shift", "_fir_decimate", "_bandpass", "_demod", "_dc_block", "_deemph",
            "_nfm_deemph", "_squelch", "_agc", "_gain",
            "_real_part", "_resampler", "_limit", "_convert",
        ):
            mod = getattr(self, stage, None)
            if mod is None:
                continue
            try:
                mod.stop()
            except Exception:  # noqa: BLE001
                log.debug("pycsdr audio stage stop raised", stage=stage, exc_info=True)
        with contextlib.suppress(Exception):
            self._out_reader.stop()
        if self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)

    # ------------------------------------------------------------------
    # Internal reader loop
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                chunk = self._out_reader.read()
            except Exception:  # noqa: BLE001
                return
            if not chunk:
                continue
            try:
                mv = memoryview(chunk)
            except TypeError:
                mv = memoryview(bytes(chunk))
            with self._lock:
                self._frames.append(mv)
