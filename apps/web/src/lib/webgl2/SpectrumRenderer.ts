/** SpectrumRenderer — WebGL2 linear plane / real-time scope.
 *
 *  Architecture (slice-1):
 *    - Each frame: upload bins as a line strip vertex buffer
 *      (X = bin index normalized to [-1, 1], Y = dBFS normalized to [-1, 1])
 *    - Optional peak-hold trace as a second line strip (CPU-side decay)
 *    - Thin GLSL line shader (cyan-450 color)
 *    - Grid drawn in the background (CSS or simple GL lines — slice-1.5)
 *
 *  DPR-aware canvas.
 */

import type { FFTFrame } from '../../sessions/ReceiverSession';
import { OverlayRenderer, EMPTY_OVERLAYS, type VizOverlays } from './overlay';
import type { FreqAxis } from '../../visualizations/freqAxis';

export interface SpectrumRendererConfig {
  minDb: number;
  maxDb: number;
  colorMap: 'viridis' | 'turbo' | 'grayscale' | 'jet'; // unused for slice-1
  peakHold: boolean;
  peakDecay: number;
}

const VERT_SRC = `#version 300 es
layout(location = 0) in vec2 a_pos;
void main() {
  gl_Position = vec4(a_pos, 0.0, 1.0);
}
`;

const FRAG_SRC = `#version 300 es
precision highp float;
uniform vec4 u_color;
out vec4 outColor;
void main() {
  outColor = u_color;
}
`;

// Cyan-450 from our theme (alpha 1.0).
const CYAN_450: [number, number, number, number] = [0.133, 0.827, 0.933, 1.0];
// Amber-450 from our theme (alpha 0.6 for the peak-hold trace).
const AMBER_450: [number, number, number, number] = [0.961, 0.620, 0.043, 0.6];

export class SpectrumRenderer {
  private gl: WebGL2RenderingContext;
  private cfg: SpectrumRendererConfig;
  private resizeObserver: ResizeObserver;
  private peakBuffer: Float32Array | null = null;

  // GL resources
  private program: WebGLProgram | null = null;
  private vao: WebGLVertexArrayObject | null = null;
  private traceBuffer: WebGLBuffer | null = null;
  private peakBufferGl: WebGLBuffer | null = null;
  private uColorLoc: WebGLUniformLocation | null = null;
  /** Crosshair/tuned-marker overlays (slice-4.6). */
  private overlay!: OverlayRenderer;
  private overlays: VizOverlays = EMPTY_OVERLAYS;
  private axis: FreqAxis | null = null;
  private lastBins: Float32Array | null = null;

  constructor(canvas: HTMLCanvasElement, cfg: SpectrumRendererConfig) {
    const gl = canvas.getContext('webgl2', {
      antialias: true,
      premultipliedAlpha: false,
    });
    if (!gl) throw new Error('WebGL2 not supported');
    this.gl = gl;
    this.cfg = cfg;

    this.initGL();
    this.resize();
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(canvas);

    console.info('[SpectrumRenderer] initialized', { cfg });
  }

  /** Current frequency axis (null before the first frame). */
  getAxis(): FreqAxis | null {
    return this.axis;
  }

  /** Set crosshair/tuned overlays and redraw immediately (mousemove-rate). */
  setOverlays(ov: VizOverlays): void {
    this.overlays = ov;
    this.draw();
  }

  /** Level (dBFS) at a frequency — for the hover readout. */
  levelAt(hz: number): number | null {
    const axis = this.axis;
    const bins = this.lastBins;
    if (!axis || !bins || bins.length === 0) return null;
    const frac = (hz - (axis.centerHz - axis.sampleRateHz / 2)) / axis.sampleRateHz;
    const idx = Math.round(frac * (bins.length - 1));
    if (idx < 0 || idx >= bins.length) return null;
    return bins[idx];
  }

