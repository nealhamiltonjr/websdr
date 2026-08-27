/** OverlayRenderer — crosshair + tuned-marker overlay quads for FFT vizes.
 *
 *  Slice-4.6 (ADR-001 feature 11): every FFT visualization can draw
 *    - a hover crosshair (vertical line at the cursor frequency),
 *    - a tuned-frequency marker (vertical line at the demod frequency),
 *    - a translucent passband band around the tuned marker.
 *
 *  Owns its own tiny flat-color program + dynamic VBO (up to 3 quads = 18
 *  vertices), independent of the host renderer's program/VAO. The host calls
 *  draw() AFTER its own scene draw, with blending enabled for this call only.
 */

import type { FreqAxis } from '../../visualizations/freqAxis';
import { fractionAtFreq } from '../../visualizations/freqAxis';

/** What to overlay on one frame draw. */
export interface VizOverlays {
  /** Hover crosshair frequency in Hz, or null when the cursor is outside. */
  cursorHz: number | null;
  /** Tuned (demod) frequency in Hz, or null before metadata lands. */
  tunedHz: number | null;
  /** Absolute passband [loHz, hiHz], or null to skip the band. */
  passbandHz: readonly [number, number] | null;
}

export const EMPTY_OVERLAYS: VizOverlays = {
  cursorHz: null,
  tunedHz: null,
  passbandHz: null,
};

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

// Theme colors (rgb from the editorial-dark tokens; alpha per element).
const CURSOR_RGBA: ReadonlyArray<number> = [0.133, 0.827, 0.933, 0.85]; // cyan-450
const TUNED_RGBA: ReadonlyArray<number> = [0.961, 0.620, 0.043, 1.0]; // amber-450
const BAND_RGBA: ReadonlyArray<number> = [0.961, 0.620, 0.043, 0.14]; // amber-450, faint

/** Line widths in CSS pixels (scaled by DPR at draw time). */
const CURSOR_LINE_PX = 1;
const TUNED_LINE_PX = 1.5;

export class OverlayRenderer {
  private gl: WebGL2RenderingContext;
  private program: WebGLProgram;
  private vao: WebGLVertexArrayObject;
  private vbo: WebGLBuffer;
  private uColorLoc: WebGLUniformLocation | null;
  private verts = new Float32Array(3 * 6 * 2); // max 3 quads × 6 verts × xy

  constructor(gl: WebGL2RenderingContext) {
    this.gl = gl;
    const vs = this.compile(gl.VERTEX_SHADER, VERT_SRC);
    const fs = this.compile(gl.FRAGMENT_SHADER, FRAG_SRC);
    const prog = gl.createProgram();
    if (!prog) throw new Error('[OverlayRenderer] failed to create program');
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      const log = gl.getProgramInfoLog(prog);
      gl.deleteProgram(prog);
      throw new Error('[OverlayRenderer] link failed: ' + log);
    }
    this.program = prog;
    this.uColorLoc = gl.getUniformLocation(prog, 'u_color');

    this.vao = gl.createVertexArray();
    gl.bindVertexArray(this.vao);
    this.vbo = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.vbo);
    gl.bufferData(gl.ARRAY_BUFFER, this.verts, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
  }

  private compile(type: number, src: string): WebGLShader {
    const gl = this.gl;
    const sh = gl.createShader(type);
    if (!sh) throw new Error('[OverlayRenderer] failed to create shader');
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
      const log = gl.getShaderInfoLog(sh);
      gl.deleteShader(sh);
      throw new Error('[OverlayRenderer] compile failed: ' + log);
    }
    return sh;
  }

  /** Draw the overlay quads. Call after the host scene, axis required. */
  draw(overlays: VizOverlays, axis: FreqAxis): void {
    const gl = this.gl;
    const canvas = gl.canvas as HTMLCanvasElement;
    const dpr = window.devicePixelRatio || 1;
    const cssWidth = canvas.clientWidth || 1;

    // Quads are pushed in draw order (band → tuned → cursor); each entry is
    // [vertexCount, color] for a sequential bufferSubData + drawArrays.
    const quads: Array<{ n: number; color: ReadonlyArray<number> }> = [];
    const pushQuad = (x0: number, x1: number, color: ReadonlyArray<number>) => {
      if (x1 <= x0) return;
      const y0 = -1;
      const y1 = 1;
      const v = this.verts;
      let i = quads.reduce((acc, q) => acc + q.n, 0) * 2;
      // Two triangles: (x0,y0)(x1,y0)(x0,y1)  (x1,y0)(x1,y1)(x0,y1)
      v[i++] = x0; v[i++] = y0; v[i++] = x1; v[i++] = y0; v[i++] = x0; v[i++] = y1;
      v[i++] = x1; v[i++] = y0; v[i++] = x1; v[i++] = y1; v[i++] = x0; v[i++] = y1;
      quads.push({ n: 6, color });
    };

    // 1) Passband band (under the lines).
    if (overlays.passbandHz) {
      const [lo, hi] = overlays.passbandHz;
      pushQuad(
        fractionAtFreq(axis, lo) * 2 - 1,
        fractionAtFreq(axis, hi) * 2 - 1,
        BAND_RGBA,
      );
    }
    // 2) Tuned marker (amber).
    if (overlays.tunedHz != null) {
      const x = fractionAtFreq(axis, overlays.tunedHz) * 2 - 1;
      const w = (TUNED_LINE_PX * dpr) / 2 / cssWidth;
      pushQuad(x - w, x + w, TUNED_RGBA);
    }
    // 3) Hover crosshair (cyan).
    if (overlays.cursorHz != null) {
      const x = fractionAtFreq(axis, overlays.cursorHz) * 2 - 1;
      const w = (CURSOR_LINE_PX * dpr) / 2 / cssWidth;
      pushQuad(x - w, x + w, CURSOR_RGBA);
    }

    if (quads.length === 0) return;

    gl.useProgram(this.program);
    gl.bindVertexArray(this.vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.vbo);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

    // Upload + draw each quad sequentially (verts were packed in this order).
    // dstByteOffset is in BYTES: 2 floats (8 bytes) per vertex.
    let byteOffset = 0;
    for (const q of quads) {
      gl.uniform4fv(this.uColorLoc, q.color as Float32List);
      gl.bufferSubData(gl.ARRAY_BUFFER, byteOffset, this.verts, byteOffset / 4, q.n * 2);
      gl.drawArrays(gl.TRIANGLES, 0, q.n);
      byteOffset += q.n * 2 * 4;
    }

    gl.disable(gl.BLEND);
  }

  dispose(): void {
    const gl = this.gl;
    gl.deleteBuffer(this.vbo);
    gl.deleteVertexArray(this.vao);
    gl.deleteProgram(this.program);
  }
}
