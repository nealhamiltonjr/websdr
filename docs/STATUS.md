# OpenWebRX+ — Status & Roadmap

**Updated:** 2026-08-27, after slice-5.3 (IQ-to-audio ADR + full-app E2E smoke test)
**Supersedes:** `docs/slice-01-plan.md` as the living status doc (kept for history).
**Companion:** `ADR/` for decision records; `docs/slice-01-plan.md` for the original slice plan (kept for history).

---

## TL;DR

The platform is a working, hardware-free, end-to-end SDR receiver: 11 source backends (real drivers, IQ-file replay, synthetic, VFO taps, and four internet remotes) → pycsdr C-backed DSP → binary FFT/audio over WebSocket → a SolidJS + Dockview + WebGL2 multi-receiver workspace with per-receiver tuning, linked crosshairs, gain/DSP controls, and TWO live ADS-B decoders — the in-process Mode S stack and the subprocess dump1090 plugin (ADR-003's second family, crash-restart + metered backpressure included). All quality gates pass. The next frontier is decoders-plus-maps, the AI cascade modules, and federation polish.

## Verified health (this snapshot, 2026-08-27)

| Gate | Result |
|---|---|
| Server tests (`scripts/run-server-tests.sh`) | **298/298 pass**, 84% coverage, ~71 s |
| `mypy --strict` (43 files) | **clean** |
| `ruff check` | **clean** |
| Web `vitest` | **95/95 pass** (8 files) |
| Web `tsc --noEmit` | **clean** |
| `vite build` | clean (this session) |

- Codebase size: **~17.5 k lines** Python (server + tests, 25 test files) · **~9.4 k lines** TS/TSX (web).
- Registered sources: **11** (`rtl_sdr`, `rtl_tcp`, `airspy`, `sdrplay`, `soapy`, `kiwi`, `spyserver`, `openwebrx_remote`, `vfo`, `file`, `simulated`). Decoder plugins: **2** (`adsb` in-process, `dump1090` subprocess) — both feed the same aircraft-table viz.
- ADRs accepted: **7** (001 workspace, 002 DSP+AI cascade, 003 decoders, 004 pycsdr/sources, 005 VFO, 006 federation, 007 IQ-to-audio enhancement rejection).
- In-app panels (slice-5.1): **Settings** (display/audio/DSP/sources/decoders/debug sections, persisted to TOML) + **Debugger** (live log ring buffer + error capture + filters + export + auto-refresh).
- DSP controls (slice-5.2): per-receiver **DSPControls** drawer with bandpass width / AGC / squelch / DC block / de-emphasis / manual gain (wired to pycsdr blocks); notch filter + noise blanker accepted but stubbed (slice-5.3 will implement them via custom Python or upstream pycsdr contribution).

## Codebase map

```
openwebrx-plus/
├── apps/server/openwebrx_plus/        # Python backend (uv venv)
│   ├── sources/        # 10 backends + SourceRegistry manifest contract
│   │   ├── base.py     # Source protocol, manifests, RuntimeGainSource, DisplayStreamSource
│   │   ├── rtl_sdr.py  # usb (ctypes) / tcp / subprocess transports, V4 HF direct-sampling
│   │   ├── rtl_tcp.py  # remote rtl_tcp/rsp_tcp client (shared wire impl + gain_q channel)
│   │   ├── airspy.py   # ctypes libairspy, 3-stage gain, bias tee
│   │   ├── sdrplay.py  # cffi ABI v3, gRdB semantics, RSPduo noted as HW-VFO anchor
│   │   ├── soapy.py    # universal SoapySDR transport
│   │   ├── kiwi.py     # KiwiSDR websocket client (Tier B channelized IQ)
│   │   ├── spyserver.py # SpyServer TCP client (Tier A raw IQ, protocol v2)
│   │   ├── openwebrx_remote.py  # OpenWebRX(+) federation client + ADPCM codecs
│   │   ├── wideband.py # IqHub fan-out + VfoChain DDC (ADR-005)
│   │   ├── file_source.py / simulated.py  # hardware-free dev sources (digital gain)
│   │   ├── directory.py    # rx.kiwisdr.com + receiverbook.de (TTL cache)
│   │   ├── probe.py    # GET /api/hardware concurrent sweep
│   │   └── _adpcm.py / _hw_common.py  # codec ports; callback→asyncio bridge, pacer
│   ├── dsp/            # pycsdr chains: FftChain, AudioChain (6 modes, raw/classic)
│   ├── sessions/       # ReceiverSession (IQ + display-stream paths), SessionRegistry
│   ├── plugins/        # DecoderPlugin ABC + registry; adsb.py (Mode S in-process),
│   │                   # dump1090.py + subprocess.py (PluginRunner, ADR-003 family #2)
│   ├── api/            # REST (receivers/sources/hardware/directory/decoders/fixtures) + WS pump
│   ├── config/ observability/
│   └── tests/          # 20 test files + fakes/fake_dump1090.py, 229 tests
├── apps/web/src/       # SolidJS frontend (vite)
│   ├── routes/         # main.tsx (tuning model + re-adoption + tune handler), popout.tsx
│   ├── components/     # AddReceiverModal (4 sections), RemoteBrowser, TuningBar,
│   │   │               # sourceForms, WorkspaceManager, GroupActions
│   │   └── workspace/  # layoutModel (localStorage v1 + stripReceivers), VizPanel
│   ├── visualizations/ # Waterfall/Spectrum/SMeter/FreqCounter/AircraftList + registry,
│   │                   # freqAxis (passbands), crosshair, tuneBus, aircraftModel
│   ├── lib/webgl2/     # Waterfall/Spectrum renderers + overlay (crosshair/passband)
│   ├── lib/audio/      # AudioPlayer (Web Audio scheduled buffers)
│   ├── sessions/       # ReceiverSession, receiverTuning model, tuneBus
│   ├── workers/        # sdr.shared-worker.ts (one WS per receiver, fan-out)
│   └── lib/api.ts      # typed REST client
├── packages/shared-types/  # TS + Python mirrors of wire formats (fft/audio/metadata/decoder)
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

## What works end-to-end today

- Boot → fixture-backed default receiver → live waterfall/spectrum/S-meter/freq-counter, dockable workspace, layouts persist across reloads (with backend re-adoption).
- Spawn receivers from: any local SDR driver, IQ file (fixture picker), synthetic presets, any public OpenWebRX/KiwiSDR/SpyServer endpoint (paste host/port or browse directories), or as VFO taps off a parent — all through one modal.
- Tune any receiver (slider/presets/click-on-canvas), switch mode, set gain (auto or dB), pick DSP mode raw/classic — per-receiver, with REST/metadata echoes and honest rejections.
- Attach an ADS-B decoder to a 2 Msps receiver → live aircraft table — in-process `adsb` (one click) or subprocess `dump1090` (REST; positions + lifecycle state when the binary decodes them) — proven on the baked fixture: 3 aircraft, CRC-valid frames, both plugin families.
- Everything above runs **hardware-free**; the same code paths take real hardware (drivers flagged for first-live-connection checks).

## What's left

### Next up (priority order — accumulated from slice exit notes)

1. **AIS decoder + map visualizations** — `AircraftMapViz`/`AisMapViz` on MapLibre (Pillar 4 "VRS-killer" story); the AIS demod can now drop onto the subprocess PluginRunner (argv+manifest only) or the in-process pattern; needs a map tile strategy.
2. **dump978 UAT** — second ADS-B decoder; the runner ships, this is argv+manifest+viz wiring.
3. **S-Meter / freq-counter linked readout** — join the cursor channel (slice-4.6 pattern is in place).
4. **Runtime-gain gaps** — soapy/airspy/sdrplay/kiwi currently spawn-time-only (rtl_tcp + USB rtl-sdr + digital file/sim + spyserver wire are done).
5. **Popout crosshair sync** — broadcast CursorState via SharedWorker (design noted in slice-4.6).
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
