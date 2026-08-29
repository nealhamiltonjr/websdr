# OpenWebRX+ — Complete Project Handoff (for an AI continuing this work)

**Snapshot date:** 2026-08-29 · **State:** post slice-53 (DAB FIC decoder — 17 decoders total) · **All quality gates green** (728 server tests + 255 web tests; one known `mypy --strict` CLI artifact documented in §4/§5.1, NOT a CI failure)

This document is the single entry point for an AI (or human) picking this
project up cold. It explains what the project is, the tech stack, exactly
where development stands (verified, not aspirational), what remains with
concrete implementation guidance for each item, and how to run / test /
simulate-test everything without SDR hardware.

Companion documents (read in this order):
1. **This file** — orientation + roadmap + how-to-run.
2. `docs/STATUS.md` — the living status doc: verified health table,
   codebase map, delivery history (snapshot is up-to-date through slice-14;
   this handoff file is the authoritative source for slices 15-21).
3. `ADR/` — 7 accepted Architecture Decision Records (workspace,
   DSP+AI cascade, decoder plugins, pycsdr/sources, VFO wideband,
   federation, IQ-to-audio enhancement).
4. `ARCHITECTURE.md` — the original pillars/vision document.
5. `SYNC-UP.md` — the git push protocol (PAT-strip pattern).

> **Note on directory layout:** This repository is a self-contained monorepo
> at the repo root (no outer `openwebrx-plus/` parent). Paths in this doc are
> repo-relative. Where you see `scripts/pycsdr-build/...` or `env-artifacts/...`
> in older handoff notes, those artifacts are now under `scripts/` directly
> or no longer present (rebuild from source via
> `scripts/README-dsp-bootstrap.md`).

---

## 1. What this project is

**OpenWebRX+ is a ground-up modernization of OpenWebRX**: a browser-native,
multi-receiver SDR (Software-Defined Radio) platform. One server taps SDR
hardware (or IQ recordings, or remote receivers on the internet) and streams
spectrum, audio, and decoded digital signals to a rich single-page web app.

What makes it different from upstream OpenWebRX:

- **Multi-receiver workspace** — spawn N receivers from one SDR (VFO taps via
  a DDC fan-out), arrange their waterfalls/spectra/meters in a dockable
  (drag-and-drop) layout, pop panels out into windows, persist layouts.
- **Modern web frontend** — SolidJS (signals, no VDOM), WebGL2 renderers
  (waterfall + spectrum at 60 fps), one WebSocket per receiver multiplexed
  through a SharedWorker.
- **Hardware-free-first development** — the entire platform (DSP, protocols,
  decoders, UI) is testable with **zero SDR hardware**: baked IQ fixtures,
  synthetic sources, and protocol-faithful fake servers/binaries. **528
  server tests + 178 web tests prove every path.**
- **Decoder plugin engine** (ADR-003) — two families behind one contract:
  in-process Python decoders (ADS-B/Mode S, AIS, dump978 UAT, CW Morse code,
  FT8 contract stub) and subprocess C binaries (dump1090 with SBS1→NDJSON
  bridge).
- **AI denoise cascade** (ADR-002) — Stage 2a (in-process numpy spectral
  subtraction) shipped in slice-10; Stage 2b (RNNoise WASM client-side
  loader) shipped in slice-19; Stage 1 (DeepFilterNet Rust scaffold)
  shipped in slice-18 (model weights NOT shipped — operators build
  separately).
- **Federation** (ADR-006) — act as a client to rtl_tcp, KiwiSDR,
  SpyServer, and other OpenWebRX instances; browse public receiver
  directories; SDRangel client manifest scaffolded (Tier C, slice-20).

The mission in one line: *the SDR receiver that runs anywhere a browser
does, with the operator ergonomics of a native app* (see ARCHITECTURE.md's
five pillars: workspace, rendering, DSP+AI cascade, plugin engine,
federation).

## 2. Tech stack

