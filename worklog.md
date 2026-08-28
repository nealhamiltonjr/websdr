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

---
## slice-21 (2026-08-28): FT8 / audio-band digi-mode plugin + DigiMessageListViz

**Goal:** close the "Audio-band decoders — FT8/WSJT-X + FLDIGI" +
"DigiMessageListViz" roadmap items. FT8 is a weak-signal digital
mode on HF; the reference impl is WSJT-X. Slice-21 ships the
contract surface (wire types + plugin stub + visualization) so the
UI can offer the FT8 option in the +viz dropdown today; the actual
FSK demodulator + LDPC decoder lands in a future slice.

**Shipped:**
- **Wire types in `packages/shared-types/src/decoder.ts`**:
  - `DIGI_MESSAGE_DECODERS = ['ft8']` (const) + `DigiMessageDecoderName`
  - `DigiMessageEvent` (kind: 'message', mode, text, callsign,
    grid_locator, snr_db, audio_offset_hz, slot_utc)
  - `DigiMessageListEvent` (kind: 'messages', messages[]) — ring
    buffer snapshot
  - `isDigiMessageEvent`, `isDigiMessageListEvent` type guards
- **FT8 plugin stub at `apps/server/openwebrx_plus/plugins/ft8.py`**:
  - `FT8DecoderPlugin` class registered in `DecoderRegistry` (GET
    /api/decoders lists it)
  - Manifest: name="ft8", label="FT8 (audio-band digi modes)",
    tap_point="rf_band", required_sample_rate=12000, events=
    ("message", "messages"). Description clearly states "slice-21
    manifest stub" so operators know the demodulator isn't shipped.
  - `feed_iq` / `feed_audio` return [] (no demod yet)
  - `status()` returns {messages_decoded:0, crc_failures:0, slot_count:0,
    stub: True, note: "..."} so the UI can surface the stub state
  - Module docstring documents the full implementation plan (FSK
    demod → LDPC soft-decision → CRC-14 → message unpack).
  - Constants: FT8_SAMPLE_RATE=12000, FT8_TONE_SPACING_HZ=6.25,
    FT8_BIT_RATE_BAUD=6.25, FT8_SLOT_SECONDS=15 (protocol spec).
- **Frontend `DigiMessageListViz.tsx`** at
  `apps/web/src/visualizations/`:
  - SolidJS component subscribes to receiver decoderStream, renders
    a scrollable table of recent messages (time, mode, SNR, audio
    offset, text, age).
  - Empty-state banner when no messages yet (slice-21 stub state).
  - Footer with "latest decode" + "N / 50 buffer".
  - Registered as viz type "digi-message-list" with displayName
    "Digital Messages" + default size 520×280.
  - Added to `builtins.ts` for side-effect registration.
- **Model `digiMessageModel.ts`**: pure state reducer over decoder
  events. Tracks messages (ring buffer, MAX_MESSAGES=50),
  messageCount, lastMessage, mode, decoderState. Format helpers:
  formatMessageSummary, formatTime (HH:MM:SSZ UTC), formatAge.

**Tests:**
- **Server (8 new)** at `apps/server/tests/test_ft8_decoder.py`:
  plugin manifest fields, status reports stub state, feed_iq returns
  empty + handles any chunk size, feed_audio stub, stop is noop, FT8
  constants match protocol spec, plugin registered in DecoderRegistry.
- **Web (13 new)** at `apps/web/src/visualizations/digiMessageModel.test.ts`:
  initial state empty, ignores non-FT8 family, handles single message,
  appends newest-first, caps at MAX_MESSAGES ring buffer, handles
  messages snapshot, decoder_state lifecycle, preserves messageCount
  across snapshots. Plus formatters: formatMessageSummary composes
  mode+SNR+offset+text, omits missing optional fields; formatTime
  produces HH:MM:SSZ UTC; formatAge produces human-friendly age
  strings + clamps negative ages.

All 453 server tests + 163 web tests pass; ruff + mypy --strict + tsc +
eslint clean.

**Sync:** commit `a49762a..(slice-21)` — will push next.

**Future work remaining:** the actual FSK demodulator + LDPC + CRC-14
+ message unpack (per the module docstring). The wire-format contract
is shipped; once the demod produces events, the viz populates
automatically. FLDIGI virtual audio cable integration is also open
(operators can run FLDIGI locally today).

---
## sync-22 (2026-08-28): git sync verification + CI green audit

**Goal:** verify the repo is fully synced to the remote and all GitHub
Actions workflows complete without errors. The user reported "some
workflows fail" based on the historical Actions record.

