# ADR-007: IQ-to-audio post-DSP enhancement (rejected for now)

**Status:** Rejected (2026-08-27) — open for reconsideration if the DSP
chain gains a real notch / NB block (slice-5.3+)

## Context

The user asked: "Can we add a switchable IQ-to-audio processing pipeline
that further enhances the digital-cleaned signal?" — i.e., after the
classic DSP (bandpass + AGC + DC block + de-emphasis) produces 8 kHz mono
int16 PCM, can we apply additional audio-band processing (LMS adaptive
filter, spectral subtraction, additional RNNoise pass, multiband
compression, etc.) to "pull out" more intelligibility?

This is a reasonable question to ask. This ADR documents the honest
evaluation.

## Decision drivers

1. **The DSP is already doing this.** The classic-mode chain already
   includes the standard audio conditioning stages — DC block, WFM
   de-emphasis (50 µs tau), soft limiter at ±1.0. Slice-5.2 added
   switchable AGC, manual makeup gain, squelch, manual bandpass
   reconfiguration, NFM de-emphasis. These ARE the "audio enhancement"
   stages — they're already there, and the user can toggle them via the
   DSPControls panel.

2. **The AI cascade (ADR-002) is the "second pass" the user is describing.**
   The cascade architecture is explicitly Stage 1 (classic DSP) →
   Stage 2a (DeepFilterNet on the server, removes steady-state noise
   like hiss / hum / AC) → Stage 2b (RNNoise on the client via WASM,
   removes transient noise like clicks / pops / voice-activity gating) →
   Stage 3 (offline Demucs / Open-Unmix source separation). When the
   AI modules ship (slice-5.4+), the "post-DSP enhancement" the user
   describes will be the AI cascade — not a separate audio-band
   pipeline.

3. **Adding a parallel "IQ-to-audio enhancement" stage would duplicate
   work the cascade already plans to do.** The cascade is the
   architecturally-correct place for post-DSP enhancement. Adding a
   bespoke audio-band pipeline would:
   - Duplicate effort (two implementations of noise reduction)
   - Confuse the operating-mode matrix (Raw / Classic-only / AI-only /
     Full-cascade becomes Raw / Classic-only / Classic+AudioEnh /
     AudioEnh-only / Classic+AI / Classic+AI+AudioEnh / ... — exponential
     in feature flags)
   - Make the metadata echo much noisier
   - Make the WS protocol harder to evolve

4. **Real-time budget.** The cascade is already tight on the
   <50ms-per-receiver budget. A second audio-band pass on top of the
   cascade would either:
   - Run in the same process (adding latency to every frame), OR
   - Run in a separate thread / process (adding synchronization
     overhead and a second AudioWorklet on the client side)

5. **CPU.** For a multi-receiver setup (target: 4 simultaneous
   receivers), an extra audio-band LMS or spectral-subtraction pass on
   every 8 kHz frame is measurable CPU. The cascade's AI modules
   already take that budget; adding another stage without removing
   one is a net regression.

## Considered options

### Option A — Accept the field in DSPParams, no-op until cascade ships

Already done for `notch_enabled` and `noise_blanker_enabled` in
slice-5.2. The fields exist so the UI can show the controls with
"experimental" badges; the chain honors them as no-ops until a real
implementation lands.

**Outcome:** Good enough for notch + NB. NOT appropriate for a
parallel "audio enhancement" pipeline because the user would expect it
to do something — and there's nothing for it to do that the cascade
doesn't already plan.

### Option B — Implement a custom Python notch filter via numpy

Implement a notch filter as a Python-side DSP stage between the pycsdr
chain and the WS audio pump. Use scipy.signal.iirnotch (offline-only
per ADR-004) to design the filter, then apply it per-frame in the
audio pump.

**Outcome:** Adds latency (Python GIL + per-frame numpy work), violates
the "scipy is offline-only" rule for the live IQ path (ADR-004), and
creates a second audio path that bypasses pycsdr's autovectorized SIMD
C++. Rejected.

### Option C — Implement a custom Python notch filter without scipy

Same as Option B but use a hand-rolled biquad IIR notch (no scipy
dependency). The biquad is small enough to implement correctly in pure
Python (it's ~10 lines of code).

**Outcome:** Adds latency (Python GIL on the audio pump's hot path),
bypasses pycsdr's SIMD C++, and is a maintenance burden (we'd own the
filter). Better than Option B but still not great.

### Option D — Contribute a Notch block upstream to pycsdr / libcsdr

The right long-term answer. libcsdr's C++ source is on GitHub at
<https://github.com/jketterl/csdr>. A notch filter is a standard
biquad implementation that fits the libcsdr architecture (a `Module`
subclass with `setReader/setWriter/process`).

**Outcome:** Long-term correct. Not something we can do in this slice
because it requires: (1) understanding libcsdr's C++ patterns, (2)
writing the block, (3) opening a PR, (4) waiting for review, (5)
updating pycsdr to wrap it, (6) updating our pycsdr pin. Estimated
2-3 days of work, blocking on upstream review.

### Option E — Defer the entire question until the AI cascade ships

Build the cascade (slice-5.4+) first. Evaluate whether the cascade's
Stage 2b (RNNoise WASM client-side) makes a "second audio enhancement
pass" redundant. If yes, close this ADR. If no, revisit with a
concrete use case.

**Outcome:** Chosen. The cascade is the architecturally-correct place
for post-DSP enhancement. Building a parallel pipeline now would
duplicate work that's already planned. Re-open this ADR if a specific
use case emerges that the cascade can't address.

## Decision

**Rejected for now.** The fields `notch_enabled`, `notch_freq_hz`,
`notch_q`, `noise_blanker_enabled`, `noise_blanker_threshold` remain
in DSPParams (accepted, no-op) so the UI can show the controls. The
"audio enhancement pipeline" concept is subsumed by the ADR-002 AI
cascade architecture — when the cascade ships (slice-5.4+), its
Stage 2b (RNNoise WASM) and Stage 3 (offline Demucs / Open-Unmix)
will be the post-DSP enhancement layers.

If a real notch filter is needed before the cascade ships (e.g., for
a specific ham band with a known interference tone), implement via
Option D (upstream pycsdr contribution) — NOT via a Python-side
parallel pipeline.

## Consequences

- The DSPControls panel will continue to show "experimental" badges
  on the notch + noise blanker sections.
- The audio path remains single-pass: pycsdr chain → resampler →
  int16 PCM → WS → client. No parallel audio pipeline.
- The cascade (when it ships) will insert its stages between the
  pycsdr chain and the WS audio pump, on the server side for Stage 2a
  (DeepFilterNet) and on the client side for Stage 2b (RNNoise WASM).
- This keeps the operating-mode matrix at 4 modes (Raw / Classic-only
  / AI-only / Full-cascade) instead of going exponential.
- The metadata echo stays small (one dspParams dict + one dspMode
  string per receiver).

## Open questions

- Should notch + NB fields be removed from DSPParams entirely (since
  they're no-op)? **No** — leaving them in lets the UI show the
  intended shape of the future feature, and the WS protocol stays
  forward-compatible (a future server that implements them won't need
  a protocol change).
- Should the UI badges say "experimental" or "queued"? **"experimental"**
  is honest — it means "we accept the field, the chain honors it as
  a no-op, real implementation is queued." This matches the project's
  honest-failures pattern.

## References

- ADR-002 — DSP+AI cascade (the architecturally-correct post-DSP enhancement)
- ADR-004 — pycsdr as the primary DSP library (scipy is offline-only)
- slice-5.2 — DSPParams dataclass + AudioChain wiring + DSPControls panel
