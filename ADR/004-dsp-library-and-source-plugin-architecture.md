# ADR-004: DSP Library (pycsdr) + SDR Source Plugin Architecture

**Status:** Accepted — **pycsdr installed & wired (slice-3)**
**Date:** 2026-08-26
**Related:** ADR-001 (multi-receiver workspace), ADR-002 (DSP+AI cascade), ADR-003 (decoder plugins), Pillar 1 (DSP+AI Cascade), Pillar 4 (Plugin Engine)

## Implementation status (2026-08-26)

- ✅ libcsdr 0.19.0 built from `jketterl/csdr@develop` (patched CMakeLists —
  upstream doesn't propagate SAMPLERATE include/link dirs; see
  `scripts/README-dsp-bootstrap.md`).
- ✅ pycsdr 0.19.0-dev built against it; importable as `pycsdr.modules`.
- ✅ `apps/server/openwebrx_plus/dsp/` created: `FftChain`
  (Fft → LogAveragePower → FftSwap) and `AudioChain`
  (Shift → FirDecimate → Bandpass → demod → DcBlock/WfmDeemphasis →
  AudioResampler → Limit → Convert), both push-in / drain-out with
  background reader threads.
- ✅ `ReceiverSession` now feeds both chains and packs the same binary
  wire formats as before (numpy stubs removed).
- ✅ `pyproject.toml` declares `pycsdr @ git+...@develop` as a runtime dep.
- ✅ 33/33 server tests pass; DSP smoke tests in `scripts/` verify FFT
  peak-bin correctness and AM/USB/LSB/NFM demodulation end-to-end.

### pycsdr gotchas learned (documented for future maintainers)

1. **`canProcess()` is strictly-greater-than.** Every module needs
   `available > length` — exactly n×fft_size input yields fewer than n
   frames. Feed loops must not assume 1:1 accounting.
2. **Ring buffers overwrite; no reader backpressure.**
   `Ringbuffer::writeable()` returns the constant `size - 1`. A write only
   fails if a single chunk ≥ ring size; otherwise slow consumers get
   silently overwritten. Buffers must be sized ≥ largest burst; the
   source must pace at real-time rate (production sources do).
3. **`Fft` drops sub-window data.** Its `every_n_samples` skip logic
   discards chunks smaller than the window when the AsyncRunner runs
   between writes. `FftChain.feed()` stages chunks < 2×fft_size in Python
   before writing the ring.
4. **`BufferReader.read()` blocks** until data is available (and returns
   `None` after `stop()`). Both chains use dedicated reader threads so the
   asyncio event loop never stalls.
5. **Decimate before demodulating.** libsamplerate's sinc resampler runs
   at only ~80 Ksps at 30:1 ratios — slower than real-time for a 240 kHz
   channel. `AudioChain` inserts `FirDecimate` (input_rate → channel_rate)
   before the demod so the audio resampler sees a small ratio. This
   mirrors upstream OpenWebRX's csdr recipe and restores ~11 Msps
   end-to-end throughput.
6. **FirDecimate's `cutoff` is output-normalized** (it divides by
   `decimation` internally), and `transition` sets the tap count as
   `4/transition` at the input rate. `AudioChain` computes both from the
   mode's channel bandwidth.
7. **`Shift(rate)` moves the spectrum UP by `rate×fs`** (mixer
   `exp(+2πi·rate·n)` — verified empirically in slice-3.5, *not* from any
   doc). To bring a slice at `+offset` to DC use `rate = −offset/fs`.
   `ShiftAddfast` is also a `FixedLengthModule` (exact 1024-sample blocks;
   runner loops automatically). The original VFO tap got the sign
   backwards: the −30 kHz carrier moved to −60 kHz, the anti-alias filter
   killed it (−70 dB), and the *alias* of −60 kHz at the 12 kSPS output
   landed at exactly 0 Hz — frequency-only assertions read it as success.
   `AudioChain`'s `channel_offset_hz` path carried the same latent
   inversion (never exercised — always constructed with offset 0) and was
   fixed in the same pass. **Lesson: spectral tests must assert amplitude,
   not just peak frequency.**

## Implementation status addendum (slice-3.5, 2026-08-26 — drivers & VFOs)

- ✅ Real drivers replace all stubs: `rtl_sdr` (USB via ctypes/librtlsdr,
  rtl_tcp native-asyncio, `rtl_sdr` CLI subprocess — auto-probed),
  `airspy` (ctypes/libairspy, linearity/sensitivity/manual gains, bias
  tee), `sdrplay` (cffi, API v3 stream callbacks, gain-reduction model —
  cdef written against the 3.07 header, verify on first hardware
  bring-up), `soapy` (universal SoapySDR transport).
- ✅ All driver logic is unit-tested without hardware: a fake rtl_tcp
  server (real sockets, command recorder), fake CLI executable, and fake
  bindings for airspy/sdrplay/soapy.
- ✅ Hardware detection sweep (`sources/probe.py` → `GET /api/hardware`):
  every driver probed concurrently; a missing SDK contributes nothing and
  never fails the sweep.
