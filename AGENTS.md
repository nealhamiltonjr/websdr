# AGENTS.md — Operating instructions for AI agents working on this repo

This file is the entry point for any AI agent (Claude Code, Cursor, Aider,
Super Z, or any other) picking up work on OpenWebRX+. Read it before
touching anything. It complements `docs/AI-HANDOFF.md` (deep orientation)
and `docs/STATUS.md` (living status snapshot).

## Non-negotiable rules

1. **Never run `uv sync` / `uv run` in `apps/server`.** The venv holds a
   manually-restored `pycsdr` (a C++ Python extension that depends on
   `libcsdr`); a sync evicts it. Always test via
   `scripts/run-server-tests.sh` (it sets `LD_LIBRARY_PATH` and prefers
   the dev venv). Restore recipe: `scripts/README-dsp-bootstrap.md`.

2. **Frontend dev server binds `::1`.** Use `http://localhost:5173`,
   not `127.0.0.1`. Same for the backend at `http://localhost:8073`.

3. **Sync up after every slice boundary.** See `SYNC-UP.md`. Do not batch
   multiple major revisions into one commit. Each slice = one push.

4. **scipy is offline/dev-only** (ADR-004) — never in the live IQ path.
   Use numpy + pycsdr's C++ blocks for runtime DSP.

5. **The subprocess-decoder runner is single-threaded-asyncio BY DESIGN.**
   Don't move feeds to threads. The stdout reader and the session's
   synchronous `feed_iq` share one event loop; the deque is lock-free.

6. **MultiEdit tool batches are not always atomic on failure.** Re-read
   the file after a failed batch before retrying (worklog slice-4.8 lesson).

7. **Fakes, not mocks.** When adding a test that needs "hardware" or a
   "remote server", build a fake that speaks the real wire protocol.
   Never mock internals. See the table in `docs/AI-HANDOFF.md` §9.

8. **Honest failures.** Actionable error strings everywhere. The
   rate-mismatch error lists achievable rates. The 502 tells you which
   binary is missing. The AI/cascade modes return honest 503s, not
   silent fallbacks. Keep this pattern.

## Quality gates (run before every commit)

```bash
# from the repo root
scripts/run-server-tests.sh                              # 728+ tests, ~85 s
cd apps/web && pnpm exec vitest run && pnpm exec tsc --noEmit  # 255+ tests, types
cd apps/web && pnpm run build                            # vite production build
cd apps/server && .venv/bin/ruff check .                 # lint clean
cd apps/server && .venv/bin/mypy openwebrx_plus         # strict types clean
```

Every gate must be green before a sync-up push. No exceptions.

## Layout cheat sheet

```
apps/server/openwebrx_plus/
  sources/     12 source backends (rtl_sdr, rtl_tcp, airspy, sdrplay, soapy,
                kiwi, spyserver, openwebrx_remote, sdrangel, wideband, file_source, simulated)
  dsp/         pycsdr chains: FftChain (FFT wire format) + AudioChain (6 modes)
                + ai_denoise.py (numpy Stage 2a) + ai_denoise_rust.py (Rust cdylib, slice-36)
  sessions/    ReceiverSession (IQ + display-stream paths) + SessionRegistry
  plugins/     DecoderPlugin ABC + 17 decoders: adsb/ais/cw/dump978/dump1090/ft8/
               rtty/psk31/sstv/olivia/wspr/ax25/jt65/jt9/fax/acars/dab (in-process)
  api/         rest.py (control) + ws.py (streams) — binary FFT/audio frames
  config/      Settings (TOML + env via pydantic-settings)
  observability/  structlog setup
apps/web/src/
  routes/      main.tsx (tuning model) + popout.tsx
  components/  AddReceiverModal, TuningBar, WorkspaceManager, RemoteBrowser,
               sourceForms, GroupActions, workspace/{VizPanel, layoutModel}
  visualizations/ WaterfallViz, SpectrumViz, SMeterViz, FreqCounterViz,
                   AircraftListViz + registry + crosshair + freqAxis + aircraftModel
  lib/webgl2/  WaterfallRenderer, SpectrumRenderer, overlay
  lib/audio/   AudioPlayer (Web Audio scheduled buffers)
  sessions/    ReceiverSession, receiverTuning, tuneBus
  workers/     sdr.shared-worker.ts (one WS per receiver, fan-out)
packages/     dsp-zig/ (stub) ai-rust/ (Rust cdylib, slice-36 wired) rnnoise-wasm/ (loader, slice-24) dsp-c/ (placeholder)
              shared-types/ (TS + Python mirrors of wire formats)
ADR/           7 accepted ADRs (workspace, DSP+AI cascade, decoders, pycsdr/sources,
               VFO wideband, federation, IQ-to-audio-enhancement [rejected])
docs/          AI-HANDOFF.md (deep orientation), STATUS.md (living snapshot),
               slice-01-plan.md (original plan, kept for history)
scripts/       run-server-tests.sh (portable test runner),
               generate_iq_fixtures.py (deterministic IQ fixture generator),
               test_fft_chain.py / test_audio_chain.py / test_audio_modes.py
               (DSP smoke tests outside pytest), README-dsp-bootstrap.md,
               probe_openwebrx_remote.py
```

