"""SDRplay RSP source backend — real driver (slice-3).

Replaces the slice-2 stub. Talks to the SDRplay **API v3** shared library
(``libmirsdrapi-rsp.so``) through cffi in ABI mode. SDRplay is the most
involved of the three native drivers because the API is callback-based:
``mir_sdr_StreamInit`` registers a stream callback that fires from the
library's own thread with separate int16 I/Q arrays; we interleave them and
push through an :class:`AsyncIqBridge` (drop-oldest under backpressure).

⚠ **API-shape note:** the cdef below was written against the 3.07 header
(``mirsdrapi-rsp.h``). The bound surface is deliberately minimal
(GetDevices / SelectDevice / StreamInit / StreamUninit / SetRf / SetFs /
GainChangeRequest / AgcControl / ReleaseDeviceIdx). On first hardware
bring-up, diff the cdef against the installed header — mismatches surface
as garbage values or crashes, so the driver logs the API version at open.

Gain semantics: SDRplay uses *gain reduction* (gRdB, higher = less gain,
20–59) plus an LNA state (0–8, model-dependent). The Source protocol's
positive ``gain`` (0–39) is inverted to ``gRdB = 59 - gain``; pass the
explicit ``grdb``/``lna_state`` fields for direct control.

RSPduo note: the Duo's two tuners are the natural hardware anchor for
ADR-005 VFO sub-receivers. Master/slave operation uses
``mir_sdr_rspduo_*`` calls not bound here yet — tracked in ADR-005 as the
hardware-VFO fast path.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import structlog

from ._hw_common import AsyncIqBridge, cs16_to_cf32
from .base import SourceInfo

log = structlog.get_logger(__name__)

_LIBMIRSDR_CANDIDATES = (
    "libmirsdrapi-rsp.so",
    "/usr/local/lib/libmirsdrapi-rsp.so",
    "/usr/lib/libmirsdrapi-rsp.so",
)

# mir_sdr_Bw_MHzT values are the kHz bandwidth enum (v3 header).
_BW_KHZ = (200, 300, 600, 1536, 5000, 6000, 7000, 8000)

_API_CDEF = r"""
typedef int mir_sdr_ErrT;

typedef struct {
    char *SerNo;
    char *RspDuoMode;
    char *hwVer;
    int devNum;
} mir_sdr_DeviceT;

typedef struct {
    int devNum;
    int bwType;
} mir_sdr_DeviceInitT;

typedef void (*mir_sdr_StreamCallback_t)(
    int16_t *xi, int16_t *xq, uint32_t firstSampleNum,
    int32_t grChanged, int32_t rfChanged, int32_t fsChanged,
    uint32_t numSamples, uint32_t hwRemoved, void *cbContext);
typedef void (*mir_sdr_GainChangeCallback_t)(
    uint32_t gRdB, uint32_t lnaGRdB, void *cbContext);

int mir_sdr_GetDevices(mir_sdr_DeviceT *devices, uint32_t *numDevs, uint32_t maxDevs);
int mir_sdr_SelectDevice(mir_sdr_DeviceInitT *devSel);
int mir_sdr_ReleaseDeviceIdx(void);
int mir_sdr_StreamInit(
    int32_t *gRdB, uint32_t fsHz, uint32_t rfHz,
    int bwType, int ifType, int LNAstate, int32_t *gRdBsystem,
    int setGrMode, int *samplesPerPacket,
    mir_sdr_StreamCallback_t streamCb, mir_sdr_GainChangeCallback_t gainCb,
    void *cbContext);
int mir_sdr_StreamUninit(void);
int mir_sdr_SetRf(uint32_t rfHz);
int mir_sdr_SetFs(uint32_t fsHz);
int mir_sdr_GainChangeRequest(int32_t gRdB, int LNAstate);
int mir_sdr_AgcControl(uint32_t disable, int setPoint_dBfs, uint32_t k,
    uint32_t decay, uint32_t fade, uint32_t trackAdjacentMode, uint32_t syncUpdate);
