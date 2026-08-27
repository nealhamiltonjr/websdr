"""Server-side ReceiverSession — owns one source, one DSP chain, one freq/mode.

This is the counterpart of the frontend ReceiverSession. The frontend session
is a thin proxy; this server-side session holds the real state:
  - The Source (subscribed and streaming IQ)
  - The DSP chains (pycsdr FftChain + AudioChain per ADR-004; optional
    DeepFilterNet on the audio path per ADR-002)
  - A list of WebSocket subscribers (clients receiving FFT + audio + metadata)
  - A configurable FFT frame rate and bin count
  - An IqHub (ADR-005): the session consumes its own source THROUGH the
    hub, so VFO sub-receiver taps can share the same stream. The hub is
    created lazily on start() and destroyed on stop().

Slice-3 status: pycsdr is live. The numpy FFT / magnitude-demod stubs are
replaced by pycsdr's SIMD C++ blocks (Fft → LogAveragePower → FftSwap for
the waterfall; Shift → FirDecimate → Bandpass → demod → AudioResampler →
Convert for the audio path). Binary wire formats are unchanged — see
packages/shared-types/src/{fft,audio}.ts.

Timing model: real-time sources (hardware, paced file/simulated replay)
flow at wall-clock rate; ``fft_fps`` throttles the FFT *broadcast* (surplus
frames are dropped — a 10 fps waterfall doesn't need 2300 fps of FFT),
while audio frames are never dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import structlog

from ..config import Settings
from ..dsp import AIDenoiser, AIDenoiserConfig, AudioChain, DSPParams, FftChain, IQPreprocessor
from ..plugins.base import (
    DecoderAlreadyAttached,
    DecoderAttachContext,
    DecoderAttachError,
    DecoderPlugin,
)
from ..plugins.registry import decoder_registry
from ..sources.base import (
    RemoteAudioFrame,
    RemoteFftFrame,
    Source,
    SourceRegistry,
)
from ..sources.file_source import FileSource
from ..sources.wideband import IqHub, destroy_hub, get_or_create_hub

log = structlog.get_logger(__name__)

# Wire-format constants — MUST match packages/shared-types/src/fft.ts
FFT_HEADER_MAGIC = 0x4F465257  # "WRFO"
FFT_HEADER_VERSION = 1
FFT_HEADER_SIZE_BYTES = 32

# struct.pack format: < little-endian, I=u32, f=f32
# 8 fields: magic(u32) version(u32) rxIdHash(u32) centerFreq(f32) sampleRate(f32)
#           minDb(f32) maxDb(f32) binCount(u32)  → 8 × 4 = 32 bytes
_HEADER_PACK_FMT = "<IIIffffI"

# Wire-format constants — MUST match packages/shared-types/src/audio.ts
AUDIO_HEADER_MAGIC = 0x41554449  # "AUDI"
AUDIO_HEADER_VERSION = 1
AUDIO_HEADER_SIZE_BYTES = 16
AUDIO_PACK_FMT = "<IIII"  # magic version sampleRate frameCount
AUDIO_SAMPLE_RATE = 8000  # Hz, mono Int16 PCM

# ADR-002 DSP+AI cascade — the four-mode control surface. raw + classic are
# LIVE (AudioChain conditioning topology, slice-4.7); ai + cascade now run
# the in-process AIDenoiser (Stage 2a, slice-10 — a real spectral-
# subtraction noise reducer that ships before DeepFilterNet). The gate
# `AI_DSP_MODES_AVAILABLE` was flipped to True in slice-10.
DSP_MODES = ("raw", "classic", "ai", "cascade")
AI_DSP_MODES_AVAILABLE = True


def _gain_range_of(source_type: str) -> tuple[float, float] | None:
    """Manifest gain range for a source type (None = no advertised range)."""
    from ..sources import SourceRegistry

    manifest = SourceRegistry.get_manifest(source_type)
    return manifest.gain_range if manifest is not None else None


@dataclass
class _DecoderAttachment:
    """One running decoder: the plugin instance + its feed task."""

    plugin: DecoderPlugin
    task: asyncio.Task[None]


@dataclass
class ReceiverSession:
    """Server-side counterpart of the frontend ReceiverSession."""

    receiver_id: str
    source: Source
    center_freq: int = 14_205_000
    sample_rate: int = 2_400_000
    mode: str = "USB"
    # Manual gain in dB, or None = auto/AGC (slice-4.7). Applied to the
    # source at spawn time AND live via Source.set_runtime_gain.
    gain: float | None = None
    # ADR-002 DSP mode: "raw" (unconditioned demod output) or "classic"
    # (DC block / deemphasis / limiter). "ai"/"cascade" pending the
    # DeepFilterNet module — see DSP_MODES above.
    dsp_mode: str = "classic"
    # Slice-5.2 fine-grained DSP controls. When any field is non-None,
    # the AudioChain rebuilds with the optional blocks (Agc/Squelch/Gain/
    # NfmDeemphasis/manual bandpass). Changes via set_dsp_params() merge
    # into this struct and trigger a chain rebuild.
    dsp_params: DSPParams = field(default_factory=DSPParams)
    fft_size: int = 1024
    fft_fps: int = 10
    min_db: float = -100.0
    max_db: float = -20.0
    # WebSocket subscriber queues — each client gets its own queue. Binary
    # frames (FFT/audio) arrive as bytes; decoder events as JSON strings.
    _subscribers: list[asyncio.Queue[bytes | str]] = field(default_factory=list, init=False)
    _stream_task: asyncio.Task[None] | None = field(default=None, init=False)
    _receiver_id_hash: int = field(default=0, init=False)
    _fft_chain: FftChain | None = field(default=None, init=False)
    _audio_chain: AudioChain | None = field(default=None, init=False)
    # Slice-7: IQ preprocessor (notch + noise blanker). Built alongside
    # the AudioChain; reconfigured on set_dsp_params. Operates on the
    # raw complex64 chunks from the hub BEFORE they're fed to the
    # pycsdr chains (so both FFT + audio see the cleaned IQ).
    _iq_preprocessor: IQPreprocessor | None = field(default=None, init=False)
    # Slice-10: AI denoiser (Stage 2a of ADR-002 cascade). Instantiated
    # lazily when dsp_mode is first set to 'ai' or 'cascade'; reset on
    # mode/source change so the noise-floor estimate starts fresh.
    _ai_denoiser: AIDenoiser | None = field(default=None, init=False)
    _hub: IqHub | None = field(default=None, init=False)
    _decoders: dict[str, _DecoderAttachment] = field(default_factory=dict, init=False)
    # Serializes chain swaps (set_mode / set_dsp_mode / stop) against the
    # feed+drain cycle in _run — a rebuild must never interleave with a
    # half-fed old chain (slice-4.7).
    _chain_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    # Lazily-resolved (gain_range, supports_agc) for this session's source
    # type — computed once, read by the 10 fps metadata pump.
    _gain_caps: tuple[tuple[float, float] | None, bool] | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        # Stable hash of the receiver_id (32-bit, for wire format).
        h = hashlib.md5(self.receiver_id.encode("utf-8")).digest()
        self._receiver_id_hash = int.from_bytes(h[:4], "little")

    async def start(self) -> None:
        if self._stream_task is not None:
            return
        # Display-stream source (ADR-006 federation client): the remote
        # receiver already computed the waterfall + demodulated the audio —
        # there is no IQ, so no pycsdr chains and no IqHub. Frames are
        # repacked into the standard wire formats in _run_display().
        if hasattr(self.source, "display_stream"):
            self._stream_task = asyncio.create_task(self._run_display())
            return
        # File-style sources ignore spawn() rate/freq — adopt the recording's
        # actual values so the FFT header and DSP chains match the data.
        fixed_rate = getattr(self.source, "fixed_sample_rate", None)
        if fixed_rate and int(fixed_rate) != self.sample_rate:
            fixed_center = int(getattr(self.source, "fixed_center_freq", self.center_freq))
            log.info(
                "session adopting source's fixed rate",
                receiver_id=self.receiver_id,
                old_rate=self.sample_rate,
                new_rate=int(fixed_rate),
                old_center=self.center_freq,
                new_center=fixed_center,
            )
            self.sample_rate = int(fixed_rate)
            self.center_freq = fixed_center

        # Build the pycsdr DSP chains (replacing the slice-1 numpy stubs).
        self._fft_chain = FftChain(
            fft_size=self.fft_size,
            avg_number=1,
            add_db=-10.0,
            center_freq=self.center_freq,
            sample_rate=self.sample_rate,
            min_db=self.min_db,
            max_db=self.max_db,
        )
        self._audio_chain = self._build_audio_chain()
        # Slice-7: build the IQ preprocessor (notch + NB) from the
        # current dsp_params. Reused on set_dsp_params via reconfigure().
        self._iq_preprocessor = IQPreprocessor(
            sample_rate=self.sample_rate,
            params=self.dsp_params,
        )
        # ADR-005: consume through a hub so VFO taps can share this stream.
        # The hub passes self.gain into source.spawn() (pre-start gain),
        # while later changes go through Source.set_runtime_gain.
        self._hub = get_or_create_hub(self)
        await self._hub.start()
        self._stream_task = asyncio.create_task(self._run())

    def _build_audio_chain(self) -> AudioChain:
        """Construct an AudioChain for the CURRENT mode + dsp_mode + dsp_params.

        Conditioning (DcBlock / WfmDeemphasis / Limit) is ON for:
          - classic: the standard analog-audio path (ADR-002 mode matrix).
          - cascade: classic + AI — the WDSP stages run, then AI denoises.
        It's OFF for raw (no processing) and ai (denoiser only, no conditioning).
        """
        return AudioChain(
            mode=self.mode,  # type: ignore[arg-type]
            input_rate=self.sample_rate,
            output_rate=AUDIO_SAMPLE_RATE,
            channel_offset_hz=0.0,
            conditioning=(self.dsp_mode in ("classic", "cascade")),
            dsp_params=self.dsp_params,
        )

    async def stop(self) -> None:
        # Decoder feed tasks consume the hub — stop them before the hub
        # itself goes away (detach cancels the task; the hub stream's
        # finally-block unsubscribes its queue).
        for name in list(self._decoders):
            await self.detach_decoder(name)
        if self._stream_task:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stream_task
            self._stream_task = None
        if self._fft_chain is not None:
            self._fft_chain.stop()
            self._fft_chain = None
        if self._audio_chain is not None:
            self._audio_chain.stop()
            self._audio_chain = None
        # Slice-7: the IQ preprocessor holds no external resources (it's
        # pure numpy state) — drop the reference so GC can reclaim it.
        self._iq_preprocessor = None
        # Slice-10: the AI denoiser is pure-numpy too — drop it so a
        # re-adopted receiver starts with a fresh noise-floor estimate.
        self._ai_denoiser = None
        # Destroying the hub stops the source once and sentinel-ends every
        # VFO tap sharing it (the hub owns the source lifecycle now).
        # Display-stream sessions never create a hub; destroy_hub is a no-op
        # for them.
        await destroy_hub(self.receiver_id)
        self._hub = None
        # Display-stream sources own a websocket — release the user slot on
        # the far end promptly (ADR-006 etiquette). Only they get an explicit
        # close: IQ sources are owned (and closed) by their IqHub.
        if hasattr(self.source, "display_stream"):
            close = getattr(self.source, "close", None)
            if close is not None:
                await close()

    def subscribe(self) -> asyncio.Queue[bytes | str]:
        q: asyncio.Queue[bytes | str] = asyncio.Queue(maxsize=64)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[bytes | str]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    # ------------------------------------------------------------------
    # Decoder plugins (ADR-003) — tap the session's IQ via its IqHub
    # ------------------------------------------------------------------

    async def attach_decoder(self, name: str) -> dict[str, Any]:
        """Start a decoder plugin on this receiver's IQ stream.

        Raises:
            KeyError: unknown decoder name.
            DecoderAlreadyAttached: this decoder is already running.
            DecoderAttachError: the receiver can't host it (display-stream
                source, or an incompatible sample rate).
        """
        plugin_cls = decoder_registry.get(name)
        if plugin_cls is None:
            raise KeyError(f"unknown decoder: {name!r}")
        if name in self._decoders:
            raise DecoderAlreadyAttached(f"decoder already attached: {name}")
        if hasattr(self.source, "display_stream"):
            raise DecoderAttachError(
                "display-stream receivers carry no raw IQ — attach the "
                "decoder to a local (IQ) receiver instead"
            )
        manifest = plugin_cls.manifest
        if (
            manifest.tap_point == "rf_band"
            and manifest.required_sample_rate is not None
            and self.sample_rate != manifest.required_sample_rate
        ):
            raise DecoderAttachError(
                f"decoder {name!r} requires {manifest.required_sample_rate} S/s; "
                f"this receiver runs {self.sample_rate} S/s"
            )
        # The decoder consumes the hub — make sure it exists (idempotent).
        await self.start()
        hub = self._hub
        assert hub is not None  # IQ sessions always build one in start()
        plugin = plugin_cls()
        # Subprocess plugins spawn their child here so failures surface as
        # attach-time errors (REST 400/502) instead of silent IQ drops.
        await plugin.on_attach(
            DecoderAttachContext(
                receiver_id=self.receiver_id,
                sample_rate=self.sample_rate,
                center_freq=self.center_freq,
            )
        )
        task = asyncio.create_task(
            self._run_decoder(name, plugin),
            name=f"decoder-{self.receiver_id}-{name}",
        )
        self._decoders[name] = _DecoderAttachment(plugin=plugin, task=task)
        log.info(
            "decoder attached",
            receiver_id=self.receiver_id,
            decoder=name,
            tap_point=manifest.tap_point,
        )
        return {"name": name, **plugin.status()}

    async def detach_decoder(self, name: str) -> bool:
        """Stop a running decoder. Returns False if it wasn't attached."""
        attachment = self._decoders.pop(name, None)
        if attachment is None:
            return False
        attachment.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await attachment.task
        # astop() awaits subprocess teardown (bounded, then SIGKILL);
        # in-process plugins default to the sync stop() hook.
        await attachment.plugin.astop()
        log.info("decoder detached", receiver_id=self.receiver_id, decoder=name)
        return True

    def decoder_status(self) -> list[dict[str, Any]]:
        """Live state of every attached decoder (REST polling surface)."""
        return [
            {"name": name, **attachment.plugin.status()}
            for name, attachment in self._decoders.items()
        ]

    async def _run_decoder(self, name: str, plugin: DecoderPlugin) -> None:
        """Feed the plugin from the IQ hub; broadcast its events as JSON.

        Ends gracefully on the hub sentinel (session stop) — same
        lifecycle as the main stream loop.
        """
        hub = self._hub
        assert hub is not None
        try:
            async for chunk in hub.stream():
                for event in plugin.feed_iq(chunk):
                    payload = json.dumps(
                        {
                            "type": "decoder",
                            "decoder": name,
                            "receiverId": self.receiver_id,
                            "event": event,
                        }
                    )
                    await self._broadcast(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception(
                "decoder stream error",
                receiver_id=self.receiver_id,
                decoder=name,
                error=str(exc),
            )

    async def _run(self) -> None:
        """Main loop: read IQ from the hub, feed pycsdr chains, broadcast.

        The source streams at its natural (real-time) pace. ``fft_fps``
        throttles only the FFT *broadcast* — surplus FFT frames are drained
        and dropped, audio frames are always delivered. This keeps the pycsdr
        rings and hub queues healthy even when a client's waterfall renders
        at a lower rate than the FFT chain produces frames.
        """
        frame_interval = 1.0 / max(1, self.fft_fps)
        last_bcast = 0.0
        hub = self._hub
        assert hub is not None  # set in start()
        try:
            async for chunk in hub.stream():
                # Slice-7: apply IQ preprocessor (notch + NB) before
                # feeding the pycsdr chains. The preprocessor is a pure-
                # numpy filter that runs in the asyncio task; it returns
                # the same complex64 view (no copy) when no stage is
                # active. When the notch or NB IS active, it produces a
                # new array — and we tobytes() that array for the chains.
                iq_arr = np.ascontiguousarray(chunk, dtype=np.complex64)
                pre = self._iq_preprocessor
                if pre is not None and pre.active:
                    iq_arr = pre.process(iq_arr)
                iq_bytes = iq_arr.tobytes()
                now = time.monotonic()
                # Feed + drain under the chain lock so a concurrent
                # set_mode/set_dsp_mode rebuild can't interleave with a
                # half-fed old chain (the swap waits for this cycle).
                async with self._chain_lock:
                    if self._fft_chain is not None:
                        self._fft_chain.feed(iq_bytes)
                        for frame in self._fft_chain.drain():
                            if now - last_bcast >= frame_interval:
                                await self._broadcast(self._pack_fft_frame(frame.bins))
                                last_bcast = now
                            # else: surplus frame intentionally dropped (fps cap)
                    if self._audio_chain is not None:
                        self._audio_chain.feed(iq_bytes)
                        for audio_frame in self._audio_chain.drain():
                            # Type widening so we can reassign to a numpy
                            # array when the AI denoiser runs (slice-10).
                            pcm: memoryview | np.ndarray = audio_frame.pcm
                            # ADR-002 Stage 2a — apply the in-process AI
                            # denoiser when dsp_mode is 'ai' or 'cascade'.
                            # 'cascade' has already passed through the
                            # classic conditioning path (DcBlock / Limit
                            # / WfmDeemphasis) before this point.
                            if self._ai_denoiser is not None and self.dsp_mode in ("ai", "cascade"):
                                # Convert memoryview → int16 ndarray for the denoiser.
                                if isinstance(pcm, memoryview):
                                    arr = np.frombuffer(pcm, dtype="<i2").copy()
                                else:
                                    arr = np.asarray(pcm, dtype="<i2").copy()
                                pcm = self._ai_denoiser.feed(arr)
                            await self._broadcast(self._pack_audio_frame(pcm))
        except Exception as exc:
            log.exception("receiver stream error", receiver_id=self.receiver_id, error=str(exc))

    # ------------------------------------------------------------------
    # Display-stream path (remote OpenWebRX receivers, ADR-006)
    # ------------------------------------------------------------------

    @property
    def display_frequency(self) -> int:
        """The frequency the operator thinks of as "the" frequency.

        For local IQ sessions this is the center frequency; for remote
        display sessions it is the tuned VFO (remote center + offset) so the
        UI's frequency display follows dspcontrol tuning.
        """
        tuned = getattr(self.source, "tuned_freq", None)
        return int(tuned) if tuned is not None else self.center_freq

    async def set_frequency(self, freq: int) -> bool:
        """Handle a setFrequency control command. Returns True if applied.

        Display sessions forward the tune to the remote demodulator; local
        sessions keep the legacy behavior (move the session center).
        """
        if freq <= 0:
            return False
        tune = getattr(self.source, "tune", None)
        if callable(tune):
            await tune(freq)
            return True
        if freq != self.center_freq:
            log.info(
                "setFrequency",
                receiver_id=self.receiver_id,
                old=self.center_freq,
                new=freq,
            )
            self.center_freq = freq
            return True
        return False

    async def set_mode(self, mode: str) -> bool:
        """Handle a setMode control command. Returns True if applied.

        Display sessions forward the mode switch to the remote; local
        sessions update the field AND rebuild the AudioChain so the new
        demodulator actually takes effect (slice-4.7 fix — before this,
        only the metadata echo changed while the original demodulator
        kept running).
        """
        if not mode:
            return False
        set_remote_mode = getattr(self.source, "set_mode", None)
        if callable(set_remote_mode):
            await set_remote_mode(mode)
            self.mode = mode
            return True
        if mode != self.mode:
            log.info(
                "setMode",
                receiver_id=self.receiver_id,
                old=self.mode,
                new=mode,
            )
            self.mode = mode
            # Rebuild the demodulator for the new mode (display sessions
            # return above; only IQ sessions have a local chain).
            await self._rebuild_audio_chain()
            return True
        return False

    # ------------------------------------------------------------------
    # Gain + DSP mode controls (slice-4.7 — de-stubbed setGain/setDSPMode)
    # ------------------------------------------------------------------

    async def set_gain(self, value: float | None) -> tuple[bool, str]:
        """Handle a setGain control command.

        ``value`` is dB (manual gain) or None (auto / AGC where the
        hardware has it; unit gain otherwise). Returns (applied, reason) —
        reason is human-readable and goes straight into a WS error frame
        when applied is False.
        """
        if hasattr(self.source, "display_stream"):
            return False, "gain is managed by the remote receiver"
        # Validate against the source's advertised range, when it has one.
        gain_range = _gain_range_of(self.source.info.type)
        if value is not None and gain_range is not None:
            lo, hi = gain_range
            if not (lo <= value <= hi):
                return False, (
                    f"gain {value:g} dB outside the {self.source.info.type} "
                    f"range [{lo:g}, {hi:g}] dB"
                )
        apply_gain = getattr(self.source, "set_runtime_gain", None)
        if self._stream_task is None:
            # Not started yet: remember it — start() passes it into
            # source.spawn() through the IqHub.
            self.gain = None if value is None else float(value)
            return True, ""
        if not callable(apply_gain):
            return False, (
                f"source {self.source.info.type!r} has no runtime gain control"
            )
        applied = apply_gain(None if value is None else float(value))
        if not applied:
            return False, (
                f"source {self.source.info.type!r} could not apply gain "
                "(not streaming on a gain-capable transport?)"
            )
        self.gain = None if value is None else float(value)
        log.info("setGain", receiver_id=self.receiver_id, gain=self.gain)
        return True, ""

    async def set_dsp_mode(self, mode: str) -> tuple[bool, str]:
        """Handle a setDSPMode control command (ADR-002 four-mode surface).

        raw  → demodulator output unconditioned (no DC block, no WFM
               de-emphasis, no limiter).
        classic → conditioned audio (the default chain).
        ai → demodulator output unconditioned + AIDenoiser (Stage 2a).
        cascade → classic conditioning + AIDenoiser (Stage 2a).
        """
        if mode not in DSP_MODES:
            return False, f"unknown DSP mode {mode!r}; valid: {list(DSP_MODES)}"
        if mode in ("ai", "cascade") and not AI_DSP_MODES_AVAILABLE:
            return False, (
                f"DSP mode {mode!r} requires the AI denoiser "
                "(ADR-002) which is not built yet — use 'raw' or 'classic'"
            )
        if hasattr(self.source, "display_stream"):
            return False, "the remote receiver runs its own DSP chain"
        if mode != self.dsp_mode:
            log.info(
                "setDSPMode",
                receiver_id=self.receiver_id,
                old=self.dsp_mode,
                new=mode,
            )
            self.dsp_mode = mode
            # Slice-10: spin up or reset the AI denoiser when entering/
            # leaving an AI mode. The denoiser is a streaming filter
            # with inter-frame state — a fresh start on mode switch
            # avoids the noise-floor estimate carrying stale samples.
            if mode in ("ai", "cascade"):
                if self._ai_denoiser is None:
                    self._ai_denoiser = AIDenoiser(
                        config=AIDenoiserConfig(sample_rate=AUDIO_SAMPLE_RATE)
                    )
                else:
                    self._ai_denoiser.reset()
            else:
                # Leaving an AI mode — flush any buffered samples via
                # drain() (preserves the last hop), then drop the denoiser
                # so the wire path doesn't pay the spectral-overhead tax.
                if self._ai_denoiser is not None:
                    self._ai_denoiser = None
            await self._rebuild_audio_chain()
            return True, ""
        return True, ""

    async def set_dsp_params(self, patch: DSPParams) -> tuple[bool, str]:
        """Merge fine-grained DSP params into the session and rebuild the
        AudioChain if any field actually changed (slice-5.2).

        The patch is a partial update — only non-None fields override the
        corresponding fields on the session's current dsp_params. Setting
        a field back to None (the "use mode default" state) requires the
        caller to send an explicit ``None`` value; pydantic-validated WS
        payloads treat missing fields as "unchanged".

        Returns (applied, reason) like set_gain — when applied is False,
        reason carries the human-readable rejection message.
        """
        if hasattr(self.source, "display_stream"):
            return False, "the remote receiver runs its own DSP chain"
        new_params = self.dsp_params.merge(patch)
        if new_params.to_dict() == self.dsp_params.to_dict():
            return True, ""  # no-op
        log.info(
            "setDSPParams",
            receiver_id=self.receiver_id,
            patch=patch.to_dict(),
        )
        self.dsp_params = new_params
        # Slice-7: reconfigure the IQ preprocessor (notch + NB). The
        # preprocessor is rebuilt from scratch — its IIR state is reset,
        # which is fine because the new params might notch a different
        # frequency entirely.
        if self._iq_preprocessor is not None:
            self._iq_preprocessor.reconfigure(new_params)
        await self._rebuild_audio_chain()
        return True, ""

    async def _rebuild_audio_chain(self) -> None:
        """Swap in a fresh AudioChain for the current mode + dsp_mode.

        The swap happens under the chain lock, so the _run loop never feeds
        a half-torn-down chain. The old chain is stopped (reader thread +
        pycsdr AsyncRunners) AFTER the new one is assigned. Not started
        sessions (no chain yet) are a no-op — start() builds the right
        chain from the current fields.
        """
        if self._stream_task is None or hasattr(self.source, "display_stream"):
            return
        async with self._chain_lock:
            old = self._audio_chain
            self._audio_chain = self._build_audio_chain()
        if old is not None:
            old.stop()

    def gain_capabilities(self) -> tuple[tuple[float, float] | None, bool]:
        """(gainRange, supportsAgc) for THIS session's source — UI hints.

        Cached after the first call (the metadata pump asks at stream fps).
        """
        if self._gain_caps is None:
            from ..sources import SourceRegistry

            manifest = SourceRegistry.get_manifest(self.source.info.type)
            self._gain_caps = (
                (manifest.gain_range, manifest.supports_agc)
                if manifest is not None
                else (None, False)
            )
        return self._gain_caps

    async def _run_display(self) -> None:
        """Remote-display loop: repack remote frames into wire formats.

        No throttling — the remote already paces the stream at its own fft
        fps; slow local subscribers are protected by the bounded broadcast
        queues (frames drop, latency never grows).
        """
        try:
            stream = self.source.display_stream()  # type: ignore[attr-defined]
            async for frame in stream:
                if isinstance(frame, RemoteFftFrame):
                    if frame.center_freq:
                        self.center_freq = int(frame.center_freq)
                    if frame.sample_rate:
                        self.sample_rate = int(frame.sample_rate)
                    if len(frame.bins) != self.fft_size:
                        self.fft_size = len(frame.bins)
                    if frame.min_db is not None:
                        self.min_db = float(frame.min_db)
                    if frame.max_db is not None:
                        self.max_db = float(frame.max_db)
                    await self._broadcast(self._pack_fft_frame(frame.bins))
                elif isinstance(frame, RemoteAudioFrame):
                    await self._broadcast(
                        self._pack_audio_frame(frame.pcm, int(frame.sample_rate))
                    )
        except Exception as exc:
            log.exception(
                "remote display stream error",
                receiver_id=self.receiver_id,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Wire-format packing (pycsdr output → binary ws frames)
    # ------------------------------------------------------------------

    def _pack_fft_frame(self, bins: memoryview | np.ndarray) -> bytes:
        """Pack one FFT frame into the binary wire format.

        ``bins`` is ``fft_size`` float32 dB power values (DC-centered by
        pycsdr's FftSwap), exactly matching the slice-1 numpy output
        format — clients see no difference. Remote display frames pass the
        same values as float32 ndarrays.
        """
        header = struct.pack(
            _HEADER_PACK_FMT,
            FFT_HEADER_MAGIC,
            FFT_HEADER_VERSION,
            self._receiver_id_hash,
            float(self.center_freq),
            float(self.sample_rate),
            float(self.min_db),
            float(self.max_db),
            self.fft_size,
        )
        return header + bytes(bins)

    def _pack_audio_frame(
        self, pcm: memoryview | np.ndarray, sample_rate: int = AUDIO_SAMPLE_RATE
    ) -> bytes:
        """Pack one audio frame into the binary wire format.

        ``pcm`` is mono int16 samples — from the pycsdr demod chain at
        AUDIO_SAMPLE_RATE, or from a remote receiver at whatever rate the
        far side streams (the header carries it; the frontend AudioPlayer
        resamples on playback). Accepts a buffer (pycsdr output) or an
        int16 ndarray (remote frames).
        """
        if isinstance(pcm, np.ndarray):
            pcm_bytes = np.ascontiguousarray(pcm).astype("<i2", copy=False).tobytes()
        else:
            pcm_bytes = bytes(pcm)
        frame_count = len(pcm_bytes) // 2
        header = struct.pack(
            AUDIO_PACK_FMT,
            AUDIO_HEADER_MAGIC,
            AUDIO_HEADER_VERSION,
            sample_rate,
            frame_count,
        )
        return header + pcm_bytes

    async def _broadcast(self, frame: bytes | str) -> None:
        """Fan out to all subscribers. Drop frame if a queue is full."""
        for q in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(frame)


def _default_fixture_path(settings: Settings | None) -> Path:
    """The dev-default IQ recording: baked 20 m evening scene (ADR-005).

    Wired as the default source so the frontend sees realistic signals
    (CW, SSB, FT8 traces, QRN) with zero hardware — swap in a real capture
    or a hardware source any time.
    """
    configured = settings.default_iq_fixture if settings is not None else None
    if configured is not None and str(configured) not in ("", "."):
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "fixtures" / "iq" / "hf_20m_evening.cf32"


def create_default_session(
    receiver_id: str = "rx-default",
    settings: Settings | None = None,
) -> ReceiverSession:
    """Factory for the default session: hardware-free by design.

    Order of preference (ADR-004/005):
      1. settings.default_source_type == "file" (default) → replay the baked
         20 m fixture — real signals, deterministic, no hardware.
      2. any other source_type → SourceRegistry (e.g. rtl_sdr when the user
         configures it on hardware-equipped hosts).
      3. fixture missing → SimulatedSource (always available).
    """
    st = settings or Settings()
    if st.default_source_type == "file":
        fixture = _default_fixture_path(st)
        try:
            src = FileSource(file_path=fixture, loop=True, realtime=True)
            log.info("default session uses IQ fixture", path=str(fixture))
            return ReceiverSession(
                receiver_id=receiver_id,
                source=src,
                center_freq=src.fixed_center_freq,
                sample_rate=src.fixed_sample_rate,
                mode="USB",
            )
        except (FileNotFoundError, ValueError):
            log.warning(
                "default IQ fixture missing — falling back to simulated source",
                path=str(fixture),
                hint="run scripts/generate_iq_fixtures.py",
            )
            source = SourceRegistry.create("simulated")
            return ReceiverSession(
                receiver_id=receiver_id,
                source=source,
                center_freq=st.default_center_freq,
                sample_rate=st.default_sample_rate,
                mode="USB",
            )

    source = SourceRegistry.create(st.default_source_type)
    return ReceiverSession(
        receiver_id=receiver_id,
        source=source,
        center_freq=st.default_center_freq,
        sample_rate=st.default_sample_rate,
        mode="USB",
    )
