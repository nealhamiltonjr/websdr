// @vitest-environment node
/** Tune bus — decoupled "please tune this receiver" channel (slice-4.6).
 *
 *  Visualization components (waterfall / spectrum) know their receiverId but
 *  have no path to the SharedWorker port — they are mounted by dockview from
 *  serialized panel params, so callback props cannot be threaded through.
 *  The main route registers ONE handler here; viz components call
 *  requestTune() on canvas click (click-to-tune).
 *
 *  Popout windows render the same viz components but register no handler —
 *  requestTune() degrades gracefully (console note) instead of throwing.
 */

type TuneHandler = (receiverId: string, hz: number) => void;

let handler: TuneHandler | null = null;

/** Register the (single) tune handler. Returns an unregister function. */
export function registerTuneHandler(fn: TuneHandler): () => void {
  handler = fn;
  return () => {
    if (handler === fn) handler = null;
  };
}

/** Request a tune from anywhere in the app (viz canvas click). */
export function requestTune(receiverId: string, hz: number): void {
  if (handler) {
    handler(receiverId, hz);
    return;
  }
  // Popouts and standalone routes have no tuning surface — degrade gracefully.
  console.info('[tuneBus] no handler registered; ignoring tune request', {
    receiverId,
    hz,
  });
}

/** Test/inspection helper. */
export function hasTuneHandler(): boolean {
  return handler != null;
}
