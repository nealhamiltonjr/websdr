/**
 * RNNoise WASM loader — Stage 2b of the AI cascade (ADR-002).
 *
 * Slice-19: ships the integration plumbing for client-side RNNoise
 * denoising without requiring the WASM binary to be built at dev time.
 * Operators build the binary separately (see
 * `packages/rnnoise-wasm/README.md` for the emcc recipe) and drop the
 * resulting `rnnoise_wasm.js` + `.wasm` into `apps/web/public/pkg/`.
 * When the binary is present, `loadRNNoiseModule()` returns a
 * `RNNoiseModule` instance backed by the WASM module; when absent,
 * it returns null — the AudioPlayer remains the default (no
 * client-side denoise).
 *
 * This mirrors the slice-18 Rust-backed AIDenoiser pattern: opt-in
 * native acceleration with a graceful fallback to the existing
 * server-side path.
 *
 * Detection is async (dynamic import + fetch probe). The loader
 * caches the result; callers should `await loadRNNoiseModule()` and
 * store the handle (don't call it per-frame).
 */

// Path under the web app's public/ dir where the WASM package must
// live. Vite serves files in public/ at the site root, so this is
// what the loader fetches.
const RNNOISE_PATH = '/pkg/rnnoise_wasm.js';

// Default frame size (samples per call). RNNoise ships with this
// baked in; the JS wrapper exposes it as a constructor arg.
export const RNNOISE_FRAME_SIZE = 480;

// Default sample rate. RNNoise is trained on 48 kHz; resample before
// calling processFrame() if the source is at a different rate.
export const RNNOISE_SAMPLE_RATE = 48000;

/** A loaded RNNoise WASM denoiser instance. */
export interface RNNoiseDenoiser {
  /** Frame size (samples per call to processFrame). */
  readonly frameSize: number;
  /** Process one frame of float32 samples in place. Length must
   * equal frameSize. Returns the denoised samples (same length). */
  processFrame(input: Float32Array): Float32Array;
  /** Reset internal state (RNN state). Call between sessions. */
  reset(): void;
  /** Free WASM memory. Idempotent. */
  dispose(): void;
}

/** The shape of the WASM module's exported Denoiser class. */
export interface RNNoiseModule {
  Denoiser: new (sampleRate?: number) => {
    process_frame: (input: Float32Array) => Float32Array;
    reset: () => void;
    free: () => void;
  };
  frame_size: number;
}

let cachedModule: RNNoiseModule | null | undefined = undefined;

/**
 * Load the RNNoise WASM module if it's been built and deployed.
 *
 * Returns:
 *   - A `RNNoiseModule` instance if the WASM package is present
 *   - `null` if the package isn't deployed (operator hasn't run
 *     `make rnnoise-wasm` yet — see packages/rnnoise-wasm/README.md)
 *
 * The result is cached; subsequent calls return the same promise.
 * Callers should hold the resolved `RNNoiseModule` and call
 * `new module.Denoiser()` per audio stream (not per frame).
 */
export async function loadRNNoiseModule(): Promise<RNNoiseModule | null> {
  if (cachedModule !== undefined) {
    return cachedModule;
  }
  // Probe: fetch the JS wrapper. If 404 or fetch fails (dev server
  // not yet running), treat as not-deployed.
  try {
    const probe = await fetch(RNNOISE_PATH, { method: 'HEAD' });
    if (!probe.ok) {
      cachedModule = null;
      return null;
    }
  } catch {
    // Network error or invalid URL — treat as not-deployed.
    cachedModule = null;
    return null;
  }
  try {
    // Dynamic import — Vite serves the JS file from public/ verbatim.
    // The wasm-bindgen output exposes `Denoiser`, `frame_size` on the
    // module namespace.
    const mod = (await import(/* @vite-ignore */ RNNOISE_PATH)) as unknown as RNNoiseModule;
    if (typeof mod.Denoiser !== 'function') {
      cachedModule = null;
      return null;
    }
    cachedModule = mod;
    return mod;
  } catch {
    // Import can fail if the WASM file is missing or the JS wrapper
    // has a syntax error. Treat as not-deployed.
    cachedModule = null;
    return null;
  }
}

/** Whether `loadRNNoiseModule()` has been called and resolved. */
export function isRNNoiseLoadAttempted(): boolean {
  return cachedModule !== undefined;
}

/** Reset the loader cache (test helper). */
export function resetRNNoiseCache(): void {
  cachedModule = undefined;
}

/**
 * Create a `RNNoiseDenoiser` instance backed by the loaded WASM module.
 *
 * Returns `null` if the module isn't loaded (call `loadRNNoiseModule()`
 * first). The caller owns the instance and must `dispose()` it.
 */
export function createDenoiser(sampleRate: number = RNNOISE_SAMPLE_RATE): RNNoiseDenoiser | null {
  if (cachedModule == null) {
    return null;
  }
  const mod = cachedModule as RNNoiseModule;
  try {
    const inner = new mod.Denoiser(sampleRate);
    const frameSize =
      (mod as unknown as { frame_size?: number }).frame_size ?? RNNOISE_FRAME_SIZE;
    let disposed = false;
    return {
      frameSize,
      processFrame(input: Float32Array): Float32Array {
        if (disposed) {
          return input;
        }
        if (input.length !== frameSize) {
          // Wrong frame size — pass through (don't break audio).
          return input;
        }
        try {
          return inner.process_frame(input);
        } catch {
          // WASM call failed — pass through unchanged.
          return input;
        }
      },
      reset(): void {
        if (disposed) return;
        try {
          inner.reset();
        } catch {
          // Ignore — best-effort reset.
        }
      },
      dispose(): void {
        if (disposed) return;
        disposed = true;
        try {
          inner.free();
        } catch {
          // Ignore — best-effort free.
        }
      },
    };
  } catch {
    // Constructor threw (invalid sample rate, OOM, etc.).
    return null;
  }
}

/** True iff the RNNoise WASM module has been successfully loaded. */
export function isRNNoiseAvailable(): boolean {
  return cachedModule != null;
}
