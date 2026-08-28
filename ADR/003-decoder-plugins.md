# ADR-003: Decoder Plugin Architecture (RF-band + Audio-band)

**Status:** Accepted — v1 in-process contract IMPLEMENTED (slice-3.8, bundled ADS-B plugin live); subprocess contract IMPLEMENTED (slice-4.9: PluginRunner + dump1090, verified against a protocol-faithful fake binary)
**Date:** 2026-08-26 (updated 2026-08-27)
**Related:** ADR-001 § Decoder Plugin Architecture, Pillar 4 (Plugin Engine)

## Context

The locked-in decoder list (DAB, AIS, ADS-B dump1090, dump978, ATC/ACARS, FreeDV, FT8/WSJT, RTTY, CW, PSK, SSTV, FAX, packet, FLDIGI bridge) mixes two fundamentally different tap points in the DSP pipeline. They need separate plugin contracts.

## Decision

Two plugin families, each with a different tap point:

### v1 as shipped (slice-3.8): in-process decoders

The first shipped contract is a *native* one — pure-Python decoders running
inside the server process, tapping the session's IQ via its IqHub (ADR-005):

- ``DecoderPlugin`` (``openwebrx_plus/plugins/base.py``): ``feed_iq(iq) -> events``
  / ``feed_audio(pcm, rate) -> events``, ``stop()``, ``status()``; class-level
  ``DecoderManifest`` (name, tap point, required sample rate, event kinds).
- ``DecoderRegistry`` (``plugins/registry.py``): class-level registration via
  decorator; REST reflects it via ``GET /api/decoders``.
- Attach/detach: ``POST/GET/DELETE /api/receivers/{id}/decoders`` — the session
  spawns a feed task consuming ``hub.stream()`` (same chunks, no copies) and
  broadcasts events to every WS subscriber as
  ``{"type": "decoder", "decoder": …, "event": {"kind": …}}`` text frames.
- Fail-fast attach validation: unknown name → 400, already attached → 409,
  display-stream (federated) receivers → 400, rate mismatch → 400.
- **Bundled plugin #1: ADS-B / Mode S** (``plugins/adsb.py`` + ``plugins/modes.py``)
  — PPM demodulator at exactly 2 MSPS, CRC-24 verification (both data and
  DF11 address parity), callsign + altitude extraction, per-receiver aircraft
  table, ``frame`` + ``aircraft`` events. Verified against the baked
  ``adsb_1090.cf32`` fixture (14/14 frames, 3 aircraft + 2 distant fragments).
  Frontend: ``AircraftListViz`` (attach/detach + live table) registered as
  ``aircraft-list``.
- Position (CPR), velocity, and Gilliam-altitude decode for live 1090 MHz
  traffic are deliberately deferred to the dump1090 subprocess plugin — the
  pure-Python decoder's altitude reads the fixture's simplified binary
  encoding (documented in ``plugins/modes.py``).

### RF-band decoders — tap raw IQ / wideband, before demod

**Subprocess contract** (upstream OpenWebRX+ plugin convention — IMPLEMENTED in slice-4.9 as `plugins/subprocess.py` + `plugins/dump1090.py`):
- Plugin spawns as a subprocess (asyncio exec; `OWRX_RX_ID` / `OWRX_SAMPLE_RATE` / `OWRX_CENTER_FREQ` / `OWRX_IQ_FORMAT` env vars carry receiver context)
- Receives IQ via stdin pipe — format `cf32` / `cs16` / `cu8` (converted from the hub's cf32 at the bridge; the dump1090 plugin requests `cs16`, dump1090's `--iformat SC16` layout)
- Emits decoded JSON events via stdout (NDJSON, one object per line; `{"kind": "ready", …}` is the optional handshake and is consumed by the runner, never broadcast)
- Lifecycle managed by `PluginRunner` (server-side): bounded crash-restart with backoff → terminal `failed` state + a synthetic `decoder_state` event for the frontend; teardown is stdin-EOF → bounded wait → SIGKILL; a wedged child gets metered drops (`dropped_chunks`) instead of unbounded buffers
- The `SubprocessDecoderPlugin` adapter plugs binaries into the same session/REST/WS surface as in-process plugins (`on_attach` spawns at attach time so failures map to precise HTTP codes: 502 binary missing, 400 not-ready)
- **Bundled plugin: dump1090** (`plugins/dump1090.py`) — 2 MSPS / cs16 / 1090 MHz; binary + flags configurable via `OPENWEBRX_PLUS_DUMP1090_BIN` / `OPENWEBRX_PLUS_DUMP1090_ARGS`. Stock dump1090 builds speak SBS1-on-TCP instead of stdout NDJSON — point the env var at a convention-speaking build or a thin wrapper. The test fake (`tests/fakes/fake_dump1090.py`) pins the contract exactly (real Mode S demod, synthetic positions marked `position_source: "synthetic"`, deliberate crash/garbage/stall modes).
- Future subprocess plugins (dump978, AIS, DAB, ACARS) reuse the runner unchanged — only argv + manifest differ.