- ✅ IQ fixtures baked (deterministic, numpy-only generator —
  `scripts/generate_iq_fixtures.py`): 20 m evening scene (CW/SSB/FT8-like/
  AM/QRN/fading), FM broadcast band (stereo pilots + RDS-like
  subcarriers), **ADS-B with valid CRC-24 Mode S frames** (dump1090-
  decodable — verified by an independent PPM decoder in the test suite).
- ✅ FileSource replays real-time paced (`RealtimePacer`) and loops — the
  dev-default session is the 20 m fixture; `SimulatedSource` is paced
  too (ADR-004 gotcha #2 made honest).
- ✅ VFO sub-receivers per ADR-005 (IqHub fan-out + pycsdr DDC taps).
- ✅ Session timing fixed: `fft_fps` throttles the FFT *broadcast* only;
  audio frames are never dropped; consumption runs at source rate.
- ✅ 103/103 server tests green.

## Context

Two questions were raised during slice-1/2 review that needed locking in before slice-3:

1. **DSP library choice — `scipy` vs `pycsdr`?**
2. **SDR plugin architecture — how do we support any SDR with a driver, without baking hardware specifics into the backend?**

Both decisions shape what goes in `apps/server/pyproject.toml`, the source backend layout, and the source contract for the next 3 slices.

---

## Decision 1: `pycsdr` is the primary DSP library. `scipy` is an offline utility only.

### Reasoning

| Criterion | `pycsdr` | `scipy.signal` |
|---|---|---|
| Designed for streaming IQ | ✓ (block-graph flow) | ✗ (batch numpy) |
| C performance in the hot path | ✓ (`csdr` is autovectorized SIMD) | ✗ (Python overhead per call) |
| SDR-specific blocks (AM/FM/SSB/CW demods, AGC, squelch, noise blanker, DC block, CTCSS) | ✓ shipped | ✗ reinvent from scratch |
| Live FFT optimized for waterfall | ✓ | △ (works, slower) |
| Filter design (FIR/IIR coefficient generation) | △ | ✓ superior — use scipy here |
| Offline analysis of recorded IQ | △ | ✓ superior — use scipy here |
| Upstream OpenWebRX+ compatibility | ✓ same library | ✗ would diverge from upstream |

**Conclusion:** `pycsdr` is the primary DSP library — it is what the upstream project uses, what our 27 source backends already speak, and what `ReceiverSession.dsp_chain` will wrap. `scipy` is added as a **dev-only** dependency for filter design, recorded-IQ analysis, unit-test fixtures, and statistical work. `scipy` is forbidden in the live IQ path.

### Dependency declaration

```toml
# pyproject.toml (apps/server)
[project]
dependencies = [
    # ... existing ...
    # pycsdr is not on PyPI — declared as a PEP 508 direct reference to
    # the upstream repo (default branch: develop). libcsdr must be built
    # and installed FIRST; see scripts/README-dsp-bootstrap.md.
    "pycsdr @ git+https://github.com/jketterl/pycsdr.git@develop",
]

[dependency-groups]
dev = [
    # ... existing ...
    "scipy>=1.14.0",  # OFFLINE ONLY: filter design, recorded-IQ analysis, fixtures
]
```

A CI lint rule (custom ruff plugin, slice-3) will reject `import scipy` outside of `tests/`, `scripts/`, and `apps/server/openwebrx_plus/offline/`.

### Boundary rule

- `pycsdr` blocks live in `apps/server/openwebrx_plus/dsp/` — the live chain.
- `scipy` usage lives in `apps/server/openwebrx_plus/offline/` — filter design, IQ analysis, fixture generation. Never imported by `dsp/`, `sources/`, `sessions/`, or `api/`.

---

## Decision 2: SDR source plugin architecture — "source connector" pattern, formalized as a `SourceRegistry`.

### The pattern (inherited from upstream OpenWebRX)

Upstream OpenWebRX already uses "source connectors" — small C/Rust binaries that wrap a vendor SDK and stream raw IQ to the backend. We inherit and formalize this as a Python-side plugin contract so that:

- The backend never knows about specific hardware.
- Any SDR with a driver can be added by writing one small `Source` implementation + a TOML manifest.
- Discovery is automatic (filesystem scan + entry-point registration).
- No backend code change is required to add a new SDR.

### The contract

Every source plugin implements the `Source` Protocol (already defined in `sources/base.py`):

```python
@runtime_checkable
class Source(Protocol):
    info: SourceInfo
    async def spawn(
        self,
        center_freq: int,
        sample_rate: int,
        gain: float | None = None,
    ) -> AsyncIterator[np.ndarray]:  # complex64 IQ
        ...
    async def close(self) -> None: ...
```

Plus a declarative `SourceManifest`:

```python
@dataclass(frozen=True)
class SourceManifest:
    source_type: str           # e.g. "rtl_sdr", "airspy", "sdrplay", "file", "simulated"
    label: str                 # human-readable
    sdk: str                   # "librtlsdr", "libairspy", "sdrplay_api", "none"
    hardware_required: bool    # False for file_source / simulated_source
    default_sample_rate: int    # Hz
    sample_rate_range: tuple[int, int]  # min, max
    gain_range: tuple[float, float] | None
    supports_bias_tee: bool = False
    supports_agc: bool = False
    factory_entrypoint: str    # "openwebrx_plus.sources:RtlSdrSource"
```

### Discovery

Two discovery mechanisms, both supported:

1. **Built-in sources** — registered in `apps/server/openwebrx_plus/sources/__init__.py` (the `SourceRegistry.BUILTIN` dict). Always available.
2. **Plugin sources** — discovered via Python entry points (group `openwebrx_plus.sources`). External packages can ship a source without modifying our codebase.

```toml
# An external SDR plugin's pyproject.toml:
[project.entry-points."openwebrx_plus.sources"]
my_exotic_sdr = "my_sdr_plugin:MyExoticSdrSource"
```

`SourceRegistry.discover()` runs at app startup, scans both, and exposes a `/api/sources` endpoint that lists available sources (so the UI can render a source picker).

### `SourceRegistry` interface

```python
class SourceRegistry:
    @classmethod
    def builtin_manifests(cls) -> list[SourceManifest]: ...
    @classmethod
    def discovered_manifests(cls) -> list[SourceManifest]: ...
    @classmethod
    def all_manifests(cls) -> list[SourceManifest]: ...
    @classmethod
    def get_manifest(cls, source_type: str) -> SourceManifest | None: ...
    @classmethod
    def create(cls, source_type: str, **kwargs) -> Source: ...
```

### Bundled source backends

Five sources ship in-repo from day one:

| source_type | hardware | status | purpose |
|---|---|---|---|
| `rtl_sdr` | RTL-SDR (incl. V4) | stub (real integration in slice-3) | primary user-facing SDR |
| `airspy` | Airspy R0/R2/Mini/HF+ Discovery | stub | user-requested |
| `sdrplay` | SDRplay RSP1/1B/2/Duo/DXR | stub | user-requested |
| `file` | None | **functional** (replays cf32 / SigMF-data) | hardware-free dev/test, regression tests |
| `simulated` | None | **functional** (multi-signal synthetic IQ) | demos, slice-1 frontend dev, unit fixtures |

The `file` and `simulated` sources are first-class — not test fixtures. They let us develop and demo the entire pipeline (FFT, audio, decoders, federation UI, popouts) **without any SDR hardware present**. This is critical for the user's request: "you will have to mimic one for your internal testing".

### Hardware-source integration (slice-3)

Real driver integration for `rtl_sdr`, `airspy`, `sdrplay` lands in slice-3 via:

- **Direct Python bindings** (preferred when mature): `pyrtlsdr`, `pyairspy` — single-process, lowest latency.
- **Subprocess source connector** (fallback): wraps the C binary (`rtl_sdr`, `airspy_rx`, `sdrplay_api_cmd`) and pipes IQ to the backend. Same pattern as upstream OpenWebRX. Used when no Python binding exists or when SDK licensing forbids static linking.

The `Source` Protocol is identical for both styles; the only difference is whether `spawn()` opens a USB device or `subprocess.Popen`s a binary.

### VFO sub-receivers (slice-3)

Per ADR-001 Feature #2, one wideband SDR serves multiple VFOs. This requires extending the `Source` Protocol with a `spawn_vfo(vfo_id, offset_hz, bandwidth_hz)` method and a `WidebandSource` mixin that demuxes IQ to multiple VFO consumers. **Not in scope for this ADR** — will land in ADR-005 (VFO sub-receivers).

---

## Consequences

- `pyproject.toml` gains `pycsdr` in `[project.dependencies]` and `scipy` in `[dependency-groups.dev]`.
- New directory `apps/server/openwebrx_plus/offline/` reserved for scipy-using modules.
- `sources/` gains `airspy.py`, `sdrplay.py`, `file_source.py`, `simulated.py`, plus `registry.py` (the `SourceRegistry`).
- `sessions/registry.py` no longer hardcodes `RtlSdrSource()` — it calls `SourceRegistry.create(source_type)`.
- REST API gains `GET /api/sources` (list manifests) and `POST /api/receivers` accepts `source_type` from the manifest list.
- CI lint rule (slice-3): `scipy` imports outside `offline/`/`tests/`/`scripts/` are errors.
- ADR-002 (DSP+AI cascade) — pycsdr feeds the WDSP stage 1 and the DeepFilterNet stage 2a. No change to ADR-002's four-mode model; pycsdr is just theStage 1 implementation.

## Open questions (deferred to slice-3)

- [ ] VFO sub-receiver source contract (ADR-005)
- [ ] Source permission model — does an external plugin source get arbitrary code execution? (yes for now; we'll add sandboxing if we ever accept community plugins)
- [ ] Hot-plug detection for USB SDRs (udev integration?)
- [ ] Source health-check / watchdog (restart on IQ stream stall)
- [ ] Per-source gain profiles (RTL-SDR has gain tables; Airspy has LNA/MIX/VGA; SDRplay has gain reduction + IF)
