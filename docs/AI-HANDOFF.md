# OpenWebRX+ — Complete Project Handoff (for an AI continuing this work)

**Snapshot date:** 2026-08-27 · **State:** post slice-4.9 · **All quality gates green**

This document is the single entry point for an AI (or human) picking this project
up cold. It explains what the project is, the tech stack, exactly where
development stands (verified, not aspirational), what remains with concrete
implementation guidance for each item, and how to run / test / simulate-test
everything without SDR hardware.

Companion documents (read in this order):
1. **This file** — orientation + roadmap + how-to-run.
2. `STATUS.md` (zip root) — the living status doc kept in-repo at
   `openwebrx-plus/docs/STATUS.md`: verified health table, codebase map,
   delivery history.
3. `worklog.md` (zip root) — the full per-slice implementation log: 16 entries,
   every design decision and debug war story.
4. `openwebrx-plus/ADR/` — 6 accepted Architecture Decision Records (workspace,
   DSP+AI cascade, decoder plugins, pycsdr/sources, VFO wideband, federation).
5. `openwebrx-plus/ARCHITECTURE.md` — the original pillars/vision document.

---

## 1. What this project is

**OpenWebRX+ is a ground-up modernization of OpenWebRX**: a browser-native,
multi-receiver SDR (Software-Defined Radio) platform. One server taps SDR
hardware (or IQ recordings, or remote receivers on the internet) and streams
spectrum, audio, and decoded digital signals to a rich single-page web app.

What makes it different from upstream OpenWebRX:

- **Multi-receiver workspace** — spawn N receivers from one SDR (VFO taps via a
  DDC fan-out), arrange their waterfalls/spectra/meters in a dockable
  (drag-and-drop) layout, pop panels out into windows, persist layouts.
- **Modern web frontend** — SolidJS (signals, no VDOM), WebGL2 renderers
  (waterfall + spectrum at 60 fps), one WebSocket per receiver multiplexed
  through a SharedWorker.
- **Hardware-free-first development** — the entire platform (DSP, protocols,
  decoders, UI) is testable with **zero SDR hardware**: baked IQ fixtures,
  synthetic sources, and protocol-faithful fake servers/binaries. 229 server
  tests + 95 web tests prove every path.
- **Decoder plugin engine** (ADR-003) — two families behind one contract:
  in-process Python decoders (ADS-B/Mode S shipped) and subprocess C binaries
  (dump1090 shipped; dump978/AIS/DAB/ACARS are argv+manifest only).
- **Federation** (ADR-006) — act as a client to rtl_tcp, KiwiSDR, SpyServer,
  and other OpenWebRX instances; browse public receiver directories.

The mission in one line: *the SDR receiver that runs anywhere a browser does,
with the operator ergonomics of a native app* (see ARCHITECTURE.md's five
pillars: workspace, rendering, DSP+AI cascade, plugin engine, federation).

## 2. Tech stack

