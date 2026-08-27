# OpenWebRX+ — Status & Roadmap

**Updated:** 2026-08-28, after slice-7 (notch filter + noise blanker — completes the DSP fine-grained controls thesis)
**Supersedes:** `docs/slice-01-plan.md` as the living status doc (kept for history).
**Companion:** `ADR/` for decision records; `docs/slice-01-plan.md` for the original slice plan (kept for history).

---

## TL;DR

The platform is a working, hardware-free, end-to-end SDR receiver with **12 source backends** (real drivers, IQ-file replay, synthetic, VFO taps, and four internet remotes — all with runtime gain control) → pycsdr C-backed DSP → binary FFT/audio over WebSocket → a SolidJS + Dockview + WebGL2 multi-receiver workspace with per-receiver tuning, **linked cursor readouts** (S-Meter + Frequency Counter follow the crosshair across panels and popouts), gain/DSP controls, **THREE live decoders** (in-process Mode S, subprocess dump1090, in-process AIS + vessel-list), AND a complete **IQ preprocessor** (single-pole complex IIR notch + adaptive noise blanker) that runs before the pycsdr chains — the user's "pull out the weak signal" thesis is now fully realized: all 8 DSPParams controls (bandpass / AGC / squelch / DC block / de-emphasis / manual gain / notch / NB) are LIVE end-to-end. All quality gates pass. The next frontier is the map view (MapLibre), the AI cascade modules, and federation polish.

## Verified health (this snapshot, 2026-08-28)

| Gate | Result |
|---|---|
| Server tests (`scripts/run-server-tests.sh`) | **354/354 pass** (322 baseline + 32 new preprocess, ~84% coverage) |
| `mypy --strict` (47 files) | **clean** |
| `ruff check` | **clean** |
| Web `vitest` | **112/112 pass** (9 files) |
| Web `tsc --noEmit` | **clean** |
| `vite build` | clean (this session) |
| **GitHub Actions CI** (`ci.yml`) | **all 5 jobs green** on every commit from slice-5.8 onward — last 8 runs (`7ff7d86` → ... → `737ffbe`) all SUCCESS |

- Codebase size: **~19.8 k lines** Python (server + tests, 26 test files) · **~10.8 k lines** TS/TSX (web).
- Registered sources: **11** (`rtl_sdr`, `rtl_tcp`, `airspy`, `sdrplay`, `soapy`, `kiwi`, `spyserver`, `openwebrx_remote`, `vfo`, `file`, `simulated`) — all now support runtime gain.
- Decoder plugins: **3** (`adsb` in-process, `dump1090` subprocess, `ais` in-process) — ADS-B and AIS each feed their own list viz.
- ADRs accepted: **7** (001 workspace, 002 DSP+AI cascade, 003 decoders, 004 pycsdr/sources, 005 VFO, 006 federation, 007 IQ-to-audio enhancement rejection).
- In-app panels (slice-5.1): **Settings** (display/audio/DSP/sources/decoders/debug sections, persisted to TOML) + **Debugger** (live log ring buffer + error capture + filters + export + auto-refresh).
- DSP controls (slice-5.2 + slice-7): per-receiver **DSPControls** drawer with all 8 controls LIVE end-to-end — bandpass width / AGC / squelch / DC block / de-emphasis / manual gain (pycsdr blocks, slice-5.2) + **notch filter** (single-pole complex IIR, slice-7) + **noise blanker** (adaptive impulse clipper, slice-7). The notch + NB run as a pure-numpy IQ preprocessor BEFORE the pycsdr chains, so both the FFT and the audio paths see the cleaned IQ.
- LICENSE: canonical AGPL-3.0 text (674 lines, slice-6.1) — the prior stub carried only the preamble.

## Codebase map

