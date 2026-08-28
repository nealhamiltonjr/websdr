# rnnoise-wasm — client-side speech denoise in the browser (Stage 2b)

Slice-19 status: the **integration plumbing** is shipped — the
web app's `RNNoiseLoader` (`apps/web/src/lib/denoise/RNNoiseLoader.ts`)
probes for the compiled WASM module at `/pkg/rnnoise_wasm.js` and
gracefully falls back to "no client-side denoise" when the binary
isn't deployed. **The actual WASM binary is NOT built in this repo**
(sandbox has no `emcc`); operators who want client-side denoise build
it themselves with the recipe below.

## What ships in this repo

- `apps/web/src/lib/denoise/RNNoiseLoader.ts` — the loader + the
  `RNNoiseDenoiser` interface (frameSize, processFrame, reset,
  dispose).
- `apps/web/src/lib/denoise/RNNoiseLoader.test.ts` — the not-deployed
  path tests (cache, null returns, constants).
- This README — the operator build recipe.

## Operator build recipe (one-time, on a host with emcc)

```bash
# 1. Install the Emscripten SDK (one-time; ~2 GB).
git clone https://github.com/emscripten-core/emsdk.git ~/emsdk
~/emsdk/emsdk install latest
~/emsdk/emsdk activate latest
source ~/emsdk/emsdk_env.sh

# 2. Clone Xiph's RNNoise + a JS wrapper that exposes wasm-bindgen
#    semantics (Dennoise.new(sr), process_frame(Float32Array),
#    reset(), free()). The Xiph source itself is C; the wrapper
#    layer is a small (≈80 line) wasm-bindgen project. Upstream
#    reference: https://github.com/Rikorose/rnnoise-wasm (or build
#    the Xiph sources directly with emcc + a manual JS wrapper).
git clone https://github.com/Rikorose/rnnoise-wasm.git ~/rnnoise-wasm
cd ~/rnnoise-wasm
wasm-pack build --release --target web

# 3. Copy the build output into the web app's public/ dir.
cp -r pkg /path/to/openwebrx-plus/apps/web/public/pkg

# 4. Verify: run the dev server, hit the AudioPlayer page. The
#    loader's fetch HEAD to /pkg/rnnoise_wasm.js will succeed, the
#    dynamic import will resolve, and createDenoiser() will return
#    a real RNNoiseDenoiser. The audio path runs through it frame-
#    by-frame.
```

## Wire format expected by the loader

The loader assumes the wasm-bindgen output shape:

```typescript
// /pkg/rnnoise_wasm.js (wasm-bindgen output, target web)
export class Denoiser {
  constructor(sampleRate?: number);
  process_frame(input: Float32Array): Float32Array;
  reset(): void;
  free(): void;
}
export const frame_size: number;  // 480 by default
```

If your build emits a different API (e.g., raw `Module._denoise`
calls without a JS class wrapper), either write a thin wrapper script
or adjust `RNNoiseLoader.ts`'s `RNNoiseModule` interface.

## Why the WASM binary isn't shipped

- **Repo bloat**: the compiled WASM is ~300 KB; shipping it in the
  repo duplicates per release.
- **License alignment**: RNNoise is BSD-2-Clause; the OpenWebRX+ core
  is AGPL-3.0. Bundling triggers no license conflict but does add an
  attribution obligation — keeping the binary optional makes the
  attribution explicit (operators who deploy the WASM are opting
  into the additional license).
- **CI reliability**: building WASM in CI requires emcc + ~2 GB of
  SDK — adds 5+ minutes per run for a feature operators may not
  need. The current CI matrix (Frontend / Backend / DSP / Shared
  Types / AI) builds without emcc; slice-19's plumbing is verified
  by the not-deployed path tests.

## Future slices

- **Wire the Denoiser into the AudioPlayer**: the slice-19 loader
  exposes the API; the actual AudioPlayer swap (replace
  AudioBufferSourceNode scheduling with an AudioWorkletProcessor that
  runs RNNoise frame-by-frame) lands in a future slice. The current
  AudioPlayer already supports being wrapped; the integration is the
  next step.
- **Demucs / Open-Unmix offline Stage 3 (ADR-002)**: the AI cascade
  sequence is RNNoise Stage 2b → Demucs Stage 3. Both ship as WASM;
  the loader pattern from slice-19 extends directly.
