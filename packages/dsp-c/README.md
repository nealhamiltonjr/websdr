# dsp-c — vendored C DSP sources
#
# Slice-1 status: empty. The following upstream C libraries will be vendored
# here in slice-1.5 (or pinned as git submodules):
#
#   - WDSP       — Warren Pratt WD4GPL's amateur radio DSP (filters, AGC, demods)
#   - csdr       — the original OpenWebRX DSP command-line tools
#   - pycsdr     — Python bindings for csdr (we use this in apps/server)
#   - rnnoise    — Xiph's RNNoise speech denoiser (server-side; client uses Wasm)
#
# Submodule strategy:
#
#   git submodule add https://github.com/wpratt/wdsp.git     WDSP
#   git submodule add https://github.com/jketterl/csdr.git   csdr
#   git submodule add https://github.com/jketterl/pycsdr.git pycsdr
#   git submodule add https://github.com/xiph/rnnoise.git    rnnoise
#
# Once vendored, the Zig wrappers in packages/dsp-zig/src/wrappers/*.zig
# will @cInclude them and the build system will link the resulting
# artifacts into both the Zig static library and the Python extension.