```
openwebrx-plus/
├── apps/server/openwebrx_plus/        # Python backend (uv venv)
│   ├── sources/        # 10 backends + SourceRegistry manifest contract
│   │   ├── base.py     # Source protocol, manifests, RuntimeGainSource, DisplayStreamSource
│   │   ├── rtl_sdr.py  # usb (ctypes) / tcp / subprocess transports, V4 HF direct-sampling
│   │   ├── rtl_tcp.py  # remote rtl_tcp/rsp_tcp client (shared wire impl + gain_q channel)
│   │   ├── airspy.py   # ctypes libairspy, 3-stage gain, bias tee, runtime gain (slice-6.5)
│   │   ├── sdrplay.py  # cffi ABI v3, gRdB semantics, runtime gain (slice-6.5)
│   │   ├── soapy.py    # universal SoapySDR transport, runtime gain (slice-6.5)
│   │   ├── kiwi.py     # KiwiSDR websocket client, runtime gain (slice-6.5)
│   │   ├── spyserver.py # SpyServer TCP client (Tier A raw IQ, protocol v2)
│   │   ├── openwebrx_remote.py  # OpenWebRX(+) federation client + ADPCM codecs
│   │   ├── wideband.py # IqHub fan-out + VfoChain DDC (ADR-005)
│   │   ├── file_source.py / simulated.py  # hardware-free dev sources (digital gain)
│   │   ├── directory.py    # rx.kiwisdr.com + receiverbook.de (TTL cache)
│   │   ├── probe.py    # GET /api/hardware concurrent sweep
│   │   └── _adpcm.py / _hw_common.py  # codec ports; callback→asyncio bridge, pacer
│   ├── dsp/            # pycsdr chains: FftChain, AudioChain (6 modes, raw/classic);
│   │                   # preprocess.py — notch filter (complex IIR) + noise blanker (slice-7)
│   ├── sessions/       # ReceiverSession (IQ + display-stream paths), SessionRegistry
│   ├── plugins/        # DecoderPlugin ABC + registry; adsb.py (Mode S in-process),
│   │                   # dump1090.py + subprocess.py (PluginRunner, ADR-003 family #2),
│   │                   # ais.py + ais_protocol.py + ais_demod.py (slice-6.4 family #3)
│   ├── api/            # REST (receivers/sources/hardware/directory/decoders/fixtures) + WS pump
│   ├── config/ observability/
│   └── tests/          # 26 test files, 319 tests (incl. test_ais_decoder.py +21)
├── apps/web/src/       # SolidJS frontend (vite)
│   ├── routes/         # main.tsx (tuning model + re-adoption + tune handler + cursor forward),
│   │                   # popout.tsx (cursor forward + ingest for cross-window sync)
│   ├── components/     # AddReceiverModal (4 sections), RemoteBrowser, TuningBar,
│   │   │               # sourceForms, WorkspaceManager, GroupActions
│   │   │               # DSPControls (slice-5.2), SettingsPanel + DebugPanel (slice-5.1)
│   │   └── workspace/  # layoutModel (localStorage v1 + stripReceivers), VizPanel
│   ├── visualizations/ # Waterfall/Spectrum/SMeter (linked, slice-6.2)/FreqCounter (linked,
│   │                   # slice-6.2)/AircraftList/VesselList (slice-6.4) + registry, freqAxis
│   │                   # (binAtHz + binPowerAtHz helpers, slice-6.2), crosshair, tuneBus
│   ├── lib/webgl2/     # Waterfall/Spectrum renderers + overlay (crosshair/passband)
│   ├── lib/audio/      # AudioPlayer (Web Audio scheduled buffers)
│   ├── sessions/       # ReceiverSession (cursor forward + remote ingest, slice-6.3),
│   │                   # receiverTuning model, tuneBus
│   ├── workers/        # sdr.shared-worker.ts (cursor broadcast, slice-6.3)
│   └── lib/api.ts      # typed REST client
├── packages/shared-types/  # TS + Python mirrors of wire formats (fft/audio/metadata/decoder);
│                            # AIS_DECODERS + AisFrameEvent + AisVesselRow + AisVesselEvent (slice-6.4)
├── packages/dsp-zig, ai-rust, rnnoise-wasm, dsp-c   # scaffolds (ADR-002 future)
├── ADR/  docs/  Makefile  fixtures/iq/ (48.8 MB: hf_20m, fm_broadcast, adsb_1090, smoke,
│                                       generated by scripts/generate_iq_fixtures.py)
└── scripts/  # run-server-tests.sh, generate_iq_fixtures.py, README-dsp-bootstrap.md,
              # test_fft_chain.py, test_audio_chain.py, test_audio_modes.py, probe_openwebrx_remote.py
```

