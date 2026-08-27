# Slice 1 — "Hello Waterfall"

**Status:** In progress (scaffold complete, real implementation next)
**Target:** One SDR source → one WebSocket → one SolidJS panel renders a basic spectrum + waterfall, with the architecture contracts from ADR-001 baked in.

## Acceptance criteria

A user should be able to:

1. Run `make install && make dev` from the repo root
2. Open http://localhost:5173 in a modern browser (Chrome 110+, Firefox 110+, Safari 16.4+ for SharedWorker + WebGL2)
3. See a Dockview workspace with four panels:
   - Waterfall (large, top-left)
   - Spectrum / linear plane (below waterfall)
   - S-Meter (right)
   - Frequency Counter (right, below S-Meter)
4. All four panels are tagged to the default `receiverId = "rx-default"`
5. The spectrum and waterfall show a moving signal (the backend's synthetic 1 kHz tone)
6. The S-Meter shows a non-zero reading
7. The Frequency Counter shows `14.2050 MHz` (the default center freq) and mode `USB`
8. Right-clicking any panel → "Pop Out" → opens `/popout/waterfall?receiverId=rx-default` in a new window
9. Closing the popout does NOT kill the receiver (other panels keep working)
10. Browser DevTools console shows SharedWorker → WebSocket → backend traffic at ~10 fps

## What's NOT in scope for slice-1

- Real RTL-SDR integration (we use a synthetic sine wave) — slice-1.5
- Windowed FFT (we use raw numpy FFT) — slice-1.5
- Audio output (no demodulated audio) — slice-1.5
- Multiple receivers (single hardcoded receiver) — slice-2
- Drag-and-drop panels between docked/popped-out — slice-2
- VFO sub-receivers — slice-2
- Digital mode decoders — slice-3

## Implementation steps (in order)

### Step 1 — Verify the scaffold builds

```bash
cd /home/z/my-project/openwebrx-plus

# Frontend
cd apps/web && pnpm install && pnpm run typecheck

# Backend
cd ../server && uv sync && uv run pytest

# Zig
cd ../../packages/dsp-zig && zig build && zig build test

# Rust
cd ../ai-rust && cargo check && cargo test
```

If any of these fail, fix before proceeding.

### Step 2 — Implement the binary FFT wire format

Currently the backend sends raw `Float32Array` bytes via `send_bytes`, and the frontend has a TODO to parse it. Implement the 32-byte header + Float32Array body format defined in `packages/shared-types/src/fft.ts`:

- Backend: `openwebrx_plus/sessions/receiver_session.py` → `_compute_fft` should pack a `struct` header + the bins
- Frontend: `apps/web/src/sessions/ReceiverSession.ts` → add a `parseFFTFrame(ArrayBuffer): FFTFrame` function
- Frontend: `apps/web/src/routes/main.tsx` → call `parseFFTFrame` in the `fft` branch of the SharedWorker message handler
- Frontend: `apps/web/src/sessions/ReceiverSession.ts` → add an `ingestFFT(frame)` call after parsing

### Step 3 — Implement WaterfallRenderer (WebGL2)

File: `apps/web/src/lib/webgl2/WaterfallRenderer.ts`

- Allocate a `historyTexture` (RGBA8, width=binCount, height=historyRows) on init
- On each `pushFrame`: write bins (after dBFS→color LUT mapping) into row 0, scroll the rest down by one row using `gl.copyTexSubImage2D`
- On each render: bind history texture to a fullscreen quad, draw with a simple vertex + fragment shader
- Color LUT: precompute a 256-entry RGBA8 texture from the chosen colormap (viridis / turbo / grayscale / jet)

### Step 4 — Implement SpectrumRenderer (WebGL2)

File: `apps/web/src/lib/webgl2/SpectrumRenderer.ts`

- On each `update`: upload bins as a line strip vertex buffer
- Optional: peak-hold trace as a second line strip
- Render with a thin GLSL line shader (cyan-450 color)

### Step 5 — Test end-to-end manually

```bash
# Terminal 1
make dev-server

# Terminal 2
make dev-web
```

Open http://localhost:5173. Verify all 10 acceptance criteria above.

### Step 6 — Add popout spawning UX

File: `apps/web/src/components/WorkspaceManager.tsx`

- Add Dockview panel context-menu action "Pop Out"
- On click: `window.open('/popout/' + panel.component + '?receiverId=' + panel.params.receiverId, '', 'width=1024,height=768')`
- Verify the popout route renders the viz and shares the FFT stream via SharedWorker

## Slice-1.5 follow-ups (next 2 weeks)

- Real RTL-SDR integration (replace synthetic tone with `pyrtlsdr`)
- Windowed FFT (Hann window) and proper dBFS scaling
- Demodulated audio output (USB mode) via AudioWorklet
- Frequency tuning UI (slider + numeric input → `setFrequency` control message)
- Mode selector (dropdown → `setMode` control message)

## Slice-2 follow-ups (next month)

- Multi-receiver spawning (a "+" button in the top bar)
- Drag-and-drop panels between docked and popped-out states
- VFO sub-receivers (one wideband SDR → multiple VFOs)
- Persistence of popout window geometry to localStorage