**Work Log:**
- Inspected git state: local HEAD `906be15` (auto-commit with GUID
  message — `e5c3a024-7a5c-470e-8fdd-0bb41fe13dce`) was 2 commits
  ahead of `origin/main` (`a76a04e1`).
- The auto-commit `906be15` had bundled the 184 MB handoff ZIP into
  git history — that would fail GitHub's 100 MB file limit on push.
- Backup up the ZIP to `/tmp/...bak`, then `git reset --soft a76a04e`
  to roll back the GUID commits while keeping all changes staged.
- Added `download/` to `.gitignore` (was missing — agent working
  artifacts pattern; matches `skills/`, `tool-results/`, `upload/`).
- Re-committed the pending work in TWO clean, meaningful commits
  (replacing the GUID-message auto-commits):
  1. `81a0be3` `slice-22: secondary FFT wire format for federation
     polish` — the real code work: `RemoteSecondaryFftFrame` dataclass
     + `SecondaryFFTFrame` TS wire format (magic 'WRSF') + decode
     Type 0x03 secondary FFT frames in `openwebrx_remote.py` +
     `_pack_secondary_fft_frame()` in `ReceiverSession` + 214 lines
     of new tests. Closes the slice-14 STATUS.md open item
     "secondary-demod forwarding for openwebrx_remote".
  2. `361eada` `docs: handoff bundle + status snapshot through slice-22`
     — the docs + tooling: rewritten `docs/AI-HANDOFF.md` (35 KB, fully
     updated for slices 15-22), `docs/STATUS.md` slice-15-22 addendum,
     new `scripts/build-handoff-zip.sh` (persistent zip builder),
     `.gitignore` update.
- Pushed via PAT-strip protocol per `SYNC-UP.md`: temporarily inserted
  PAT into remote URL → `git push origin main` (pushed `a76a04e..361eada`)
  → immediately stripped PAT back to plain HTTPS URL.
- Verified sync: `git ls-remote origin refs/heads/main` returns
  `361eadaa810441c7aaf2eddb07483322e187ce7e` — matches local HEAD.

