"""RTL-SDR source backend — real driver (slice-3).

Replaces the slice-1 stub tone generator with three real transports,
resolved at ``spawn()`` time (or pinned via ``transport=``):

  usb        ctypes bindings to librtlsdr. ``rtlsdr_read_async`` runs in a
             dedicated thread; its callback copies cu8 blocks into an
             :class:`AsyncIqBridge` (drop-oldest under backpressure).
  tcp        A native-asyncio rtl_tcp client — delegates to the shared
             :func:`~openwebrx_plus.sources.rtl_tcp.rtl_tcp_stream`
             implementation (12-byte "RTL0" handshake, big-endian >BI commands,
             cu8 stream). Works over the network, is hardware-optional, and
             is fully unit-testable against a fake server
             (see tests/test_rtl_sdr_driver.py).
  subprocess Spawns the ``rtl_sdr`` CLI and reads raw cu8 from stdout.
             Useful wherever the CLI exists but the shared library is
             awkward to load (containers, Nix, macOS).

RTL-SDR Blog **V4** (R828D tuner): supported through any librtlsdr with
V4 support — osmocom >= 0.8.0 (April 2024) or the rtl-sdr-blog fork
(>= 2.x). The V4's headline feature, built-in HF coverage via the
direct-sampling path (0.5–28.8 MHz), is exposed as
``direct_sampling=2`` (0 = off, 1 = I branch, 2 = Q branch/HF).

Gain semantics: ``gain`` is dB at the tuner (0.0–49.6 typical). ``None``
means tuner AGC (hardware-managed). ``rtl_agc`` toggles the RTL2832's
digital IF AGC on top (default on, as in every SDR application).
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import shutil
import threading
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import structlog

from ._hw_common import AsyncIqBridge, cu8_to_cf32
from .base import SourceInfo
from .rtl_tcp import rtl_tcp_stream

log = structlog.get_logger(__name__)

_LIBRTLSDR_CANDIDATES = (
    "librtlsdr.so.2",
    "librtlsdr.so.0",
    "librtlsdr.so",
    "librtlsdr.dylib",
)

# rtlsdr_read_async callback: (const uint8_t *buf, uint32_t len, void *ctx)
_RTLSDR_ASYNC_CB = ctypes.CFUNCTYPE(
    None, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint32, ctypes.c_void_p
)


def _load_librtlsdr() -> ctypes.CDLL | None:
    """Locate a usable librtlsdr shared library. None if absent."""
    for name in _LIBRTLSDR_CANDIDATES:
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


def _configure_librtlsdr(lib: ctypes.CDLL) -> None:
    """Set argtypes/restype for the subset of the API we call."""
    c_void = ctypes.c_void_p
    u32 = ctypes.c_uint32
    i32 = ctypes.c_int
    lib.rtlsdr_get_device_count.restype = u32
    lib.rtlsdr_get_device_count.argtypes = []
    lib.rtlsdr_get_device_name.restype = ctypes.c_char_p
    lib.rtlsdr_get_device_name.argtypes = [u32]
    lib.rtlsdr_open.restype = i32
    lib.rtlsdr_open.argtypes = [ctypes.POINTER(c_void), u32]
    lib.rtlsdr_close.restype = i32
    lib.rtlsdr_close.argtypes = [c_void]
    lib.rtlsdr_set_center_freq.restype = i32
    lib.rtlsdr_set_center_freq.argtypes = [c_void, u32]
    lib.rtlsdr_set_freq_correction.restype = i32
    lib.rtlsdr_set_freq_correction.argtypes = [c_void, i32]
    lib.rtlsdr_set_sample_rate.restype = i32
    lib.rtlsdr_set_sample_rate.argtypes = [c_void, u32]
    lib.rtlsdr_set_tuner_gain_mode.restype = i32
    lib.rtlsdr_set_tuner_gain_mode.argtypes = [c_void, i32]
    lib.rtlsdr_set_tuner_gain.restype = i32
    lib.rtlsdr_set_tuner_gain.argtypes = [c_void, i32]
    lib.rtlsdr_set_agc_mode.restype = i32
    lib.rtlsdr_set_agc_mode.argtypes = [c_void, i32]
    lib.rtlsdr_set_direct_sampling.restype = i32
    lib.rtlsdr_set_direct_sampling.argtypes = [c_void, i32]
    lib.rtlsdr_reset_buffer.restype = i32
    lib.rtlsdr_reset_buffer.argtypes = [c_void]
    lib.rtlsdr_read_async.restype = i32
    lib.rtlsdr_read_async.argtypes = [c_void, _RTLSDR_ASYNC_CB, c_void, i32, i32]
    lib.rtlsdr_cancel_async.restype = i32
    lib.rtlsdr_cancel_async.argtypes = [c_void]


class _RtlUsbTransport:
    """Direct USB transport via ctypes/librtlsdr."""

    def __init__(self, cfg: RtlSdrSource, lib: ctypes.CDLL) -> None:
        self._cfg = cfg
        self._lib = lib
        self._dev = ctypes.c_void_p()
        self._reader_thread: threading.Thread | None = None
        self._bridge = AsyncIqBridge(max_blocks=32)
        # Keep a reference to the CFUNCTYPE wrapper — librtlsdr holds the raw
        # pointer; if Python GC'd the wrapper mid-stream the callback dies.
        self._cb = _RTLSDR_ASYNC_CB(self._on_samples)

    def set_runtime_gain(self, gain_db: float | None) -> bool:
        """Apply a tuner-gain change while streaming (slice-4.7).

        Mirrors the open-time logic in stream(). Called from the WS
        listener task while the reader thread runs — librtlsdr gain calls
        are safe concurrent with read_async (standard practice; flagged
        for first-live-connection check like the rest of the driver).
        """
        dev = self._dev
        if not dev.value:
            return False  # not streaming
        lib = self._lib
        if gain_db is None:
            lib.rtlsdr_set_tuner_gain_mode(dev, 0)  # tuner AGC
            return True
        lib.rtlsdr_set_tuner_gain_mode(dev, 1)
        rc = lib.rtlsdr_set_tuner_gain(dev, int(round(gain_db * 10)))
        return int(rc) == 0

    def _check(self, rc: int, what: str) -> None:
        if rc != 0:
            raise RuntimeError(f"librtlsdr {what} failed with rc={rc}")

    async def stream(
        self, center_freq: int, sample_rate: int, gain: float | None
    ) -> AsyncIterator[np.ndarray]:
        cfg, lib = self._cfg, self._lib
        self._check(lib.rtlsdr_open(ctypes.byref(self._dev), cfg.device_index), "open")
        dev = self._dev
        try:
            self._check(lib.rtlsdr_set_sample_rate(dev, sample_rate), "set_sample_rate")
            if cfg.ppm:
                self._check(
                    lib.rtlsdr_set_freq_correction(dev, cfg.ppm), "set_freq_correction"
                )
            self._check(lib.rtlsdr_set_center_freq(dev, center_freq), "set_center_freq")
            if gain is None:
                lib.rtlsdr_set_tuner_gain_mode(dev, 0)  # tuner AGC
            else:
                lib.rtlsdr_set_tuner_gain_mode(dev, 1)
                self._check(
                    lib.rtlsdr_set_tuner_gain(dev, int(round(gain * 10))), "set_tuner_gain"
                )
            self._check(lib.rtlsdr_set_agc_mode(dev, 1 if cfg.rtl_agc else 0), "set_agc_mode")
            if cfg.direct_sampling:
                self._check(
                    lib.rtlsdr_set_direct_sampling(dev, cfg.direct_sampling),
                    "set_direct_sampling",
                )
            # Optional calls — older/stock librtlsdr builds lack them.
            if cfg.tuner_bandwidth:
                fn = getattr(lib, "rtlsdr_set_tuner_bandwidth", None)
                if fn is not None:
                    fn.restype = ctypes.c_int
                    fn.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                    fn(dev, cfg.tuner_bandwidth)
            if cfg.bias_tee:
                fn = getattr(lib, "rtlsdr_set_bias_tee", None)
                if fn is not None:
                    fn.restype = ctypes.c_int
                    fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
                    fn(dev, 1)
            self._check(lib.rtlsdr_reset_buffer(dev), "reset_buffer")

            self._bridge.bind()
            self._reader_thread = threading.Thread(
                target=self._read_loop,
                args=(dev, self._cb, 8, cfg.chunk_size * 2),
                name=f"rtlsdr-usb-{cfg.device_index}",
                daemon=True,
            )
            self._reader_thread.start()
            log.info(
                "rtl-sdr usb streaming",
                device_index=cfg.device_index,
                center_freq=center_freq,
                sample_rate=sample_rate,
            )
            async for raw in self._bridge.stream():
                yield cu8_to_cf32(raw)
        finally:
            self._teardown(dev)

    def _read_loop(self, dev: ctypes.c_void_p, cb: object, bufcnt: int, blocklen: int) -> None:
        """Blocking read_async — runs in its own thread until cancelled."""
        try:
            self._lib.rtlsdr_read_async(dev, cb, None, bufcnt, blocklen)
        except Exception:  # noqa: BLE001 — thread boundary; log and stop
            log.exception("rtlsdr_read_async crashed")
        finally:
            self._bridge.close()

    def _on_samples(self, samples: object, length: int, _ctx: object) -> None:
        """librtlsdr callback — COPY the buffer (the lib reuses it)."""
        assert samples is not None
        ptr = ctypes.cast(samples, ctypes.POINTER(ctypes.c_ubyte * length))  # type: ignore[arg-type]
        arr = np.frombuffer(ptr.contents, dtype=np.uint8).copy()
        self._bridge.push(arr)

    def _teardown(self, dev: ctypes.c_void_p) -> None:
        self._bridge.close()
        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._lib.rtlsdr_cancel_async(dev)
            self._reader_thread.join(timeout=2.0)
        if dev.value:
            self._lib.rtlsdr_close(dev)


class _RtlTcpTransport:
    """rtl_tcp client — delegates to the shared protocol implementation."""

    def __init__(self, cfg: RtlSdrSource) -> None:
        self._cfg = cfg

    async def stream(
        self, center_freq: int, sample_rate: int, gain: float | None
    ) -> AsyncIterator[np.ndarray]:
        cfg = self._cfg
        stream = rtl_tcp_stream(
            cfg.host,
            cfg.port,
            center_freq=center_freq,
            sample_rate=sample_rate,
            gain=gain,
            rtl_agc=cfg.rtl_agc,
            direct_sampling=cfg.direct_sampling,
            bias_tee=cfg.bias_tee,
            ppm=cfg.ppm,
            gain_q=cfg._gain_q,
            chunk_size=cfg.chunk_size,
        )
        async for chunk in stream:
            yield chunk


class _RtlSubprocessTransport:
    """Spawn the ``rtl_sdr`` CLI and stream raw cu8 from stdout."""

    def __init__(self, cfg: RtlSdrSource) -> None:
        self._cfg = cfg

    async def stream(
        self, center_freq: int, sample_rate: int, gain: float | None
    ) -> AsyncIterator[np.ndarray]:
        cfg = self._cfg
        binary = shutil.which(cfg.rtl_sdr_binary) or cfg.rtl_sdr_binary
        if shutil.which(binary) is None:
            raise RuntimeError(f"rtl_sdr binary not found: {cfg.rtl_sdr_binary!r}")
        args = [
            binary,
            "-f",
            str(center_freq),
            "-s",
            str(sample_rate),
            "-d",
            str(cfg.device_index),
        ]
        if gain is not None:
            # rtl_sdr's -g takes tenths of dB.
            args += ["-g", str(int(round(gain * 10)))]
        if cfg.direct_sampling:
            args += ["-D", str(cfg.direct_sampling)]
        if cfg.bias_tee:
            args += ["-T"]
        args += ["-"]  # stream to stdout forever

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        log.info("rtl_sdr subprocess streaming", args=args[1:-1])
        try:
            need = cfg.chunk_size * 2
            try:
                while True:
                    raw = await proc.stdout.readexactly(need)
                    yield cu8_to_cf32(np.frombuffer(raw, dtype=np.uint8))
            except asyncio.IncompleteReadError as exc:
                if exc.partial:
                    yield cu8_to_cf32(np.frombuffer(exc.partial, dtype=np.uint8))
                return  # EOF — process exited
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


@dataclass
class RtlSdrSource:
    """RTL-SDR source (real driver — replaces the slice-1 stub).

    Args:
        device_index: USB device index (usb/subprocess transports).
        transport: "auto" (default) probes usb → tcp → subprocess in order;
            or pin "usb" | "tcp" | "subprocess".
        host/port: rtl_tcp server endpoint (tcp transport / auto-probe).
        ppm: frequency correction in ppm (usb + tcp transports).
        direct_sampling: 0 off, 1 I-branch, 2 Q-branch. RTL-SDR Blog V4
            uses 2 for the built-in HF path (0.5–28.8 MHz).
        bias_tee: power the bias tee (V3/V4 with the modification / BLOG V4).
        tuner_bandwidth: Hz, 0 = auto (usb transport, newer librtlsdr).
        rtl_agc: RTL2832 digital AGC (default on).
        chunk_size: complex samples per yielded chunk.
        rtl_sdr_binary: executable for the subprocess transport.
    """

    device_index: int = 0
    transport: str = "auto"
    host: str = "127.0.0.1"
    port: int = 1234
    ppm: int = 0
    direct_sampling: int = 0
    bias_tee: bool = False
    tuner_bandwidth: int = 0
    rtl_agc: bool = True
    chunk_size: int = 65536
    rtl_sdr_binary: str = "rtl_sdr"
    info: SourceInfo = field(default_factory=lambda: SourceInfo(
        type="rtl_sdr",
        label="RTL-SDR",
        sample_rate=2_400_000,
    ))
    # Runtime gain channel for the tcp transport (slice-4.7) — consumed
    # between chunks by rtl_tcp_stream. USB applies gain directly on the
    # live device handle; subprocess has no runtime channel.
    _gain_q: asyncio.Queue[float | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=1), init=False, repr=False
    )
    # The transport instance spawned by the current/last spawn() — used to
    # dispatch set_runtime_gain.
    _active_transport: object = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.transport not in ("auto", "usb", "tcp", "subprocess"):
            raise ValueError(
                f"invalid transport {self.transport!r}; "
                "expected auto|usb|tcp|subprocess"
            )
        if self.direct_sampling not in (0, 1, 2):
            raise ValueError("direct_sampling must be 0, 1, or 2")

    def set_runtime_gain(self, gain_db: float | None) -> bool:
        """Dispatch a runtime gain change to the ACTIVE transport.

        usb → applied on the live device handle; tcp → queued for the
        stream loop; subprocess → not supported (restart the receiver to
        change gain). Returns False when it can't be honored.
        """
        t = self._active_transport
        if isinstance(t, _RtlUsbTransport):
            return t.set_runtime_gain(gain_db)
        if isinstance(t, _RtlTcpTransport):
            # Latest-wins: drop any stale request, then enqueue ours.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._gain_q.get_nowait()
            self._gain_q.put_nowait(None if gain_db is None else float(gain_db))
            return True
        return False

    # ------------------------------------------------------------------
    # Transport resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _usb_available() -> bool:
        lib = _load_librtlsdr()
        if lib is None:
            return False
        try:
            _configure_librtlsdr(lib)
            return int(lib.rtlsdr_get_device_count()) > 0
        except (AttributeError, OSError):
            return False

    async def _tcp_available(self, timeout_s: float = 0.3) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=timeout_s
            )
        except (TimeoutError, OSError):
            return False
        try:
            header = await asyncio.wait_for(reader.readexactly(12), timeout=timeout_s)
            return header[:4] == b"RTL0"
        except (TimeoutError, asyncio.IncompleteReadError):
            return False
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    def _subprocess_available(self) -> bool:
        return shutil.which(self.rtl_sdr_binary) is not None

    def _resolve_transport(self) -> str:
        if self.transport != "auto":
            return self.transport
        if self._usb_available():
            return "usb"
        # tcp/subprocess availability is async/expensive — checked in spawn().
        return "auto"

    # ------------------------------------------------------------------
    # Source protocol
    # ------------------------------------------------------------------

    async def spawn(
        self,
        center_freq: int,
        sample_rate: int,
        gain: float | None = None,
    ) -> AsyncGenerator[np.ndarray, None]:
        """Stream complex64 IQ from the first available transport."""
        transport = self._resolve_transport()
        if transport == "auto":
            if await self._tcp_available():
                transport = "tcp"
            elif self._subprocess_available():
                transport = "subprocess"
            else:
                raise RuntimeError(
                    "no RTL-SDR transport available: no USB device "
                    "(librtlsdr), no rtl_tcp server, no rtl_sdr binary. "
                    "Plug in a stick, start rtl_tcp, or install rtl-sdr."
                )

        object.__setattr__(
            self,
            "info",
            SourceInfo(
                type="rtl_sdr",
                label=f"RTL-SDR ({transport})",
                endpoint=(
                    f"{self.host}:{self.port}" if transport == "tcp"
                    else f"usb:{self.device_index}" if transport == "usb"
                    else "subprocess"
                ),
                sample_rate=sample_rate,
            ),
        )
        log.info(
            "RtlSdrSource spawning",
            transport=transport,
            center_freq=center_freq,
            sample_rate=sample_rate,
            gain=gain,
            direct_sampling=self.direct_sampling,
        )

        if transport == "usb":
            lib = _load_librtlsdr()
            if lib is None:
                raise RuntimeError("librtlsdr not loadable (usb transport)")
            _configure_librtlsdr(lib)
            if int(lib.rtlsdr_get_device_count()) <= self.device_index:
                raise RuntimeError(
                    f"RTL-SDR device index {self.device_index} not present "
                    f"({int(lib.rtlsdr_get_device_count())} device(s) found)"
                )
            t: Any = _RtlUsbTransport(self, lib)
        elif transport == "tcp":
            t = _RtlTcpTransport(self)
        elif transport == "subprocess":
            t = _RtlSubprocessTransport(self)
        else:  # pragma: no cover — guarded by __post_init__
            raise ValueError(f"invalid transport {transport!r}")
        self._active_transport = t

        async for chunk in t.stream(center_freq, sample_rate, gain):
            yield chunk

    async def close(self) -> None:
        return None