int mir_sdr_DebugEnable(int gpo1, int gpo2);
"""


def _pick_bandwidth(sample_rate: int) -> int:
    """Nearest supported bandwidth enum (kHz) that is >= 1.2 × the rate."""
    needed = sample_rate * 1.2 / 1000.0
    for bw in _BW_KHZ:
        if bw >= needed:
            return bw
    return _BW_KHZ[-1]


class SdrplayBinding:
    """cffi (ABI-mode) wrapper around libmirsdrapi-rsp (API v3).

    Seamed like :class:`~openwebrx_plus.sources.airspy.AirspyBinding`:
    tests inject a fake implementing the same surface. The stream callback
    is normalized to ``callback(xi: np.ndarray, xq: np.ndarray)``.
    """

    def __init__(self, lib_path: str | None = None) -> None:
        try:
            import cffi
        except ImportError as exc:  # pragma: no cover — cffi is a dep
            raise RuntimeError("cffi is required for the SDRplay driver") from exc
        self._ffi = cffi.FFI()
        self._ffi.cdef(_API_CDEF)
        paths: tuple[str, ...] = (lib_path,) if lib_path else _LIBMIRSDR_CANDIDATES
        self._lib: Any = None
        for candidate in paths:
            try:
                self._lib = self._ffi.dlopen(candidate)
                break
            except OSError:
                continue
        if self._lib is None:
            raise RuntimeError(
                "libmirsdrapi-rsp.so not found. Install the SDRplay API "
                "(https://www.sdrplay.com/downloads/) — API v3.x."
            )
        # Callback trampolines — keep references alive for the lib's lifetime.
        self._stream_cb: Any = None
        self._gain_cb: Any = None

    # -- discovery ----------------------------------------------------------

    def list_devices(self) -> list[dict[str, Any]]:
        """Enumerate RSP devices: [{serial, hw_ver, dev_num, duo_mode}]."""
        ffi, lib = self._ffi, self._lib
        max_devs = 8
        devices = ffi.new("mir_sdr_DeviceT[]", max_devs)
        num = ffi.new("uint32_t *")
        rc = lib.mir_sdr_GetDevices(devices, num, max_devs)
        if rc != 0:
            raise RuntimeError(f"mir_sdr_GetDevices failed rc={rc}")
        out: list[dict[str, Any]] = []
        for i in range(num[0]):
            d = devices[i]
            out.append(
                {
                    "serial": self._opt_str(d.SerNo),
                    "duo_mode": self._opt_str(d.RspDuoMode),
                    "hw_ver": self._opt_str(d.hwVer),
                    "dev_num": int(d.devNum),
                }
            )
        return out

    def _opt_str(self, cchar_p: Any) -> str | None:
        if not cchar_p:
            return None
        value: str = self._ffi.string(cchar_p).decode("utf-8", errors="replace")
        return value

    # -- lifecycle / streaming ------------------------------------------------

    def select_device(self, dev_num: int) -> None:
        ffi, lib = self._ffi, self._lib
        sel = ffi.new("mir_sdr_DeviceInitT *")
        sel.devNum = dev_num
        sel.bwType = 0  # mir_sdr_BW_Undefined — bandwidth set at StreamInit
        rc = lib.mir_sdr_SelectDevice(sel)
        if rc != 0:
            raise RuntimeError(f"mir_sdr_SelectDevice(devNum={dev_num}) failed rc={rc}")

    def release_device(self) -> None:
        self._lib.mir_sdr_ReleaseDeviceIdx()

    def stream_init(
        self,
        *,
        sample_rate: int,
        center_freq: int,
        bandwidth_khz: int,
        lna_state: int,
        grdb: int,
        agc: bool,
        callback: Callable[[np.ndarray, np.ndarray], None],
    ) -> int:
        """Start streaming. ``callback(xi_int16, xq_int16)`` runs on the
        library's callback thread. Returns samples-per-packet."""
        ffi, lib = self._ffi, self._lib

        @ffi.callback("mir_sdr_StreamCallback_t")
        def _stream_cb(xi: Any, xq: Any, _first: int, _gr: int, _rf: int, _fs: int,
                       num_samples: int, hw_removed: int, _ctx: Any) -> None:
            if hw_removed:
                return
            n = int(num_samples)
            xi_arr = np.frombuffer(
                ffi.buffer(xi, n * 2), dtype=np.int16
            )
            xq_arr = np.frombuffer(
                ffi.buffer(xq, n * 2), dtype=np.int16
            )
            callback(xi_arr, xq_arr)

        @ffi.callback("mir_sdr_GainChangeCallback_t")
        def _gain_cb(_grdb: int, _lna: int, _ctx: Any) -> None:
            pass  # informational only

        self._stream_cb = _stream_cb
        self._gain_cb = _gain_cb

        grdb_c = ffi.new("int32_t *", int(grdb))
        grdb_sys = ffi.new("int32_t *")
        spp = ffi.new("int *")
        rc = lib.mir_sdr_StreamInit(
            grdb_c,
            int(sample_rate),
            int(center_freq),
            int(bandwidth_khz),
            0,  # mir_sdr_IF_ZERO — baseband/zero-IF
            int(lna_state),
            grdb_sys,
            1,  # mir_sdr_USE_SET_GR_ALT_MODE (RSP1A+; RSP1 may need 0)
            spp,
            _stream_cb,
            _gain_cb,
            ffi.NULL,
        )
        if rc != 0:
            self._stream_cb = None
            self._gain_cb = None
            raise RuntimeError(f"mir_sdr_StreamInit failed rc={rc}")
        # Keep the effective gRdB the API chose (post ALT-mode negotiation).
        self.applied_grdb = int(grdb_c[0])
        return int(spp[0])

    def stream_uninit(self) -> None:
        rc = self._lib.mir_sdr_StreamUninit()
        self._stream_cb = None
        self._gain_cb = None
        if rc != 0:
            raise RuntimeError(f"mir_sdr_StreamUninit failed rc={rc}")

    def set_rf(self, freq_hz: int) -> None:
        rc = self._lib.mir_sdr_SetRf(int(freq_hz))
        if rc != 0:
            raise RuntimeError(f"mir_sdr_SetRf failed rc={rc}")

    def set_fs(self, rate: int) -> None:
        rc = self._lib.mir_sdr_SetFs(int(rate))
        if rc != 0:
            raise RuntimeError(f"mir_sdr_SetFs failed rc={rc}")

    def gain_change_request(self, grdb: int, lna_state: int) -> None:
        rc = self._lib.mir_sdr_GainChangeRequest(int(grdb), int(lna_state))
        if rc != 0:
            raise RuntimeError(f"mir_sdr_GainChangeRequest failed rc={rc}")

    def agc_control(self, enable: bool) -> None:
        # Power-linear AGC defaults per the API; setpoint −30 dBFS.
        rc = self._lib.mir_sdr_AgcControl(
            0 if enable else 1, -30, 20, 20, 0, 0, 0
        )
        if rc != 0:
            raise RuntimeError(f"mir_sdr_AgcControl failed rc={rc}")


