"""Airspy source backend — real driver (slice-3).

Replaces the slice-1/2 stub. Talks to **libairspy** through a thin ctypes
binding (:class:`AirspyBinding`) that is deliberately seamed: tests inject
:class:`~tests.test_airspy_driver.FakeAirspyBinding` implementing the same
narrow interface, so the driver logic is fully tested without hardware.

Device facts baked into the defaults:
  * Airspy R2 / Mini: 24–1700 MHz, 2.5–20 MSPS (R2: 2.5/3/6/9/10), 12-bit ADC.
  * Airspy HF+ Discovery: 9 kHz–31 MHz (60 MHz extended), ≤ 660 kSPS in
    "classic" HF mode. Different sample-rate set — query via the API.
  * Three gain stages (LNA/Mixer/VGA) OR Airspy's composite gain modes:
    ``linearity`` and ``sensitivity`` (each 0–21). This driver exposes both.

The Source protocol's single ``gain`` float maps to the composite
``linearity`` gain by default; set ``gain_mode="manual"`` + the three stage
values for full control.
"""

from __future__ import annotations

import ctypes
import threading
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import structlog

from ._hw_common import AsyncIqBridge, cs16_to_cf32
from .base import SourceInfo

log = structlog.get_logger(__name__)

_LIBAIRSPY_CANDIDATES = (
    "libairspy.so.2",
    "libairspy.so.1",
    "libairspy.so.0",
    "libairspy.so",
    "libairspy.dylib",
)

# airspy_sample_type enum values (airspy.h)
_SAMPLE_INT16_IQ = 1

# airspy_transfer_t layout (airspy.h):
#   struct airspy_device* device;  void* ctx;  void* samples;
#   uint32_t sample_count;  uint8_t lost_samples;  enum sample_type (int)
_TRANSFER_FIELDS = [
    ("device", ctypes.c_void_p),
    ("ctx", ctypes.c_void_p),
    ("samples", ctypes.c_void_p),
    ("sample_count", ctypes.c_uint32),
    ("lost_samples", ctypes.c_uint8),
    ("sample_type", ctypes.c_int),
]

# void (*)(airspy_transfer_t*)
_AIRSPY_RX_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p)


class _AirspyTransfer(ctypes.Structure):
    """Mirror of airspy_transfer_t (airspy.h)."""

    _fields_ = _TRANSFER_FIELDS  # type: ignore[assignment]


def _load_libairspy() -> ctypes.CDLL | None:
    for name in _LIBAIRSPY_CANDIDATES:
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


