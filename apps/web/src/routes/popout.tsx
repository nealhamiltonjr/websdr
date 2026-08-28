/** Popout route — `/popout/:vizType?receiverId=...&config=...`
 *
 *  A popout window loads the same SolidJS app but in single-visualization mode.
 *  Connects to the existing ReceiverSession via the SharedWorker (already
 *  running in the main window). This route is responsible for:
 *    - Parsing the URL params
 *    - Spinning up a SharedWorker port (subscribes to receiverId)
 *    - Rendering a single visualization, full-window, DPR-aware
 *
 *  Slice-1 status: route exists (URL contract honored), but popout spawning
 *  UX (window.open from WorkspaceManager) lands in slice-2.
 */

import { onMount, Suspense, Show, createSignal } from 'solid-js';
import { useSearchParams, useParams } from '@solidjs/router';
import { getViz } from '../visualizations/builtins';
import { receiverRegistry, parseFFTFrame } from '../sessions/ReceiverSession';
import type { ReceiverMetadata } from '@openwebrx-plus/shared-types';

export function PopoutRoute(): import('solid-js').JSX.Element {
  const params = useParams();
  const [searchParams] = useSearchParams();
  const vizType = (params.vizType as string) ?? 'waterfall';
  const receiverId = (searchParams.receiverId as string) ?? 'rx-default';
  const [ready, setReady] = createSignal(false);

  onMount(() => {
    // Bootstrap a SharedWorker port for this popout and subscribe.
    const worker = new SharedWorker(
      new URL('../workers/sdr.shared-worker.ts', import.meta.url),
      { type: 'module', name: 'openwebrx-sdr' },
    );
    const port = worker.port;
    port.start();

    const session = receiverRegistry.getOrCreate(receiverId);

    // Slice-6.3 — wire the cursor forward sink BEFORE any mouseover fires so
    // the very first cursor event in the popout propagates to the main window.
    // The popout's viz will call session.setCursor() on mousemove; the sink
    // forwards to the worker; the worker fans out to all subscribers of this
    // receiverId, including the main window.
    session.setCursorForward((hz, sourceVizId) => {
      port.postMessage({ type: 'cursor', receiverId, hz, sourceVizId });
    });

    port.onmessage = (ev: MessageEvent) => {
      const msg = ev.data;
      if (!msg || typeof msg !== 'object') return;
      if (msg.type === 'metadata') {
        try {
          const env = msg.data as {
            type: string;
            receiverId: string;
            frequency: number;
            mode: string;
            source: { type: string; label: string; sampleRate: number; endpoint?: string };
          };
          const meta: ReceiverMetadata = {
            receiverId: env.receiverId,
            frequency: env.frequency,
            mode: env.mode as ReceiverMetadata['mode'],
            source: {
              type: env.source.type as ReceiverMetadata['source']['type'],
              label: env.source.label,
              sampleRate: env.source.sampleRate,
              endpoint: env.source.endpoint,
            },
          };
          session.ingestMetadata(meta);
        } catch (e) {
          console.error('[popout] failed to parse metadata', e);
        }
      } else if (msg.type === 'fft') {
        try {
          const frame = parseFFTFrame(msg.data as ArrayBuffer, msg.receiverId);
          session.ingestFFT(frame);
        } catch (e) {
          console.error('[popout] failed to parse FFT frame', e);
        }
      } else if (msg.type === 'cursor') {
        // Slice-6.3 — cursor state broadcast from another window (the main
        // window or another popout). The viz that originated the cursor
        // skips its own echo via `sourceVizId === vizId` in attachCrosshair.
        session.ingestRemoteCursor(msg.hz, msg.sourceVizId);
      }
    };
    port.postMessage({ type: 'subscribe', receiverId });
    setReady(true);
  });

  const manifest = getViz(vizType);
  const config = searchParams.config
    ? (() => {
        try {
          return JSON.parse(decodeURIComponent(searchParams.config as string));
        } catch {
          return undefined;
        }
      })()
    : undefined;

  return (
    <div class="flex h-screen w-screen items-stretch justify-stretch bg-base-900">
      <Show
        when={manifest}
        fallback={
          <div class="flex h-full w-full items-center justify-center font-mono text-base-400">
            Unknown viz type: {vizType}
          </div>
        }
      >
        <Suspense
          fallback={
            <div class="flex h-full w-full items-center justify-center font-mono text-base-400">
              Loading popout…
            </div>
          }
        >
          <div class="flex h-full w-full flex-col">
            <header class="flex h-8 items-center justify-between border-b border-base-800 px-3">
              <span class="font-mono text-xs text-cyan-450">{manifest!.displayName}</span>
              <span class="font-mono text-[10px] text-base-400">rx: {receiverId.slice(0, 8)}</span>
            </header>
            <div class="flex-1 overflow-hidden">
              {(() => {
                if (!ready()) return null;
                const Component = manifest!.component;
                return <Component receiverId={receiverId} config={config} />;
              })()}
            </div>
          </div>
        </Suspense>
      </Show>
    </div>
  );
}
