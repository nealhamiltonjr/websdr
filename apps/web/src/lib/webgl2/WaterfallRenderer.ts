/** WaterfallRenderer — WebGL2 scrolling waterfall.
 *
 *  Architecture (slice-1):
 *    - History buffer: a Uint8Array on the CPU side (historyRows × binCount × 4 bytes RGBA).
 *      Each frame: shift the buffer down by 1 row, write the new FFT bins to row 0
 *      (mapped through dBFS → 0..255 via the colormap LUT).
 *    - History texture: upload the CPU buffer to a GL texture each frame via
 *      gl.texSubImage2D.
 *    - Color LUT: precomputed 256×1 RGBA8 texture for each colormap.
 *    - Render: fullscreen-triangle vertex shader, fragment shader samples the
 *      history texture (R channel = bin value 0..255) and uses it as the U
 *      coordinate to look up the colormap LUT.
 *
 *  Y axis: row 0 (newest) at the TOP. In WebGL convention, the bottom-left is
 *  (0,0), so we flip Y in the fragment shader (sample with 1 - v_uv.y).
 *
 *  DPR-aware: canvas backing store = clientWidth * devicePixelRatio.
 */

import type { FFTFrame } from '../../sessions/ReceiverSession';
import { OverlayRenderer, EMPTY_OVERLAYS, type VizOverlays } from './overlay';
import type { FreqAxis } from '../../visualizations/freqAxis';

export interface WaterfallRendererConfig {
  minDb: number;
  maxDb: number;
  colorMap: 'viridis' | 'turbo' | 'grayscale' | 'jet';
  historyRows: number;
}

const VERT_SRC = `#version 300 es
layout(location = 0) in vec2 a_pos;
out vec2 v_uv;
void main() {
  // a_pos in [-1, 1]. Map to UV [0, 1].
  v_uv = a_pos * 0.5 + 0.5;
  gl_Position = vec4(a_pos, 0.0, 1.0);
}
`;

const FRAG_SRC = `#version 300 es
precision highp float;
in vec2 v_uv;
out vec4 outColor;
uniform sampler2D u_history;
uniform sampler2D u_lut;
void main() {
  // Newest row is at row 0 of the history texture (top of the buffer).
  // WebGL textures have origin at bottom-left, so v_uv.y=0 is the BOTTOM of
  // the displayed quad. We want the newest at the TOP of the display, so flip:
  vec2 uv = vec2(v_uv.x, 1.0 - v_uv.y);
  float v = texture(u_history, uv).r;
  // LUT is 256x1; sample at (v, 0.5).
  outColor = texture(u_lut, vec2(v, 0.5));
}
`;

// --- Colormap generators (256 entries each, returned as Uint8Array RGBA) ---

function lutViridis(): Uint8Array {
  // Approximation of the Viridis colormap. Pre-computed for performance.
  // Source: matplotlib's _viridis_data, sampled at 256 points.
  return buildLut((t) => {
    // 5-stop interpolation. Stops from matplotlib viridis:
    // #440154 #414487 #2a788e #22a884 #7ad151 #fde725
    const stops = [
      [0.267, 0.005, 0.329],
      [0.282, 0.140, 0.457],
      [0.254, 0.265, 0.530],
      [0.207, 0.372, 0.483],
      [0.163, 0.471, 0.558],
      [0.128, 0.566, 0.551],
      [0.135, 0.659, 0.418],
      [0.267, 0.749, 0.441],
      [0.478, 0.821, 0.318],
      [0.741, 0.873, 0.298],
      [0.993, 0.912, 0.130],
    ];
    const idx = t * (stops.length - 1);
    const i = Math.floor(idx);
    const f = idx - i;
    const a = stops[i];
    const b = stops[Math.min(i + 1, stops.length - 1)];
    return [lerp(a[0], b[0], f), lerp(a[1], b[1], f), lerp(a[2], b[2], f)];
  });
}