**Bundled plugins (v1):**
| Decoder | Source | Default freq | Notes |
|---|---|---|---|
| ADS-B dump1090 / readsb | C, fork-friendly | 1090 MHz | SHIPPED slice-4.9 (subprocess runner + fake binary); NDJSON on stdout per the contract above |
| dump978 (UAT) | C | 978 MHz | US-only; pairs with `AircraftMapViz` — drops onto the same runner |
| AIS | C (`rtl-ais` / `aisdeco`) | 161.975 / 162.025 MHz | Pairs with `AisMapViz` |
| DAB / DAB+ | C (`dabtools`) | 174–240 MHz | Emits AAC audio stream |
| ATC / ACARS | C (`acarsdec`) | 131.550 MHz | Emits text messages |
| FreeDV | C (in upstream DSP chain) | varies | Not a subprocess; native in DSP chain |

**v2+ contract:** native Rust port (readsb-rr, etc.) for performance and to drop the subprocess overhead. Optional Wasm re-impl for in-browser decoding.

### Audio-band decoders — tap post-demod audio

**Two integration styles:**

1. **External DIGI app via virtual audio cable** (FT8/WSJT, FLDIGI)
   - v1: auto-create a `pipewire` null sink named `openwebrx-rxN-out` per ReceiverSession
   - Route the receiver's demodulated audio to that sink
   - Expose a "Launch WSJT-X" / "Launch FLDIGI" button that pre-configures the audio device
   - Removes the manual loopback setup pain that hams hate

2. **Built-in Wasm plugins** (RTTY, CW, PSK31/63, Olivia, SSTV, FAX, packet AX.25)
   - In-browser, sandboxed Wasm
   - Tap the post-demod audio stream directly (no external app, no loopback)
   - Output: decoded text/messages rendered via `DigiMessageListViz`

### Map-based visualizations (VRS-killer)

- `AircraftMapViz` consumes JSON events from the attached ADS-B/UAT decoder
- `AisMapViz` consumes the AIS decoder stream
- Native MapLibre GL JS renderer (no VRS server needed)
- Optional v2: subscribe to public MLFeed (ADS-B Exchange, OpenSky) for sky-fill cross-reference

## Plugin SDK interface (v1, Python side)

```python
class PluginManifest:
    name: str
    version: str
    tap_point: Literal["rf_band", "audio_band"]
    sample_rate_hint: int  # Hz
    iq_format: Literal["cf32", "cs16"]  # only for rf_band
    event_schema: dict    # JSON schema for emitted events

class PluginRunner:
    async def spawn(manifest: PluginManifest, receiver_id: str) -> None: ...
    async def feed_iq(samples: np.ndarray) -> None: ...           # rf_band
    async def feed_audio(samples: np.ndarray) -> None: ...        # audio_band
    async def events() -> AsyncIterator[dict]: ...
    async def stop() -> None: ...
```

## Open questions

- [x] Plugin discovery mechanism — v1 shipped as class-level `DecoderRegistry`
      with decorator registration (in-process only). Filesystem scan / PyPI
      entry points remain future work for the subprocess family.
- [ ] Sandboxing for Wasm plugins (WASI? Capability-based?)
- [ ] Hot-reload of plugins during development
- [ ] Plugin permission model (does an AIS plugin get network access? filesystem? probably no)
- [x] Unified event schema — v1: per-plugin `event.kind` discriminator inside
      one envelope (`{"type": "decoder", "decoder": …, "event": {"kind": …}}`);
      shared-types TS mirrors live in `packages/shared-types/src/decoder.ts`
      (incl. the `ADSB_DECODERS` family + subprocess row position fields).
- [x] Subprocess PluginRunner (slice-4.9) — stdin IQ (cf32/cs16/cu8), stdout
      NDJSON, ready handshake, bounded restarts, metered backpressure, and the
      dump1090 plugin on top. dump978/AIS/DAB/ACARS land as argv+manifest only.
- [ ] Control channel via stdin (`set_frequency` etc.) — specced, not needed
      by any bundled binary yet.
