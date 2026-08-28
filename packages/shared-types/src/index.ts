/** Shared types — wire-format contracts between Python backend and TypeScript frontend.
 *
 *  Slice-1 status: TypeScript shapes only. Slice-2 will add zod schemas
 *  for runtime validation; the Python counterparts live in
 *  `python/openwebrx_plus_types/`.
 *
 *  These are deliberately framework-agnostic — the frontend imports them
 *  directly, the backend generates JSON matching these shapes.
 */

export * from './fft.js';
export * from './receiver.js';
export * from './metadata.js';
export * from './control.js';
export * from './audio.js';
export * from './decoder.js';