function lutTurbo(): Uint8Array {
  // Approximation of the Turbo colormap (Google's improved jet).
  return buildLut((t) => {
    // Polynomial approximation of Turbo. Source: Google's turbo_colormap.cpp.
    const r = Math.max(0, Math.min(1, 1.5 - Math.abs(4.0 * t - 3.0)));
    const g = Math.max(0, Math.min(1, 1.5 - Math.abs(4.0 * t - 2.0)));
    const b = Math.max(0, Math.min(1, 1.5 - Math.abs(4.0 * t - 1.0)));
    // Boost saturation a touch — Turbo is brighter than this naive triangle.
    return [Math.min(1, r * 1.2), Math.min(1, g * 1.2), Math.min(1, b * 1.2)];
  });
}

function lutGrayscale(): Uint8Array {
  return buildLut((t) => [t, t, t]);
}

function lutJet(): Uint8Array {
  return buildLut((t) => {
    let r = 0,
      g = 0,
      b = 0;
    if (t < 0.25) {
      b = 0.5 + t * 2.0 * 0.5;
    } else if (t < 0.5) {
      b = 1.0;
      g = (t - 0.25) * 4.0;
    } else if (t < 0.75) {
      g = 1.0;
      r = (t - 0.5) * 4.0;
    } else {
      r = 1.0;
      b = 1.0 - (t - 0.75) * 4.0;
    }
    return [r, g, b];
  });
}

function buildLut(fn: (t: number) => [number, number, number]): Uint8Array {
  const out = new Uint8Array(256 * 4);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    const [r, g, b] = fn(t);
    out[i * 4 + 0] = Math.max(0, Math.min(255, Math.round(r * 255)));
    out[i * 4 + 1] = Math.max(0, Math.min(255, Math.round(g * 255)));
    out[i * 4 + 2] = Math.max(0, Math.min(255, Math.round(b * 255)));
    out[i * 4 + 3] = 255;
  }
  return out;
}

function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}

function colormapLut(name: WaterfallRendererConfig['colorMap']): Uint8Array {
  switch (name) {
    case 'viridis':
      return lutViridis();
    case 'turbo':
      return lutTurbo();
    case 'grayscale':
      return lutGrayscale();
    case 'jet':
      return lutJet();
    default:
      return lutTurbo();
  }
}

export class WaterfallRenderer {
  private gl: WebGL2RenderingContext;
  private cfg: WaterfallRendererConfig;
  private rafId: number | null = null;
  private pendingFrame: FFTFrame | null = null;
  private resizeObserver: ResizeObserver | null = null;
  /** Crosshair/tuned-marker overlays (slice-4.6). */
  private overlay!: OverlayRenderer;
  private overlays: VizOverlays = EMPTY_OVERLAYS;
  private axis: FreqAxis | null = null;
  /** The canvas we render into (HTMLCanvasElement on main thread,
   *  OffscreenCanvas when running in a worker via slice-11). */
  private canvasEl: HTMLCanvasElement | OffscreenCanvas;

  // GL resources
  private program: WebGLProgram | null = null;
  private vao: WebGLVertexArrayObject | null = null;
  private historyTexture: WebGLTexture | null = null;
  private lutTexture: WebGLTexture | null = null;
  private uHistoryLoc: WebGLUniformLocation | null = null;
  private uLutLoc: WebGLUniformLocation | null = null;

  // CPU-side history buffer (RGBA8, historyRows × binCount).
  private historyBuffer: Uint8Array;
  private binCount: number = 0;

  /** The backing canvas element (HTMLCanvasElement on main thread,
   *  OffscreenCanvas when running in a worker via slice-11). */
  get canvas(): HTMLCanvasElement | OffscreenCanvas {
    return this.canvasEl;
  }

