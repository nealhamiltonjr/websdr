# ADR-005: VFO Sub-Receivers — WidebandSource Contract & IqHub Fan-Out

**Status:** Accepted — implemented & tested (slice-3)
**Date:** 2026-08-26
**Related:** ADR-001 (multi-receiver workspace, Feature #2), ADR-004 (pycsdr + source plugins)

## Context

ADR-001 Feature #2 locked **VFO sub-receivers**: one wideband SDR capture
serving N independent narrowband receivers — the SDRconnect-style model,
and the single biggest unlock for multi-receiver UX (one RTL-SDR V4 at
2.4 MSPS can host a CW skimmer on 7.025, an SSB QSO on 7.160, and a FT8
monitor on 14.074 simultaneously).

The questions this ADR settles:

1. Who owns the hardware when N receivers want one dongle?
2. How does a VFO relate to a `ReceiverSession` (ADR-001's unit of work)?
3. What are the admission constraints (span, decimation, CPU budget)?
4. What happens on parent teardown / bad configurations?

## Decision

### 1. The IqHub — one source spawn, N subscribers

```
parent ReceiverSession (owns the source: hardware / file / simulated)
  └─ IqHub (background pump task; keyed by parent receiver_id)
      ├─ parent session's own FFT/audio chains   (full-span view)
      ├─ VfoTapSource #1 → pycsdr DDC → 12 kSPS  (child session #1)
      └─ VfoTapSource #2 → pycsdr DDC → 12 kSPS  (child session #2)
```

- The parent source is spawned **exactly once**. Every subscriber (the
  parent session itself + each VFO tap) gets its own bounded
  `asyncio.Queue` fed with the **same chunk objects** (no copies).
- **Drop-oldest backpressure**: real-time streams never block on a slow
  consumer; the oldest queued chunk is dropped and the loss counted
  (`hub.dropped_chunks`). Stale IQ is worthless; fresh IQ is mandatory.
- The pump yields to the event loop every chunk (`await asyncio.sleep(0)`)
  so unpaced sources can't starve their own subscribers.
- **Lifecycle = parent session lifecycle**: the hub is created lazily on
  `session.start()` and destroyed on `session.stop()`, which cancels the
  pump, closes the source once, and sentinel-ends every subscriber stream.
  Child VFO sessions observe graceful stream end (not an error).

### 2. A VFO tap is a pure-software DDC — and a drop-in Source

`VfoTapSource(parent_receiver_id=...)` implements the standard Source
protocol (`spawn(center, rate, gain) → AsyncIterator[complex64]`), so a
child ReceiverSession is byte-for-byte a normal session: own FFT chain,
own audio chain, own WS subscribers, own receiver_id — the frontend needs
**zero special cases** for VFO children beyond spawning them.

The DDC is two pycsdr blocks (both SIMD C++, ≈11 Msps measured):

```
in_buf → Shift(rate=−offset/parent_rate) → FirDecimate(decimation) → out_buf
```

- **Shift sign convention (pycsdr gotcha #7, see ADR-004):** `Shift(rate)`
  multiplies by `exp(+2πi·rate·n)` — the spectrum moves **UP** by
  `rate×fs`. A slice at `+offset` reaches DC with `rate = −offset/fs`.
  This was verified empirically after a spectacular debugging session
  (wrong sign moved a −30 kHz carrier to −60 kHz; the anti-alias filter
  killed it; the *alias* of −60 kHz at the 12 kSPS output rate landed at
  exactly 0 Hz, which frequency-only assertions mistook for success).
  **Spectral tests must assert amplitude, not just peak frequency.**
- `FirDecimate(decimation, transition=0.05, cutoff=0.47)`: measured
  passband ≈ ±2.8 kHz at −1 dB into a 12 kSPS slice, −66 dB at ±11 kHz,
  −92 dB at ±15 kHz.

### 3. Admission constraints (enforced at spawn)

| Constraint | Rule | Rationale |
|---|---|---|
| Span | `|offset| + vfo_rate/2 ≤ parent_rate/2` | a slice outside the capture is fiction |
| Decimation | `parent_rate % vfo_rate == 0` | pycsdr FirDecimate is integer-only; fractional resampling is a future `FractionalDecimator` tap |
| Subscriber budget | `len(subscribers) ≤ 8` (default) | CPU, see below |
| Parent liveness | parent session must be started | validated at REST-create → 400, and again at spawn → RuntimeError |

**CPU budget rule-of-thumb:** each tap costs ~one parent-rate
Shift+FirDecimate pass (≈11 Msps on this box). A 2.4 MSPS parent with 7
taps ≈ 17 MSPS of DSP — comfortable on a NUC. A 10 MSPS Airspy parent
supports ~2–3 taps; beyond that, cascade (tap a tap — every session owns
a hub, so VFO-of-VFO works) or move to the hardware-VFO fast path below.

### 4. REST surface

```
POST /api/receivers
{
  "source_type": "vfo",
  "source_kwargs": {"parent_receiver_id": "rx-default"},
  "center_freq": 14074000,          # inside the parent's span
  "sample_rate": 12000,             # divides the parent rate
  "mode": "USB"
}
```

- Missing/unknown/not-started parent → **400** with an actionable message
  (never a silently-dead 201).
- VFO sessions are destroyed like any other (`DELETE /api/receivers/{id}`);
  destroying the parent cascade-ends the children's streams gracefully.

### 5. Hardware VFOs (future fast path — not built here)

The RSPduo's dual tuners are the natural hardware anchor: a future
`wideband group` can map VFOs onto physical tuners (master/slave via
`mir_sdr_rspduo_*` calls) when the requested set doesn't fit one
capture. The REST surface above stays unchanged — only admission and
scheduling change. Tracked for hardware bring-up day.

### 6. Timing model (applies to every session, landed with this ADR)

Sources stream at wall-clock rate (hardware naturally; file replay and
the simulator are `RealtimePacer`-paced). `fft_fps` throttles only the
FFT **broadcast** — surplus FFT frames are drained and dropped (a 10 fps
waterfall doesn't need 2300 fps of FFT) — while **audio frames are never
dropped**. Before this, the session slept per chunk, which silently
rate-limited consumption and would have dropped real-time IQ under load.

## Implementation status (2026-08-26)

- ✅ `sources/wideband.py`: `IqHub`, `VfoChain` (pycsdr DDC),
  `VfoTapSource`, hub registry (`get_or_create_hub` / `register_hub` /
  `destroy_hub`).
- ✅ `ReceiverSession` consumes its own source **through** its hub;
  adopts `fixed_sample_rate`/`fixed_center_freq` from file-style sources.
- ✅ REST validation for `vfo` parents (400s), vfo spawn/list/destroy.
- ✅ Default dev session = baked 20 m fixture replay (`file` source,
  real-time paced, looping) — the frontend sees CW/SSB/FT8/QRN with zero
  hardware.
- ✅ 103/103 server tests, including: hub fan-out identity, drop-oldest
  under backpressure, sentinel teardown cascade, DDC centering (CW at
  −30 kHz → DC, amplitude-preserved), two concurrent taps, span/decimation
  rejection, REST lifecycle.

## Consequences

- **+/−** Every session now owns a hub → one extra task + queue per
  receiver; negligible, and it makes ANY source fan-out-able (recording
  multiple decoders off one capture is the same mechanism).
- **+/−** Integer decimation only (v1); document the supported rates in
  the UI (240 k → 12 k, 2.4 M → 12 k/48 k/…).
- **−** Parent re-tune while VFOs are attached is not yet supported
  (offsets are computed at spawn). A `vfo.retune` message + hub event is
  the follow-up when the tuning UI lands.
- **+** Zero frontend special-casing: a VFO child is just a receiver.
