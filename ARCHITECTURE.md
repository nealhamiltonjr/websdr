# Architecture

OpenWebRX+ is a four-pillar platform. This document is the high-level overview; for specific decisions, see `ADR/`.

## Pillar 1 — DSP+AI Cascade

Four operating modes, switchable per ReceiverSession:

| Mode | Stage 1 (Classic WDSP) | Stage 2a (DeepFilterNet, server) | Stage 2b (RNNoise, client WASM) | Stage 3 (Demucs/Open-Unmix, offline) |
|---|---|---|---|---|
| Raw | ✗ | ✗ | ✗ | ✗ |
| Classic-only | ✓ | ✗ | ✗ | ✗ |
| AI-only | ✗ | ✓ | ✓ | optional |
| Full-cascade | ✓ | ✓ | ✓ | ✓ |

AI is only active on USB / LSB / AM / FM / FreeDV. CW / digital modes pass through unchanged.

## Pillar 2 — Visualization

- **WebGL2** for waterfall + spectrum rendering (GLSL shaders, scrolling texture for waterfall, line geometry for spectrum)
- **OffscreenCanvas + Web Worker** to keep rendering off the main thread
- **Dockview** for in-window docking (tabs, splits, groups)
- **True pop-outs** via `window.open()` + a separate SolidJS route `/popout/:vizType`
- **SharedWorker** fans out FFT/metadata frames across the main window and all popouts; one WebSocket per ReceiverSession lives in the worker
- **Per-window AudioContext** so different receivers can route to different speakers / gain / devices

The core abstraction is `ReceiverSession`. Every visualization is a SolidJS component with `{ receiverId, config, linkedVizIds? }` props — they don't know about panels or windows.

## Pillar 3 — Federation

- Remote SDR sources: KiwiSDR / Spyserver / SDRangel / SoxyRemote / WebSDR
- Public directory browser (catalog of public SDRs with metadata: location, bands, queue length)
- QoS layer: prioritize the user's local SDR > paid federation > free public
- Ethics/ToS layer: respect per-source ToS (e.g., KiwiSDR's "no commercial use" rule), display attribution

## Pillar 4 — Plugin Engine

Two plugin families, two runtimes:

- **Python legacy plugins** — subprocess contract: IQ in via Unix socket / stdin, JSON events out via stdout. Preserves upstream OpenWebRX+ plugin compatibility.
- **Wasm modern plugins** — in-browser, sandboxed, faster, no external dependencies.

### Decoder plugin contracts

**RF-band decoders** (tap raw IQ before demod):
- ADS-B dump1090 / readsb (1090 MHz)
- dump978 UAT (978 MHz, US)
- AIS (marine, 161.975 / 162.025 MHz)
- DAB / DAB+
- ATC / ACARS (131.550 MHz)
- FreeDV (already native)

**Audio-band decoders** (tap post-demod audio):
- FT8 / JT65 / JT9 / WSPR / Q65 via WSJT-X virtual audio cable
- FLDIGI modes via FLDIGI virtual audio cable
- RTTY / CW / PSK31/63 / Olivia / SSTV / FAX / packet AX.25 as in-browser Wasm plugins

### Map visualizations (VRS-killer)

- `AircraftMapViz` — live aircraft on MapLibre world map, fed by ADS-B/UAT decoder
- `AisMapViz` — live vessels on MapLibre, fed by AIS decoder
- `DigiMessageListViz` — decoded message stream (FT8 spots, packet frames, ACARS messages, ATIS text)

VRS (Virtual Radar Server) becomes redundant — we render the map natively in-browser.

## Core abstractions

### `ReceiverSession` (frontend)

The source of truth for one logical receiver. Holds subscription to FFT/metadata/audio streams. Visualizations look it up by UUID via a singleton registry.

```ts
ReceiverSession {
  id: UUID
  fftStream: Subject<FFTFrame>
  audioStream: AudioWorkletNode
  metadataStream: Subject<ReceiverMetadata>
}
```

### `VisualizationRegistry` (frontend)

Every viz type is registered as a SolidJS component with the contract:

```ts
type VizProps = {
  receiverId: UUID
  config: VizConfig
  linkedVizIds?: UUID[]
}
```

### `WorkspaceManager` (frontend)

Owns the Dockview root, the set of open popout windows, and the spawn/teardown command flow. Persists layout to localStorage.

### `SharedWorker` (frontend)

Holds the WebSocket per ReceiverSession. Fans out FFT frames to all subscribers (main window + popouts). Single source of truth for connection state.

### Backend services (`apps/server`)

- `Source` — abstract SDR source (RTL-SDR / KiwiSDR / Spyserver / ...). Spawns IQ stream.
- `ReceiverSession` (server-side) — owns one source, a DSP chain, a frequency, a mode. Multiplexes FFT/audio/metadata to WebSocket subscribers.
- `PluginRunner` — manages RF-band decoder subprocesses (IQ in via Unix socket, JSON out via stdout).
- `RestApi` + `WebSocketServer` — clients communicate via REST for control, WS for streams.

## What's deliberately NOT here

- No Qt / native UI — pure web
- No monolithic backend — orchestration only; DSP is in C / Zig / Rust
- No video / image AI — only audio (RNNoise, DeepFilterNet, Demucs)
- No persistence of IQ by default — opt-in recording per receiver

## Implementation phases

- **Slices 1–4.7** (done) — FFT/audio wire formats, WebGL2 renderers, multi-receiver + VFO spawning, real drivers + fixtures, pycsdr DSP chains, federation clients (rtl_tcp/KiwiSDR/OpenWebRX remote), ADS-B decoder plugin, Dockview workspace, per-receiver tuning/gain/DSP controls. See `docs/STATUS.md`.
- **Slice 5+** — decoder breadth (AIS, dump978, dump1090 subprocess) + map visualizations, SpyServer client, AI cascade wiring (DeepFilterNet/RNNoise), propagation intelligence, QSL logging, mobile design, deployment story.

## References

- `ADR/001-multi-receiver-workspace.md` — Base + Spawn architecture, viz/decoder plugin contracts
- `ADR/002-dsp-ai-cascade.md` — (pending) Four-mode DSP pipeline
- `ADR/003-decoder-plugins.md` — (pending) RF-band and audio-band decoder plugin architecture
- `docs/slice-01-plan.md` — Current vertical slice acceptance criteria