  constructor(canvas: HTMLCanvasElement | OffscreenCanvas, cfg: WaterfallRendererConfig) {
    const gl = (canvas as HTMLCanvasElement).getContext('webgl2', {
      antialias: false,
      premultipliedAlpha: false,
      preserveDrawingBuffer: false,
    }) as WebGL2RenderingContext | null;
    if (!gl) throw new Error('WebGL2 not supported in this browser');
    this.gl = gl;
    this.canvasEl = canvas;
    this.cfg = cfg;

    this.historyBuffer = new Uint8Array(cfg.historyRows * 4); // initial 0×0; resized on first frame

    this.initGL();
    this.resize();
    // ResizeObserver is only available on the main thread; in a worker
    // (OffscreenCanvas), the host must post explicit 'resize' messages.
    if (typeof ResizeObserver !== 'undefined') {
      this.resizeObserver = new ResizeObserver(() => this.resize());
      this.resizeObserver.observe(canvas as HTMLCanvasElement);
    }

    console.info('[WaterfallRenderer] initialized', { cfg, worker: typeof window === 'undefined' });
  }

  /** Current frequency axis (null before the first frame). */
  getAxis(): FreqAxis | null {
    return this.axis;
  }

  /** Update crosshair/tuned overlays; redraws on the next RAF (no upload). */
  setOverlays(ov: VizOverlays): void {
    this.overlays = ov;
    this.schedule();
  }

  private initGL(): void {
    const gl = this.gl;

    // Compile shaders.
    const vs = this.compile(gl.VERTEX_SHADER, VERT_SRC);
    const fs = this.compile(gl.FRAGMENT_SHADER, FRAG_SRC);
    const prog = gl.createProgram();
    if (!prog) throw new Error('failed to create program');
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      const log = gl.getProgramInfoLog(prog);
      gl.deleteProgram(prog);
      throw new Error('program link failed: ' + log);
    }
    this.program = prog;

