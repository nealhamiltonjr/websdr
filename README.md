# OpenWebRX+ — Modernized

A world-class, browser-native amateur radio SDR platform. Multi-receiver, dockable+undockable, WebGL2-rendered, with a cascaded DSP+AI pipeline and a federation layer for remote SDR sources.

> **Status:** Pre-alpha, actively slicing. Core receive loop is live end-to-end (slices 1–4.7): 10 source backends, pycsdr DSP, Dockview/WebGL2 multi-receiver workspace, per-receiver tuning/gain/DSP controls, ADS-B decoding, OpenWebRX/KiwiSDR federation clients. See `docs/STATUS.md` for the current snapshot and roadmap.

## What this is

A ground-up modernization of upstream [OpenWebRX+](https://github.com/0xAF/openwebrxplus) with a four-pillar architecture:

1. **DSP+AI Cascade** — WDSP (Stage1) → server-side DeepFilterNet (Stage2a) → client-side WASM RNNoise (Stage2b) → offline Demucs/Open-Unmix (Stage3). Four operating modes: Raw / Classic-only / AI-only / Full-cascade.
2. **Visualization** — WebGL2 + OffscreenCanvas + Comlink for the waterfall, spectrum, S-meter, band heatmap. Dockview for in-window docking; true pop-out windows via `window.open` + SharedWorker fan-out.
3. **Federation** — remote SDR sources (KiwiSDR / Spyserver / SDRangel / SoxyRemote / WebSDR), public directory browser, QoS layer, ethics/ToS layer.
4. **Plugin Engine** — Python legacy plugins (subprocess IQ-in, JSON-out contract) + Wasm modern plugins. RF-band decoders (dump1090, dump978, AIS, DAB, ACARS) ship as first-class.

## Tech stack

| Layer | Tech | Notes |
|---|---|---|
| Frontend | TypeScript + SolidJS + Vite + Tailwind 4 + Park UI + Dockview + WebGL2 | `apps/web` |
| Backend orchestration | Python (uv-managed) | `apps/server` — preserves upstream 27 source backends + pycsdr chain + WebSocket protocol |
| New DSP wrappers | Zig | `packages/dsp-zig` — wraps WDSP/RNNoise, compile-time verified |
| Existing DSP | C (unchanged) | `packages/dsp-c` — vendored or submodule |
| AI inference | Rust | `packages/ai-rust` — DeepFilterNet bindings |
| RNNoise WASM | Rust + `wasm-bindgen` | `packages/rnnoise-wasm` — client-side noise reduction |
| Shared schemas | TypeScript (zod) + Python (pydantic) | `packages/shared-types` |

## Repository layout

```
openwebrx-plus/
├── apps/
│   ├── web/                          # SolidJS frontend
│   └── server/                       # Python backend orchestration
├── packages/
│   ├── dsp-zig/                      # Zig DSP wrappers
│   ├── dsp-c/                        # Vendored C DSP (csdr, pycsdr, WDSP)
│   ├── ai-rust/                      # Rust AI inference (DeepFilterNet)
│   ├── rnnoise-wasm/                # RNNoise compiled to WASM
│   └── shared-types/                 # Cross-language schemas
├── ADR/                              # Architecture Decision Records
├── docs/                             # Plans and documentation
├── .github/workflows/ci.yml          # CI skeleton
├── Makefile                          # Top-level targets
├── package.json                      # pnpm workspace root
└── pnpm-workspace.yaml
```

## Getting started

Requires: Node 22+, pnpm 9+, Python 3.12+, `uv`, Rust stable, Zig 0.13+, and an RTL-SDR (or other supported SDR) for slice 1+.

```bash
# 1. Install all workspace dependencies
make install

# 2. Run frontend dev server (http://localhost:5173)
make dev-web

# 3. Run backend (http://localhost:8073)
make dev-server

# 4. Or run both via
make dev
```

See `docs/slice-01-plan.md` for the current vertical-slice acceptance criteria.

## Architecture

See `ARCHITECTURE.md` for the high-level overview and `ADR/` for decision records. The cornerstone is ADR-001: every visualization takes `{ receiverId }` as a prop, the Workspace Manager owns layout, and a SharedWorker fans out FFT/metadata streams across popout windows.

## License

This project is licensed under the GNU Affero General Public License v3.0 or
later (`AGPL-3.0-or-later`), inheriting the license of upstream
[OpenWebRX+](https://github.com/0xAF/openwebrxplus). A `LICENSE` file containing
the full AGPL-3.0 text should be added at the repo root; until then, the full
license text is available at <https://www.gnu.org/licenses/agpl-3.0.txt>.

External contributions are welcome — see `docs/AI-HANDOFF.md` for orientation
and `docs/STATUS.md` for the current development snapshot.
