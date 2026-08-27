# ADR-002: DSP + AI Cascade (Four-Mode Pipeline)

**Status:** Accepted — control surface + raw/classic LIVE (slice-4.7); ai/cascade gated on the DeepFilterNet module
**Date:** 2026-08-26 (updated 2026-08-27)
**Related:** Pillar 1 (DSP+AI Cascade), ADR-001

## Context

Modern SDR receivers can benefit from AI-based noise reduction (DeepFilterNet, RNNoise) on top of classical DSP (WDSP). However, AI is not always desired:
- CW operators often prefer raw signal (no AI artifacts)
- Some signals (digital modes) must not be processed by AI (would corrupt mode-specific waveforms)
- CPU/power budgets may require disabling AI

## Decision (outline)

Four operating modes per ReceiverSession:

| Mode | Stage 1 (WDSP) | Stage 2a (DeepFilterNet, server) | Stage 2b (RNNoise, client WASM) | Stage 3 (Demucs, offline) |
|---|---|---|---|---|
| `raw` | ✗ | ✗ | ✗ | ✗ |
| `classic` | ✓ | ✗ | ✗ | ✗ |
| `ai` | ✗ | ✓ | ✓ | optional |
| `cascade` | ✓ | ✓ | ✓ | ✓ |

AI active only on: `USB`, `LSB`, `AM`, `FM`, `FreeDV`. Bypassed for: `CW`, `RTTY`, `PSK`, `FT8`, packet, SSTV, FAX, and all digital modes.

### v1 as shipped (slice-4.7): the control surface + the two live modes

The four-mode CONTROL surface is live end-to-end — `setDSPMode` wire command,
`ReceiverSession.set_dsp_mode()`, metadata + REST echo, TuningBar dropdown
(ai/cascade rendered disabled with the reason). The v1 mapping of the two
available modes onto the pycsdr AudioChain (see `dsp/audio.py`):

- **`raw`** — demodulator output goes straight to resample + int16 convert:
  no DcBlock after AM/NFM, no WfmDeemphasis after WFM, no Limit. Structurally
  identical to classic for SSB/CW (RealPart IS the demodulator).
- **`classic`** — conditioned audio (the default since slice-3): DcBlock,
  50 µs WFM de-emphasis, ±1.0 soft limiter. When the WDSP stage lands
  (`packages/dsp-zig/src/wrappers/wdsp.zig`), classic gains its AGC /
  noise-blanker stages and the mapping stays honest.
- **`ai` / `cascade`** — rejected with a clear error until
  `ReceiverSession.AI_DSP_MODES_AVAILABLE` flips true (the DeepFilterNet
  Rust module in `packages/ai-rust` is not built yet).

Mode switches REBUILD the AudioChain under a chain lock (`_rebuild_audio_chain`)
— the same machinery that fixed the setMode-never-rebuilds bug in slice-4.7.

## Open questions (to resolve before locking in)

- [x] Mode-switching API — v1: WS control command `setDSPMode` (slice-4.7),
      echoed in metadata (`dspMode`) + REST (`GET /api/receivers` → `dsp_mode`).
- [ ] Sample-rate contract between stages (48 kHz? 16 kHz? Per-stage resample?)
- Server→client transport for Stage 2a output (compressed OPUS? raw PCM via WS?)
- Latency budget per stage (target: <50 ms end-to-end for SSB QSK)
- DeepFilterNet model size / GPU requirement on Intel NUC-class hardware
- Stage 3 (Demucs) trigger policy — manual? auto on recording? scheduled?

## TODO before locking in

- [x] Control surface shipped (slice-4.7): raw + classic live, ai/cascade gated.
- [ ] Bench DeepFilterNet on Intel NUC (i5-1240P or equivalent)
- [ ] Decide on RNNoise model variant (original 2018 weights vs retrained)
- [ ] Write latency budget document
- [ ] Specify transport format between server and client
