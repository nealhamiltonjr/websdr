"""Source backends — one class per supported SDR type + simulated/file sources.

Each backend implements the Source protocol: spawn() → AsyncGenerator[np.ndarray]
yielding complex IQ samples at a known sample rate (aclose-able — the hub,
sessions, and tests all stop sources via aclose()).

Source plugin discovery:
  - SourceRegistry.builtin_manifests() → manifests for the 11 sources in this
    package (rtl_sdr, rtl_tcp, airspy, sdrplay, soapy, kiwi, spyserver,
    openwebrx_remote, file, simulated, vfo)
  - SourceRegistry.discovered_manifests() → manifests from external plugins
    (via entry points in group "openwebrx_plus.sources")
  - SourceRegistry.create(source_type, **kwargs) → instantiate a Source by type

Slice-3 status: ALL hardware backends are real drivers.

  rtl_sdr   librtlsdr over USB (ctypes), rtl_tcp (asyncio), or the rtl_sdr
            CLI (subprocess) — auto-probed, pinned via ``transport=``.
            RTL-SDR Blog V4 (R828D): direct_sampling=2 gives the built-in
            HF path. Unit-tested against a fake rtl_tcp server.
  rtl_tcp   REMOTE source (ADR-006): any rtl_tcp/rsp_tcp server on the
            network or internet — the wire protocol lives in rtl_tcp.py and
            is shared with RtlSdrSource's tcp transport.
  airspy    ctypes/libairspy with linearity/sensitivity/manual gain modes
            and bias tee. Tested against a fake binding.
  sdrplay   cffi against the SDRplay API v3 (callback-based streaming),
            gain-reduction model, RSPduo dual-tuner hooks for ADR-005.
            Tested against a fake binding; verify the cdef against the
            installed mirsdrapi-rsp.h on first hardware bring-up.
  soapy     Universal SoapySDR transport — any SDR with a Soapy module
            works with zero per-device code (the "any SDR via plugin"
            promise from ADR-004). Remote SoapySDR servers work via
            soapy_args={"driver": "remote", "remote": "tcp://host:8080"}.
  kiwi      REMOTE source (ADR-006): any public KiwiSDR receiver (0–30 MHz
            HF) via its websocket protocol, mod=IQ int16 stream. Verified
            against the fake Kiwi server in tests/test_kiwi_driver.py;
            handshake literals flagged for first-live-connection check.
  openwebrx_remote
            REMOTE source (ADR-006 federation receive-side): any public
            OpenWebRX / OpenWebRX+ receiver over the internet — paste a
            browser-style deep link (host/port + freq/mod/sql) and stream
            the remote's waterfall + demodulated audio. A *display-stream*
            source (no raw IQ): the session bypasses pycsdr and forwards
            tuning via dspcontrol. Verified against the fake server in
            tests/test_openwebrx_remote_driver.py; ADPCM interop flagged
            for first-live-connection check.
  file      IQ recording replay (cf32/cs16/cu8/SigMF), real-time paced,
            looping. Baked fixtures in apps/server/fixtures/iq/.
  simulated Synthetic multi-signal scenes, real-time paced.
  vfo       VFO sub-receiver (ADR-005): taps a wideband receiver's stream
            and extracts a slice via a pycsdr Shift → FirDecimate DDC.

Remote-receiver discovery: ``directory.DirectoryService`` lists public
KiwiSDR + OpenWebRX receivers (rx.kiwisdr.com / receiverbook.de) and powers
``GET /api/directory/*`` — the seed of the federation pillar (ADR-006).

Hardware detection: ``probe.detect_hardware()`` sweeps every driver and
powers ``GET /api/hardware``.
"""

from .airspy import AirspySource
from .base import (
    DisplayStreamSource,
    RemoteAudioFrame,
    RemoteFftFrame,
    Source,
    SourceInfo,
    SourceManifest,
    SourceRegistry,
    SourceType,
)
from .directory import (
    DirectoryService,
    DirectoryUnavailable,
    RemoteReceiver,
    directory_service,
)
from .file_source import FileSource
from .kiwi import KiwiSdrSource
from .openwebrx_remote import RemoteDisplaySource, RemoteTarget, parse_openwebrx_url
from .rtl_sdr import RtlSdrSource
from .rtl_tcp import RtlTcpSource
from .sdrplay import SDRplaySource
from .simulated import SimulatedSource
from .soapy import SoapySource
from .spyserver import SpyServerDeviceInfo, SpyServerSource, parse_server_info
from .wideband import (
    IqHub,
    VfoTapSource,
    get_hub,
    get_or_create_hub,
    register_hub,
)

__all__ = [
    "AirspySource",
    "DirectoryService",
    "DirectoryUnavailable",
    "DisplayStreamSource",
    "FileSource",
    "IqHub",
    "KiwiSdrSource",
    "RemoteAudioFrame",
    "RemoteDisplaySource",
    "RemoteFftFrame",
    "RemoteReceiver",
    "RemoteTarget",
    "RtlSdrSource",
    "RtlTcpSource",
    "SDRplaySource",
    "SimulatedSource",
    "SoapySource",
    "Source",
    "SourceInfo",
    "SourceManifest",
    "SourceRegistry",
    "SourceType",
    "SpyServerDeviceInfo",
    "SpyServerSource",
    "parse_server_info",
    "VfoTapSource",
    "directory_service",
    "get_hub",
    "get_or_create_hub",
    "parse_openwebrx_url",
    "register_hub",
]