  private initGL(): void {
    const gl = this.gl;
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
    this.uColorLoc = gl.getUniformLocation(prog, 'u_color');

    // VAO with two vertex buffers (trace + peak), both bound to attrib 0.
    this.vao = gl.createVertexArray();
    gl.bindVertexArray(this.vao);
    this.traceBuffer = gl.createBuffer();
    this.peakBufferGl = gl.createBuffer();
    // We re-bind the appropriate buffer in render() before each draw call.

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
    const canvas = this.gl.canvas as HTMLCanvasElement;
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.floor(canvas.clientWidth * dpr));
    const h = Math.max(1, Math.floor(canvas.clientHeight * dpr));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    this.gl.viewport(0, 0, w, h);
  }

  update(frame: FFTFrame): void {
    const gl = this.gl;
    const bins = frame.bins;
    const n = bins.length;
    this.lastBins = bins;
    this.axis = { centerHz: frame.centerFreq, sampleRateHz: frame.sampleRate };

    // Initialize or resize CPU peak buffer.
    if (!this.peakBuffer || this.peakBuffer.length !== n) {
      this.peakBuffer = new Float32Array(n).fill(-Infinity);
    }

    // Update peak buffer (decay toward -inf, capture new max).
    if (this.cfg.peakHold) {
      const decay = this.cfg.peakDecay;
      for (let i = 0; i < n; i++) {
        const p = this.peakBuffer[i];
        const v = bins[i];
        if (v > p) {
          this.peakBuffer[i] = v;
        } else {
          // Exponential decay toward the noise floor.
          this.peakBuffer[i] = p * decay + (this.cfg.minDb - 20) * (1 - decay);
        }
      }
    }

    // Build the trace vertex buffer: X = bin index → [-1, 1], Y = dBFS → [-1, 1].
    const traceVerts = new Float32Array(n * 2);
    const range = this.cfg.maxDb - this.cfg.minDb;
    for (let i = 0; i < n; i++) {
      const x = (n === 1 ? 0 : (i / (n - 1)) * 2 - 1);
      const t = (bins[i] - this.cfg.minDb) / range;
      const y = Math.max(-1, Math.min(1, t * 2 - 1));
      traceVerts[i * 2] = x;
      traceVerts[i * 2 + 1] = y;
    }

    gl.clearColor(0.027, 0.035, 0.051, 1.0);
    gl.clear(gl.COLOR_BUFFER_BIT);

    gl.useProgram(this.program);
    gl.bindVertexArray(this.vao);

    // Draw main trace.
    gl.bindBuffer(gl.ARRAY_BUFFER, this.traceBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, traceVerts, gl.DYNAMIC_DRAW);
    // Attribute 0 is bound via layout(location=0) in the vertex shader.
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.uniform4fv(this.uColorLoc, CYAN_450);
    gl.drawArrays(gl.LINE_STRIP, 0, n);

    // Draw peak-hold trace (if enabled).
    if (this.cfg.peakHold && this.peakBuffer) {
      const peakVerts = new Float32Array(n * 2);
      for (let i = 0; i < n; i++) {
        const x = (n === 1 ? 0 : (i / (n - 1)) * 2 - 1);
        const t = (this.peakBuffer[i] - this.cfg.minDb) / range;
        const y = Math.max(-1, Math.min(1, t * 2 - 1));
        peakVerts[i * 2] = x;
        peakVerts[i * 2 + 1] = y;
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, this.peakBufferGl);
      gl.bufferData(gl.ARRAY_BUFFER, peakVerts, gl.DYNAMIC_DRAW);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
      gl.uniform4fv(this.uColorLoc, AMBER_450);
      gl.drawArrays(gl.LINE_STRIP, 0, n);
    }

    this.drawOverlay();
  }

  /** Re-draw the scene from the last uploaded buffers (overlay updates). */
  private draw(): void {
    const gl = this.gl;
    const n = this.lastBins ? this.lastBins.length : 0;
    if (n === 0) return;

    gl.clearColor(0.027, 0.035, 0.051, 1.0);
    gl.clear(gl.COLOR_BUFFER_BIT);

    gl.useProgram(this.program);
    gl.bindVertexArray(this.vao);

    gl.bindBuffer(gl.ARRAY_BUFFER, this.traceBuffer);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.uniform4fv(this.uColorLoc, CYAN_450);
    gl.drawArrays(gl.LINE_STRIP, 0, n);

    if (this.cfg.peakHold) {
      gl.bindBuffer(gl.ARRAY_BUFFER, this.peakBufferGl);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
      gl.uniform4fv(this.uColorLoc, AMBER_450);
      gl.drawArrays(gl.LINE_STRIP, 0, n);
    }

    this.drawOverlay();
  }

  private drawOverlay(): void {
    if (this.axis) this.overlay.draw(this.overlays, this.axis);
  }

  dispose(): void {
    this.resizeObserver.disconnect();
    this.overlay.dispose();
    const gl = this.gl;
    if (this.traceBuffer) gl.deleteBuffer(this.traceBuffer);
    if (this.peakBufferGl) gl.deleteBuffer(this.peakBufferGl);
    if (this.vao) gl.deleteVertexArray(this.vao);
    if (this.program) gl.deleteProgram(this.program);
  }
}