## Slice protocol

A slice is a vertical-slice feature delivery: implement across all layers
(server + web + tests + docs), then push.

1. **Implement** the feature end-to-end. No partial slices.
2. **Tests green** — both server and web suites.
3. **Static gates clean** — ruff + mypy + tsc + vite build.
4. **Append a worklog entry** to `docs/worklog.md` (or your project's
   worklog location) with Task ID, what was built, debug war stories,
   stage summary.
5. **Update `docs/STATUS.md`** — bump the snapshot date, update the
   health table, add a delivery-history row, update "What's left".
6. **Sync up** — `git add -A && git commit && git push origin main`.

## Commit message template

```
slice-X.Y: <one-line subject>

What was built:
- <feature/fix 1>
- <feature/fix 2>

What was fixed:
- <bug 1>

Open follow-ups:
- <deferred item, if any>

Quality gates:
- server: 728/728 tests, mypy strict, ruff clean
- web: 255/255 tests, tsc clean, vite build clean
```

## When you don't know what to do

1. Read `docs/AI-HANDOFF.md` §10 — the two recipes you'll actually need
   (add a source backend, add a decoder plugin).
2. Read the closest existing implementation and copy its shape:
   - New source backend → `sources/rtl_tcp.py` (wire) or `sources/file_source.py` (replay)
   - New decoder plugin → `plugins/adsb.py` (in-process) or `plugins/dump1090.py` (subprocess)
   - New visualization → `visualizations/SMeterViz.tsx` (simplest example)
   - New REST endpoint → `api/rest.py` (follow the existing pattern)
   - New WS message → `api/ws.py` (follow the existing pattern)
3. Read the relevant ADR. New subsystems get an `ADR/00X-*.md` first.
4. If still stuck, leave a clear `TODO(<slice>)` marker in the code and
   move on — don't block the slice on uncertainty.

## Environment restore recipe (if the sandbox wiped ~/.local/usr)

The venv at `apps/server/.venv` and the `~/.local/usr` prefix get recycled
by the sandbox. Restore them from the parent handoff bundle:

```bash
# 1. venv + deps (uv is at /usr/local/bin/uv)
cd apps/server
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python \
    fastapi uvicorn websockets httpx pydantic pydantic-settings \
    numpy structlog prometheus-client
uv pip install --python .venv/bin/python -e . --no-deps
uv pip install --python .venv/bin/python \
    pytest pytest-asyncio pytest-cov anyio ruff mypy

# 2. pycsdr package + native libs (from the handoff bundle's parent dir)
cp -r <handoff-bundle>/scripts/pycsdr-build/build/lib.linux-x86_64-cpython-312/pycsdr \
    .venv/lib/python3.12/site-packages/
mkdir -p ~/.local/usr
cp -r <handoff-bundle>/env-artifacts/usr/* ~/.local/usr/

# 3. IQ fixtures (deterministic generator)
cd ..  # back to repo root
apps/server/.venv/bin/python scripts/generate_iq_fixtures.py
```

If the handoff bundle isn't available, build pycsdr from source via
`scripts/README-dsp-bootstrap.md` (requires libcsdr build, takes ~5 minutes).