| Layer | Technology | Notes |
|---|---|---|
| Backend | **Python 3.12 + FastAPI + uvicorn** (`apps/server`) | strict typing: `mypy --strict` clean, `ruff` clean |
| DSP | **pycsdr / libcsdr 0.19** (C/C++ SIMD blocks, built from jketterl's sources) | FftChain, AudioChain, VfoChain (Shift→FirDecimate DDC). **Not on PyPI** — see §7 restore recipe |
| Math | numpy (live paths), scipy (dev/offline ONLY — ADR-004 forbids it in live DSP) | |
| Sources | ctypes/cffi drivers: RTL-SDR (USB), Airspy, SDRplay, SoapySDR; asyncio TCP/WS clients: rtl_tcp, KiwiSDR, SpyServer, OpenWebRX remote | 11 registered backends |
| Decoders | in-process numpy (ADS-B/Mode S incl. CRC-24) + subprocess runner (dump1090) | |
| Frontend | **SolidJS + Vite + TypeScript** (`apps/web`), Tailwind v4 styling | `tsc --noEmit` clean |
| Workspace UI | **dockview** (drag/dock/popout) via `dockview-solid` | |
| Rendering | **WebGL2** custom renderers (waterfall history texture + colormap LUTs, spectrum line strips w/ peak-hold) | |
| Realtime transport | **WebSocket**: binary frames (FFT `WRFO`, audio `AUDI`) + JSON text frames (metadata, decoder events) | one WS per receiver, fan-out via **SharedWorker** |
| Shared schemas | `packages/shared-types` — TS source of truth for wire formats | |
| Tests | pytest + pytest-asyncio (server), vitest (web), agent-browser E2E scripts | |
| Monorepo | pnpm workspaces (web) + uv (server venv) | |

## 3. Repository layout (what's in this zip)

```
openwebrx-plus/                      THE monorepo
├── apps/server/
│   ├── openwebrx_plus/
│   │   ├── sources/     11 backends + SourceRegistry (manifest contract,
│   │   │                runtime-gain protocol); wideband.py = IqHub+VfoChain
│   │   ├── dsp/         pycsdr chains (FftChain, AudioChain)
│   │   ├── sessions/    ReceiverSession (IQ + display-stream paths), registry
│   │   ├── plugins/     base (contract), adsb + modes (in-process Mode S),
│   │   │                subprocess.py (PluginRunner) + dump1090.py
│   │   ├── api/         REST + WebSocket pump
│   │   └── config/, observability/
│   ├── tests/           20 test files, 229 tests + fakes/fake_dump1090.py
│   ├── fixtures/iq/     ⚠ EMPTY in this zip — regenerate (§7): 47 MB of
│   │                    deterministic cf32 recordings (HF, FM, ADS-B, smoke)
│   └── pyproject.toml, uv.lock
├── apps/web/src/        SolidJS app: routes/, components/ (AddReceiverModal,
│                       TuningBar, sourceForms, WorkspaceManager), sessions/,
│                       visualizations/ (+ registry), lib/webgl2/, workers/
├── packages/            shared-types (TS wire formats) + scaffolds for ADR-002
│                       future work (dsp-zig, ai-rust, rnnoise-wasm, dsp-c)
├── ADR/                 6 decision records
├── docs/                STATUS.md (living), AI-HANDOFF.md (this file), plans
├── scripts/             generate_iq_fixtures.py (the fixture baker)
├── Makefile             dev-server / dev-web targets
└── ARCHITECTURE.md, README.md, pnpm-workspace.yaml, lockfiles

scripts/                             workspace-level dev tooling
├── run-server-tests.sh              THE server test entry point (env wrapper)
├── verify_*_e2e.sh                  agent-browser E2E suites (per feature)
├── README-dsp-bootstrap.md          from-source libcsdr/pycsdr build recipe
├── generate/verify/debug/bench...   development history tooling (kept)
└── pycsdr-build/build/lib.../pycsdr/  ⚙ compiled pycsdr RESTORE ARTIFACT
                                     (copy into site-packages — §7)

env-artifacts/usr/                   ⚙ machine-local native libs pycsdr needs
├── lib/libcsdr.so*                  (copy to ~/.local/usr/lib — §7)
├── lib/x86_64-linux-gnu/libsamplerate*
└── include/                         headers (only needed for from-source rebuilds)

STATUS.md, worklog.md, CHECKSUMS-fixtures.txt   status + history + regen verify
```

**Not in this zip** (all regenerable or upstream): `node_modules/`, `.venv/`,
`dist/`, `.git/`, caches, the vendored upstream reference clone, pre-monorepo
legacy prototypes, and the 47 MB of `.cf32` fixtures (byte-identical
regeneration — proven by checksum, see §7).

## 4. Where we are — verified status

Every number below was re-verified at snapshot time (not carried forward):

| Gate | Result |
|---|---|
| Server tests: `scripts/run-server-tests.sh` | **229/229 pass**, 82% coverage |
| `mypy --strict` (40 files) | clean |
| `ruff check` | clean |
| Web `vitest` | **95/95 pass** |
| Web `tsc --noEmit` | clean |
| `vite build` | clean |

**What works end-to-end today, hardware-free:**

- Boot → default receiver replaying the HF fixture → live waterfall + spectrum
  (WebGL2) + S-meter + frequency counter; dockable workspace persists across
  reloads with backend re-adoption.
- Spawn receivers from: any local SDR driver config, IQ file (fixture picker),
  synthetic signal presets, public OpenWebRX/KiwiSDR/SpyServer endpoints
  (quick-connect or directory browser), or as VFO taps off a parent receiver.
- Per-receiver tuning (slider/presets/click-on-canvas), mode switching, gain
  (auto or dB, runtime), DSP mode (raw/classic), linked crosshairs across
  panels, click-to-tune from any canvas.
- **Two ADS-B decoder plugins**, both proven on the baked fixture (3 aircraft,
  14 CRC-valid Mode S frames):
  - `adsb` — in-process (PPM demod + CRC-24 + field decode), one-click attach
    from the Aircraft viz.
  - `dump1090` — subprocess (stdin cs16 IQ in, NDJSON events out, crash-restart
    with backoff, metered backpressure, `decoder_state` lifecycle events,
    position-bearing rows when the binary decodes CPR).
- Federation clients for the four remote ecosystems (raw IQ: rtl_tcp, SpyServer,
  SoapyRemote; channelized: KiwiSDR; display streams: OpenWebRX + ADPCM).

**Delivery history (slices):** 1/1.5/2 (wire formats, WebGL2, tuning, audio,
multi-receiver) → ADR-004 (source plugin architecture) → 3 (pycsdr DSP, real
drivers, fixtures, VFO DDC) → 3.6–3.8 (federation clients, receiver browser,
ADS-B in-process decoder) → 4/4.5/4.6/4.7 (dockview workspace, active receiver,
linked crosshairs, gain/DSP controls) → 4.8 (SpyServer client) → 4.9 (subprocess
PluginRunner + dump1090). Full detail: `worklog.md`.

## 5. What's left — prioritized roadmap with implementation guidance

### 5.1 AIS decoder + map visualizations (next up, #1 priority)

The "VRS-killer" story (ARCHITECTURE.md Pillar 4). Two independent halves:

- **AIS demod** — 161.975/162.025 MHz, GMSK 9600 baud, HDLC-framed, CRC-16.
  Choose ONE:
  - *In-process* (recommended, matches `plugins/adsb.py` pattern): numpy GMSK
    demod + HDLC deframer in `plugins/ais.py` (+ `_hdlc.py`), feed from the
    session's IQ hub. AIS needs ~96 kS/s — no strict rate requirement, but
    pick and enforce one in the manifest (`required_sample_rate`).
  - *Subprocess*: `rtl-ais`/`aisdeco` via the existing PluginRunner — literally
    a `SubprocessSpec` + manifest (see `plugins/dump1090.py`, ~60 lines).
- **Map viz** — new `AisMapViz`/`AircraftMapViz` components on **MapLibre GL
  JS**. Register in `visualizations/registry.ts` (same as `aircraft-list`).
  Decide the tile strategy early: self-hosted raster tiles for offline, or a
  CDN with an offline fallback. Ship rows already carry lat/lon
  (`AdsbAircraftRow` has the optional fields — dump1090 path already populates
  them; a fixture with synthetic AIS packets + positions extends the fake
  pattern from `tests/fakes/fake_dump1090.py`).

### 5.2 dump978 (UAT) decoder

Second ADS-B family member, 978 MHz, 2.083 MSPS. The runner ships: write
`plugins/dump978.py` (manifest + argv for `dump978-fa`), add `'dump978'` to
`ADSB_DECODERS` in `packages/shared-types/src/decoder.ts`, extend the fake
binary pattern for tests. ~half a day given 4.9's machinery.

### 5.3 S-Meter / freq-counter linked readout

Join the cursor channel so the S-meter/freq-counter read the value under the
**shared cursor**, not just the receiver center. The slice-4.6 cursor broadcast
(`visualizations/crosshair`, `tuneBus`) already carries CursorState to every
panel; compute channel power at the cursor's bin from the latest FFT frame
(zero new wire format — pure frontend), subscribe the two viz components.

### 5.4 Runtime-gain gaps

soapy/airspy/sdrplay/kiwi accept gain only at spawn. Extend each to the
`RuntimeGainSource` protocol (`sources/base.py`) using the established
`_gain_q` latest-wins channel — reference implementations:
`sources/rtl_tcp.py` (wire command path) and `sources/rtl_sdr.py` (USB handle
path). Airspy = 3-stage gains flattened; sdrplay = gRdB semantics (inverted!);
kiwi = wire `SET` command; soapy = element gains via the SoapySDR API.

### 5.5 Popout crosshair sync

Popouts (separate windows) don't see the main window's cursor. Broadcast
CursorState through the SharedWorker (`workers/sdr.shared-worker.ts` already
fans out per receiver; add a cross-channel message type all ports receive).
Design notes in worklog slice-4.6.

### 5.6 dump1090 real-binary bring-up

Stock dump1090 speaks SBS1 CSV on TCP 30003, not stdout NDJSON. Ship a thin
`dump1090-sbs1-ndjson` wrapper (Python, ~100 lines: spawn dump1090
`--net`, connect to :30003, translate CSV → the pinned NDJSON event schema)
and point `OPENWEBRX_PLUS_DUMP1090_BIN` at it. The contract is pinned in
`plugins/dump1090.py`'s docstring and enforced by the test fake.

### 5.7 Mid-term (from STATUS.md / ADRs)

- **AI cascade (ADR-002)**: DeepFilterNet noise reduction as a Rust module in
  `packages/ai-rust`, RNNoise as WASM in `packages/rnnoise-wasm`; the DSP-mode
  control surface (`ai`/`cascade`) already exists in the UI.
- **Audio-band decoders**: FT8/WSJT + FLDIGI via virtual audio cable (v1: auto
  pipewire null sink per receiver); RTTY/CW/PSK31 as in-browser Wasm;
  `DigiMessageListViz`.
- **Federation polish**: HD-audio + secondary-demod forwarding for
  `openwebrx_remote`; SDRangel client; self-listing in receiverbook.
- **Rendering thread-off**: OffscreenCanvas + Web Worker per renderer
  (currently main-thread).
- Long-term: propagation intelligence, QSL logging, mobile layout, deployment.

### The working protocol (how to ship a slice)

1. Implement (follow the file/pattern guidance above; ADRs first when
   architectural).
2. `scripts/run-server-tests.sh` + `pnpm --filter openwebrx-plus-web run`
   vitest/tsc — all green, every time.
3. `ruff check` + `mypy openwebrx_plus` (strict) clean.
4. Append a worklog.md entry (template at the top of the file) and update
   `docs/STATUS.md` (health table, delivery history, what's-left).
5. For UI features: an E2E `scripts/verify_*_e2e.sh` using agent-browser.

## 6. How to run (dev)

```bash
# 0) one-time bootstrap — see §7 for the pycsdr/fixture steps first!

# 1) frontend deps
cd openwebrx-plus && pnpm install

# 2) backend venv + deps — VERIFIED sequence (do NOT plain-install the
#    project: its pycsdr dependency is a git direct reference that wants
#    to BUILD pycsdr from source, which needs libcsdr already installed —
#    the chicken-and-egg this zip's restore artifact solves):
cd apps/server
uv venv
uv pip install --python .venv/bin/python \
    fastapi uvicorn websockets httpx pydantic pydantic-settings \
    numpy structlog prometheus-client
uv pip install --python .venv/bin/python -e . --no-deps
uv pip install --python .venv/bin/python \
    pytest pytest-asyncio pytest-cov anyio httpx ruff mypy
#    then restore pycsdr per §7.1 and regenerate fixtures per §7.2

# 3) run (two terminals)
cd openwebrx-plus && make dev-server   # http://localhost:8073  (API + WS)
cd openwebrx-plus && make dev-web      # http://localhost:5173  (binds ::1 —
                                       #  use "localhost", NOT 127.0.0.1)
```

The default receiver replays `fixtures/iq/hf_20m_evening.cf32` in real time —
waterfall, audio, tuning all work with no SDR attached. Spawn more receivers
via the "+ receiver" modal (file/fixture picker, synthetic presets, remote
endpoints, VFO taps). Attach the ADS-B decoder from an Aircraft viz panel on
any 2 MSPS receiver (e.g. the adsb_1090 fixture).

> **This exact bootstrap was executed and verified** when the archive was
> built: fresh venv → explicit deps → `--no-deps` editable install → pycsdr
> restored from `scripts/pycsdr-build/...` → `LD_LIBRARY_PATH` pointed at
> `env-artifacts/usr/lib[.../x86_64-linux-gnu]` → fixtures regenerated
> (checksums matched `CHECKSUMS-fixtures.txt`) → 58/58 tests passed
> (fixtures + ADS-B + subprocess-plugin suites) from the unzipped tree.

## 7. One-time bootstrap: pycsdr restore + fixture regeneration

### 7.1 Restore the compiled DSP stack (linux-x86_64, CPython 3.12)

pycsdr is not on PyPI; this zip carries the compiled restore artifacts:

```bash
# 1) the python package → into your venv's site-packages
cp -r scripts/pycsdr-build/build/lib.linux-x86_64-cpython-312/pycsdr \
      apps/server/.venv/lib/python3.12/site-packages/

# 2) the native libs → anywhere; ~/.local/usr matches the scripts
mkdir -p ~/.local/usr && cp -r env-artifacts/usr/* ~/.local/usr/

# 3) every pycsdr import needs this on the path
export LD_LIBRARY_PATH="$HOME/.local/usr/lib:$HOME/.local/usr/lib/x86_64-linux-gnu"
#    (scripts/run-server-tests.sh sets it for you)
```

Notes: the archive stores only the loader-required SONAME files
(`libcsdr.so.0.19`, `libsamplerate.so.0`) — symlink aliases were deduped
away. `libfftw3f.so.3` is expected from the system (`apt install libfftw3-3`
on Debian/Ubuntu). Compiled artifacts are linux-x86_64 / CPython 3.12.

Sanity check: `apps/server/.venv/bin/python -c "import pycsdr.modules; print('ok')"`.

From-source rebuild recipe (if you're on another arch): `scripts/README-dsp-bootstrap.md`.

### 7.2 Regenerate the IQ fixtures (deterministic — this is why they're not in the zip)

```bash
cd openwebrx-plus
apps/server/.venv/bin/python scripts/generate_iq_fixtures.py
# → apps/server/fixtures/iq/{hf_20m_evening,vhf_fm_broadcast,adsb_1090,smoke}.cf32 (+ .meta)
```

Verify against `CHECKSUMS-fixtures.txt` (zip root) — regeneration is seeded and
byte-identical (proven when this zip was built). Server tests need these files.

### 7.3 Run the full verification battery

```bash
scripts/run-server-tests.sh                       # 229 tests, ~70 s
cd openwebrx-plus/apps/web && pnpm exec vitest run && pnpm exec tsc --noEmit
cd /home/z/my-project 2>/dev/null || true         # (workspace-level scripts below)
scripts/verify_adsb_e2e.sh                        # agent-browser E2E examples
```

## 8. Landmines (read before touching anything)

1. **NEVER `uv sync` / `uv run` in `apps/server` after restoring pycsdr** — a
   sync evicts the manually-restored package and tests break with import
   errors. Always test via `scripts/run-server-tests.sh`. Restore recipe: §7.1.
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
   stdout reader task and the session's synchronous `feed_iq` share one event
   loop, so the event deque is lock-free. Don't move feeds to threads.
8. MultiEdit tool batches are not always atomic on failure — re-read the file
   after a failed batch (a worklog slice-4.8 lesson).

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
| dump1090 binary | `tests/fakes/fake_dump1090.py` — REAL Mode S demod, contract-exact NDJSON, deliberate crash/garbage/stall modes | `tests/test_subprocess_plugins.py` |
| Browser user | agent-browser E2E scripts (screenshot + DOM assertions) | `scripts/verify_*_e2e.sh` |

**Adding a test that needs "hardware":** never mock internals — build a fake
that speaks the real wire protocol (see the table). That's what keeps the
production path honest.

**Test entry points:**
- Server: `scripts/run-server-tests.sh [pytest args...]` (from anywhere).
- Web: `cd openwebrx-plus/apps/web && pnpm exec vitest run` (pure models are
  node-environment tests; no DOM needed).
- Static gates: `ruff check` + `mypy openwebrx_plus` (strict) in apps/server.
- E2E: `scripts/verify_{adsb,crosshair,dockview,gain,tuning,frontend}_e2e.sh`.

## 10. How to extend (the two recipes you'll actually need)

### Add a source backend

1. Subclass the `Source` protocol in `apps/server/openwebrx_plus/sources/`
   (async `spawn()` yielding cf32 chunks; manifest with rate/gain ranges).
   Copy the closest existing driver (`rtl_tcp.py` for wire protocols,
   `rtl_sdr.py` for USB, `file_source.py` for replays).
2. Register in `sources/__init__.py` + the manifest table in `base.py`.
3. REST picks it up automatically (`GET /api/sources`); add a form in
   `apps/web/src/components/sourceFormModel.ts` (+ test list) for the modal.
4. Tests: wire protocol → fake server; driver → fixture IQ through the chain.

### Add a decoder plugin

1. **In-process**: subclass `DecoderPlugin`, set `manifest`, implement
   `feed_iq` (numpy in → event dicts out). Register with
   `@DecoderRegistry.register`. Copy `plugins/adsb.py`.
   **Subprocess**: subclass `SubprocessDecoderPlugin`, set `manifest` +
   `spec` (argv, iq_format, restart_backoff). Copy `plugins/dump1090.py`.
2. Events flow to the frontend automatically over the receiver WS as
   `{"type":"decoder","decoder":NAME,"event":{...}}`.
3. Frontend: extend `packages/shared-types/src/decoder.ts` (family const if it
   feeds an existing viz), build/extend a viz component, register it.
4. Tests: in-process → fixture IQ; subprocess → extend the fake-binary
   pattern (ready line with argv/env echoes + failure-mode flags).

## 11. Conventions worth keeping

- **Slice protocol** (§5): implement → gates green → worklog entry → STATUS.md
  update. Never leave gates red across slices.
- **worklog.md** is append-only and lives at the workspace ROOT (outside the
  monorepo) — in this zip it's at the zip root. Every entry: Task ID, what was
  built, debug war stories, stage summary. It is the project's real history.
- **ADRs before architecture**: new subsystems get an `ADR/00X-*.md` first.
- **Honest failures**: actionable error strings everywhere (the rate-mismatch
  error lists achievable rates; the 502 tells you which binary is missing).
- **Drop-with-counters, never block, never grow unbounded**: IqHub queues,
  FFT fps throttle, subprocess transport buffer — all metered the same way.
- Naming: slices `4.x` continue; keep the "what's left" list in STATUS.md as
  the single prioritized backlog.
