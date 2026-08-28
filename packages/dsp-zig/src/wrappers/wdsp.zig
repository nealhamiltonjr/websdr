// WDSP wrapper — Zig bindings for the WDSP library by Warren Pratt WD4GPL.
//
// WDSP provides classic amateur radio DSP: AGC, filters, noise blankers,
// AM/FM/SSB/CW demodulators, etc. Upstream OpenWebRX+ uses the C version
// directly; here we wrap the most commonly-called entry points in Zig
// for compile-time safety and to expose a cleaner API to the rest of our
// stack (Python via ctypes, Rust via libloading).
//
// Slice-1 status: declarations only. The actual C library is in
// packages/dsp-c and not yet vendored. Once vendored, uncomment the
// @cImport and implement the wrapper bodies.

const std = @import("std");

// Once packages/dsp-c/WDSP is vendored:
// const wdsp_c = @cImport({
//     @cInclude("WDSP/wdsp.h");
// });

pub const Channel = struct {
    handle: *anyopaque,
    sample_rate: u32,
    channels: u8,

    /// Initialize a new RX channel. Wraps `RXA` in WDSP.
    /// TODO: implement once WDSP is vendored.
    pub fn init(sample_rate: u32, channels: u8) !Channel {
        _ = sample_rate;
        _ = channels;
        return error.NotImplemented;
    }

    /// Tear down. Wraps `RXADelete` in WDSP.
    pub fn deinit(self: *Channel) void {
        _ = self;
    }
};

/// Process a block of complex samples through the channel's RX chain.
/// Returns the same buffer (in-place) for caller convenience.
/// TODO: implement once WDSP is vendored.
pub fn process(channel: *Channel, samples: []f32) void {
    _ = channel;
    _ = samples;
}

test "Channel.init returns NotImplemented in stub" {
    try std.testing.expectError(error.NotImplemented, Channel.init(48_000, 1));
}
