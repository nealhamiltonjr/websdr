// OpenWebRX+ DSP wrappers in Zig.
//
// Build modes:
//   zig build                  — default: static library, host target
//   zig build -Dtarget=native  — explicit native
//   zig build -Dtarget=x86_64-linux-gnu
//
// Build artifacts:
//   zig-out/lib/libowrx_dsp.a   — static library, link into Python/Rust
//   zig-out/bin/owrx_dsp_test   — tests
//
// Slice-1 status: stub. The C wrappers for WDSP/RNNoise are not yet wired
// (those land in slice-1.5 once we vendor the C sources into packages/dsp-c).
// For now, this file builds a trivial static library so the rest of the
// monorepo's CI can verify the Zig workspace compiles.

const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    // --- Static library: libowrx_dsp.a ----------------------------------
    const lib_mod = b.createModule(.{
        .root_source_file = b.path("src/main.zig"),
        .target = target,
        .optimize = optimize,
    });
    const lib = b.addStaticLibrary(.{
        .name = "owrx_dsp",
        .root_module = lib_mod,
    });
    b.installArtifact(lib);

    // --- Test step ------------------------------------------------------
    const tests = b.addTest(.{
        .root_module = lib_mod,
    });
    // Zig 0.14: addRunArtifact returns *Build.Step.Run, not *Build.Step.
    // To pass it to dependOn (which wants *Build.Step), access .step.
    const run_tests = b.addRunArtifact(tests);
    const test_step = b.step("test", "Run unit tests");
    test_step.dependOn(&run_tests.step);

    // --- FFI exports: ensure C ABI symbols exist (stub) -----------------
    // Real WDSP/RNNoise wrappers are in src/wrappers/*.zig
}