    // Fullscreen triangle (covers viewport with a single triangle; clip-space).
    const verts = new Float32Array([-1, -1, 3, -1, -1, 3]);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, verts, gl.STATIC_DRAW);

    const vao = gl.createVertexArray();
    gl.bindVertexArray(vao);
    // Attribute location 0 is hardcoded via layout(location=0) in the shader.
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    this.vao = vao;

    // LUT texture: 256×1 RGBA8.
    this.lutTexture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.lutTexture);
    const lut = colormapLut(this.cfg.colorMap);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, lut);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

    // Uniforms.
    this.uHistoryLoc = gl.getUniformLocation(this.program, 'u_history');
    this.uLutLoc = gl.getUniformLocation(this.program, 'u_lut');

    // Overlay pass (own program + VAO; slice-4.6 crosshair/tuned markers).
    this.overlay = new OverlayRenderer(gl);
  }

  private compile(type: number, src: string): WebGLShader {
    const gl = this.gl;
    const sh = gl.createShader(type);
    if (!sh) throw new Error('failed to create shader');
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
      const log = gl.getShaderInfoLog(sh);
      gl.deleteShader(sh);
      throw new Error('shader compile failed: ' + log);
    }
    return sh;
  }

  resize(): void {
    // The canvas is either an HTMLCanvasElement (main thread) or an
    // OffscreenCanvas (worker). On main thread we read clientWidth +
    // devicePixelRatio; in the worker we use the existing width/height
    // (the host posts explicit resize messages when its OffscreenCanvas
    // backing needs to change).
    const dpr = (typeof window !== 'undefined' ? window.devicePixelRatio : 1) || 1;
    const clientW = (this.canvasEl as HTMLCanvasElement).clientWidth ?? this.canvasEl.width;
    const clientH = (this.canvasEl as HTMLCanvasElement).clientHeight ?? this.canvasEl.height;
    const w = Math.max(1, Math.floor(clientW * dpr));
    const h = Math.max(1, Math.floor(clientH * dpr));
    if (this.canvasEl.width !== w || this.canvasEl.height !== h) {
      this.canvasEl.width = w;
      this.canvasEl.height = h;
    }
    this.gl.viewport(0, 0, w, h);
  }

  pushFrame(frame: FFTFrame): void {
    this.pendingFrame = frame;
    this.schedule();
  }

  /** Schedule one RAF pass (deduped — fast mousemoves coalesce). */
  private schedule(): void {
    if (this.rafId == null) {
      this.rafId = requestAnimationFrame(() => this.render());
    }
  }

  private render(): void {
    this.rafId = null;
    const frame = this.pendingFrame;
    this.pendingFrame = null;

    const gl = this.gl;

    if (frame) {
      this.axis = { centerHz: frame.centerFreq, sampleRateHz: frame.sampleRate };

      // Resize history buffer if binCount changed (e.g., on first frame).
      if (this.binCount !== frame.bins.length) {
        this.binCount = frame.bins.length;
        this.historyBuffer = new Uint8Array(this.cfg.historyRows * this.binCount * 4);
        // (Re)create history texture.
        if (this.historyTexture) gl.deleteTexture(this.historyTexture);
        this.historyTexture = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, this.historyTexture);
        gl.texImage2D(
          gl.TEXTURE_2D,
          0,
          gl.RGBA,
          this.binCount,
          this.cfg.historyRows,
          0,
          gl.RGBA,
          gl.UNSIGNED_BYTE,
          this.historyBuffer,
        );
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      }

      // Shift the CPU history buffer down by 1 row.
      // (memcpy-style: copy from row 0..N-2 to row 1..N-1, then write row 0)
      const rowBytes = this.binCount * 4;
      this.historyBuffer.copyWithin(rowBytes, 0, (this.cfg.historyRows - 1) * rowBytes);

      // Write the new frame to row 0, mapping dBFS → 0..255.
      const bins = frame.bins;
      const range = this.cfg.maxDb - this.cfg.minDb;
      const row0 = this.historyBuffer.subarray(0, rowBytes);
      for (let i = 0; i < this.binCount; i++) {
        const db = bins[i];
        const t = (db - this.cfg.minDb) / range;
        const v = Math.max(0, Math.min(255, Math.round(t * 255)));
        row0[i * 4 + 0] = v;
        row0[i * 4 + 1] = v;
        row0[i * 4 + 2] = v;
        row0[i * 4 + 3] = 255;
      }

      // Upload the entire history buffer to the texture.
      gl.bindTexture(gl.TEXTURE_2D, this.historyTexture);
      gl.texSubImage2D(
        gl.TEXTURE_2D,
        0,
        0,
        0,
        this.binCount,
        this.cfg.historyRows,
        gl.RGBA,
        gl.UNSIGNED_BYTE,
        this.historyBuffer,
      );
    } else if (!this.historyTexture) {
      // Overlay-only redraw before any frame arrived — nothing to draw under.
      return;
    }

    // Draw the fullscreen triangle.
    gl.clearColor(0.027, 0.035, 0.051, 1.0);
    gl.clear(gl.COLOR_BUFFER_BIT);

    gl.useProgram(this.program);
    gl.bindVertexArray(this.vao);

    // Bind history texture to unit 0.
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.historyTexture);
    gl.uniform1i(this.uHistoryLoc, 0);

    // Bind LUT texture to unit 1.
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, this.lutTexture);
    gl.uniform1i(this.uLutLoc, 1);

    gl.drawArrays(gl.TRIANGLES, 0, 3);

    // Crosshair / tuned-marker overlays (slice-4.6).
    if (this.axis) this.overlay.draw(this.overlays, this.axis);
  }

  dispose(): void {
    if (this.rafId != null) {
      cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }
    this.resizeObserver?.disconnect();
    this.resizeObserver = null;
    this.overlay.dispose();
    const gl = this.gl;
    if (this.historyTexture) gl.deleteTexture(this.historyTexture);
    if (this.lutTexture) gl.deleteTexture(this.lutTexture);
    if (this.vao) gl.deleteVertexArray(this.vao);
    if (this.program) gl.deleteProgram(this.program);
  }
}
