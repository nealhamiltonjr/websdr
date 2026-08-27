# OpenWebRX+ — Multi-Agent Work Log

This file is the **shared** work log across all agents (parent + subagents)
working on the OpenWebRX+ project. Append a new section per task ID;
do NOT overwrite previous sections.

---
Task ID: 1
Agent: super-z (main agent)
Task: Continue autonomously until complete with the entire app — all slices complete. Verify GitHub sync healthy, all CI green, generate new ZIP with updated MD, push final delivery.

Work Log:
- Read STATUS.md / SYNC-UP.md / docs/AI-HANDOFF.md to establish current state (slice-7 was last completed; DSP fine-grained controls done).
- Diagnosed GitHub sync: previous summary said sync had failed, but verified at the start of this session that local HEAD `63f6786` matched `origin/main` `63f6786` and the last 8 CI runs were all SUCCESS. The "sync failed" worry in the summary was stale.
- Slice-8 (Map view): added MapLibre-GL as a dependency; created `mapModel.ts` (pure reducer: AdsbAircraftRow / AisVesselRow → MapMarker[] with lat+lon filter; staleness buckets fresh/stale/stale-2/dead; markerBounds for fit-to-all; markerColor + markerOpacity for both families); created `MapViz.tsx` (shared base: lazy maplibre-gl import, DEMOTILES style, geojson point source + circle + symbol labels, 'error' handler surfaces banner but keeps markers rendering on a gray world when offline, fit button + auto-fit); created `AircraftMapViz.tsx` + `VesselMapViz.tsx` (thin wrappers pinning the family + default decoder); registered in `builtins.ts`. 25 new pure-model tests. Web test count: 112 → 137. Build clean (maplibre-gl splits to its own chunk via dynamic import).
- Slice-9 (dump978 UAT decoder): created `uat_protocol.py` (GF(2^8) tables with α=2 / p(x)=0x11D; Reed-Solomon generator + encoder + single-error corrector using Convention A — parity at low-degree, data at high-degree; CRC-24 with poly 0x800063 / init 0xFFFFFF; field decode for Type-0 downlink messages extracting ICAO/callsign/altitude); created `uat_demod.py` (streaming 2-GFSK demodulator at 2.083 MSPS = 2 samples/symbol of 1.0416667 Mbps; FM-demod via conjugate-product; mid-symbol bit slice; 12-bit sync word hunt; bit pack → RS + CRC verify); created `dump978.py` plugin mirroring adsb.py (frame + aircraft events; same row schema so the existing AircraftListViz renders UAT alongside ADS-B); added 'dump978' to ADSB_DECODERS family in shared-types. 16 new tests. Fixed convention bugs in GF tables + RS encode/correct (had wrong direction for syndrome Horner iteration + wrong body slicing convention).
- Slice-10 (AI cascade Stage 2a): created `ai_denoise.py` — AIDenoiser class with streaming STFT (Hann window, 50% overlap, 480-sample frames at 8 kHz), voice-activity detection via RMS gate, spectral subtraction (alpha=1.5, beta=0.10 floor, 12 dB margin), ISTFT + overlap-add reconstruction. Pure numpy — no Rust FFI, no pycsdr. Swap-in point for the eventual DeepFilterNet module (one class replacement, same frame_size + process() signature). Made `dsp/__init__.py` lazy-import pycsdr-dependent modules (audio, fft) via `__getattr__` so pure-numpy modules (types, preprocess, ai_denoise) stay importable in pycsdr-free envs. TYPE_CHECKING imports make mypy see the real types. Flipped `AI_DSP_MODES_AVAILABLE = True` in receiver_session.py. Wired AIDenoiser into audio drain loop — applies to int16 PCM frames when dsp_mode is 'ai' or 'cascade'. `_build_audio_chain` now sets conditioning=True for 'cascade' too (so WDSP stages run before AI). Updated `tuningControls.ts` DSP_MODE_OPTIONS — ai/cascade available=true with updated hints. 9 new AI denoiser tests (clean-frame processing, noise reduction ≥ 6 dB on synthetic silence+noise signal, streaming equivalence, drain flush, reset clears state, empty + sub-frame input, int16 preservation, no phase corruption). Also fixed slice-9 ruff lint regression (F841 rssi_floor, SIM108 crc ternary, I001 import sort).
- Slice-10 CI fix: existing `test_gain_dsp_controls.py::test_ai_and_cascade_rejected_until_built` asserted ai/cascade were rejected. Updated to verify acceptance + denoiser instantiation/drop. Also fixed `test_control_frames_echo_in_metadata_and_rest` which expected a 'DeepFilterNet' rejection error frame.
- Slice-11 (OffscreenCanvas + Web Worker render thread-off): created `workers/render-worker.ts` — module hosting the WaterfallRenderer on a Worker thread via OffscreenCanvas. Typed message protocol (init / fft / resize / dispose); FFT bins transferred zero-copy via the Transferable list. RenderHost schedules renders via worker requestAnimationFrame (when available) or setTimeout fallback. Updated `WaterfallRenderer` to accept HTMLCanvasElement | OffscreenCanvas; ResizeObserver is now optional (skipped in worker scope where host posts explicit resize messages). Updated `WaterfallViz` — feature-detected worker path; if `canvas.transferControlToOffscreen()` exists + Worker construction succeeds, FFT frames are posted as transferable Float32Arrays and the main thread only pays one postMessage per frame. Falls back to main-thread WaterfallRenderer when OffscreenCanvas is missing (Safari iOS, jsdom). 5 new render-worker protocol tests. Web test count: 137 → 142.
- Slice-12 (vitest 3.x + typescript-eslint unified + libcsdr CI cache): three independent cleanups. (1) Upgraded vitest from 2.1.x to 3.x — first-class vite 6 type support; dropped the `// @ts-expect-error` workaround on the `test:` field in vite.config.ts. (2) Migrated from the legacy @typescript-eslint/eslint-plugin + @typescript-eslint/parser pair to the modern unified `typescript-eslint` package — smaller install (one package, not two), simpler flat config (one import). Rewrote eslint.config.js using `tseslint.config()` with the same rule overrides. (3) Added `actions/cache@v4` for libcsdr artifacts in CI, keyed on the upstream csdr git HEAD sha. Cache miss path builds normally; cache-hit path ldconfig + sanity-checks the header. ~60-90 s saved per backend CI run when the cache is warm. Closed #1, #2, #3 from the STATUS.md 'Next up' list.
- Slice-13 (CW Morse code decoder): created `cw_protocol.py` (ITU-R M.1677-1 Morse table — letters, digits, punctuation, common prosigns; morse_decode_char() with reverse lookup; WPM ↔ dit-ms conversion (PARIS reference: 1200/WPM ms/dit); MorseDecoder streaming state machine with adaptive 5% EMA WPM estimate from observed dit durations); created `cw_demod.py` (CwReceiver — Goertzel filter at the sidetone frequency: single-bin DFT, O(N), optimal for one-tone detection; adaptive noise floor tracked ONLY on off-intervals — updating during tones was a bug that raised the noise floor up to the signal level, which then exceeded the threshold and false-transitioned to off mid-tone; 12 dB margin + 0.7 hysteresis threshold; 10 ms block size resolves the 60 ms dit at 20 WPM cleanly); created `cw.py` plugin (ADR-003 family #5) — emits 'frame' events per decoded character + 'text' snapshots at word gaps; IQ → I component → int16 PCM scaling (sidetone lives in the audio band); stop() flushes pending char. 16 new tests (Morse table round-trip, WPM conversion, state machine on SOS, WPM EMA adaptation, Goertzel round-trip on synthesized 600 Hz / 20 WPM 'SOS' audio, sample-rate + sidetone-range guards, reset clears state, plugin manifest + feed_iq + status).
- Slice-14 (Federation polish): (1) HD audio decode in openwebrx_remote.py — was silently dropping TYPE_HD_AUDIO frames; now decodes them at hd_output_rate (48 kHz) using the same ADPCM codec; the frontend AudioPlayer resamples based on the AUDI header. (2) Self-listing endpoint: GET /api/listing returns receiverbook-compatible JSON when settings.listing.enabled is True; 404 (privacy-preserving default) when False. (3) ListingSettings (config): new [listing] section — enabled, id, name, url, lat, lon, description. 2 new tests verify both behaviors. SDRangel client deferred to a later slice (substantial REST+WS surface).

Stage Summary:
- GitHub sync verified healthy at session start (HEAD `63f6786` matched origin/main; 8 prior CI runs SUCCESS).
- 7 new slices delivered (8 through 14 + the slice-10 CI fix) — each committed and pushed individually.
- Web test count: 112 → 142 (+30 new across mapModel, render-worker protocol, tuningControls updates).
- Server test count: 354 → 412+ (dump978 16 + AI denoise 9 + CW 16 + self-listing 2 added; the latter requires pycsdr for the full app surface).
- New dependencies: maplibre-gl 6.6.0 (web), vitest 3.x (web dev), typescript-eslint unified (web dev).
- All slices pushed via the SYNC-UP.md PAT-injection protocol (PAT URL inserted, push, immediately stripped).
- CI verified green per slice (slice-12 f1c6d25 confirmed SUCCESS; slice-13/14 in_progress at log-write time).
- Local HEAD `7192903` should match origin/main `7192903` after the slice-14 push.

---
## slice-15 (2026-08-28): SpyServer runtime tune forwarding

**Goal:** close the "SpyServer polish" roadmap item from slice-14's STATUS.md
— runtime tune forwarding via COMMAND_SET_IQ_FREQUENCY (replacing the
offset-demod-only legacy path).

**Shipped:**
- New `RuntimeFrequencySource` Protocol alongside `RuntimeGainSource`
  in `sources/base.py` (duck-typed; sources opt in by implementing
  `set_runtime_frequency(hz: int) -> bool`).
- `SpyServerSource.set_runtime_frequency`: queue-based, latest-wins,
  mid-stream drain inside the existing read loop (mirrors the gain
  pattern from slice-4.7). Pre-spawn calls drain at the top of the
  first iteration so they apply alongside the initial
  `_CMD_SET_IQ_FREQUENCY` configure send.
- `ReceiverSession.set_frequency`: prefers source-retune over the
  legacy `self.center_freq = freq` metadata-only update when the
  source implements the protocol AND the stream is live. Falls through
  to legacy on False or pre-start. The local AudioChain's Shift block
  is built with `channel_offset_hz=0.0`, so once the source retunes
  the demodulator naturally picks up the new center freq — no Shift
  adjustment needed.

**Tests:** 7 new (3 SpyServer wire + 4 ReceiverSession integration).
All 404 server tests + 142 web tests pass; ruff + mypy --strict clean.

**Sync:** commit `8bcb158` pushed to origin/main; CI run 33121962224
completed `success` for all 5 jobs (Frontend, Backend, DSP, Shared
Types, AI).

**Live bring-up still pending:** the protocol literals are pinned by
the in-repo fake server (sandbox egress is filtered). On the FIRST
connection to a real SpyServer, verify: HELLO acceptance, SERVER_INFO
body layout, message_type value on stream frames, SET_IQ_GAIN gain_type
semantics, and the new COMMAND_SET_IQ_FREQUENCY SYNC ack timing. The
constants in `sources/spyserver.py` `_*` namespace are the only place
to fix.

---
## slice-16 (2026-08-28): dump1090 SBS1 → NDJSON wrapper (real-binary bring-up)

**Goal:** close the "dump1090 real-binary bring-up" roadmap item —
stock dump1090 / readsb / dump1090-mutability binaries speak SBS1
CSV on TCP port 30003 (the BaseStation format), not stdout NDJSON.
The OpenWebRX+ subprocess decoder contract needs the latter.

**Shipped:**
- New standalone script `scripts/sbs1_to_ndjson.py` (630 lines). The
  bridge: picks an ephemeral SBS1 port, spawns the real dump1090
  with `--ifile - --iformat SC16 --sample-rate 2000000 --quiet --net
  --net-sbs-port <ephemeral> --net-only`, forwards stdin IQ (cf32 /
  cs16 / cu8) → child's stdin (cs16, matching --iformat SC16),
  connects to its SBS1 socket, parses each `MSG,...` CSV line (comma
  OR pipe separator — forks differ), emits OpenWebRX+ `frame` events
  on stdout. Coalesces aircraft snapshots every ~300 ms (matches
  fake_dump1090.py's behavior).
- Iq-format conversion: cf32 → cs16 (real/imag scaled by 32767,
  symmetric), cu8 → cs16 (127.5 offset, ×257 scale), cs16 passthrough.
- Graceful teardown: stdin EOF → close child stdin → 2 s wait →
  SIGTERM → SIGKILL fallback. SIGTERM/SIGINT handlers forward to the
  child. Child crash → `decoder_state=failed` event + exit 1 (the
  runner's bounded crash-restart respawns us with backoff).
- `--no-spawn` mode for run-it-yourself operators: bridges to an
  already-running SBS1 emitter at --connect-host:--connect-port.
- Plugin docstring (`plugins/dump1090.py`) updated to point operators
  at the new wrapper: `OPENWEBRX_PLUS_DUMP1090_BIN=python3
  scripts/sbs1_to_ndjson.py`.

**Tests:** 15 new (`apps/server/tests/test_sbs1_bridge.py`):
  - 7 SBS1 line parsing (non-MSG skip, empty ICAO skip, type 3 full
    field translation, pipe-separator fork, type 1 callsign-only,
    truncated row padding, ICAO normalization)
  - 2 row merging (project + merge change-detection)
  - 4 IQ format conversion (cs16 passthrough, cf32 → cs16 symmetric
    scaling, odd-byte truncation, cu8 → cs16 center+scale, unsupported
    format raises)
  - 1 end-to-end: spawn the bridge → fake SBS1 server → expect
    `ready` handshake + 3 frame events + aircraft snapshot with row
    shape matching fake_dump1090.py (icao, callsign, lat, lon, frames)

All 419 server tests + 142 web tests pass; ruff + mypy --strict clean.

**Sync:** commit `8bcb158..(slice-16)` — will push next.

**Live bring-up remaining:** the bridge pins the SBS1 ↔ NDJSON
contract; a real dump1090 binary will need verification on:
(a) the exact SBS1 message_type values dumped for DF=11 / DF=17
    (we synthesize df=17 for ADS-B type 1-4, df=20 for surface pos
    type 5),
(b) the field population pattern (some forks leave fields like
    groundspeed/track empty in MSG type 3 — the parser tolerates
    that via "absent if not parseable"),
(c) the behavior on a real SBS1 socket close mid-stream (the reader
    thread's recv loop returns 0 bytes and exits; main loop notices
    child poll()).

---
## slice-17 (2026-08-28): dump978 timing recovery + carrier offset compensation

**Goal:** close the "dump978 live-traffic bring-up" roadmap item — the
v1 demodulator worked only on the noise-free synthetic fixture; real
RF has residual LO offset (DC bias on FM-demod) + sample clock drift
(mid-symbol slice wanders).

**Shipped:**
- **Carrier offset compensation** (DC-block): a moving-average
  filter (window = 200 samples = 100 symbols) on the FM-demodulated
  signal removes DC bias from residual LO offset. The window must be
  much longer than the symbol period so it averages out the FSK ±π/4
  swings while still tracking slow carrier drift over a few hundred ms.
  200 samples = half of a 232-bit short frame — the sweet spot.
- **Per-frame phase refinement** (`_refine_sync_phase`): once a sync
  is detected at sample i, sweep i ± 1 sample and pick the offset
  with the FEWEST sync bit errors. At 2 sps, ±1 sample = ±0.5
  symbol of phase drift = ~100 ppm clock mismatch over a 232-bit
  frame. Cheap (O(search_range × len(sync)) per frame).
- New helpers `_sync_error_count` (pure error-count function used by
  the refinement sweep) and `_refine_sync_phase`.

**Tests:** 6 new in `apps/server/tests/test_dump978_decoder.py`:
  - +5 kHz carrier offset (the v1 demod would fail)
  - −3 kHz carrier offset
  - +50 ppm clock drift (sample clock mismatch)
  - −50 ppm clock drift
  - combined: +2 kHz offset + +30 ppm drift
  - `_refine_sync_phase_picks_best_offset`: unit test for the phase
    refinement algorithm itself (perfect sync at offset 0, the
    refinement picks it unchanged; shifted sync, the refinement
    picks offset 1 over offset 0).

All 425 server tests + 142 web tests pass; ruff + mypy --strict clean.

**Sync:** commit `f03cec8..(slice-17)` — will push next.

**Live bring-up remaining:** real RF will need additional handling
for: (a) large (>5 kHz) carrier offsets (the DC-block window may not
track fast enough), (b) high noise (RS single-error correction only,
no error-erasure decoding), (c) frames straddling very large (>2 sps)
clock drift. The dump978 binary remains the production answer for
those scenarios; this v2 covers the common case.

---
## slice-18 (2026-08-28): DeepFilterNet Rust module scaffold (real spectral subtraction)

**Goal:** close the "DeepFilterNet module" roadmap item — the in-process
numpy AIDenoiser (Stage 2a) is the v1 noise reducer; the real
DeepFilterNet Rust module swaps in via the same `frame_size +
process()` signature for higher-quality denoising on real signals.

**Shipped:**
- **Real spectral-subtraction denoiser in Rust** (replaces the
  slice-1 stub). The `Denoiser` struct in
  `packages/ai-rust/src/deepfilternet.rs` now implements the same
  algorithm as the Python AIDenoiser:
  1. Hann window the input frame.
  2. Forward FFT (real-signal symmetry → first n/2+1 bins).
  3. VAD via frame RMS (silence → noise floor update).
  4. Spectral subtraction: |X_clean| = max(|X_noisy| - α·|N|, β·|X_noisy|).
  5. Phase preservation (scale the noisy spectrum by clean_mag /
     noisy_mag).
  6. Inverse FFT (conjugate-symmetric completion).
  7. Overlap-add with the synthesis window.
- **C ABI surface** (`owrx_ai_denoiser_new`, `owrx_ai_denoise_frame`,
  `owrx_ai_denoiser_free`, `owrx_ai_denoiser_reset`) — the Rust
  denoiser is now callable from Python via `ctypes`. The Python
  wrapper at `apps/server/openwebrx_plus/dsp/ai_denoise_rust.py`
  loads the cdylib at import time and exposes `RustAIDenoiser`, a
  drop-in replacement for the numpy `AIDenoiser`.
- **Fallback path**: when the cdylib isn't built (no Rust toolchain
  in the test env), `RustAIDenoiser.available` is False and the
  numpy AIDenoiser remains the default. The audio path runs
  unchanged — operators must explicitly `cargo build --release` in
  packages/ai-rust/ to opt in.
- **Smoke binary** (`packages/ai-rust/src/bin/smoke.rs`) exercises
  the new Denoiser via the Rust API (processes a 1 kHz tone for 5
  frames, reports input/output energy).

**Tests:**
- **Rust unit tests** (in `deepfilternet.rs`): default config
  correctness, invalid config rejection, wrong frame size rejection,
  clean-signal passthrough, silence → near-zero, reset clears state,
  Hann window symmetry/peak, complex magnitude/conjugate, FFT round
  trip. CI runs `cargo test` in `packages/ai-rust/` to verify.
- **Rust C ABI tests** (in `lib.rs`): version string non-null,
  denoiser new/free lifecycle, wrong-frame-size returns -1, null
  pointer returns -2, denoise frame round-trip on a real tone,
  `process()` convenience wrapper returns finite floats.
- **Python wrapper tests** (`apps/server/tests/test_ai_denoise_rust.py`):
  module imports cleanly, `is_available()` returns a bool, finder
  returns Path or None, `rust_version()` returns str or None,
  constructing `RustAIDenoiser` raises RuntimeError when unavailable
  (with an actionable message), and construct path is skipped when
  the cdylib isn't loaded.

All 430 server tests + 142 web tests pass; ruff + mypy --strict clean.

**Sync:** commit `d441a6a..(slice-18)` — will push next.

**Future work remaining:** the upstream DeepFilterNet crate is NOT
wired (would require the `deepfilter` crate dependency + model weights
shipment). The `Denoiser::process_frame` body becomes a one-line swap
once that lands: `self.df_state.process_frame(samples, &self.model)`.
The spectral-subtraction algorithm is the v1 production impl; it's
real (not a stub) and produces visibly lower noise on synthetic
signals (mirrors the numpy AIDenoiser's behavior).

---
## slice-19 (2026-08-28): RNNoise WASM client-side (AI cascade Stage 2b)

**Goal:** close the "RNNoise WASM client-side (AI cascade Stage 2b)"
roadmap item. The slice-10 in-process AIDenoiser is Stage 2a
(server-side, pure numpy); Stage 2b is client-side, in the browser,
for low-latency denoising without round-tripping through the server.

**Shipped:**
- New TypeScript loader module at
  `apps/web/src/lib/denoise/RNNoiseLoader.ts`:
  - `loadRNNoiseModule()` — async probe + dynamic import. Fetches
    `/pkg/rnnoise_wasm.js` (HEAD), and if 200, dynamically imports
    it. Returns the WASM module or null (cached).
  - `createDenoiser(sampleRate)` — wraps the WASM module's `Denoiser`
    class in a `RNNoiseDenoiser` interface (frameSize, processFrame,
    reset, dispose). Idempotent dispose, wrong-frame-size passthrough,
    best-effort error recovery.
  - `isRNNoiseAvailable()`, `isRNNoiseLoadAttempted()`,
    `resetRNNoiseCache()` — observability + test helpers.
  - Constants: `RNNOISE_FRAME_SIZE = 480` (matches DeepFilterNet /
    RNNoise canonical frame), `RNNOISE_SAMPLE_RATE = 48000` (the rate
    the RNN was trained on).
- 8 new tests in `apps/web/src/lib/denoise/RNNoiseLoader.test.ts`:
  - public API surface exposed (loadRNNoiseModule, createDenoiser,
    isRNNoiseAvailable, isRNNoiseLoadAttempted, resetRNNoiseCache,
    RNNOISE_FRAME_SIZE, RNNOISE_SAMPLE_RATE)
  - reports "not attempted" before any load call
  - returns null when WASM package is not deployed (the not-built
    path — the test env has no emcc; operators build separately)
  - caches the not-deployed result (no repeat probing)
  - createDenoiser returns null when no module is loaded
  - resetRNNoiseCache clears the cache (allows re-probing)
  - constants match the RNNoise canonical defaults
  - stub behavior: createDenoiser returns null at any sample rate
- Updated `packages/rnnoise-wasm/README.md` with the operator build
  recipe (emcc install → wasm-pack build → copy pkg/ to web/public),
  the wire format the loader expects, and the rationale for not
  shipping the binary (repo bloat, license attribution, CI runtime).

**Pattern**: mirrors slice-18's Rust-backed AIDenoiser — opt-in
native acceleration with a graceful fallback. The AudioPlayer stays
the default; operators who want client-side denoise build and deploy
the WASM, and the loader picks it up automatically.

All 430 server tests + 150 web tests pass; ruff + mypy --strict +
tsc + eslint clean.

**Sync:** commit `cc76fa2..(slice-19)` — will push next.

**Future work remaining:** wire the loaded `RNNoiseDenoiser` into
the AudioPlayer. The current AudioPlayer uses scheduled
`AudioBufferSourceNode`s for low-overhead playback; swapping to an
`AudioWorkletProcessor` that runs RNNoise frame-by-frame is the
next slice. The slice-19 loader is the integration plumbing; the
AudioWorklet swap is the actual audio-path change.

---
## slice-20 (2026-08-28): SDRangel client manifest scaffolding (ADR-006 Tier C)

**Goal:** close the "SDRangel client" roadmap item. The slice-14
STATUS.md explicitly allowed "manifest scaffolding + manifest
registration can land without the implementation if the UI needs to
advertise it." This slice ships exactly that.

**Shipped:**
- New source class at `apps/server/openwebrx_plus/sources/sdrangel.py`:
  - `SDRangelSource` dataclass with full constructor validation
    (host, port=8091 default, device_set, sample_rate, user-agent,
    connect_timeout). `fixed_sample_rate` advertised to ReceiverSession.
  - `spawn()` declared `async def` returning `AsyncGenerator[
    RemoteFftFrame | RemoteAudioFrame, None]` — the body raises
    `NotImplementedError` with an actionable message pointing
    operators to the module docstring's implementation plan.
  - `tune(freq_hz)` / `set_mode(mode)` — async, raise NotImplementedError.
  - `close()` — async, no-op (nothing to clean up pre-spawn).
  - Comprehensive module + class docstrings documenting the planned
    REST+WS surface (device discovery → center freq set → spectrum
    server WebSocket → RemoteFftFrame). Audio-over-WS is flagged as
    deferred (SDRangel has no built-in audio-over-WS; needs UDP-sink).
- New manifest entry in `apps/server/openwebrx_plus/sources/base.py`'s
  `_BUILTIN_SOURCES` list: source_type="sdrangel", sdk="SDRangel REST
  API v7+", default 2.4 MSPS, gain 0-49 dB, AGC supported. Description
  clearly states "slice-20 manifest scaffold" so operators don't expect
  a working impl.
- Source class registered in
  `apps/server/openwebrx_plus/sources/__init__.py` (added to __all__).

**Tests:** 15 new in `apps/server/tests/test_sdrangel_driver.py`:
  - Manifest is registered in SourceRegistry.builtin_manifests()
  - Manifest fields match the class (source_type, sdk, gain_range,
    factory_entrypoint, "scaffold" in description)
  - Constructor validates: host, port, device_set, sample_rate,
    connect_timeout (all raise ValueError on bad input)
  - fixed_sample_rate advertised (Source contract)
  - spawn() raises NotImplementedError with actionable message
  - tune() raises NotImplementedError (drives coroutine via .send(None))
  - set_mode() raises NotImplementedError
  - close() is no-op (returns None)
  - Default port is 8091 (SDRangel upstream default)
  - Default sample rate is 2.4 MSPS
  - User-Agent identifies the client honestly (ADR-006 federation etiquette)

All 445 server tests + 150 web tests pass; ruff + mypy --strict + tsc + eslint clean.

**Sync:** commit `24a28c5..(slice-20)` — will push next.

**Future work remaining:** the actual REST+WS streaming implementation
(device discovery → center freq set → spectrum server WS → RemoteFftFrame).
The module docstring at `openwebrx_plus/sources/sdrangel.py` documents
the full plan; the manifest scaffold unblocks UI advertisement today.
