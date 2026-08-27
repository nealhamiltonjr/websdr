# OpenWebRX+ DSP (Zig)

Thin Zig wrappers around the upstream C DSP libraries (WDSP, RNNoise) used by OpenWebRX+. Wrapping in Zig gives us compile-time guarantees and a cleaner FFI surface for the rest of the stack (Python via ctypes, Rust via libloading).

## Status (slice-1)

- [x] `build.zig` builds a static library (`libowrx_dsp.a`)
- [x] Smoke-test C ABI exports (`owrx_dsp_version`, `owrx_dsp_add`)
- [ ] Real WDSP wrapper (waits for `packages/dsp-c` vendoring in slice-1.5)
- [ ] Real RNNoise wrapper (same)

## Build

```bash
cd packages/dsp-zig
zig build         # produces zig-out/lib/libowrx_dsp.a
zig build test    # runs unit tests
```

## FFI exports

Once wired, the library exposes a small C ABI:

```c
const char *owrx_dsp_version(void);
int32_t     owrx_dsp_add(int32_t a, int32_t b);

// WDSP
owrx_wdsp_channel_t  *owrx_wdsp_channel_init(uint32_t sample_rate, uint8_t channels);
void                 owrx_wdsp_channel_deinit(owrx_wdsp_channel_t *ch);
void                 owrx_wdsp_process(owrx_wdsp_channel_t *ch, float *samples, size_t n);

// RNNoise
owrx_rnnoise_t      *owrx_rnnoise_init(uint32_t sample_rate);
void                 owrx_rnnoise_deinit(owrx_rnnoise_t *state);
void                 owrx_rnnoise_process_frame(owrx_rnnoise_t *state, float *samples);
```

## Layout

```
src/
├── main.zig              # Root module + C ABI exports
└── wrappers/
    ├── wdsp.zig          # WDSP bindings
    └── rnnoise.zig       # RNNoise bindings
```