class AirspyBinding:
    """ctypes wrapper around libairspy.

    The public surface is intentionally tiny (open / configure / start /
    stop / close) so a fake can impersonate it in tests. The stream
    callback is normalized to ``callback(samples: bytes)`` — raw
    interleaved int16 I/Q — and is invoked from libairspy's reader thread.
    """

    def __init__(self, lib: ctypes.CDLL | None = None) -> None:
        lib_obj = lib if lib is not None else _load_libairspy()
        if lib_obj is None:
            raise RuntimeError(
                "libairspy not found. Install the Airspy driver "
                "(apt install airspy / build from github.com/airspy/"
                "airspyone_host)."
            )
        self._lib = lib_obj
        self._configure(self._lib)
        rc = self._lib.airspy_init()
        if rc != 0:
            raise RuntimeError(f"airspy_init failed rc={rc}")
        self._transfer: type[ctypes.Structure] = _AirspyTransfer
        self._cb_ref: Any | None = None  # keep the CFUNCTYPE wrapper alive

    @staticmethod
    def _configure(lib: ctypes.CDLL) -> None:
        c_void = ctypes.c_void_p
        u32 = ctypes.c_uint32
        i32 = ctypes.c_int
        u8 = ctypes.c_uint8
        lib.airspy_init.restype = i32
        lib.airspy_init.argtypes = []
        lib.airspy_exit.restype = i32
        lib.airspy_exit.argtypes = []
        lib.airspy_list_devices.restype = i32
        lib.airspy_list_devices.argtypes = [ctypes.POINTER(ctypes.c_uint64), u32]
        lib.airspy_open_sn.restype = i32
        lib.airspy_open_sn.argtypes = [ctypes.POINTER(c_void), ctypes.c_uint64]
        lib.airspy_open.restype = i32
        lib.airspy_open.argtypes = [ctypes.POINTER(c_void)]
        lib.airspy_close.restype = i32
        lib.airspy_close.argtypes = [c_void]
        lib.airspy_set_sample_type.restype = i32
        lib.airspy_set_sample_type.argtypes = [c_void, i32]
        lib.airspy_set_samplerate.restype = i32
        lib.airspy_set_samplerate.argtypes = [c_void, u32]
        lib.airspy_set_freq.restype = i32
        lib.airspy_set_freq.argtypes = [c_void, u32]
        lib.airspy_set_lna_gain.restype = i32
        lib.airspy_set_lna_gain.argtypes = [c_void, u8]
        lib.airspy_set_mixer_gain.restype = i32
        lib.airspy_set_mixer_gain.argtypes = [c_void, u8]
        lib.airspy_set_vga_gain.restype = i32
        lib.airspy_set_vga_gain.argtypes = [c_void, u8]
        lib.airspy_set_linearity_gain.restype = i32
        lib.airspy_set_linearity_gain.argtypes = [c_void, u8]
        lib.airspy_set_sensitivity_gain.restype = i32
        lib.airspy_set_sensitivity_gain.argtypes = [c_void, u8]
        lib.airspy_set_lna_agc.restype = i32
        lib.airspy_set_lna_agc.argtypes = [c_void, u8]
        lib.airspy_set_mixer_agc.restype = i32
        lib.airspy_set_mixer_agc.argtypes = [c_void, u8]
        lib.airspy_set_rf_bias.restype = i32
        lib.airspy_set_rf_bias.argtypes = [c_void, u8]
        lib.airspy_start_rx.restype = i32
        lib.airspy_start_rx.argtypes = [c_void, _AIRSPY_RX_CB, c_void]
        lib.airspy_stop_rx.restype = i32
        lib.airspy_stop_rx.argtypes = [c_void]

    # -- device lifecycle -------------------------------------------------

    def list_serials(self) -> list[int]:
        """Serial numbers of connected Airspy devices."""
        buf = (ctypes.c_uint64 * 16)()
        n = self._lib.airspy_list_devices(buf, 16)
        return [int(buf[i]) for i in range(n) if buf[i] != 0]

    def open(self, serial: int | None = None) -> Any:
        """Open a device by serial (or the first one). Returns a handle."""
        dev = ctypes.c_void_p()
        if serial is None:
            rc = self._lib.airspy_open(ctypes.byref(dev))
        else:
            rc = self._lib.airspy_open_sn(ctypes.byref(dev), serial)
        if rc != 0:
            raise RuntimeError(f"airspy_open failed rc={rc}")
        return dev

    def close(self, dev: Any) -> None:
        self._lib.airspy_close(dev)

    # -- configuration ----------------------------------------------------

    def set_iq16(self, dev: Any) -> None:
        """Select the INT16_IQ sample type (what this driver consumes)."""
        rc = self._lib.airspy_set_sample_type(dev, _SAMPLE_INT16_IQ)
        if rc != 0:
            raise RuntimeError(f"airspy_set_sample_type failed rc={rc}")

    def set_samplerate(self, dev: Any, rate: int) -> None:
        rc = self._lib.airspy_set_samplerate(dev, rate)
        if rc != 0:
            raise RuntimeError(
                f"airspy_set_samplerate({rate}) failed rc={rc} — "
                "check the device's supported rates"
            )

    def set_freq(self, dev: Any, freq_hz: int) -> None:
        rc = self._lib.airspy_set_freq(dev, freq_hz)
        if rc != 0:
            raise RuntimeError(f"airspy_set_freq failed rc={rc}")

    def set_gains(
        self,
        dev: Any,
        *,
        gain_mode: str,
        linearity: int,
        sensitivity: int,
        lna: int,
        mixer: int,
        vga: int,
    ) -> None:
        if gain_mode == "linearity":
            self._lib.airspy_set_linearity_gain(dev, linearity)
        elif gain_mode == "sensitivity":
            self._lib.airspy_set_sensitivity_gain(dev, sensitivity)
        elif gain_mode == "manual":
            self._lib.airspy_set_lna_gain(dev, lna)
            self._lib.airspy_set_mixer_gain(dev, mixer)
            self._lib.airspy_set_vga_gain(dev, vga)
        else:  # pragma: no cover — validated in the source dataclass
            raise ValueError(f"invalid gain_mode {gain_mode!r}")

    def set_rf_bias(self, dev: Any, on: bool) -> None:
        rc = self._lib.airspy_set_rf_bias(dev, 1 if on else 0)
        if rc != 0:
            raise RuntimeError(f"airspy_set_rf_bias failed rc={rc}")

    # -- streaming ---------------------------------------------------------

    def start_rx(self, dev: Any, callback: Callable[[bytes], None]) -> None:
        """Start streaming; ``callback(raw_cs16_bytes)`` runs on the USB thread."""
        transfer_cls = self._transfer
        cb_wrapper = _AIRSPY_RX_CB(
            lambda t_ptr: self._dispatch(t_ptr, transfer_cls, callback)
        )
        self._cb_ref = cb_wrapper  # prevent GC while streaming
        rc = self._lib.airspy_start_rx(dev, cb_wrapper, None)
        if rc != 0:
            self._cb_ref = None
            raise RuntimeError(f"airspy_start_rx failed rc={rc}")

    def stop_rx(self, dev: Any) -> None:
        self._lib.airspy_stop_rx(dev)
        self._cb_ref = None

    def _dispatch(
        self,
        t_ptr: int,
        transfer_cls: type[ctypes.Structure],
        callback: Callable[[bytes], None],
    ) -> None:
        """Unpack airspy_transfer_t and hand raw samples to the callback."""
        tr = ctypes.cast(t_ptr, ctypes.POINTER(transfer_cls)).contents
        count = int(tr.sample_count)  # complex sample count for IQ types
        n_bytes = count * 4  # int16 I+Q
        buf = (ctypes.c_char * n_bytes).from_address(tr.samples)
        callback(bytes(buf))


