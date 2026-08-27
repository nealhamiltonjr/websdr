# ADR-001: Multi-Receiver Dockable Workspace Architecture

**Status:** Accepted
**Date:** 2026-08-26
**Related:** ADR-002 (DSP+AI Cascade, pending), Pillar 2 (Visualization), Pillar 3 (Federation)

---

## Context

A world-class amateur radio SDR application must support:
- Multiple simultaneous receivers (local SDR + remote federated sources)
- Multiple visualizations per receiver (waterfall, spectrogram, linear spectrum, S-meter, frequency counter, band heatmap, etc.)
- Multi-monitor usage with true pop-out windows
- Clean separation between "what data flows" and "where it renders"

Existing SDR UIs (OpenWebRX, GQRX, SDR#, WebSDR, SDR Console V3, SDRconnect) each cover a subset. None combine all of: web-based, multi-receiver, dockable AND undockable, WebGL2 rendering, federation-aware.

## Decision

Adopt a **Base + Spawn** workspace architecture built on four layers.

### Layer 1 — ReceiverSession (core abstraction)

Each ReceiverSession is the source of truth for one logical receiver:

```
ReceiverSession {
  id: UUID                    // stable across windows
  source: SdrSource           // local SDR / KiwiSDR / Spyserver / SDRangel / WebSDR / ...
  vfoId?: UUID               // optional: parent wideband source if this is a VFO
  frequency: Hz
  mode: USB | LSB | AM | FM | FreeDV | ...
  dspChain: Pipeline          // WDSP (Stage1) → DeepFilterNet (2a) → RNNoise (2b) → Demucs (3)
  fftStream: Subject<FFTFrame>
  audioStream: AudioWorkletNode
  metadataStream: Subject<Metadata>
}
```

Visualizations know only `receiverId`. They do NOT know about panels, windows, or layouts. This decoupling is the linchpin.

### Layer 2 — VisualizationRegistry

Every visualization type is a SolidJS component with the contract:

```ts
type VizProps = {
  receiverId: UUID
  config: VizConfig
  linkedVizIds?: UUID[]   // for crosshair syncing within the same receiver
}
```

Registered types (v1):
- `WaterfallViz`      — time × freq × intensity (scrolling)
- `SpectrumViz`       — linear plane, freq × amplitude (real-time scope)
- `SpectrogramViz`    — short-window STFT view (for digital modes)
- `SMeterViz`         — signal strength gauge
- `FrequencyCounterViz` — numeric frequency display
- `BandHeatmapViz`    — band activity over time
- `AudioSpectrumViz`  — FFT of demodulated audio (not RF)
- `PhaseConstellationViz` — for digital modes
- `HistogramViz`      — SNR / RSSI statistics
- `AircraftMapViz`    — live aircraft positions on MapLibre world map, fed by ADS-B/UAT decoder
- `AisMapViz`         — live vessel positions on MapLibre, fed by AIS decoder
- `DigiMessageListViz` — decoded digital mode messages (FT8 spots, packet frames, RTTY text, CW, ACARS messages, ATIS text)

### Layer 3 — Workspace Manager

Manages the in-window Dockview layout AND the set of open popout windows. Routes spawn/teardown commands. Persists layout to localStorage.

### Layer 4 — Popout Window Runtime

Route: `/popout/:vizType?receiverId=:rid&config=...`

A popout loads the same SolidJS app in single-visualization mode. Connects to the existing ReceiverSession via SharedWorker. Scales to the host monitor via WebGL2 + DPR-aware canvas.

## Decoder Plugin Architecture

Two distinct decoder families, each with a different tap point in the DSP pipeline:

### RF-band decoders (process IQ / wideband directly)
Tap point: **before** demodulation, on the raw IQ stream or a wideband FFT slice.

| Decoder | Source | Integration (v1) | Integration (v2+) |
|---|---|---|---|
| ADS-B dump1090 / readsb | C, fork-friendly | Subprocess + JSON-over-HTTP | Native Rust port (readsb-rr) |
| dump978 (UAT, 978 MHz, US) | C | Subprocess + JSON-over-HTTP | Native Rust port |
| AIS (marine, 161.975 / 162.025 MHz) | C (`rtl-ais`) or `aisdeco` | Subprocess | Wasm re-impl |
| DAB / DAB+ | C (`dabtools`) | Subprocess + AAC stream | Rust `dab-plus-rs` |
| ATC / ACARS (131.550 MHz) | C (`acarsdec`) | Subprocess | Wasm re-impl |
| FreeDV | C (already in upstream) | Native (in DSP chain) | Native (unchanged) |

v1 contract: each RF-band decoder is a **subprocess plugin** that receives IQ via a Unix socket / stdin pipe and emits decoded JSON events over stdout. This matches the upstream OpenWebRX+ plugin convention and gets us all of dump1090/dump978/AIS/DAB/ACARS for free in v1.

### Audio-band decoders (process post-demod audio)
Tap point: **after** demodulation, on the audio stream. Two integration styles:

| Decoder | Style | Notes |
|---|---|---|
| FT8 / JT65 / JT9 / WSPR / Q65 | Virtual audio cable → external `WSJT-X` / `JTDX` | Subprocess + audio loopback (`pulseaudio` / `pipewire` null sink). User runs WSJT-X normally. |
| RTTY / CW / PSK31/63 / Olivia / SSTV / FAX / packet AX.25 | Built-in Wasm plugins | In-browser decoders via Wasm; no external app needed. |
| FLDIGI modes | Virtual audio cable → external `FLDIGI` | Same loopback pattern as WSJT-X. |

v1 contract for external DIGI apps: **auto-create a `pipewire` null sink** named `openwebrx-rxN-out`, route the receiver's audio there, expose a "Launch WSJT-X / FLDIGI" button that pre-configures the audio device. Removes the manual loopback setup pain that hams hate.

### Map-based visualizations
- `AircraftMapViz` consumes the JSON event stream from the attached ADS-B/UAT decoder.
- `AisMapViz` consumes the AIS decoder stream.
- Native MapLibre GL JS renderer (no VRS server needed — VRS becomes redundant since we render the map in-browser).
- Optional: subscribe to a public MLFeed (e.g., ADS-B Exchange) for cross-referencing / sky-fill.

## Cross-Window Data Sharing

- **FFT / metadata streams**: SharedWorker holds the WebSocket per receiver. Main window + each popout subscribe via `postMessage`. Worker fans out one FFT frame to N consumers.
- **Audio**: each window gets its own `AudioContext` + `AudioWorklet`. Different receivers → different speakers / gain / routing. This also aligns with browser audio policy (per-window AudioContext is what browsers want).

## Feature Lock-In (Scope for v1)

| # | Feature | Status |
|---|---|---|
| 1 | Multi-receiver concurrent (local + federated) | Locked |
| 2 | VFO sub-receivers within a wideband SDR source | Locked |
| 3 | Waterfall, Spectrogram, Linear Spectrum as separate movable views | Locked |
| 4 | Dockable within main window (Dockview) | Locked |
| 5 | True pop-out to separate windows | Locked |
| 6 | Per-viz `receiverId` tagging | Locked |
| 7 | Multi-monitor DPR-aware scaling | Locked |
| 8 | Base + spawn UX model | Locked |
| 9 | Attachments (freq counter, S-meter, mode indicator) as first-class vizes | Locked |
| 10 | Independent audio per window | Locked |
| 11 | Linked visualizations (crosshair sync within a receiver) | Locked |
| 12 | Built-in **RF-band** decoders as plugins (process IQ/wideband directly): ADS-B dump1090 (1090 MHz) / readsb, dump978 UAT (978 MHz, US), AIS (marine), DAB, ATC/ACARS, FreeDV | Locked |
| 13 | Built-in **audio-band** decoders as plugins (process post-demod audio): FT8/WSJT via WSJT-X bridge, JT65, JT9, WSPR, packet (AX.25), RTTY, CW, PSK31/63, Olivia, SSTV, FAX | Locked |
| 14 | DIGI-mode integration via virtual audio cable (FLDIGI / WSJT-X / JTDX) | Locked |
| 15 | Aircraft tracking web-map visualization (VRS-equivalent, native MapLibre + live ADS-B/UAT feed) | Locked |

## Out of Scope for v1

- Drag-and-drop panels between docked and popped-out states (may come in v2; for v1, popout is a context-menu action)
- Persisting popout window geometry across browser sessions
- Cross-receiver linked visualizations

## Consequences

**Positive:**
- World-class UX that no current SDR app matches
- Clean separation enables incremental slice delivery
- Aligned with Pillar 2 (Visualization) and Pillar 3 (Federation)

**Negative:**
- SharedWorker adds build complexity
- Multiple WebGL contexts across popouts → VRAM cost; enforce a max-popouts limit (recommend 6)
- Per-window AudioContext means user must click each popout once to satisfy browser autoplay policy (mitigation: clear "Click to enable audio" affordance in popout chrome)

## Implementation Order

**Slice 1 ("Hello waterfall") MUST ship with:**
- `ReceiverSession` (even if hardcoded to one local SDR)
- `WaterfallViz` + `SpectrumViz` components taking `{ receiverId }` as prop
- Dockview workspace hosting them
- Stub popout route `/popout/:vizType` (URL contract exists; full UX in slice 2)

**Slice 2 adds:** SharedWorker fan-out, `window.open` UX, 2nd-receiver spawn, VFO sub-receivers.

**Slice 3 adds:** Built-in digital mode decoders as plugins, federation directory browser.
