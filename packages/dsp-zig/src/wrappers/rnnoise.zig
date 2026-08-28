// RNNoise wrapper — Zig bindings for RNNoise (Xiph's RNNoise library
// by Jean-Marc Valin, used for speech noise reduction).
//
// RNNoise is used in two places in the OpenWebRX+ stack:
//   - Server-side (Stage 2b candidate) — direct link via this wrapper
//   - Client-side (Stage 2b actual) — compiled to WASM (packages/rnnoise-wasm)
//
// Slice-1 status: declarations only. The C source lives in
// packages/dsp-c/rnnoise and is not yet vendored.

const std = @import("std");

// Once packages/dsp-c/rnnoise is vendored:
// const rnnoise_c = @cImport({
//     @cInclude("rnnoise.h");
// });

pub const DenoiseState = struct {
    handle: *anyopaque,
    frame_size: usize,  // typically 480

    pub fn init(sample_rate: u32) !DenoiseState {
        _ = sample_rate;
        return error.NotImplemented;
    }

    pub fn deinit(self: *DenoiseState) void {
        _ = self;
    }

    /// Process one frame (frame_size samples of f32 interleaved audio).
    /// Returns the denoised samples in the same buffer.
    pub fn process_frame(self: *DenoiseState, samples: []f32) void {
        _ = self;
        _ = samples;
    }
};

test "DenoiseState.init returns NotImplemented in stub" {
    try std.testing.expectError(error.NotImplemented, DenoiseState.init(48_000));
}