@dataclass
class AirspySource:
    """Airspy source (real driver — replaces the slice-1/2 stub).

    Args:
        serial_number: device serial (int) or None for the first device.
        gain_mode: "linearity" (default) | "sensitivity" | "manual".
        linearity_gain / sensitivity_gain: composite gains, 0–21.
        lna_gain / mixer_gain / vga_gain: manual stage gains, 0–15/15/15.
        bias_tee: 4.5 V bias tee for powered antennas/LNAs.
        chunk_size: complex samples per yielded chunk.
        binding: inject an AirspyBinding-compatible object (tests).
    """

    serial_number: int | str | None = None
    gain_mode: str = "linearity"
    linearity_gain: int = 10
    sensitivity_gain: int = 10
    lna_gain: int = 8
    mixer_gain: int = 8
    vga_gain: int = 6
    bias_tee: bool = False
    chunk_size: int = 65536
    binding: Any | None = None
    info: SourceInfo = field(default_factory=lambda: SourceInfo(
        type="airspy",
        label="Airspy",
        sample_rate=10_000_000,
    ))

    def __post_init__(self) -> None:
        if self.gain_mode not in ("linearity", "sensitivity", "manual"):
            raise ValueError(
                f"invalid gain_mode {self.gain_mode!r}; "
                "expected linearity|sensitivity|manual"
            )
        if isinstance(self.serial_number, str) and self.serial_number.isdigit():
            self.serial_number = int(self.serial_number)

    def _make_binding(self) -> Any:
        if self.binding is not None:
            return self.binding
        return AirspyBinding()

    async def spawn(
        self,
        center_freq: int,
        sample_rate: int,
        gain: float | None = None,
    ) -> AsyncGenerator[np.ndarray, None]:
        binding = self._make_binding()
        serials = binding.list_serials()
        if not serials:
            raise RuntimeError("no Airspy devices found (airspy_list_devices empty)")
        serial = None
        if self.serial_number is not None:
            serial = int(self.serial_number)
            if serial not in serials:
                raise RuntimeError(
                    f"Airspy serial {serial} not present; found {serials}"
                )
        dev = binding.open(serial)
        bridge = AsyncIqBridge(max_blocks=32)
        running = threading.Event()

        def on_samples(raw: bytes) -> None:
            bridge.push(np.frombuffer(raw, dtype=np.int16).copy())

        try:
            binding.set_iq16(dev)
            binding.set_samplerate(dev, sample_rate)
            binding.set_freq(dev, center_freq)
            if gain is not None:
                # Protocol gain (dB-ish composite) overrides linearity_gain.
                binding.set_gains(
                    dev,
                    gain_mode="linearity" if self.gain_mode != "manual" else "manual",
                    linearity=int(round(gain)),
                    sensitivity=int(round(gain)),
                    lna=self.lna_gain,
                    mixer=self.mixer_gain,
                    vga=self.vga_gain,
                )
            else:
                binding.set_gains(
                    dev,
                    gain_mode=self.gain_mode,
                    linearity=self.linearity_gain,
                    sensitivity=self.sensitivity_gain,
                    lna=self.lna_gain,
                    mixer=self.mixer_gain,
                    vga=self.vga_gain,
                )
            if self.bias_tee:
                binding.set_rf_bias(dev, True)

            bridge.bind()
            running.set()
            binding.start_rx(dev, on_samples)
            object.__setattr__(
                self,
                "info",
                SourceInfo(
                    type="airspy",
                    label=f"Airspy {hex(serial) if serial is not None else '(first)'}",
                    endpoint=str(serial) if serial is not None else "usb:first",
                    sample_rate=sample_rate,
                ),
            )
            log.info(
                "AirspySource streaming",
                serial=serial,
                center_freq=center_freq,
                sample_rate=sample_rate,
                gain_mode=self.gain_mode,
            )
            buffer = bytearray()
            need = self.chunk_size * 4  # int16 I+Q bytes per complex sample
            async for raw in bridge.stream():
                buffer += raw.tobytes()
                while len(buffer) >= need:
                    block = bytes(buffer[:need])
                    del buffer[:need]
                    yield cs16_to_cf32(np.frombuffer(block, dtype=np.int16))
        finally:
            bridge.close()
            if running.is_set():
                try:
                    binding.stop_rx(dev)
                except Exception:  # noqa: BLE001 — teardown best-effort
                    log.debug("airspy stop_rx raised", exc_info=True)
            binding.close(dev)

    async def close(self) -> None:
        return None
