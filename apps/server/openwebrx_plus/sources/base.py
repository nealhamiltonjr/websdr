"""Source protocol + SourceManifest + SourceRegistry — the SDR plugin contract.

A Source is anything that:
  - Has an identity (source_type, label, endpoint, sample rate)
  - Can be spawned with a (center_freq, sample_rate, gain) tuple
  - Produces an async stream of complex IQ samples (np.ndarray of complex64)

A SourceManifest is the declarative metadata that lets the backend and UI
discover sources without instantiating them. It includes:
  - source_type (string key used in API + UI)
  - label (human-readable)
  - sdk (underlying driver/sdk name)
  - hardware_required (False for file_source / simulated_source)
  - default/range sample rates
  - gain range
  - feature flags (bias_tee, agc, etc.)
  - factory_entrypoint (dotted path to the Source class)

SourceRegistry:
  - Discovers sources at startup via:
    (a) BUILTIN dict in this module (always available)
    (b) Python entry points in group "openwebrx_plus.sources" (external plugins)
  - Exposes all_manifests() / get_manifest() / create() for the REST API + UI.
  - The REST endpoint GET /api/sources lists manifests.
  - POST /api/receivers accepts { source_type: ... } matching a manifest.

See ADR-004 for the full rationale.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

import numpy as np

SourceType = Literal[
    "rtl_sdr",
    "rtl_tcp",
    "sdrplay",
    "hackrf",
    "airspy",
    "airspyhf",
    "kiwi",
    "openwebrx_remote",
    "spyserver",
    "sdrangel",
    "soapy",
    "websdr",
    "file",
    "simulated",
    "vfo",
]


@dataclass(frozen=True)
class SourceInfo:
    """Runtime identity of a Source instance."""

    type: SourceType
    label: str
    endpoint: str | None = None
    sample_rate: int = 0


@dataclass(frozen=True)
class SourceManifest:
    """Declarative description of a Source plugin.

    Used by SourceRegistry to discover + list sources without instantiating
    them. The factory_entrypoint is a dotted path like
    "openwebrx_plus.sources.rtl_sdr:RtlSdrSource".
    """

    source_type: str
    label: str
    sdk: str
    hardware_required: bool
    default_sample_rate: int
    sample_rate_range: tuple[int, int]
    gain_range: tuple[float, float] | None = None
    supports_bias_tee: bool = False
    supports_agc: bool = False
    factory_entrypoint: str = ""  # "module.path:ClassName"
    description: str = ""


@runtime_checkable
class Source(Protocol):
    """Synchronous-source protocol. Implementations are async generators.

    Note: `spawn` is declared as a regular `def` returning `AsyncIterator`
    (not `async def`) because implementations are async generator functions
    (they use `yield` inside `async def`). Calling an async generator function
    returns an AsyncIterator directly — no `await` needed. Declaring the
    protocol method as `async def` would type it as a coroutine-returning
    function, which doesn't match async generator implementations.
    """

    info: SourceInfo

    def spawn(
        self,
        center_freq: int,
        sample_rate: int,
        gain: float | None = None,
    ) -> AsyncGenerator[np.ndarray, None]:
        """Start streaming IQ samples. Yields chunks of complex64 numpy arrays.

        Implementations are async generator functions
        (`async def spawn(...) -> AsyncGenerator[np.ndarray, None]: yield ...`).
        Callers iterate via `async for chunk in source.spawn(...):` and may
        `await gen.aclose()` to stop the source early — AsyncGenerator (not
        plain AsyncIterator) is the declared return type precisely because
        aclose() is part of the contract consumers rely on (IqHub teardown,
        session stop, tests).
        """
        ...

    async def close(self) -> None: ...


@runtime_checkable
class RuntimeGainSource(Protocol):
    """A Source whose gain can be changed WHILE STREAMING (slice-4.7).

    Optional companion to ``Source`` — detected via ``hasattr`` by the
    ReceiverSession when a ``setGain`` control command arrives. Sources
    that don't implement it (VFO taps, remote display streams, drivers
    without a live handle) simply keep their spawn-time gain.

    Implementations MUST be safe to call from any asyncio task while the
    spawn() generator is being consumed (assignment of an atomic field or
    a queue put — never blocking I/O on the hot path).

    Semantics:
      - ``gain_db`` numeric → apply that manual gain (dB).
      - ``None`` → auto / AGC where the hardware has it; otherwise reset
        to unit gain (0 dB digital).
      - Returns True if the request was applied (or queued for the stream
        loop); False when the source can't honor it right now.
    """

    def set_runtime_gain(self, gain_db: float | None) -> bool: ...


@runtime_checkable
class RuntimeFrequencySource(Protocol):
    """A Source whose center frequency can be re-tuned WHILE STREAMING
    (slice-15 — SpyServer polish; covers rtl_tcp + USB drivers too).

    Optional companion to ``Source`` — detected via ``hasattr`` by the
    ReceiverSession when a ``setFrequency`` control command arrives, BEFORE
    falling back to the legacy ``self.center_freq = freq`` metadata-only
    update (which leaves the actual IQ stream centered on the original
    frequency and the local Shift block does offset-demod).

    Sources that implement this protocol tell the underlying SDR (or the
    remote SpyServer / rtl_tcp server) to physically recenter the stream
    on the new frequency. The ReceiverSession, on a True return, updates
    its ``center_freq`` to match and emits a metadata frame so the
    frontend's frequency axis re-renders around the new center.

    Implementations MUST be safe to call from any asyncio task while the
    spawn() generator is being consumed (assignment of an atomic field or
    a queue put — never blocking I/O on the hot path).

    Semantics:
      - ``hz`` integer → request the source recenter on that frequency.
      - Returns True if the request was applied (or queued for the stream
        loop); False when the source can't honor it right now (e.g. file
        replay, simulated, or a driver whose handle isn't live yet).
    """

    def set_runtime_frequency(self, hz: int) -> bool: ...


# ---------------------------------------------------------------------------
# Display-stream sources (ADR-006 federation client)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteFftFrame:
    """One pre-computed FFT frame from a remote receiver.

    ``bins`` are float32 dB values, DC-centered (negative→positive
    frequency), exactly like the output of the local pycsdr FftChain —
    the ReceiverSession repacks them into the standard WRFO wire format
    and the frontend renders them unchanged.
    """

    bins: np.ndarray
    center_freq: int  # Hz — the remote SDR's center frequency
    sample_rate: int  # Hz — the remote's displayed bandwidth
    min_db: float | None = None  # remote waterfall_levels, when advertised
    max_db: float | None = None


@dataclass(frozen=True)
class RemoteSecondaryFftFrame:
    """One pre-computed secondary FFT frame (slice-22 — federation polish).

    The OpenWebRX federation protocol carries a SECONDARY FFT stream
    (Type 0x03 frames) — the narrowband spectrum of the demodulated
    channel. For digital modes (FT8, CW, PSK31) this is the "channel
    scope" view showing the FSK tones or CW sidetone within the
    demod passband.

    Same shape as :class:`RemoteFftFrame` but the center_freq /
    sample_rate describe the SECONDARY channel (the demod channel),
    not the wideband span. The session repacks these into the
    ``SECONDARY_FFT_HEADER_MAGIC`` wire format so the frontend WS
    demux routes them to a separate stream.

    ``bins`` are float32 dB values, DC-centered, same as
    :class:`RemoteFftFrame`.
    """

    bins: np.ndarray
    center_freq: int  # Hz — the demod channel center (often the tuned freq)
    sample_rate: int  # Hz — the demod channel span (much narrower than wideband)
    min_db: float | None = None
    max_db: float | None = None


@dataclass(frozen=True)
class RemoteAudioFrame:
    """One chunk of demodulated audio from a remote receiver."""

    pcm: np.ndarray  # int16 mono
    sample_rate: int  # Hz


@runtime_checkable
class DisplayStreamSource(Protocol):
    """A source that yields *display frames* instead of raw IQ (ADR-006).

    The OpenWebRX federation client is the reference implementation: a
    remote OpenWebRX(+) receiver computes the FFT and demodulates the tuned
    channel itself, so there is no IQ to feed the local pycsdr chains. The
    ReceiverSession detects ``display_stream`` and bypasses its chains,
    repacking frames into the standard WRFO/AUDI wire formats. Tuning is
    forwarded to the remote (its demodulator, not ours, does the work).

    Like ``Source.spawn``, ``display_stream`` is declared as a regular
    ``def`` returning AsyncGenerator because implementations are async
    generator functions.
    """

    info: SourceInfo

    def display_stream(
        self,
    ) -> AsyncGenerator[
        RemoteFftFrame | RemoteSecondaryFftFrame | RemoteAudioFrame, None
    ]:
        """Yield remote FFT/secondary-FFT/audio frames until the connection ends.

        Raises RuntimeError on connect failure or remote refusal
        (``backoff``) — display sources never retry on their own.
        """
        ...

    async def tune(self, freq_hz: int) -> None:
        """Tune the remote demodulator to an absolute frequency (Hz)."""
        ...

    async def set_mode(self, mode: str) -> None:
        """Switch the remote demodulator."""
        ...

    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# SourceRegistry
# ---------------------------------------------------------------------------

_ENTRY_POINT_GROUP = "openwebrx_plus.sources"


@dataclass(frozen=True)
class _BuiltinManifest:
    """Helper for declaring built-in manifests succinctly."""

    source_type: str
    label: str
    sdk: str
    hardware_required: bool
    default_sample_rate: int
    sample_rate_min: int
    sample_rate_max: int
    gain_min: float | None = None
    gain_max: float | None = None
    bias_tee: bool = False
    agc: bool = False
    entrypoint: str = ""
    description: str = ""


# The built-in source manifests. Always available. Each manifest corresponds
# to a Source implementation in this package. The entrypoint is filled in
# lazily by SourceRegistry (so we don't have a circular import).
_BUILTIN_SOURCES: list[_BuiltinManifest] = [
    _BuiltinManifest(
        source_type="rtl_sdr",
        label="RTL-SDR (incl. V4)",
        sdk="librtlsdr / rtl_tcp / rtl_sdr CLI",
        hardware_required=True,
        default_sample_rate=2_400_000,
        sample_rate_min=250_000,
        sample_rate_max=3_200_000,
        gain_min=0.0,
        gain_max=49.0,
        bias_tee=True,  # RTL-SDR Blog V3/V4 bias tee
        agc=True,
        entrypoint="openwebrx_plus.sources.rtl_sdr:RtlSdrSource",
        description=(
            "RTL2832U-based USB SDR — real driver (slice-3). Three "
            "transports, auto-probed: USB via librtlsdr, rtl_tcp (network), "
            "or the rtl_sdr CLI. RTL-SDR Blog V4 (R828D) supported; V4 HF "
            "via direct_sampling=2 (0.5–28.8 MHz) — needs librtlsdr >= 0.8 "
            "or the rtl-sdr-blog fork."
        ),
    ),
    _BuiltinManifest(
        source_type="rtl_tcp",
        label="rtl_tcp remote (network RTL-SDR)",
        sdk="rtl_tcp wire protocol",
        hardware_required=False,  # the SDR is on the other end of the TCP connection
        default_sample_rate=2_400_000,
        sample_rate_min=250_000,
        sample_rate_max=3_200_000,
        gain_min=0.0,
        gain_max=49.0,
        bias_tee=False,  # deliberately off in the UI — remote bias tee is a foot-gun
        agc=True,
        entrypoint="openwebrx_plus.sources.rtl_tcp:RtlTcpSource",
        description=(
            "Remote RTL-SDR over the internet (ADR-006): connect to any "
            "rtl_tcp server — run 'rtl_tcp -a 0.0.0.0' next to your own "
            "RTL-SDR V4 and use it from anywhere, or use a public server. "
            "rsp_tcp (SDRplay) speaks the same protocol shape. cu8 → cf32; "
            "ppm correction and direct_sampling pass through to the server."
        ),
    ),
    _BuiltinManifest(
        source_type="airspy",
        label="Airspy R0/R2/Mini/HF+",
        sdk="libairspy",
        hardware_required=True,
        default_sample_rate=10_000_000,
        sample_rate_min=2_500_000,
        sample_rate_max=20_000_000,
        gain_min=0.0,
        gain_max=21.0,
        bias_tee=True,
        agc=False,
        entrypoint="openwebrx_plus.sources.airspy:AirspySource",
        description=(
            "Airspy 10 MSPS / HF+ Discovery SDR — real driver (slice-3) via "
            "ctypes/libairspy. Gain modes: linearity / sensitivity / manual "
            "(LNA+Mixer+VGA), bias tee, 24–1800 MHz (R2) or 9 kHz–31 MHz "
            "(HF+ Discovery). Higher dynamic range than RTL-SDR."
        ),
    ),
    _BuiltinManifest(
        source_type="sdrplay",
        label="SDRplay RSP1/1B/2/Duo/DXR",
        sdk="sdrplay_api v3 (libmirsdrapi-rsp)",
        hardware_required=True,
        default_sample_rate=2_000_000,
        sample_rate_min=2_000_000,  # SDRplay API min
        sample_rate_max=10_000_000,
        gain_min=0.0,
        gain_max=39.0,  # inverted to gRdB 20–59 (gain reduction)
        bias_tee=True,  # RSP2/RSPdx support bias tee
        agc=True,
        entrypoint="openwebrx_plus.sources.sdrplay:SDRplaySource",
        description=(
            "SDRplay RSP series — real driver (slice-3) via cffi against "
            "the API v3 library (callback-based streaming). 1 kHz–2 GHz, "
            "14-bit ADC, gain-reduction model. RSPduo dual tuners are the "
            "natural ADR-005 hardware-VFO anchor. Verify the cdef against "
            "the installed mirsdrapi-rsp.h on first bring-up."
        ),
    ),
    _BuiltinManifest(
        source_type="soapy",
        label="SoapySDR (universal driver layer)",
        sdk="SoapySDR",
        hardware_required=True,
        default_sample_rate=1_000_000,
        sample_rate_min=100_000,
        sample_rate_max=61_440_000,
        entrypoint="openwebrx_plus.sources.soapy:SoapySource",
        description=(
            "Universal transport (slice-3): ANY SDR with a SoapySDR module "
            "(Airspy, HackRF, BladeRF, LimeSDR, PlutoSDR, USRP, remote, …) "
            "works with zero per-device code. Set soapy_args like "
            '\'{"driver": "hackrf"}\'. Needs python3-soapysdr + the '
            "soapysdr-module-* packages."
        ),
    ),
    _BuiltinManifest(
        source_type="spyserver",
        label="SpyServer remote (Airspy network protocol)",
        sdk="SpyServer TCP protocol v2",
        hardware_required=False,  # remote receiver
        default_sample_rate=768_000,
        sample_rate_min=32_000,
        sample_rate_max=10_000_000,
        gain_min=0.0,
        gain_max=48.0,
        bias_tee=False,
        agc=False,  # gain_type semantics flagged for live bring-up
        entrypoint="openwebrx_plus.sources.spyserver:SpyServerSource",
        description=(
            "Connect to any SpyServer receiver over the internet (ADR-006 "
            "Tier A raw-IQ remote) — the server side of the Airspy "
            "ecosystem: one machine runs 'spyserver' with an Airspy HF+/"
            "Discovery/R2 or RTL-SDR attached, clients stream float32 IQ "
            "over TCP. All pycsdr DSP runs locally. source_kwargs: "
            '{"host": "sdr.example.com", "port": 5555, "sample_rate": '
            '768000}. The rate must be exactly device_max/2**k (768000 '
            "matches an HF+ server at full rate; 2400000 for RTL-SDR "
            "servers) — the server's achievable rates are listed in the "
            "error message otherwise. Runtime gain via COMMAND_SET_IQ_GAIN "
            "(latest-wins). Protocol literals verified against the fake "
            "server in tests/test_spyserver_driver.py; flagged for "
            "first-live-connection check."
        ),
    ),
    _BuiltinManifest(
        source_type="kiwi",
        label="KiwiSDR remote (public HF network)",
        sdk="KiwiSDR websocket protocol",
        hardware_required=False,  # remote receiver
        default_sample_rate=12_000,
        sample_rate_min=6_000,
        sample_rate_max=48_000,
        gain_min=None,
        gain_max=None,
        bias_tee=False,
        agc=False,  # gain/AGC is managed Kiwi-side
        entrypoint="openwebrx_plus.sources.kiwi:KiwiSdrSource",
        description=(
            "Connect to any of the 1000+ public KiwiSDR receivers "
            "(0–30 MHz HF, up to 8 users each) — see GET /api/directory/kiwi "
            "for the live list. Streams the Kiwi's mod=IQ channel as int16 → "
            "cf32, so the full pycsdr chain runs locally. The session adopts "
            "the Kiwi sound rate (12 kHz default). Protocol literals are "
            "flagged for verification on first live connection (ADR-006)."
        ),
    ),
    _BuiltinManifest(
        source_type="openwebrx_remote",
        label="OpenWebRX remote (federation client)",
        sdk="OpenWebRX(+) websocket protocol",
        hardware_required=False,  # the receiver is on the other end
        default_sample_rate=2_400_000,  # informational — remote decides
        sample_rate_min=8_000,
        sample_rate_max=61_440_000,
        gain_min=None,
        gain_max=None,
        bias_tee=False,
        agc=False,
        entrypoint="openwebrx_plus.sources.openwebrx_remote:RemoteDisplaySource",
        description=(
            "Connect to any public OpenWebRX / OpenWebRX+ receiver over the "
            "internet (ADR-006 federation receive-side). Paste the URL you "
            "would open in a browser — deep link included — as source_kwargs"
            '.url, e.g. {"url": "http://boomerthedog.com:8073/'
            '#freq=3570000,mod=lsb,sql=-150"}; host/port/freq/mod/squelch '
            "are parsed from it. Browse receivers via GET /api/directory/"
            "receiverbook. The remote supplies processed FFT + demodulated "
            "audio (not raw IQ): the session bypasses its pycsdr chains and "
            "forwards tuning to the remote dspcontrol. Verified against the "
            "fake server in tests/test_openwebrx_remote_driver.py; ADPCM "
"interop flagged for first-live-connection check."
        ),
    ),
    _BuiltinManifest(
        source_type="vfo",
        label="VFO sub-receiver (tap a wideband receiver)",
        sdk="pycsdr (Shift + FirDecimate)",
        hardware_required=False,
        default_sample_rate=12_000,
        sample_rate_min=1_000,
        sample_rate_max=2_400_000,
        entrypoint="openwebrx_plus.sources.wideband:VfoTapSource",
        description=(
            "ADR-005 VFO sub-receiver: taps another receiver's wideband "
            "stream and extracts a narrowband slice with a pycsdr DDC "
            "(Shift → FirDecimate). Requires source_kwargs.parent_receiver_id "
            "pointing at a STARTED wideband receiver. Integer decimation "
            "from the parent rate; slice must fit inside the parent span."
        ),
    ),
    _BuiltinManifest(
        source_type="file",
        label="File source (IQ recording replay)",
        sdk="none",
        hardware_required=False,
        default_sample_rate=2_400_000,
        sample_rate_min=1,
        sample_rate_max=100_000_000,
        gain_min=-20.0,  # digital gain (slice-4.7) — replayed samples are scaled
        gain_max=20.0,
        bias_tee=False,
        agc=False,
        entrypoint="openwebrx_plus.sources.file_source:FileSource",
        description=(
            "Replays recorded IQ from disk (cf32 / .cfile / .cs16 / .cu8 / "
            ".sigmf-data) at wall-clock real time, looping by default — "
            "the waterfall scrolls exactly like live SDR. Gain is digital "
            "(±20 dB scaling of the replayed samples). Pair with the "
            "baked fixtures (scripts/generate_iq_fixtures.py) or your own "
            "captures (rtl_sdr writes cu8 directly)."
        ),
    ),
    _BuiltinManifest(
        source_type="simulated",
        label="Simulated source (synthetic signals)",
        sdk="none",
        hardware_required=False,
        default_sample_rate=2_400_000,
        sample_rate_min=100_000,
        sample_rate_max=20_000_000,
        gain_min=-20.0,  # digital gain (slice-4.7) — scales the synthetic scene
        gain_max=20.0,
        bias_tee=False,
        agc=False,
        entrypoint="openwebrx_plus.sources.simulated:SimulatedSource",
        description=(
            "Generates multi-signal synthetic IQ (carriers, noise floor, "
            "optional pulsed signals). For demos, fixtures, and slice-1 "
            "frontend work without any SDR hardware. Gain is digital "
            "(±20 dB scaling of the output samples)."
        ),
    ),
    _BuiltinManifest(
        source_type="sdrangel",
        label="SDRangel (remote, REST+WS — manifest only)",
        sdk="SDRangel REST API v7+",
        hardware_required=False,  # the SDR is on the other end of the REST API
        default_sample_rate=2_400_000,
        sample_rate_min=250_000,
        sample_rate_max=20_000_000,
        gain_min=0.0,
        gain_max=49.0,
        bias_tee=False,
        agc=True,
        entrypoint="openwebrx_plus.sources.sdrangel:SDRangelSource",
        description=(
            "Slice-20 manifest scaffold: a remote SDRangel instance "
            "controlled via its REST + WebSocket API (ADR-006 Tier C). "
            "Registered so the UI can advertise SDRangel support; the "
            "REST+WS streaming path raises NotImplementedError today "
            "and lands in a future slice. Operators who want SDRangel "
            "now can run a local instance and connect via its own web "
            "UI. See openwebrx_plus/sources/sdrangel.py module docstring "
            "for the implementation plan (device discovery → center "
            "freq set → spectrum server WS → RemoteFftFrame)."
        ),
    ),
]


def _to_manifest(b: _BuiltinManifest) -> SourceManifest:
    gain_range: tuple[float, float] | None = None
    if b.gain_min is not None and b.gain_max is not None:
        gain_range = (b.gain_min, b.gain_max)
    return SourceManifest(
        source_type=b.source_type,
        label=b.label,
        sdk=b.sdk,
        hardware_required=b.hardware_required,
        default_sample_rate=b.default_sample_rate,
        sample_rate_range=(b.sample_rate_min, b.sample_rate_max),
        gain_range=gain_range,
        supports_bias_tee=b.bias_tee,
        supports_agc=b.agc,
        factory_entrypoint=b.entrypoint,
        description=b.description,
    )


def _resolve_entrypoint(entrypoint: str) -> type[Source]:
    """Resolve "module.path:ClassName" → the Source subclass.

    Raises ImportError if the module can't be imported, or AttributeError
    if the class doesn't exist on the module.
    """
    module_path, _, class_name = entrypoint.partition(":")
    if not module_path or not class_name:
        raise ValueError(f"invalid entrypoint: {entrypoint!r}")
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    if not isinstance(cls, type):
        raise TypeError(f"entrypoint {entrypoint!r} does not resolve to a class")
    return cls


class SourceRegistry:
    """Source plugin discovery + instantiation.

    All methods are classmethods — the registry is a process-wide singleton
    backed by the BUILTIN_SOURCES list plus any entry points discovered at
    startup.
    """

    _discovered: list[SourceManifest] | None = None  # cached

    @classmethod
    def builtin_manifests(cls) -> list[SourceManifest]:
        """Manifests for sources shipped in this repo."""
        return [_to_manifest(b) for b in _BUILTIN_SOURCES]

    @classmethod
    def discovered_manifests(cls) -> list[SourceManifest]:
        """Manifests from external packages via Python entry points.

        Discovery runs once and is cached. If no external plugins are
        installed, returns [].
        """
        if cls._discovered is not None:
            return list(cls._discovered)

        discovered: list[SourceManifest] = []
        try:
            import importlib.metadata as ilm
        except ImportError:  # Python <3.10 — shouldn't happen, we require 3.12
            cls._discovered = discovered
            return discovered

        for ep in ilm.entry_points(group=_ENTRY_POINT_GROUP):
            # External entry points give us a Source *class* directly. We
            # extract the manifest from a class attribute if present.
            try:
                cls_obj = ep.load()
            except Exception:
                # Don't let a broken plugin kill startup.
                continue
            manifest = getattr(cls_obj, "MANIFEST", None)
            if isinstance(manifest, SourceManifest):
                discovered.append(manifest)
        cls._discovered = discovered
        return list(discovered)

    @classmethod
    def all_manifests(cls) -> list[SourceManifest]:
        """Union of builtin + discovered manifests."""
        return [*cls.builtin_manifests(), *cls.discovered_manifests()]

    @classmethod
    def get_manifest(cls, source_type: str) -> SourceManifest | None:
        """Look up a manifest by source_type. Returns None if not found."""
        for m in cls.all_manifests():
            if m.source_type == source_type:
                return m
        return None

    @classmethod
    def create(cls, source_type: str, **kwargs: Any) -> Source:
        """Instantiate a Source by source_type.

        kwargs are passed through to the Source's __init__. Common kwargs:
          - device_index: int (for USB sources — 0 by default)
          - file_path: str (for FileSource)
          - signal_set: str (for SimulatedSource — "default", "am_band", etc.)

        Raises KeyError if source_type is not registered.
        """
        manifest = cls.get_manifest(source_type)
        if manifest is None:
            raise KeyError(f"unknown source_type: {source_type!r}")
        source_cls = _resolve_entrypoint(manifest.factory_entrypoint)
        return source_cls(**kwargs)
