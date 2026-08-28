// OpenWebRX+ DSP wrapper root.
//
// Public surface:
//   - Re-exports WDSP wrapper functions (src/wrappers/wdsp.zig)
//   - Re-exports RNNoise wrapper functions (src/wrappers/rnnoise.zig)
//   - Provides a minimal C ABI for FFI from Python (via ctypes) and Rust
//     (via libloading)
//
// Slice-1 status: stub. The wrappers are declared but the underlying C
// sources (WDSP, RNNoise) live in packages/dsp-c and are not yet vendored.
// Building this library succeeds but the symbols are no-ops.

const std = @import("std");
const wdsp = @import("wrappers/wdsp.zig");
const rnnoise = @import("wrappers/rnnoise.zig");

pub const WDSP = wdsp;
pub const RNNoise = rnnoise;

/// Library version string — exported via C ABI.
export fn owrx_dsp_version() [*c]const u8 {
    return "openwebrx-plus dsp 0.1.0 (slice-1 stub)";
}

/// Smoke-test: returns the sum of two i32 — proves the C ABI works.
export fn owrx_dsp_add(a: i32, b: i32) i32 {
    return a + b;
}

test "owrx_dsp_add returns correct sum" {
    try std.testing.expectEqual(@as(i32, 7), owrx_dsp_add(3, 4));
}

test "owrx_dsp_version returns non-empty string" {
    const v = std.mem.span(owrx_dsp_version());
    try std.testing.expect(v.len > 0);
}