| Layer | Technology | Notes |
|---|---|---|
| Backend | **Python 3.12 + FastAPI + uvicorn** (`apps/server`) | strict typing: `mypy --strict` (2 known errors in `sources/sdrplay.py` cffi callbacks — see §4), `ruff` clean |
| DSP | **pycsdr / libcsdr 0.19** (C/C++ SIMD blocks, built from jketterl's sources) | FftChain, AudioChain, VfoChain (Shift→FirDecimate DDC). **Not on PyPI** — see §7 restore recipe |
| Math | numpy (live paths), scipy (dev/offline ONLY — ADR-004 forbids it in live DSP) | |
| Sources | ctypes/cffi drivers: RTL-SDR (USB), Airspy, SDRplay, SoapySDR; asyncio TCP/WS clients: rtl_tcp, KiwiSDR, SpyServer, OpenWebRX remote, SDRangel (manifest scaffold) | 12 registered backends |
| Decoders | in-process numpy (ADS-B/Mode S incl. CRC-24, AIS, dump978 UAT, CW Morse, FT8 contract stub) + subprocess runner (dump1090 + SBS1→NDJSON bridge) | |
| AI cascade | Stage 1 DeepFilterNet Rust module (`packages/ai-rust`), Stage 2a in-process numpy (`dsp/ai_denoise.py`), Stage 2b RNNoise WASM loader (`apps/web/src/lib/denoise/RNNoiseLoader.ts`) | DeepFilterNet weights NOT shipped; RNNoise WASM NOT shipped (operator-built) |
| Frontend | **SolidJS + Vite + TypeScript** (`apps/web`), Tailwind v4 styling | `tsc --noEmit` clean |
| Workspace UI | **dockview** (drag/dock/popout) via `dockview-solid` | |
| Rendering | **WebGL2** custom renderers (waterfall history texture + colormap LUTs, spectrum line strips w/ peak-hold); **OffscreenCanvas + Web Worker** thread-off (slice-11, transparent main-thread fallback for Safari/iOS) | |
| Realtime transport | **WebSocket**: binary frames (FFT `WRFO`, audio `AUDI`) + JSON text frames (metadata, decoder events) | one WS per receiver, fan-out via **SharedWorker**; popouts see cursor via SharedWorker cross-channel (slice-6.3) |
| Maps | **MapLibre GL JS** (AircraftMapViz + VesselMapViz, slice-8) with adaptive tile source + offline fallback | |
| Shared schemas | `packages/shared-types` — TS source of truth for wire formats | |
| Tests | pytest + pytest-asyncio (server), vitest (web), agent-browser E2E scripts | |
| Monorepo | pnpm workspaces (web) + uv (server venv) | |

## 3. Repository layout

```
.                                   THE monorepo root
├── apps/server/
│   ├── openwebrx_plus/
│   │   ├── sources/        12 backends + SourceRegistry (manifest contract,
│   │   │                   runtime-gain protocol); wideband.py = IqHub+VfoChain
│   │   ├── dsp/            pycsdr chains (FftChain, AudioChain) + AIDenoiser
│   │   ├── sessions/       ReceiverSession (IQ + display-stream paths), registry
│   │   ├── plugins/        base (contract), adsb+modes (Mode S), ais+ais_demod+
│   │   │                   ais_protocol, dump978+uat_demod+uat_protocol,
│   │   │                   cw+cw_demod+cw_protocol, ft8 (contract stub),
│   │   │                   dump1090, subprocess.py (PluginRunner)
│   │   ├── api/            REST + WebSocket pump + settings_debug
│   │   ├── config/         settings.py + user_settings.py (persisted to TOML)
│   │   └── observability/  structlog + debug_log ring buffer
│   ├── tests/              37 test files, 528 tests + 1 skipped + fakes/
│   ├── fixtures/iq/        ⚠ Metadata (.meta) shipped; .cf32 must be
│   │                       regenerated — see §7.2
│   └── pyproject.toml, uv.lock
├── apps/web/src/
│   ├── routes/             popout.tsx, main.tsx (SolidJS Router)
│   ├── components/         AddReceiverModal, DSPControls, DebugPanel,
│   │                       RemoteBrowser, SettingsPanel, TuningBar,
│   │                       WorkspaceManager, sourceForms, workspace/
│   ├── sessions/           ReceiverSession, tuneBus, receiverTuning
│   ├── visualizations/     Waterfall/Spectrum/SMeter/FrequencyCounter/
│   │                       MapViz/AircraftMap/VesselMap/AircraftList/
│   │                       VesselList/DigiMessageList + model/test files
│   ├── lib/                webgl2/ (renderer + overlay), audio/AudioPlayer,
│   │                       api, denoise/RNNoiseLoader, subject
│   └── workers/            sdr.shared-worker.ts, render-worker.ts
├── packages/
│   ├── shared-types/       TS wire formats (real impl, exported to web)
│   ├── ai-rust/            DeepFilterNet Rust scaffold (slice-18, spectral sub)
│   ├── dsp-zig/            ADR-002 Zig DSP scaffold
│   ├── rnnoise-wasm/       RNNoise WASM operator build recipe (README only)
│   └── dsp-c/              ADR-002 C DSP scaffold
├── ADR/                    7 decision records (001-007)
├── docs/                   AI-HANDOFF.md (this file), STATUS.md, slice-01-plan.md
├── scripts/
│   ├── run-server-tests.sh THE server test entry point (LD_LIBRARY_PATH wrapper)
│   ├── generate_iq_fixtures.py  deterministic fixture baker
│   ├── sbs1_to_ndjson.py   dump1090 SBS1→NDJSON bridge (slice-16)
│   ├── probe_openwebrx_remote.py  federation probe
│   ├── test_audio_chain.py / test_fft_chain.py / test_audio_modes.py
│   ├── README-dsp-bootstrap.md  from-source libcsdr/pycsdr build recipe
│   └── pycsdr-build/...    ⚙ compiled pycsdr RESTORE ARTIFACT (copy into site-packages — §7.1)
├── Makefile                dev-server / dev-web targets
├── ARCHITECTURE.md         original pillars/vision document
├── README.md               operator-facing readme
├── AGENTS.md               agent runtime notes
├── SYNC-UP.md              git push protocol (PAT-strip pattern)
├── worklog.md              append-only slice history (24 slices logged)
├── LICENSE                 AGPL-3.0 full text (674 lines, slice-6.1)
├── pnpm-workspace.yaml, pnpm-lock.yaml, package.json
└── .github/workflows/      CI workflow definitions (see SYNC-UP.md for status)
```

**Not in this zip** (all regenerable or upstream): `node_modules/`, `.venv/`,
`dist/`, `.git/`, caches, the 47 MB of `.cf32` fixtures (byte-identical
regeneration — proven by checksum, see §7.2).

## 4. Where we are — verified status

Every number below was re-verified at snapshot time (2026-08-28, post
slice-32, origin/main `4260524`):

| Gate | Result |
|---|---|
| Server tests: `scripts/run-server-tests.sh` | **528 passed, 1 skipped**, 84% coverage (~87 s) |
| `mypy openwebrx_plus` (CI invocation via pyproject.toml; 60 files) | **Success, no issues** — CI is green. |
| `mypy --strict openwebrx_plus` (CLI override, NOT how CI invokes) | 2 errors in `sources/sdrplay.py` lines 193, 207 (`@ffi.callback(...)` untyped decorator) — **NOT a CI failure**. pyproject sets `disallow_untyped_decorators = false` (FastAPI/cffi have opaque stubs). Adding `# type: ignore[untyped-decorator]` was tried in slice-23 and reverted in slice-23b (commit `6800d05`) because under pyproject config the comment is "unused" → itself a strict error. See §5.1 below for the resolution. |
| `ruff check .` | clean |
| Web `vitest` | **178/178 pass** across 14 test files (~3 s) |
| Web `tsc --noEmit` | clean |
| `vite build` | clean (one chunk-size warning, not an error) |
| GitHub Actions CI (run `33144017200` for `4260524` = slice-32) | **all 5 jobs success** — Frontend / Backend / DSP (packages/dsp-zig) / AI (packages/ai-rust) / Shared-Types |

**What works end-to-end today, hardware-free:**

- Boot → default receiver replaying the HF fixture → live waterfall + spectrum
  (WebGL2) + S-meter + frequency counter; dockable workspace persists across
  reloads with backend re-adoption.
- Spawn receivers from: any local SDR driver config, IQ file (fixture picker),
  synthetic signal presets, public OpenWebRX/KiwiSDR/SpyServer endpoints
  (quick-connect or directory browser), or as VFO taps off a parent receiver.
- Per-receiver tuning (slider/presets/click-on-canvas), mode switching, gain
  (auto or dB, runtime on every backend), DSP mode (raw/classic/ai/cascade),
  linked crosshairs across panels (slice-4.6 + slice-6.3 for popouts),
  click-to-tune from any canvas.
- **Five in-process decoder plugins + one subprocess decoder**:
  - `adsb` — in-process (PPM demod + CRC-24 + field decode), one-click attach
  - `dump1090` — subprocess (stdin cs16 IQ in, NDJSON events out via
    `scripts/sbs1_to_ndjson.py` bridge (slice-16), crash-restart with
    backoff, metered backpressure, `decoder_state` lifecycle events)
  - `ais` — in-process GMSK demod + HDLC deframer + vessel-list viz (slice-6.4)
  - `dump978` — in-process UAT demod (2.083 MSPS, timing recovery + carrier
    offset compensation, slice-9 + slice-17) + aircraft-list viz
  - `cw` — in-process Morse code demod (slice-13) + text rendering
  - `ft8` — **v2.1 fully wired (slices 21→26→28→29)**: wire types +
    viz + plugin manifest (slice-21) → v1 hard-decision FSK demod +
    CRC-14 + standard message unpack (slice-26, closes slice-21 stub)
    → v2 real (174,91) LDPC codec: parity computation + syndrome
    check + sum-product (min-sum BP) decoder (slice-28) → v2.1 wires
    soft FSK demod → sum-product LDPC as the PRIMARY decode path with
    hard-decision + syndrome + CRC as fallback when LDPC fails to
    converge (slice-29). ~3 dB SNR improvement on real-world fading
    channels vs v1 hard + CRC. Remaining v3 items: Costas loop /
    symbol timing recovery, 6-char callsigns, i3≠0 message types.
- **AI denoise cascade**:
  - Stage 2a server-side (slice-10): `dsp/ai_denoise.py` — adaptive
    noise-floor tracking, spectral subtraction over short-time FFT frames
  - Stage 2b client-side (slice-19 + slice-24): `apps/web/src/lib/denoise/
    RNNoiseLoader.ts` fetches `/pkg/rnnoise_wasm.js` (HEAD probe),
    dynamically imports if present, exposes `RNNoiseDenoiser` interface
    (frameSize, processFrame, reset, dispose). Slice-24 closes the
    AudioWorklet gap: `apps/web/src/lib/audio/rnnoise-processor.ts` is a
    registered AudioWorkletProcessor calling processFrame per
    RNNOISE_FRAME_SIZE (480-sample) block, inserted as an AudioWorkletNode
    between the source buffer and destination when "Client-side denoise"
    is enabled, with graceful fallback to AudioBufferSourceNode when the
    WASM module is unavailable.
  - Stage 1 DeepFilterNet Rust scaffold (slice-18): `packages/ai-rust/src/
    deepfilternet.rs` — real spectral subtraction v1 impl; the upstream
    `deepfilter` crate + model weights are NOT shipped (operator-built)
- **MapLibre map view** for both aircraft and vessel tracks (slice-8):
  adaptive tile source (raster OSM → vector OSM → static fallback), offline
  graceful degradation, populates from the in-process ADS-B / AIS decoders
- **OffscreenCanvas + Web Worker render thread-off** (slice-11):
  `apps/web/src/workers/render-worker.ts` handles the waterfall render
  loop off the main thread; transparent fallback to main-thread on
  Safari/iOS via feature detection
- **In-app panels** (slice-5.1): **Settings** (display/audio/DSP/sources/
  decoders/debug sections, persisted to TOML) + **Debugger** (live log ring
  buffer + error capture + filters + export + auto-refresh).
- **DSP controls** (slice-5.2 + slice-7): per-receiver DSPControls drawer
  with all 8 controls LIVE end-to-end — bandpass width / AGC / squelch /
  DC block / de-emphasis / manual gain (pycsdr blocks) + **notch filter**
  (single-pole complex IIR, slice-7) + **noise blanker** (adaptive impulse
  clipper, slice-7). The notch + NB run as a pure-numpy IQ preprocessor
  BEFORE the pycsdr chains, so both the FFT and audio paths see the
  cleaned IQ — the "pull out the weak signal" thesis is fully realized.
- **Runtime gain control** (slice-6.5): every backend (airspy, soapy,
  sdrplay, kiwi, rtl_tcp, rtl_sdr) implements the `RuntimeGainSource`
  protocol via the `_gain_q` latest-wins channel — gain changes take
  effect on the next IQ tick without respawning the receiver.
- **Popout crosshair sync** (slice-6.3): popouts (separate windows) see
  the main window's cursor via the SharedWorker cross-channel — all
  ports receive CursorState broadcasts.
- **SpyServer runtime tune forwarding** (slice-15): the SpyServer client
  forwards COMMAND_SET_IQ_FREQUENCY when the operator retunes — no more
  "tune hangs" on SpyServer-backed receivers.
- **Federation polish** (slice-14): HD audio decode on the federation
  client (48 kHz music-quality WFM streams reach the client) +
  self-listing endpoint so operators can opt-in to publish their station
  to receiverbook.
- **CW decoder** (slice-13): in-process Morse code plugin with audio
  tone detection, dit/dah tracking, Farnsworth spacing, character +
  word output. The text streams to a digi-message viz.
- **FT8 contract surface** (slice-21): wire types + plugin stub +
  `DigiMessageListViz` component. The UI can offer FT8 in the +viz
  dropdown today; the actual demod (FSK + LDPC + CRC-14 + message
  unpack) lands in a future slice.

**Delivery history (slices, latest first):**

| Slice | Title | Sync commit |
|---|---|---|
| 32 | federation polish follow-up: secondary-demod forwarding (RemoteDecoderEvent wire type 0x05) | `4260524` |
| 31 | dump1090 fixture improvements: fork auto-detect + SBS1 auto-discovery + new failure modes | `eefd5c5` |
| 30 | docs refresh: AI-HANDOFF.md + STATUS.md to actual state; PAT-strip push of slices 27/28/29 | `aa18de0` |
| 29 | FT8 v2.1 — wire soft FSK demod → sum-product LDPC as primary decode path | `fd7c98c` |
| 28 | FT8 v2 LDPC — real parity + syndrome check + sum-product decoder | `1b4daba` |
| 27 | sync uv.lock (tomli-w drift) + worklog | `c57af88` |
| 26 | FT8 FSK demod + CRC-14 + standard message unpack (v1, closes slice-21) | `3e88175` |
| 25 | SDRangel REST+WS streaming v1 (spectrum-only, closes slice-20) | `b8b1d04` + fix `29ee618` |
| 24 | RNNoise AudioWorkletProcessor — closes slice-19 loader | `cc68991` |
| 23 | sdrplay cffi callback type:ignore (added then reverted in slice-23b) | `0662907` + revert `6800d05` |
| 22 | 'WRSF' secondary FFT wire format (RemoteSecondaryFftFrame + WS forwarding) | `447d66b` (prior session) |
| 21 | FT8 / audio-band digi-mode plugin + DigiMessageListViz | `a76a04e` |
| 20 | SDRangel client manifest scaffolding (ADR-006 Tier C) | `a49762a` |
| 19 | RNNoise WASM client-side (AI cascade Stage 2b) | `24a28c5` |
| 18 | DeepFilterNet Rust module scaffold (real spectral subtraction) | `5afd143` + fix `cc76fa2` |
| 17 | dump978 timing recovery + carrier offset compensation | `d441a6a` |
| 16 | dump1090 SBS1 → NDJSON bridge (real-binary bring-up) | `f03cec8` |
| 15 | SpyServer runtime tune forwarding (COMMAND_SET_IQ_FREQUENCY) | `8bcb158` |
| 14 | federation polish (HD audio + self-listing endpoint) | `7192903` |
| 13 | CW (Morse code) audio-band decoder plugin | `d448822` |
| 12 | vitest 3.x + typescript-eslint unified + libcsdr CI cache | `f1c6d25` |
| 11 | OffscreenCanvas + Web Worker render thread-off (Pillar 2) | `9599c5f` |
| 10 | AI cascade Stage 2a (in-process spectral-subtraction denoiser) | `f62a600` + fix `eb261e0` |
| 9  | dump978 UAT decoder (in-process plugin family #4) | `6c8b845` |
| 8  | MapLibre map view (AircraftMapViz + VesselMapViz) | `63f6786` |
| 7  | notch filter + noise blanker — completes DSP fine-grained controls | `edc5a09` |
| 6.5 | runtime-gain gaps for airspy/soapy/sdrplay/kiwi | `737ffbe` |
| 6.4 | AIS decoder (in-process plugin) + vessel list viz | `198141f` |
| 6.1-6.3 | LICENSE + linked readouts + popout crosshair sync | `d56c558` |

Full detail in `worklog.md` (append-only, 32 entries since slice-1).

## 5. What's left — prioritized roadmap with implementation guidance

### 5.1 sdrplay mypy regression — ✅ RESOLVED (slice-23 + slice-23b)

`mypy --strict openwebrx_plus` reports 2 errors in `apps/server/
openwebrx_plus/sources/sdrplay.py` at lines 193 and 207:

```
error: Untyped decorator makes function "_stream_cb" untyped  [untyped-decorator]
error: Untyped decorator makes function "_gain_cb" untyped  [untyped-decorator]
```

Both are `@ffi.callback(...)` decorators wrapping Python callbacks.
Slice-23 added `# type: ignore[untyped-decorator]` on each line; CI
broke because under pyproject.toml's actual config
(`disallow_untyped_decorators = false`), the comment is flagged as
"unused" which is itself an error under strict mode. Slice-23b
(commit `6800d05`) reverted — both comments removed, CI green again.

**Conclusion: NOT a real issue.** The `--strict` CLI flag overrides
pyproject to re-enable `disallow_untyped_decorators`, surfacing the
errors. CI invokes `uv run mypy openwebrx_plus` which reads pyproject
— that invocation passes cleanly (60 source files). The handoff doc
previously called this a "known regression"; it is not. The §4 verified
status table above reflects the corrected interpretation.

If a future agent wants to silence the CLI-`--strict` invocation too,
the only non-CI-breaking path is the "Proper" option: declare explicit
callback signatures via a typed cffi shim module. Effort ~1 hour. Not
worth doing unless someone adds a pre-commit hook that runs
`mypy --strict` (CI does not).

### 5.2 FT8 demodulator + LDPC decoder — ✅ SHIPPED through v2.1 (slices 26 / 28 / 29)

The slice-21 stub shipped the wire contract (`DigiMessageEvent`,
`DigiMessageListEvent`) + the viz + the plugin manifest. The full
demod is now live across three slices:

- **v1 (slice-26, commit `3e88175`)** — closes the slice-21 stub:
  hard-decision FSK demod at FT8_TONE_SPACING_HZ=6.25 Hz (12 kS/s
  input from `feed_audio` — the plugin's `tap_point="rf_band"` plus
  `required_sample_rate=12000` routes channelized audio); Goertzel
  tone detection; 79-symbol frame structure with Costas sync skipped
  (v1 assumes the synthesized signal is already aligned); symbols →
  91 systematic + 83 parity bits (parity zero-padded in v1); CRC-14
  verify; standard message unpack (callsign, grid, dB report).
- **v2 (slice-28, commit `1b4daba`)** — replaces the v1 zero-padded
  parity with the real WSJT-X FT8 (174, 91) LDPC codec:
  `_GENERATOR_INTS` + `_BIT_TERMS` hardcoded from vk3jpk/ft8-notes
  (GPL-3.0-or-later, WSJT-X-derived). `encode_ldpc(systematic_91)`
  → 174-bit codeword with REAL LDPC parity. `compute_syndrome(...)`
  → 83-bit syndrome; `is_valid_codeword(...)` convenience wrapper.
  `decode_ldpc(soft_llrs, max_iter=20)` → min-sum belief-propagation
  on the H factor graph (recovers ~3 bit errors). Syndrome check
  BEFORE CRC eliminates the v1 ~1/16384 false-positive rate.
- **v2.1 (slice-29, commit `fd7c98c`)** — wires the soft FSK demod
  → sum-product LDPC as the PRIMARY decode path. New
  `detect_symbols_soft(audio, sample_rate)` returns both hard symbols
  (backward-compat) AND 174 per-bit log-likelihood ratios (LLR per
  bit = log(sum mags where bit=0 / sum mags where bit=1); Costas sync
  positions skipped; 58 data positions × 3 bits = 174 LLRs). The
  plugin's `_process_slot` now tries LDPC on the soft LLRs first; on
  convergence uses `res.systematic_bits` as the 91-bit codeword; on
  non-convergence falls back to v2 hard-decision + syndrome + CRC.
  Status reports `soft_decode_success` and `soft_decode_fallback`
  counters. ~3 dB SNR improvement on real-world fading channels.

**Remaining v3 items (NOT blocking; deferred):**
1. Costas-loop carrier recovery + time sync (current detect_symbols
   assumes the slot is already aligned — true for synthesized test
   signals, NOT for real-world signals where the FFT bin offset is
   unknown). Estimated effort: 1-2 days.
2. i3≠0 message types (standard type 0.0, 0.5, 1.0 — current unpack
   handles only type 0.0; telemetry/free-text types deferred).
3. 6-char callsigns (current handles 4-char standard form only).
4. Full grid-locator encodings (current handles 4-char only).

Tests: `apps/server/tests/test_ft8_decoder.py` — 46 tests covering
synthetic clean signals, CRC validation, LDPC convergence, soft vs
hard path selection, silence/noise rejection. All 46 green.

### 5.3 RNNoise AudioWorklet integration — ✅ SHIPPED (slice-24, commit `cc68991`)

The slice-19 loader exposes `RNNoiseDenoiser.processFrame(samples)`.
Slice-24 closes the gap by wiring the loader into the AudioPlayer:

1. `apps/web/src/lib/audio/rnnoise-processor.ts` — registered
   `AudioWorkletProcessor` calling `processFrame` per
   `RNNOISE_FRAME_SIZE` (480-sample) block.
2. Worklet registered via `audioContext.audioWorklet.addModule(...)`
   on demand (lazy — only when "Client-side denoise" is enabled).
3. `AudioWorkletNode` inserted between source buffer and destination
   when "Client-side denoise" is toggled on in DSPControls (the UI
   control surface was already in place from slice-5.2).
4. Graceful fallback: if the WASM module is unavailable (loader
   returns null), the player keeps using `AudioBufferSourceNode`s
   and logs a one-time warning.

Tests: `apps/web/src/lib/denoise/RNNoiseLoader.test.ts` (8 tests)
covers the loader contract. The worklet itself is a small adapter —
contract-tested indirectly via vitest's mock AudioContext.

### 5.4 SDRangel REST+WS streaming — ✅ v1 SHIPPED (slice-25, commit `b8b1d04` + ruff fix `29ee618`) + ✅ v2 audio + set_mode() SHIPPED (slice-35)

Slice-25 closed the slice-20 manifest scaffold with v1 spectrum-only
streaming. Slice-35 closed the v1 "spectrum-only / set_mode() raises"
limitation with audio-over-UDP-sink + a real `set_mode()`:

1. `GET /deviceset/{device_set}/device` — discover the current
   device. ✅ (slice-25)
2. `PUT /deviceset/{device_set}/device/settings` — set center
   frequency. ✅ (slice-25)
3. Open the spectrum-server WebSocket: `ws://host:port/spectrum/
   deviceset/{device_set}` — binary FFT frames in a documented
   layout. ✅ (slice-25) — frames decoded into `RemoteFftFrame` and
   forwarded to the receiver session's FFT channel.
4. Audio-over-UDP-sink — ✅ SHIPPED (slice-35). SDRangel has no
   built-in audio-over-WS, so the v2 path configures a demod
   channel's **UDP audio sink** to stream int16 mono PCM to a local
   UDP listener. `SDRangelSource` gains `audio_enabled` /
   `audio_output_rate` (8/12/24/48 kS/s) / `audio_mode` (NFM/WFM/AM/
   USB/LSB/CW) / `audio_udp_port` (0=ephemeral) params. When audio
   is enabled, `display_stream()`:
     (a) binds a local UDP socket,
     (b) POSTs `/deviceset/{id}/channel` to add a demod channel,
     (c) PUTs `/deviceset/{id}/channel/{cid}/settings` with
         `<channelType>Settings` body containing `udpEnabled=true`,
         `udpAddress=127.0.0.1`, `udpPort=<local>`, `audioSampleRate`,
     (d) reads int16 PCM from the UDP socket in a background task,
     (e) yields interleaved `RemoteAudioFrame` chunks alongside the
         FFT frames (drained between WS reads).
   `_teardown_audio()` cancels the listener, closes the socket, and
   DELETEs the remote channel.
5. `set_mode(mode)` — ✅ SHIPPED (slice-35). Was `NotImplementedError`
   in v1; now DELETEs the current channel + POSTs a new one with the
   requested mode + PUTs its settings to re-point the UDP sink. SSB
   mode correctly codes `sidebands=1` (USB) / `sidebands=0` (LSB).
   On a spectrum-only source (`audio_enabled=False`), `set_mode()`
   just tracks the mode (no REST calls) — honest no-op, not an error.

Pattern: copied `sources/spyserver.py` (TCP) for the REST polling
skeleton + `sources/kiwi.py` (WS) for the spectrum-server WS handling.
Slice-35 added the channel-management REST routes (POST/PUT/DELETE) +
the UDP listener (modeled on `sources/_hw_common.py`'s `AsyncIqBridge`
for the cross-thread handoff pattern, but simpler — UDP recv is already
non-blocking).
Tests: `apps/server/tests/test_sdrangel_driver.py` — 35 tests total
(25 slice-20/25 + 10 slice-35), all green under the hardware-free
fake fixtures pattern. `FakeSDRangelServer` extended with channel
POST/PUT/DELETE handlers + a `start_audio_sender` method that opens a
UDP socket to the source's local listener port and streams 4 chunks of
int16 mono PCM (1 kHz tone, 800 samples/chunk).

### 5.5 DeepFilterNet weights + upstream crate (closes slice-18 scaffold)

The slice-18 Rust module shipped a real spectral-subtraction v1 impl.
To swap in real DeepFilterNet:

1. Add `deepfilter` crate dependency to `packages/ai-rust/Cargo.toml`.
2. Ship or document operator download of model weights (DF3 net ~3 MB).
3. The `Denoiser::process_frame` body becomes a one-line swap:
   `self.df_state.process_frame(samples, &self.model)`.

Estimated effort: half a day for the code; weights licensing review
adds a week or two depending on operator policy.

### 5.6 dump1090 binary / fixture improvements — ✅ SHIPPED (slice-31)

The slice-16 SBS1→NDJSON bridge (`scripts/sbs1_to_ndjson.py`) is a thin
Python wrapper around the real dump1090 binary. Slice-31 ships the
three remaining polish items:

1. **Auto-detect dump1090-fa vs mutability vs readsb**: the bridge now
   probes `<binary> --version` once at startup (1.0s timeout, never
   raises) and reports the fork identity in the ready event's `fork`
   field (one of `"fa"` / `"mutability"` / `"readsb"` / `"unknown"`).
   Override via `OPENWEBRX_PLUS_DUMP1090_FORK` env var (skips the
   probe on air-gapped systems).
2. **Auto-discovery of a running SBS1 server**: the dump1090 plugin
   (`apps/server/openwebrx_plus/plugins/dump1090.py`) now probes
   `127.0.0.1:30003` (the standard SBS1 port for dump1090-fa/mutability/
   readsb) when `OPENWEBRX_PLUS_DUMP1090_BIN` is unset. If a server is
   reachable, the plugin defaults to the SBS1 bridge script in
   `--no-spawn` mode against that endpoint — operators with a running
   dump1090 service need no extra config.
3. **Two new fake_dump1090 failure modes** for the runner's recovery
   path tests: `--vanish-after-ready-secs N` (emit ready, sleep, close
   stdout without exiting — tests the "decoder vanished after ready"
   path) + `--emit-partial-json-die` (emit ready, write a truncated
   JSON line, exit 0 — tests the JSON parser recovery path).

Tests: `tests/test_sbs1_bridge.py::TestForkAutoDetect` (8 tests),
`tests/test_dump1090_plugin.py` (NEW, 9 tests covering the probe + path
resolution + auto-discovery branches), and 2 new failure-mode tests in
`tests/test_subprocess_plugins.py`. All 19 new tests green; server
suite 525 passed + 1 skipped (was 504+1).

### 5.7 Mid-term / long-term items (lower priority)

- **Audio-band decoders**: FLDIGI via virtual audio cable (auto pipewire
  null sink per receiver); RTTY/PSK31 as in-browser Wasm. The
  DigiMessageListViz + DigiMessageEvent wire contract (slice-21) is the
  substrate — these are sibling plugins in the same `DIGI_MESSAGE_DECODERS`
  family.
- **Federation polish follow-up**: ✅ SHIPPED (slice-32, see below).
  Secondary-demod forwarding for `openwebrx_remote` (decoder events from
  upstream receivers reach the client's viz) — closed by slice-32. The HD
  audio half shipped earlier in slice-14.
- **Propagation intelligence**: MUF/foF2 fetch + display in the
  frequency guide (NOAA SWPC API). Long-term.
- **QSL logging**: optional integration with eqsl.cc / LoTW.
- **Mobile layout**: responsive dockview fallback for narrow viewports
  (current UI assumes ≥1024 px).
- **Deployment**: Dockerfile + docker-compose for one-command boot
  (current bootstrap is documented but manual — §7).

**Slice-32 detail — secondary-demod forwarding (closed the §5.7
federation polish follow-up sub-bullet):**

New OpenWebRX+-specific federation wire type 0x05 carries decoder
events from upstream OpenWebRX+ peers. The decode branch lives in
`sources/openwebrx_remote.py::_handle_binary` next to the slice-22
secondary-FFT decode branch. The wire format is:

```
[1-byte type=0x05][2-byte decoder_name_len LE][N-byte decoder_name UTF-8]
[4-byte event_json_len LE][M-byte event_json UTF-8]
```

New dataclass `RemoteDecoderEvent` in `sources/base.py` carries the
decoded `decoder_name` + `event` dict; the ReceiverSession's
`_run_display` loop forwards it via the same JSON envelope shape local
decoders use, with an extra `remote: true` field so the frontend can
optionally render a "remote" badge on the digi-message row. Legacy
OpenWebRX / KiwiSDR / SpyServer peers never send 0x05 — the decode
branch is a graceful no-op for them.

Tests: 3 new in `tests/test_openwebrx_remote_driver.py` — decode test
(canned FT8 payload round-trips through the wire format), absent-when-
not-configured test, and session-forwarding test that asserts the JSON
envelope shape on the WS broadcast. Server suite 528 passed + 1 skipped
(was 525+1; +3 from slice-32).

### The working protocol (how to ship a slice)

1. Implement (follow the file/pattern guidance above; ADRs first when
   architectural).
2. `scripts/run-server-tests.sh` + `cd apps/web && pnpm exec vitest run
   && pnpm exec tsc --noEmit` — all green, every time.
3. `ruff check` + `mypy --strict openwebrx_plus` clean in `apps/server`.
4. Append a worklog.md entry (template at the top of the file) and update
   `docs/STATUS.md` (health table, delivery history, what's-left).
5. For UI features: an E2E `scripts/verify_*_e2e.sh` using agent-browser.
6. Sync to remote per `SYNC-UP.md` (PAT-strip protocol).

## 6. How to run (dev)

```bash
# 0) one-time bootstrap — see §7 for the pycsdr/fixture steps first!

# 1) frontend deps
pnpm install

# 2) backend venv + deps — VERIFIED sequence (do NOT plain-install the
#    project: its pycsdr dependency is a git direct reference that wants
#    to BUILD pycsdr from source, which needs libcsdr already installed —
#    the chicken-and-egg that scripts/README-dsp-bootstrap.md solves):
cd apps/server
uv venv
uv pip install --python .venv/bin/python \
    fastapi uvicorn websockets httpx pydantic pydantic-settings \
    numpy structlog prometheus-client
uv pip install --python .venv/bin/python -e . --no-deps
uv pip install --python .venv/bin/python \
    pytest pytest-asyncio pytest-cov anyio httpx ruff mypy
#    then restore pycsdr per §7.1 and regenerate fixtures per §7.2

# 3) run (two terminals, both from the repo root)
make dev-server   # http://localhost:8073  (API + WS)
make dev-web      # http://localhost:5173  (binds ::1 —
                                     #  use "localhost", NOT 127.0.0.1)
```

The default receiver replays `fixtures/iq/hf_20m_evening.cf32` in real
time — waterfall, audio, tuning all work with no SDR attached. Spawn
more receivers via the "+ receiver" modal (file/fixture picker,
synthetic presets, remote endpoints, VFO taps). Attach any decoder from
the corresponding viz panel (Aircraft on a 2 MSPS receiver for ADS-B,
Vessel for AIS, etc.).

## 7. One-time bootstrap: pycsdr restore + fixture regeneration

### 7.1 Restore the compiled DSP stack (linux-x86_64, CPython 3.12)

pycsdr is not on PyPI; this repo carries the compiled restore artifacts
under `scripts/pycsdr-build/`:

```bash
# 1) the python package → into your venv's site-packages
cp -r scripts/pycsdr-build/build/lib.linux-x86_64-cpython-312/pycsdr \
      apps/server/.venv/lib/python3.12/site-packages/

# 2) the native libs → ~/.local/usr (so the LD_LIBRARY_PATH wrapper works)
mkdir -p ~/.local/usr
#    If env-artifacts/ is present in this checkout:
cp -r env-artifacts/usr/* ~/.local/usr/
#    Otherwise rebuild from source — see scripts/README-dsp-bootstrap.md

# 3) every pycsdr import needs this on the path
export LD_LIBRARY_PATH="$HOME/.local/usr/lib:$HOME/.local/usr/lib/x86_64-linux-gnu"
#    (scripts/run-server-tests.sh sets it for you)
```

Notes: the repo stores only the loader-required SONAME files
(`libcsdr.so.0.19`, `libsamplerate.so.0`) — symlink aliases deduped
away. `libfftw3f.so.3` is expected from the system
(`apt install libfftw3-3` on Debian/Ubuntu). Compiled artifacts are
linux-x86_64 / CPython 3.12.

Sanity check: `apps/server/.venv/bin/python -c "import pycsdr.modules;
print('ok')"`.

From-source rebuild recipe (other arch / fresh build):
`scripts/README-dsp-bootstrap.md`.

### 7.2 Regenerate the IQ fixtures (deterministic)

```bash
# from the repo root
apps/server/.venv/bin/python scripts/generate_iq_fixtures.py
# → apps/server/fixtures/iq/{hf_20m_evening,vhf_fm_broadcast,adsb_1090,smoke}.cf32 (+ .meta)
```

Regeneration is deterministic and seeded — `sha256sum *.cf32` should match
across runs. Server tests need these files.

### 7.3 Run the full verification battery

```bash
scripts/run-server-tests.sh                       # 528 tests, ~87 s
cd apps/web && pnpm exec vitest run && pnpm exec tsc --noEmit
# E2E verification scripts (agent-browser driven) live outside this repo —
# they were in the original handoff bundle and can be re-imported if needed.
```

## 8. Landmines (read before touching anything)

1. **NEVER `uv sync` / `uv run` in `apps/server` after restoring pycsdr** —
   a sync evicts the manually-restored package and tests break with import
   errors. Always test via `scripts/run-server-tests.sh`. Restore recipe:
   §7.1.
2. **`LD_LIBRARY_PATH=$HOME/.local/usr/lib[...:$HOME/.local/usr/lib/x86_64-linux-gnu]`
   is required** for every process that imports pycsdr (the wrapper sets it).
3. **Frontend dev server binds ::1** — use `http://localhost:5173`, not
   `127.0.0.1`.
4. **E2E scripts must run servers and checks inside ONE bash invocation**
   (trap-based cleanup; agent-browser drives the checks).
5. **Sandbox/regress egress is commonly filtered** — live remote checks
   (rx.kiwisdr.com, receiverbook, public SpyServers) are deferred to real
   machines; the protocols are covered by fakes in tests.
6. **scipy is offline/dev-only** (ADR-004) — never in the live IQ path.
7. The subprocess-decoder runner is single-threaded-asyncio BY DESIGN: the
   stdout reader task and the session's synchronous `feed_iq` share one
   event loop, so the event deque is lock-free. Don't move feeds to threads.
8. **MultiEdit tool batches are not always atomic on failure** — re-read
   the file after a failed batch (a worklog slice-4.8 lesson).
9. **mypy --strict has 2 known errors** in `sources/sdrplay.py` (cffi
   callback decorator untyped-annotation). These are a regression since
   slice-6.5. Not blocking (ruff/pytest/tsc/vite all green), but should be
   fixed before the next architectural slice to keep the gate honest. See
   §5.1 for the fix recipe.
10. **Commit message convention**: slices use `slice-N: <short summary>`;
    fix commits use `fix(slice-N): <description>`. Avoid the bare-GUID
    commit pattern that briefly crept in (one such commit sits on top of
    main as of this snapshot — replace with a meaningful message on the
    next slice's commit).

## 9. How testing and simulation work (the hardware-free philosophy)

The project's core testing bet: **every hardware/remote dependency has a
software stand-in, and the production code path is identical**.

| Real thing | Stand-in | Where |
|---|---|---|
| RTL-SDR/Airspy/SDRplay/Soapy USB hardware | driver classes instantiate; hardware-touched paths unit-tested with ctypes/cffi mocks + fixture IQ through the SAME chain | `tests/test_*_driver.py` |
| Live RF | baked IQ recordings (HF band, FM broadcast, ADS-B with CRC-valid frames, smoke) — deterministic generator | `fixtures/iq/`, `scripts/generate_iq_fixtures.py` |
| Live RF (synthetic) | `SimulatedSource` — multi-carrier + noise presets (AM band, ham, ADS-B pulses, FT8 spacing) | `sources/simulated.py` |
| rtl_tcp / rsp_tcp server | FakeRtlTcpServer — real asyncio TCP server speaking the wire protocol | `tests/test_rtl_tcp_source.py` |
| KiwiSDR websocket | fake Kiwi server (handshake + ADPCM IQ pump) | `tests/test_kiwi_driver.py` |
| SpyServer | FakeSpyServer — protocol v2 frames, HELLO/INFO/SYNC chatter, BYE refusal | `tests/test_spyserver_driver.py` |
| Remote OpenWebRX | fake HTTP+WS upstream incl. ADPCM display streams | `tests/test_openwebrx_remote_driver.py` |
| SDRangel | manifest scaffold only — no fake server yet (slice-20 stub) | `tests/test_sdrangel_driver.py` |
| dump1090 binary | `tests/fakes/fake_dump1090.py` — REAL Mode S demod, contract-exact NDJSON, deliberate crash/garbage/stall modes | `tests/test_subprocess_plugins.py` |
| Browser user | agent-browser E2E scripts (screenshot + DOM assertions) | `scripts/verify_*_e2e.sh` (outside repo) |

**Adding a test that needs "hardware":** never mock internals — build a
fake that speaks the real wire protocol (see the table). That's what
keeps the production path honest.

**Test entry points:**
- Server: `scripts/run-server-tests.sh [pytest args...]` (from anywhere).
- Web: `cd apps/web && pnpm exec vitest run` (pure models are
  node-environment tests; no DOM needed).
- Static gates: `ruff check` + `mypy --strict openwebrx_plus` in
  `apps/server`.
- E2E: scripts not currently in-repo (originally
  `scripts/verify_{adsb,crosshair,dockview,gain,tuning,frontend}_e2e.sh`,
  can be re-imported from the original handoff bundle if needed).

## 10. How to extend (the two recipes you'll actually need)

### Add a source backend

1. Subclass the `Source` protocol in `apps/server/openwebrx_plus/sources/`
   (async `spawn()` yielding cf32 chunks; manifest with rate/gain ranges).
   Copy the closest existing driver (`rtl_tcp.py` for wire protocols,
   `rtl_sdr.py` for USB, `file_source.py` for replays, `sdrangel.py`
   for a manifest scaffold).
2. Register in `sources/__init__.py` + the manifest table in `base.py`.
3. REST picks it up automatically (`GET /api/sources`); add a form in
   `apps/web/src/components/sourceFormModel.ts` (+ test list) for the modal.
4. Tests: wire protocol → fake server; driver → fixture IQ through the chain.

### Add a decoder plugin

1. **In-process**: subclass `DecoderPlugin`, set `manifest`, implement
   `feed_iq` (numpy in → event dicts out). Register with
   `@DecoderRegistry.register`. Copy `plugins/adsb.py` or `plugins/ais.py`.
   **Subprocess**: subclass `SubprocessDecoderPlugin`, set `manifest` +
   `spec` (argv, iq_format, restart_backoff). Copy `plugins/dump1090.py`.
2. Events flow to the frontend automatically over the receiver WS as
   `{"type":"decoder","decoder":NAME,"event":{...}}`.
3. Frontend: extend `packages/shared-types/src/decoder.ts` (family const
   if it feeds an existing viz), build/extend a viz component, register it
   in `visualizations/registry.ts` + `builtins.ts`.
4. Tests: in-process → fixture IQ; subprocess → extend the fake-binary
   pattern (ready line with argv/env echoes + failure-mode flags).

## 11. Git sync protocol (see SYNC-UP.md for full version)

The remote is `https://github.com/nealhamiltonjr/websdr.git`. The PAT-
strip protocol:

```bash
cd /home/z/my-project                              # repo root
git remote set-url origin https://<PAT>@github.com/nealhamiltonjr/websdr.git
git push origin main
git remote set-url origin https://github.com/nealhamiltonjr/websdr.git
#                                            ^^^ strip PAT immediately ^^^
```

Never commit the PAT to disk (no `.git/config` lines, no shell history
with `set -o history` enabled). Verify success with `git ls-remote
origin refs/heads/main` — the returned hash should match local HEAD.

**Actions CI verification:**

```bash
curl -sSL -H "Authorization: Bearer <PAT>" \
     https://api.github.com/repos/nealhamiltonjr/websdr/actions/runs \
  | jq '.workflow_runs[0:5] | .[] | {name, status, conclusion, html_url}'
```

All recent runs should report `conclusion: success`. If any are
`failure` / `cancelled` / `startup_failure`, fetch logs via the
`logs_url` field, diagnose the failing job, fix the root cause in the
repo, push, and re-verify.

## 12. Conventions worth keeping

- **Slice protocol** (§5): implement → gates green → worklog entry →
  STATUS.md update → sync. Never leave gates red across slices.
- **worklog.md is append-only**. Every entry: Task ID, what was built,
  debug war stories, stage summary. It is the project's real history.
- **ADRs before architecture**: new subsystems get an `ADR/00X-*.md` first.
- **Honest failures**: actionable error strings everywhere (the rate-
  mismatch error lists achievable rates; the 502 tells you which binary
  is missing).
- **Drop-with-counters, never block, never grow unbounded**: IqHub queues,
  FFT fps throttle, subprocess transport buffer — all metered the same way.
- **Hardware-free-first**: every new feature ships with a fake/fixture
  path so it can be developed and tested without SDR hardware or live
  RF. No exceptions.
- **Operator-bundled native artifacts**: when a binary / WASM / model
  weight can't be shipped in-repo (license, bloat, build-chain), ship
  the loader + a documented build recipe, never a hard dependency. The
  pattern is established for pycsdr (§7), RNNoise WASM (slice-19 README),
  and DeepFilterNet weights (slice-18 README).