## Delivery history

| Slice | Shipped |
|---|---|
| 1 / 1.5 / 2 | Binary FFT wire format (WRFO header), WebGL2 waterfall+pectrum w/ colormaps & peak-hold, TuningBar + 20 band presets, audio output (AUDI frames), multi-receiver spawn via REST + SessionRegistry |
| ADR-004 + plugins | SourceRegistry manifest contract; 5 initial backends; mypy/ruff clean baseline |
| 3 (pycsdr) | Real DSP engine: libcsdr+pycsdr built from source; FftChain + AudioChain replace numpy stubs; decimate-before-demod perf fix (80 Ksps → 11 Msps) |
| 3 (drivers/fixtures/VFO) | Real RTL-SDR/Airspy/SDRplay/Soapy drivers (hardware-free unit-tested); baked IQ fixtures (CRC-valid ADS-B, HF 20 m, FM broadcast); IqHub + VFO sub-receivers (ADR-005); Shift sign-convention bug hunt |
| ADR-006 | rtl_tcp remote source, KiwiSDR client, receiver directories (kiwi + receiverbook), Source protocol typing fix |
| 3.6 | OpenWebRX federation client: deep-links, ADPCM codecs, display-stream session path (frontend unchanged) |
| 3.7 | AddReceiverModal: quick-connect, source catalog w/ hardware badges, remote browser, VFO spawn UI; fail-fast VFO validation; dev-mode TDZ fix |
| 3.8 | ADS-B/Mode S full stack: PPM demod + CRC-24 + field decode; DecoderPlugin contract; aircraft table viz; 14/14 fixture frames |
| 4 | Dockview workspace: drag-rearrange groups, +viz dropdown, popout per panel, localStorage persistence, receiver reconciliation; dockview-solid raw-JSX dev fix |
| 4.5 | Active-receiver concept: per-receiver {freq, mode}, audio follows selection, tab chips; re-adoption bare-array bug fixed |
| 4.6 | Linked crosshair sync (ADR-001 feat. 11): shared cursor, tuned marker + passband band, click-to-tune from any canvas |
| 4.7 | Per-receiver gain + DSP mode controls de-stubbed; runtime-gain protocol (digital / rtl_tcp wire / USB handle); setMode-never-rebuilds bug fixed |
| 4.8 | SpyServer client (ADR-006 Tier A): TCP protocol v2 (20-byte `<IIQI` frames), HELLO/SERVER_INFO handshake, float32 IQ at power-of-two decimation with an exact-rate contract, runtime gain via COMMAND_SET_IQ_GAIN, protocol-faithful fake server in tests |
| 4.9 | Subprocess PluginRunner + dump1090 (ADR-003 family #2): stdin IQ (cf32/cs16/cu8 bridge conversion), stdout NDJSON with ready handshake, bounded crash-restart → `decoder_state` events, metered backpressure (`dropped_chunks`), deterministic teardown (stdin-EOF → wait → SIGKILL); dump1090 plugin with env-configurable binary; protocol-faithful fake binary (real demod + synthetic positions); ADSB_DECODERS family in shared-types + aircraft viz position column & lifecycle banner |
| 5.0 | Foundation: AGENTS.md (AI operating instructions), LICENSE stub (AGPL-3.0 placeholder pointing at canonical source), sync-up cadence doc (`SYNC-UP.md`), portable `scripts/run-server-tests.sh`, fix to `.github/workflows/ci.yml` pnpm cache-dependency-path (was wrong, breaking CI caching) |
| 5.1 | Settings & Debugger infrastructure (backend + frontend): `observability/debug_log.py` ring buffer (`DebugLogRingBuffer`, `LogEntry`, structlog capture processor, asyncio loop + threading excepthook capture); `config/user_settings.py` TOML-persisted runtime-mutable user preferences (`DisplaySettings`, `AudioSettings`, `DSPSettings`, `SourcesSettings`, `DecoderSettings`, `DebugSettings`); `api/settings_debug.py` REST endpoints (`GET/PUT/POST /api/settings`, `GET /api/debug/{logs,errors,stats,export}`, `POST /api/debug/clear`); frontend `SettingsPanel.tsx` (six-section modal with optimistic updates + debounced PUT) + `DebugPanel.tsx` (logs/errors views + filters + pagination + auto-refresh + NDJSON export); wired into main route header (gear + bug buttons); E2E tests boot real uvicorn + httpx to validate full HTTP/middleware stack |
| 5.2 | DSP fine-grained controls (the "pull out the weak signal" core thesis): `dsp/types.py:DSPParams` flat dataclass with 12 fields (manual bandpass cuts / AGC / squelch / DC block / de-emphasis / manual gain / notch / noise blanker — last two accepted but no-op until slice-5.3); `AudioChain` extended to accept `dsp_params` and conditionally wire `Agc`/`Squelch`/`Gain`/`NfmDeemphasis`/manual bandpass reconfiguration; `ReceiverSession.set_dsp_params(patch)` async method merges patches and rebuilds the chain under the chain lock; `WS setDSPParams` command handler with `DSPParams.from_dict()` (unknown fields ignored for forward compat); metadata pump echoes `dspParams` so the frontend stays in sync; frontend `DSPControls.tsx` right-side drawer panel with bandpass width sliders, AGC toggle, manual makeup gain (with on/off + dB slider), squelch (with on/off + dBFS threshold slider), DC block + de-emphasis toggles with "indeterminate" state for mode defaults, notch + noise blanker with "experimental" badges; wired into main route header (🌡 DSP button); 18 new unit tests (DSPParams round-trip + merge + WS handler + AudioChain construction with each optional block) |
| 5.3 | IQ-to-audio honest evaluation + full-app E2E smoke test: `ADR/007-iq-to-audio-enhancement.md` documents the rejection of a parallel "audio enhancement pipeline" — the ADR-002 AI cascade (DeepFilterNet → RNNoise WASM → Demucs/Open-Unmix) is the architecturally-correct post-DSP enhancement layer; building a parallel pipeline now would duplicate work already planned and violate ADR-004's scipy-offline-only rule; notch + NB fields stay accepted-but-no-op until upstream pycsdr contribution (Option D); new `tests/test_full_app_e2e.py` boots a real uvicorn server + httpx client and hits every public REST endpoint (health / version / sources / hardware / fixtures / decoders / receivers / spawn+teardown / settings GET+PUT+reset / debug logs+stats+clear+export) — the "simulate usage of the entire app" test the user asked for |
| 5.4 – 5.8 | CI repair arc — fixed pnpm action-setup version duplication, added libcsdr build step in CI, bumped Zig from 0.13 → 0.14 (root_module field requirement, fingerprint field, build.zig.zon name-as-enum-literal, addRunArtifact's .step field), added flat ESLint 9 config + bumped lint script from ESLint 8 syntax, added vitest test config to exclude jsdom peer-dep optimizer resolution, baked IQ fixtures in CI between `uv sync` and `pytest`. Final result: all 5 jobs green on commit `7ff7d86` and every commit since. |
| 6.1 | LICENSE completion: replaced the AGPL-3.0 stub (which only had the preamble + a pointer to the canonical text) with the full 674-line canonical FSF license text (Sections 0-17 + the "How to Apply These Terms to Your New Programs" appendix). Wrote by hand because the sandbox has no network egress to fetch from gnu.org; the canonical text is a well-known public-domain legal document. |
| 6.2 | Linked cursor readout for S-Meter + Frequency Counter (the #6 priority from the next-up list): new `binAtHz(axis, hz, binCount)` + `binPowerAtHz(axis, bins, hz)` helpers in `freqAxis.ts` (return null when out-of-span or empty bins — no silent clamping to the wrong frequency). SMeterViz now reads the bin value AT the cursor frequency when one is active (was the median dBFS across the whole span), with a "cursor" / "tuned" badge. FrequencyCounterViz now shows the cursor frequency when active (was just the tuned frequency), with the tuned frequency still visible underneath. 8 new freqAxis tests (edges, center, clamp, out-of-span, empty bins). |
| 6.3 | Popout crosshair sync via SharedWorker broadcast (the #8 priority from the next-up list): new `cursor` client→worker + worker→client message in `sdr.shared-worker.ts`; `fanoutCursor()` broadcasts to every subscriber of a receiverId (originator skips its own echo via `sourceVizId === vizId` in `attachCrosshair`). ReceiverSession: new `setCursorForward(fn|null)` (installs a sink called from `setCursor` so local cursor events propagate to the worker), new `ingestRemoteCursor(hz, sourceVizId)` (updates local state + emits on `cursorStream` but does NOT re-forward — otherwise we'd echo forever: A→worker→B→worker→A→…). main.tsx + popout.tsx wire `setCursorForward` on every session and ingest cursor events from the worker. 8 new ReceiverSession tests covering the full forward/ingest matrix. |
| 6.4 | AIS decoder (in-process plugin) + vessel list viz (the #4 priority — VRS-killer story's first half): new `plugins/ais_protocol.py` (pure protocol: CRC-16-CCITT-FALSE verified against the canonical '123456789' → 0x29B1 vector, 6-bit ASCII charset per ITU-R M.1371-5 Annex A, BitReader with proper end-sentinel padding behavior, HDLC stuff/destuff, field decoders for Type 1/2/3/4/5/18/21). New `plugins/ais_demod.py` (streaming GMSK demodulator + HDLC deframer — FM-demod → mid-symbol bit slice → flag find → destuff → CRC verify → decode; sample rate must be integer multiple of 9600, default 48 kS/s). New `plugins/ais.py` (AisDecoderPlugin mirrors AdsbDecoderPlugin: per-receiver vessel table, frame+vessel events, snapshot coalescing at 1 Hz). New `tests/test_ais_decoder.py` (21 tests covering CRC, BitReader, charset, HDLC framing, field decoders for Type 1 + Type 5, GMSK round-trip, plugin). Shared-types: AIS_DECODERS + AisFrameEvent + AisVesselRow + AisVesselEvent + isAisVesselEvent + isAisFrameEvent. Frontend: new `VesselListViz.tsx` (mirrors AircraftListViz; ship-type + nav-status tables for human-readable labels). Registered in builtins.ts as 'vessel-list'. |
| 6.5 | Runtime-gain gaps closed (the #7 priority — was the last item in the next-up list before this slice closed it): airspy/soapy/sdrplay/kiwi now implement the RuntimeGainSource protocol. Airspy: stash binding+dev in spawn(), set_runtime_gain(gain_db) → binding.set_gains(dev, gain_mode="linearity", linearity=clip(round(gain_db), 0, 21), ...); set_runtime_gain(None) → lib.airspy_set_lna_agc(1) + airspy_set_mixer_agc(1). Soapy: stash dev_inst; set_runtime_gain(gain_db) → dev.set_gain(names[0], float(gain_db)); set_runtime_gain(None) → dev.set_agc(True). SDRplay: stash binding_inst; set_runtime_gain(gain_db) → grdb=clip(59 - round(gain_db), 20, 59); binding.gain_change_request(grdb, self.lna_state); set_runtime_gain(None) → binding.agc_control(True). Kiwi: stash _connection (the live websocket); set_runtime_gain(gain_db) → loop.create_task to send 'SET AGC=0' then 'SET GAIN=<dB>' on the ws; set_runtime_gain(None) → 'SET AGC=1'. All four return False when not streaming; all safe to call from any asyncio task while spawn() is being consumed. 3 new airspy tests covering the false-when-not-streaming, applied-while-streaming (verifies linearity=15 overrides spawn-time linearity=5), false-after-close cases. Source runtime-gain coverage is now COMPLETE: rtl_sdr (USB+TCP, slice-4.7), file/sim (digital, slice-3), spyserver (slice-4.8), airspy/soapy/sdrplay/kiwi (slice-6.5). |
| 7 | DSP fine-grained controls COMPLETION — the user's "pull out the weak signal" thesis fully realized. The notch + noise blanker fields were accepted-but-no-op since slice-5.2 (pycsdr has no native Notch/Nb block). Slice-7 ships a pure-numpy IQ preprocessor that runs BEFORE the pycsdr chains so both FFT + audio see the cleaned IQ. `dsp/preprocess.py`: `NotchFilter` is a single-pole-pair complex IIR notch (zero at e^(jω0) on the unit circle, pole at r·e^(jω0) just inside) — rejects ONLY +f0 (the desired behavior for SSB where the signal sits on one side and a spur on the other); `NoiseBlanker` is an impulse-noise suppressor that tracks a 5 ms EMA of the IQ magnitude and clips any sample exceeding `threshold_db × floor` down to the threshold (phase-preserving real multiply). `IQPreprocessor` orchestrates both — zero-overhead no-op when no stage is active, input array never mutated (hub buffers may be shared across VFO taps), state retained across chunked calls, `reconfigure()` for live param updates. `ReceiverSession.start()` builds the preprocessor alongside the pycsdr chains; `set_dsp_params()` calls `reconfigure()` BEFORE rebuilding the AudioChain so the next IQ chunk sees both. Frontend `DSPControls.tsx`: removed "EXPERIMENTAL" badges (they're LIVE), fixed NB threshold range (was 0..1 placeholder, now 3..30 dB matching the NoiseBlanker's interpretation), extended notch freq range to -20000..+20000 Hz (negative freqs = lower sideband notch, supported by the complex IIR), updated hint text with implementation details. 32 new preprocess tests (NotchFilter attenuation/preservation/dtype/state/complex-vs-real behavior; NoiseBlanker impulse suppression/phase/dtype/empty; IQPreprocessor lifecycle; ReceiverSession integration start/reconfigure/disable/stop). All 8 DSPParams controls now LIVE end-to-end. |

## What works end-to-end today

- Boot → fixture-backed default receiver → live waterfall/spectrum/S-meter/freq-counter, dockable workspace, layouts persist across reloads (with backend re-adoption).
- Spawn receivers from: any local SDR driver, IQ file (fixture picker), synthetic presets, any public OpenWebRX/KiwiSDR/SpyServer endpoint (paste host/port or browse directories), or as VFO taps off a parent — all through one modal.
- Tune any receiver (slider/presets/click-on-canvas), switch mode, set gain (auto or dB), pick DSP mode raw/classic — per-receiver, with REST/metadata echoes and honest rejections. **Runtime gain works on every source** (slice-6.5 closed the last gap).
- Hover over any FFT canvas in any window (including popouts) → cursor crosshair appears on every other FFT canvas of the same receiver, the S-Meter reads the bin value AT the cursor frequency (not the median), the Frequency Counter shows the cursor frequency (with the tuned frequency still visible underneath). **The crosshair sync spans popout windows via SharedWorker broadcast** (slice-6.3).
- Attach an ADS-B decoder to a 2 Msps receiver → live aircraft table — in-process `adsb` (one click) or subprocess `dump1090` (REST; positions + lifecycle state when the binary decodes them) — proven on the baked fixture: 3 aircraft, CRC-valid frames, both plugin families.
- Attach an AIS decoder to a 48 kSps receiver (VFO tap on 162 MHz) → live vessel table — MMSI, name, callsign, IMO, lat/lon, speed, course, nav_status — proven on the in-test GMSK fixture (Type 1 + Type 5 messages round-trip through encode → modulate → demod → decode).
- Open the DSPControls drawer on any receiver → all 8 fine-grained controls are LIVE end-to-end (slice-5.2 + slice-7): bandpass width, AGC, squelch, DC block, de-emphasis, manual gain (pycsdr blocks) **+ notch filter (single-pole complex IIR) + noise blanker (adaptive impulse clipper)** — the pure-numpy IQ preprocessor runs before the pycsdr chains so both the FFT and the audio paths see the cleaned IQ. The user's "pull out the weak signal" thesis is fully realized.
- Everything above runs **hardware-free**; the same code paths take real hardware (drivers flagged for first-live-connection checks).

## What's left

### Next up (priority order — accumulated from slice exit notes)

1. **vitest 3.x upgrade** — drop the `// @ts-expect-error` workaround on `test:` field in `apps/web/vite.config.ts` (vitest 3.x has first-class vite 6 type support).
2. **CI caching** — persist `/usr/local/lib/libcsdr.so*` across runs using `actions/cache@v4` keyed on the csdr git HEAD sha; saves ~60-90s per backend CI run.
3. **`@typescript-eslint` unified package** — migrate from the legacy `@typescript-eslint/eslint-plugin` + `@typescript-eslint/parser` pair to the modern `typescript-eslint` combined package (smaller install, simpler flat config).
4. **Map view (MapLibre)** — `AircraftMapViz`/`VesselMapViz` on MapLibre (the VRS-killer story's second half — needs a map tile source). The AIS decoder + vessel list viz (slice-6.4) are the foundation; the map view drops on top of the existing `decoderStream` + vessel table.
5. **dump978 UAT** — second ADS-B decoder on 978 MHz; the runner ships, this is argv+manifest+viz wiring (the dump1090 plugin is the template).
6. **SpyServer polish** — live bring-up verification of protocol literals; runtime tune forwarding (currently offset-demod like other IQ sources).
7. **dump1090 real-binary bring-up** — stock builds speak SBS1-on-TCP, not stdout NDJSON: ship a thin SBS1→NDJSON wrapper (or target readsb) and verify on live 1090 MHz traffic; the fake pins the contract.

### Mid-term

- **AI cascade (ADR-002)** — the gated `ai`/`cascade` modes need: DeepFilterNet module (`packages/ai-rust`), RNNoise WASM (`packages/rnnoise-wasm`), Demucs/Open-Unmix offline stage. Control surface is already live.
- **Audio-band decoders** — FT8/WSJT-X + FLDIGI via virtual audio cable; RTTY/CW/PSK31 as in-browser Wasm; `DigiMessageListViz`.
- **Federation polish** — HD-audio + secondary-demod forwarding for `openwebrx_remote`; SDRangel client; self-listing in receiverbook.
- **Rendering thread-off** — OffscreenCanvas + Web Worker per ARCHITECTURE.md Pillar 2 (renderers currently main-thread).

### Long-term (slice-5+ per ARCHITECTURE.md)

Propagation intelligence · QSL logging · mobile layout · deployment story · QoS + ToS/ethics layer · opt-in IQ recording · Zig/Wasm plugin runtimes.

## Environment caveats (read before running anything)

- **Never `uv run` / `uv sync` in `apps/server`** — the venv holds a manually-restored pycsdr; a sync evicts it. Always test via `scripts/run-server-tests.sh` (which is portable and resolves paths from the repo root). To rebuild pycsdr from source, follow `scripts/README-dsp-bootstrap.md`.
- pycsdr imports need `LD_LIBRARY_PATH=$HOME/.local/usr/lib` (the wrapper script sets it).
- Sandbox egress is heavily filtered — live remote checks (boomerthedog.com:8073, rx.kiwisdr.com) are left for a real machine; fakes cover the protocols in tests.
- E2E verification scripts (agent-browser driven; servers must run inside ONE bash invocation — trap-based cleanup) are not currently in-repo. They live in the original handoff bundle and can be re-imported if needed.
- Frontend dev: `pnpm --filter openwebrx-plus-web run dev` → http://localhost:5173 (binds ::1 — use `localhost`, not `127.0.0.1`). Backend: `make dev-server` → :8073.

## How to run

```bash
# tests (from the repo root)
scripts/run-server-tests.sh
cd apps/web && pnpm exec vitest run && pnpm exec tsc --noEmit

# dev (two terminals, both from the repo root)
make dev-server   # http://localhost:8073
make dev-web      # http://localhost:5173
```