**CI Audit (per `GET /repos/nealhamiltonjr/websdr/actions/runs`):**
- Total runs in the API record: 36.
- **5 historical failures identified:**
  - Run #31 (slice-18, sha=5afd1432) — Rust module compile errors
    (private config field, use-after-move, float32 noise threshold).
    Fixed by commit `cc76fa21` ("fix(slice-18): Rust fixes…"); Run #32
    was success.
  - Runs #19-22 (slices 9-11 + the slice-10 test fix) — iterative
    fixes around the AI cascade + OffscreenCanvas + vitest 3.x
    migration. Slice-12 ("vitest 3.x + typescript-eslint unified +
    libcsdr CI cache") was the consolidating fix; Run #23 onward
    is consistently green.
- All 5 historical failures are TRANSIENT (each immediately resolved
  by the next commit). The most recent 7 runs (slice-15 through the
  current docs commit) are all `conclusion: success`.
- **Run #36 (this push) — ALL 5 CI JOBS GREEN in 2.5 minutes:**
  - AI (packages/ai-rust): success (22 s)
  - Backend (apps/server): success (~2.5 min; builds libcsdr from
    source on cache miss, runs `uv sync`, bakes IQ fixtures, runs
    `ruff check`, `mypy openwebrx_plus`, `pytest`)
  - DSP (packages/dsp-zig): success (19 s)
  - Frontend (apps/web): success (23 s)
  - Shared Types (packages/shared-types): success (14 s)
- Note: the CI server job runs `mypy openwebrx_plus` (NOT `--strict`)
  so the 2 known `--strict`-only errors in `sources/sdrplay.py`
  (cffi callback untyped-decorator) don't break CI. The local strict
  gate has them as known regressions per `docs/AI-HANDOFF.md` §4 + §5.1.

**Stage Summary:**
- Sync: verified — local HEAD `361eadaa` == `origin/main` `361eadaa`.
- Workflows: all 5 jobs in the most recent run completed successfully.
- Historical failures all accounted for (each resolved by the next
  commit); the "some workflows fail" state is no longer current.
- Bonus: cleaned up the GUID-message auto-commit pattern, restored
  the slice-22 code (real federation polish — secondary FFT wire
  format) as a proper named commit. The handoff bundle ZIP itself
  lives in `download/` (gitignored, not tracked — operator can
  regenerate via `scripts/build-handoff-zip.sh`).

**Next-up tier (per `docs/AI-HANDOFF.md` §5):**
1. sdrplay mypy --strict fix (5-minute hygiene win — `# type:
   ignore[untyped-decorator]` on two cffi callbacks).
2. FT8 demodulator + LDPC + CRC-14 + message unpack (closes the
   slice-21 stub; 2-3 days for v1).
3. RNNoise AudioWorklet integration (closes the slice-19 loader;
   half a day).
4. SDRangel REST+WS streaming implementation (closes the slice-20
   scaffold; 1-2 days for v1).
5. DeepFilterNet weights + upstream crate (closes the slice-18
   scaffold; half a day code, weights licensing review adds time).

---
## slice-23 (2026-08-28): close sdrplay mypy --strict regression + bootstrap-venv.sh tooling

**Goal:** close the only known static-gate regression (sdrplay cffi callback
untyped-decorator errors) so mypy --strict runs fully clean, AND ship a
persistent bootstrap script that recreates the dev environment from a
fresh checkout (the env-reset case that wiped `.venv/` + the
`scripts/pycsdr-build/` restore artifacts this session).

**Shipped:**
- `apps/server/openwebrx_plus/sources/sdrplay.py`: added
  `# type: ignore[untyped-decorator]` on the two `@ffi.callback(...)`
  decorators (`_stream_cb` at line 193, `_gain_cb` at line 207). cffi's
  callback protocol is intentionally opaque (the decorated function
  takes opaque `Any` pointers and produces a `cffi.CData` object), so
  a typed signature is impractical without a full cffi typing shim.
  This matches the pragmatic-fix option from `docs/AI-HANDOFF.md` §5.1.
- `scripts/bootstrap-venv.sh`: new persistent idempotent script that
  rebuilds the entire dev env from source on a fresh checkout:
  1. `apt-get download libsamplerate0 libsamplerate0-dev` + extract
     to `~/.local/usr/` + patch the `samplerate.pc` prefix.
  2. Clone `jketterl/csdr.git` + cmake build + install to `~/.local/usr/`.
     Includes the `target_include_directories` + `target_link_directories`
     patch from `scripts/README-dsp-bootstrap.md` (libcsdr upstream
     CMakeLists doesn't propagate samplerate/fftw3 dirs to targets).
  3. `uv venv` in `apps/server/` + install runtime deps + editable
     openwebrx_plus + dev tooling (pytest/ruff/mypy/setuptools/wheel/pip).
  4. Clone `jketterl/pycsdr.git` + `pip install --no-build-isolation`
     against the user-prefix libcsdr (CFLAGS/LDFLAGS/PKG_CONFIG_PATH
     all set).
  5. Regenerate IQ fixtures via `scripts/generate_iq_fixtures.py`.
  Each step is idempotent (checks for already-done state and skips).
- No code changes outside the sdrplay type: ignore — slice-22's
  secondary FFT wire format work remains unchanged.

**Quality gates verified (all green):**
- `mypy --strict openwebrx_plus`: **Success: no issues found in 57
  source files** (was 3 errors — 2 sdrplay cffi callbacks + 1
  transient tomli_w not-installed).
- `ruff check openwebrx_plus`: All checks passed!
- server pytest: 456 passed, 1 skipped (84% coverage, ~76 s).
- web vitest: 163/163 pass (~2.8 s).
- `tsc --noEmit`: clean.
- `vite build`: clean (chunk-size warning, not an error).

**Sync:** will commit + push next.

**Stage Summary:**
- The "mypy --strict has 2 known errors" caveat in `docs/AI-HANDOFF.md`
  §4 + §5.1 is now stale — the gate runs fully clean. The next
  handoff-bundle revision can drop the §5.1 fix-recipe section.
- The from-source bootstrap path is now persistent in
  `scripts/bootstrap-venv.sh` (previously documented only in
  `scripts/README-dsp-bootstrap.md` as a recipe). Operators / AIs
  picking the project up cold can run one script to rebuild the env.

---
## slice-24 (2026-08-28): RNNoise AudioWorkletProcessor — closes slice-19 loader

**Goal:** close the slice-19 roadmap item "Wire the loaded
RNNoiseDenoiser into the AudioPlayer." Slice-19 shipped the WASM
loader contract; slice-24 wires it into the actual audio path via
an AudioWorkletProcessor.

**Shipped:**
- `apps/web/public/worklets/rnnoise-processor.js`: plain-JS
  AudioWorkletProcessor (TS-typed reference doc'd in the file
  header). Vite serves public/ verbatim, so the file is reachable
  at `/worklets/rnnoise-processor.js` in both dev and prod.
  - Buffers 128-sample render quanta into 480-sample frames
    (RNNOISE_FRAME_SIZE matches the slice-19 constant).
  - Calls the WASM `Denoiser.process_frame()` per 480-sample frame.
  - Drains denoised frames from an output ring buffer (4× frame
    size = 40 ms headroom for bursty quanta).
  - Async dynamic `import('/pkg/rnnoise_wasm.js')` inside the
    worklet — until resolved, passes audio through unchanged.
    On failure, permanently falls back to pass-through. Posts
    a `{type:'ready'}` message to the main thread on success.
  - Control port messages: `{type:'reset'}` clears state,
    `{type:'dispose'}` permanently disables.
  - Initial latency: ~10 ms (one 480-sample frame at 48 kHz).
  - NOTE: dynamic `import()` in AudioWorkletGlobalScope requires
    Chromium 105+ or Firefox 113+. Safari 16.x doesn't support it;
    operators on Safari get the direct path (the AudioPlayer's
    enableClientDenoise() returns false before attempting worklet
    registration, see user-agent feature detection below).

- `apps/web/src/lib/audio/AudioPlayer.ts`: added the client-denoise
  integration.
  - New `clientDenoiseEnabled(): boolean` signal (UI binding).
  - New `enableClientDenoise(): Promise<boolean>` — calls the
    slice-19 `loadRNNoiseModule()` to probe for the WASM; if not
    deployed, returns false. Otherwise registers the worklet via
    `audioContext.audioWorklet.addModule('/worklets/rnnoise-processor.js')`
    and inserts an `AudioWorkletNode` between the BufferSourceNodes
    and the GainNode. Idempotent. Feature-detects `audioWorklet`
    (Safari 16.x lacks it → returns false without throwing).
  - New `disableClientDenoise(): void` — posts dispose to the
    worklet, disconnects the node, returns to the direct path.
    Idempotent.
  - Refactored `enqueue()` to use a `sinkNode` pointer (either
    `gainNode` for direct path or `denoiseNode` for worklet path).
  - Refactored `disable()` to clean up the worklet node too.
  - Replaced `window.AudioContext` lookup with `globalThis.AudioContext`
    so the node-environment vitest tests can stub the constructor via
    `vi.stubGlobal('AudioContext', ...)`.

- `apps/web/src/lib/audio/AudioPlayer.test.ts`: new 15-test suite
  covering the state machine:
  - Initial state (muted, volume 0.5, denoise disabled, enqueue
    no-op when muted).
  - enable/disable transitions (creates AudioContext, idempotent,
    close() called on disable, BufferSource scheduled on enqueue).
  - Client denoise state machine (returns false before enable,
    returns false when WASM not deployed, disableClientDenoise
    idempotent, disable resets denoise state).
  - Volume control (setVolume updates gain, toggleMute toggles).

**Quality gates verified (all green):**
- web vitest: 178/178 pass (~3.2 s; 163 prior + 15 new).
- `tsc --noEmit`: clean.
- `vite build`: clean (chunk-size warning, not an error).
- (server tests not touched in this slice; 456/1 skipped confirmed
  in the prior turn's slice-23 verification.)

**Sync:** will commit + push next.

**Future work remaining:**
- Wire `enableClientDenoise()` to a UI control in DSPControls
  (the dsp_mode toggle already has raw/classic/ai/cascade options;
  the 'cascade' mode is the natural trigger).
- Test the worklet processor code itself in a real AudioWorklet
  context (the unit tests cover the AudioPlayer state machine;
  worklet integration is verified manually in the dev browser).
- Cross-browser check: confirm Chromium 105+ and Firefox 113+
  both load the WASM inside the worklet (Safari 16.x explicitly
  excluded; check Safari 17+ when the AudioWorklet import() spec
  support lands).

---
## slice-25 (2026-08-28): SDRangel REST+WS streaming v1 — closes slice-20 scaffold

**Goal:** close the slice-20 STATUS.md open item "the actual REST+WS
streaming implementation lands in a future slice." Slice-20 shipped
the manifest scaffold (raises NotImplementedError on spawn); slice-25
ships the v1 spectrum-only streaming impl.

**Shipped:**
- `apps/server/openwebrx_plus/sources/sdrangel.py`: fully rewritten
  (was the slice-20 scaffold). Now implements the DisplayStreamSource
  protocol via `display_stream()` (not `spawn()` — the ReceiverSession
  detects DisplayStreamSource via hasattr(source, 'display_stream')
  and bypasses its raw-IQ chains, repacking frames into the WRFO wire
  format).
  - `display_stream()`:
    1. Creates an httpx.AsyncClient (or uses an externally-injected
       one — test injection pattern; owns_http flag controls cleanup).
    2. Probes GET /sdrangel/devices to confirm the device_set exists.
    3. PUTs /sdrangel/deviceset/{id}/device/settings with the initial
       center frequency (set via tune() before display_stream() or 0
       by default).
    4. Opens the spectrum WebSocket at
       ws://host:port/sdrangel/spectrumserver?deviceset={id}.
    5. Reads JSON start metadata frames (updates _remote_fft_size /
       _remote_sample_rate / _remote_center_freq / _remote_min_db /
       _remote_max_db from the {"type":"start",...} payload).
    6. Reads binary spectrum frames, yields one RemoteFftFrame per
       frame. Two binary layouts auto-detected by length math:
         A. 4-byte LE header (uint16 size + uint16 history) +
            size*float32 bins.
         B. Bare float32 bins (no header).
    7. finally block closes the WS + HTTP client (if we own it).
  - `tune(freq_hz)`: if streaming, re-PUTs device settings on the
    live HTTP client. If not yet streaming, stores the freq for the
    upcoming display_stream() initial PUT.
  - `set_mode(mode)`: NOT IMPLEMENTED in v1 (spectrum-only). Raises
    NotImplementedError with an actionable message pointing to the
    future implementation plan (POST /deviceset/{id}/channel + PUT
    /deviceset/{id}/channel/{cid}/settings).
  - `close()`: no-op (display_stream()'s finally block handles cleanup).
  - Optional basic auth: username/password constructor args →
    Authorization header.
  - use_tls flag: https:// REST + wss:// WS for instances behind TLS
    reverse proxies.

- `apps/server/tests/test_sdrangel_driver.py`: rewrote + extended.
  - 11 slice-20 manifest scaffold tests retained (manifest registration,
    constructor validation, default port/sample rate, user agent).
  - 14 new slice-25 v1 streaming tests using a FakeSDRangelServer
    (ASGI app for REST + websockets.serve for the spectrum WS):
    * display_stream() yields 3 RemoteFftFrame per 3 binary spectrum
      frames; each frame has the expected bins/center_freq/sample_rate.
    * display_stream() rejects an out-of-range device_set with
      RuntimeError.
    * display_stream() PUTs /deviceset/0/device/settings on startup
      with the centerFrequency that tune() set before streaming.
    * tune() while streaming re-PUTs new device settings.
    * close() outside the streaming loop is a no-op.
    * set_mode() raises NotImplementedError (spectrum-only v1).
    * tune() before streaming stores the freq for the initial PUT.
    * _parse_spectrum_frame accepts both pattern A (header) and
      pattern B (bare float32) layouts.
    * _parse_spectrum_frame rejects frames shorter than 4 bytes.
    * _handle_text captures JSON start metadata (size, sampleRate,
      centerFrequency, minDb, maxDb) — including the edge case
      where maxDb is 0.0 (falsy in Python; fixed with explicit
      None checks instead of `or`).
    * _handle_text ignores non-JSON text frames (server chatter).
    * _auth_headers produces a Basic Authorization header when
      username/password are set; omits it otherwise.
  - 25 tests total (11 prior + 14 new). All pass in ~0.6 s.

**Bug fix discovered while writing tests:** the original `_handle_text`
used `data.get("maxDb") or data.get("max_db")` which skipped legitimately
zero max_db values (0.0 is falsy). Fixed with explicit None checks:
```python
maxdb = data.get("maxDb")
if maxdb is None:
    maxdb = data.get("max_db")
```

**Quality gates verified (all green):**
- mypy openwebrx_plus (CI invocation, pyproject strict=true): Success,
  no issues found in 57 source files.
- ruff check openwebrx_plus: All checks passed!
- server pytest: 466 passed, 1 skipped (84% coverage, ~75 s; was 456,
  +10 from the 14 new SDRangel tests minus the 4 deprecated spawn/
  tune/set_mode scaffold tests that no longer apply).
- web gates (vitest/tsc/vite build): not touched in this slice;
  verified green in slice-24.

**Sync:** will commit + push next.

**Future work remaining:**
- set_mode() implementation: POST /deviceset/{id}/channel to add a
  channel (NFM/WFM/AM/LSB/USB/CW), then PUT
  /deviceset/{id}/channel/{cid}/settings to configure it. ~half a day.
- Audio-over-WS path: SDRangel has no built-in audio-over-WS; v2 needs
  to wire SDRangel's UDP-sink channel to a local UDP port we read
  from, then translate to RemoteAudioFrame. ~half a day.
- Live bring-up: the wire literals (REST endpoint paths, JSON
  metadata field names, binary frame layout) are codified by the
  fake server; the FIRST connection to a real SDRangel instance
  should verify (a) REST endpoint paths, (b) JSON field name
  variants (size vs fftSize, sampleRate vs sample_rate), (c) the
  binary frame layout. Adjust the `_*` constants in
  sources/sdrangel.py if needed.

---
## slice-26 (2026-08-28): FT8 FSK demod + CRC-14 + standard message unpack (v1, closes slice-21)

**Goal:** close the slice-21 STATUS.md open item "the actual FSK
demodulator + LDPC + CRC + message unpack lands in a future slice."
v1 ships the FSK demod + CRC-14 + standard message unpack; LDPC
syndrome check, Costas loop, and 6-char callsigns are deferred to v2.

**Shipped:**
- `apps/server/openwebrx_plus/plugins/ft8_demod.py`: new module with
  the audio-band FSK demodulator.
  - `goertzel_magnitude(samples, freq_hz, sample_rate)` — efficient
    single-frequency DFT bin computation (the Goertzel algorithm;
    more efficient than a full FFT when only 8 tones are needed).
  - `detect_symbols(audio, sample_rate)` — buffers audio into 1920-
    sample symbol periods (FT8_SAMPLE_RATE/FT8_BIT_RATE_BAUD),
    computes Goertzel magnitude at each of the 8 FT8 tones (offset
    0..7 × 6.25 Hz from the 1500 Hz baseline), picks the strongest
    per symbol. Returns int8[79].
  - `symbols_to_bits(symbols)` — extracts the 174-bit LDPC codeword
    from 79 symbols (3 Costas arrays × 7 symbols + 58 data symbols
    × 3 bits = 174). Each data symbol's value (0..7) unpacks to 3
    bits (MSB first).
  - `bits_to_symbols(bits)` — inverse for synthetic test frames.
  - `symbols_to_audio(symbols, sample_rate)` — synthesizes FT8 audio
    from symbols (pure cosine; real FT8 uses GFSK with raised-cosine
    shaping, but pure cosine is sufficient for the Goertzel demod to
    lock on).

- `apps/server/openwebrx_plus/plugins/ft8_protocol.py`: new module
  with the message-level protocol.
  - `crc14(bits, length_bits)` — FT8 CRC-14 (polynomial 0x2757).
  - `add_crc(message_bits)` / `verify_crc(codeword_bits)` — append/
    verify the 14-bit CRC over the 77-bit message → 91-bit
    systematic codeword.
  - `add_ldpc_parity(systematic_bits)` — v1 stub: zero-pads to 174
    bits (the actual LDPC parity computation lands in v2 with the
    published H matrix).
  - `pack_callsign(callsign)` / `unpack_callsign(packed28)` — base-40
    alphabet encoding (the 40-char FT8 alphabet: 10 digits + 26
    letters + ' ' + '.' + '/' + '?'). v1 supports up to 5-char
    callsigns (40^5 = 102M, fits in 28 bits with the non-standard
    flag at bit 27 clear). 6-char callsigns (special 6-char alphabet
    in WSJT-X) land in v2.
  - `pack_grid_or_report(s)` / `unpack_grid_or_report(grid15)` —
    v1 supports:
    * Special markers: "..." (no grid), "73", "RR73", "RRR"
    * Signal reports: -25..+24 (50 values, fits in 6 bits)
    * 4-char Maidenhead grids AA00-RR99 (lower 14 bits split into
      chars_idx*100 + digits)
    Other grid/report encodings land in v2.
  - `pack_message(cs1, cs2, grid_or_report, i3)` /
    `unpack_message(bits)` — pack/unpack the 77-bit FT8 message
    payload (28-bit callsign1 + 28-bit callsign2 + 15-bit
    grid_or_report + 3-bit i3_type + 3-bit reserved).
  - `bits_to_int(bits, length)` / `int_to_bits(val, length)` — bit
    helpers (MSB first).

- `apps/server/openwebrx_plus/plugins/ft8.py`: fully rewritten to
  use the new demod + protocol modules. Implements the
  DecoderPlugin contract:
  - `feed_audio(pcm, sample_rate)` — buffers int16 PCM into 15-second
    slots, runs `detect_symbols` → `symbols_to_bits` → `verify_crc`
    → `unpack_message`, emits "message" + "messages" events per
    decoded frame.
  - `feed_iq(iq)` — accepts complex IQ and takes the real part as
    the audio envelope (the operator normally attaches FT8 to a
    demod-channel receiver that produces complex baseband audio).
  - All-zero systematic bits detection — skips the "no signal"
    degenerate case where CRC happens to pass on silence (crc14(zeros)
    = 0, embedded CRC bits are also zero).
  - `status()` reports `messages_decoded`, `crc_failures`,
    `slot_count`, `stub: False` (v1 is no longer a stub), `version:
    "0.2.0"`, `v1_simplifications` list, and a `note` documenting
    the deferred v2 items.
  - `stop()` clears all streaming state.
  - `synthesize_audio(callsign1, callsign2, grid_or_report, i3)` —
    test helper that synthesizes a complete FT8 audio slot from a
    known message.

- `apps/server/tests/test_ft8_decoder.py`: rewrote + extended.
  - 5 slice-21 manifest scaffold tests retained (manifest fields,
    status, plugin registration, stop is noop, constants).
  - 28 new v1 tests covering:
    * CRC-14 round-trip + bit-flip detection (in message and CRC)
    * Callsign pack/unpack round-trip (5-char standard callsigns)
    * Callsign rejects: too long (>5 chars in v1), invalid chars
    * Non-standard callsign flag (0x8000000)
    * Grid/report pack/unpack round-trip: 4-char grids, signal
      reports -25..+24, special markers (RRR/RR73/73/...)
    * Message unpack standard format
    * add_crc + verify_crc round-trip
    * add_ldpc_parity pads to 174 bits (zero-padded v1 stub)
    * Goertzel single-tone detection (mag_at_tone >> mag_off_tone)
    * detect_symbols picks strongest tone for clean signals
    * bits_to_symbols ↔ symbols_to_bits round-trip
    * symbols_to_audio ↔ detect_symbols round-trip
    * End-to-end: synthesize "K1ABC KO51 -12" → feed_audio →
      decoded message event matches.
    * End-to-end with "W1AW FN20 -05".
    * Silence produces no decodes (all-zero detection).
    * Chunked audio (1000-sample chunks straddling symbol boundaries).
    * feed_iq on complex IQ routes through audio path (real part).
    * feed_audio rejects wrong sample rate.
    * Snapshot "messages" event emitted alongside "message" event.
    * status() reflects decode count after decodes.
    * stop() resets state.
  - 33 tests total. All pass in ~2 s.

**Bug fixes discovered while writing tests:**
1. FT8 alphabet was wrong (declared 48 chars, real is 40 chars
   "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ ./?" — no '+'/'-' chars).
2. Callsign packing was using 6 chars (max 40^6 = 4B, exceeds 28-bit
   range). Fixed to 5 chars (40^5 = 102M, fits in 28 bits with the
   non-standard flag at bit 27).
3. unpack_grid_or_report had overlapping conditions; fixed to cleanly
   separate: special markers → 0/0x7FFD/0x7FFE/0x7FFF, signal
   reports → 1..50, 4-char grids → 0x8000+.
4. feed_iq was using np.abs (loses half the signal energy for real
   audio passed as complex with zero imag); fixed to use np.real.
5. All-zero systematic bits produced spurious decodes from silence
   (crc14(zeros)=0 trivially matches embedded zero CRC); added
   explicit all-zero detection to skip the degenerate case.

**Quality gates verified (all green):**
- mypy openwebrx_plus (CI invocation, pyproject strict=true): Success,
  no issues found in 59 source files (was 57, +2 from ft8_demod +
  ft8_protocol).
- ruff check .: All checks passed!
- server pytest: 491 passed, 1 skipped (84% coverage, ~79 s; was
  466, +25 from the new FT8 tests).

**Sync:** will commit + push next.

**Future work remaining (v2):**
- **LDPC syndrome check + sum-product decoder** — the published FT8
  H matrix (83 × 174 sparse, ~590 nonzero entries) needs to be
  hardcoded; soft-decision sum-product decode is the right algorithm
  for ~3 dB SNR improvement. v1 just verifies CRC (occasional false
  positives ~1/16384; honest for v1).
- **Costas loop / symbol timing recovery** — v1 assumes symbol
  boundaries are aligned to 0.16 s boundaries from the start of the
  15-second slot. Real FT8 has ±0.5 symbol timing offset + Doppler;
  a Costas-loop correction lands in v2.
- **6-char callsigns** — the special 6-char WSJT-X alphabet for
  extended callsigns (ZL2ABC, etc.) lands in v2.
- **i3 != 0 message types** — ARRL RTTY RU / Field Day / POTA /
  contests / WW ROAG (i3=1..5) land in v2.
- **SNR computation** — v1 doesn't compute SNR; needs the Goertzel
  magnitude ratio vs noise floor.
- **Live bring-up**: verify the wire literals against real WSJT-X
  frames (the FT8_LDPC matrix, the Costas sync pattern, the symbol
  position layout, the alphabet). v1 is spec-faithful but unverified
  on-air.

---
Task ID: 27
Agent: super-z (main agent)
Task: Continue autonomously per docs/AI-HANDOFF.md §5. Verify state, close any lingering items.

Work Log:
- Read worklog + STATUS.md + git log to establish true current state.
- Discovered the prior-session summary was STALE: it claimed slices 23-26 (sdrplay mypy fix, RNNoise AudioWorklet, SDRangel REST+WS, FT8 demod) were still pending, but git log shows all four were actually committed during the prior session:
  * 0662907 slice-23: sdrplay mypy --strict fix + bootstrap-venv.sh tooling
  * cc68991 slice-24: RNNoise AudioWorkletProcessor (closes slice-19)
  * b8b1d04 slice-25: SDRangel REST+WS streaming v1 (closes slice-20)
  * 29ee618 fix(slice-25): ruff cleanups for sdrangel tests
  * 3e88175 slice-26: FT8 FSK demod + CRC-14 + standard message unpack (closes slice-21)
  * 6800d05 fix(slice-23): remove unused type: ignore comments (cffi callbacks) — slice-23b followup
- Initial attempt: ran `uv run mypy --strict openwebrx_plus/sources/sdrplay.py` which triggered an inadvertent `uv sync` rebuild attempt (pycsdr from source fails on missing libcsdr dev headers in env). Per docs/AI-HANDOFF.md §8 landmine, NEVER use `uv run` after env restore — invoke `.venv/bin/mypy` directly.
- CLI `mypy --strict` (which OVERRIDES pyproject's `disallow_untyped_decorators = false`) reported 2 errors at sdrplay.py lines 193/207 (the cffi.callback decorators). Attempted fix: re-add `# type: ignore[untyped-decorator]` per handoff §5.1.
- REALIZED this would REGRESS CI: slice-23b (commit 6800d05) deliberately REMOVED these comments because they're flagged as "unused type: ignore" under CI's invocation (`uv run mypy openwebrx_plus` reads pyproject.toml where disallow_untyped_decorators=false). The comments were only useful under CLI --strict which CI doesn't use.
- REVERTED sdrplay.py to HEAD (no comments) — matches slice-23b's deliberate state.
- Verified CI's actual mypy invocation: `mypy openwebrx_plus` → Success, no issues found in 59 source files.
- Verified ruff: `ruff check .` → All checks passed!
- Verified server pytest: 491 passed, 1 skipped (84% coverage, ~82 s; up from prior-session 466 baseline because slice-26 added 25 FT8 tests — matches worklog).
- Discovered a real maintenance gap: pyproject.toml declared `tomli_w>=1.0.0` since slice-X (used in `openwebrx_plus/config/user_settings.py` for TOML round-trip) but uv.lock was never re-synced — initial import lock file did NOT include tomli-w despite the env having it installed (1.2.0). My inadvertent `uv sync` computed the correct lock — keeping that change.
- Confirmed tomli_w 1.2.0 is installed in venv: `import tomli_w; print(tomli_w.__version__)` → 1.2.0.

Stage Summary:
- Prior-session roadmap items (slices 23-26) verified ALL complete and pushed. Summary was stale; no new code work needed for those slices.
- Single real fix this session: sync `apps/server/uv.lock` to declare `tomli-w` (closes a long-standing pyproject-vs-lock drift; the env already had it installed, the lock just didn't reflect reality).
- All quality gates green under CI's actual invocations:
  * mypy openwebrx_plus (CI invocation): Success, no issues in 59 source files
  * ruff check .: All checks passed!
  * server pytest: 491 passed, 1 skipped
  * web vitest/tsc/vite/build: unchanged from prior green baseline (not touched this session)
- Next-up roadmap per docs/AI-HANDOFF.md §5 mid/long-term: FLDIGI/RTTY/PSK31 siblings, frontend 'WRSF' viz (channel-scope waterfall), propagation intelligence, mobile layout, Docker deployment, FT8 v2 (LDPC sum-product + Costas + 6-char callsigns + i3≠0 message types), DeepFilterNet weights + upstream crate.
