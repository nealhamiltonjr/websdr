# rnnoise-wasm — client-side speech denoise in the browser (Stage 2b).
#
# Slice-1 status: empty. Slice-2 will compile Xiph's RNNoise to Wasm via
# Rust + wasm-bindgen, exposing:
#
#   export class Denoiser {
#     constructor(sampleRate: number): Denoiser;
#     processFrame(samples: Float32Array): Float32Array;
#     dispose(): void;
#   }
#
# Compiled artifact: packages/rnnoise-wasm/pkg/rnnoise_wasm.js + .wasm
# Loaded by the AudioWorklet in apps/web/src/workers/audio.worklet.ts