@dataclass
class SDRplaySource:
    """SDRplay RSP source (real driver — replaces the slice-2 stub).

    Args:
        serial: device serial substring/None for first device.
        antenna: "a" | "b" | "c" (RSP2/RSPdx multi-antenna ports). Recorded
            and reported; per-model antenna switching is bound at hardware
            bring-up (uses mir_sdr_RSP* calls that vary per model).
        grdb: baseband gain reduction 20–59 (higher = less gain). None →
            derived from the protocol ``gain`` (59 − gain).
        lna_state: 0–8, model-dependent (0 = most gain).
        agc: enable SDRplay AGC (overrides manual grdb when on).
        chunk_size: complex samples per yielded chunk.
        binding: inject an SdrplayBinding-compatible object (tests).
    """

    serial: str | None = None
    antenna: str = "a"
    grdb: int | None = None
    lna_state: int = 3
    agc: bool = False
    chunk_size: int = 65536
    binding: Any | None = None
    info: SourceInfo = field(default_factory=lambda: SourceInfo(
        type="sdrplay",
        label="SDRplay RSP",
        sample_rate=2_000_000,
    ))

    def __post_init__(self) -> None:
        if self.antenna not in ("a", "b", "c"):
            raise ValueError(f"invalid antenna {self.antenna!r}; expected a|b|c")
        # Slice-6.5: runtime-gain handle storage. Set in spawn(), cleared
        # in the finally block. SDRplay uses a SINGLE binding instance
        # (not a per-device handle like Airspy/Soapy) — we stash the
        # binding itself so set_runtime_gain() can call its methods.
        self._binding_inst: Any = None

    def _make_binding(self) -> Any:
        if self.binding is not None:
            return self.binding
        return SdrplayBinding()

    async def spawn(
        self,
        center_freq: int,
        sample_rate: int,
        gain: float | None = None,
    ) -> AsyncGenerator[np.ndarray, None]:
        binding = self._make_binding()
        devices = binding.list_devices()
        if not devices:
            raise RuntimeError("no SDRplay RSP devices found (mir_sdr_GetDevices)")

        chosen = devices[0]
        if self.serial is not None:
            matches = [
                d for d in devices
                if d.get("serial") and self.serial in str(d["serial"])
            ]
            if not matches:
                raise RuntimeError(
                    f"SDRplay serial matching {self.serial!r} not found; "
                    f"devices: {[d.get('serial') for d in devices]}"
                )
            chosen = matches[0]

        grdb = self.grdb if self.grdb is not None else (
            59 - int(gain) if gain is not None else 50
        )
        grdb = max(20, min(59, grdb))
        bw_khz = _pick_bandwidth(sample_rate)

        bridge = AsyncIqBridge(max_blocks=32)
        running = threading.Event()

        def on_samples(xi: np.ndarray, xq: np.ndarray) -> None:
            interleaved = np.empty(xi.size * 2, dtype=np.int16)
            interleaved[0::2] = xi
            interleaved[1::2] = xq
            bridge.push(interleaved)

        binding.select_device(chosen["dev_num"])
        # Slice-6.5: stash the binding for set_runtime_gain() (the
        # SDRplay gain API is gain_change_request(grdb, lna_state) +
        # agc_control(enable); both safe concurrent with the stream
        # callback per the SDRplay API contract).
        self._binding_inst = binding
        try:
            if self.agc:
                binding.agc_control(True)
            # bind() BEFORE stream_init: callbacks start firing immediately
            # and need the loop reference for thread-safe wakeups.
            bridge.bind()
            spp = binding.stream_init(
                sample_rate=sample_rate,
                center_freq=center_freq,
                bandwidth_khz=bw_khz,
                lna_state=self.lna_state,
                grdb=grdb,
                agc=self.agc,
                callback=on_samples,
            )
            running.set()
            object.__setattr__(
                self,
                "info",
                SourceInfo(
                    type="sdrplay",
                    label=(
                        f"RSP {chosen.get('hw_ver') or ''} "
                        f"{chosen.get('serial') or ''}".strip()
                    ),
                    endpoint=str(chosen.get("serial")),
                    sample_rate=sample_rate,
                ),
            )
            log.info(
                "SDRplaySource streaming",
                serial=chosen.get("serial"),
                center_freq=center_freq,
                sample_rate=sample_rate,
                bandwidth_khz=bw_khz,
                grdb=grdb,
                lna_state=self.lna_state,
                samples_per_packet=spp,
                antenna=self.antenna,
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
                    binding.stream_uninit()
                except Exception:  # noqa: BLE001 — teardown best-effort
                    log.debug("sdrplay stream_uninit raised", exc_info=True)
            binding.release_device()
            # Slice-6.5: clear the runtime-gain handle so a post-close
            # set_runtime_gain() returns False.
            self._binding_inst = None

    async def close(self) -> None:
        return None

    def set_runtime_gain(self, gain_db: float | None) -> bool:
        """Apply a gain change while streaming (slice-6.5 RuntimeGainSource).

        - ``gain_db`` numeric: mapped to SDRplay's ``grdb`` (gain reduction
          dB) via the standard convention ``grdb = 59 - gain_db`` (clamped
          to the supported 20-59 range). LNA state stays at the spawn-time
          value — adjusting LNA at runtime requires hardware-specific
          heuristics that belong in a future slice.
        - ``None``: enable SDRplay's AGC (its hardware auto-gain).

        Returns True when applied; False when the binding isn't live
        (between close and respawn, or never opened). Safe to call from
        any asyncio task while spawn() is being consumed (the gain_change
        API is non-blocking and safe concurrent with the stream callback).
        """
        binding = self._binding_inst
        if binding is None:
            return False
        try:
            if gain_db is None:
                binding.agc_control(True)
                return True
            binding.agc_control(False)
            grdb = max(20, min(59, 59 - int(round(gain_db))))
            binding.gain_change_request(grdb, self.lna_state)
            return True
        except Exception:  # noqa: BLE001
            log.debug("sdrplay runtime gain failed", exc_info=True)
            return False
