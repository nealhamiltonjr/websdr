/// <reference lib="webworker" />
/** SharedWorker — single source of truth for SDR WebSocket connections.
 *
 *  One WebSocket per ReceiverSession lives here. The main window and any
 *  popout windows subscribe to a receiverId via postMessage; the worker
 *  fans out received FFT/metadata/audio frames to all subscribers.
 *
 *  Connection lifecycle:
 *    - First subscriber for a receiverId → worker opens the WebSocket
 *    - Last subscriber unsubscribes → worker closes the WebSocket
 *
 *  Binary frame routing (peek at the first 4 bytes for the magic):
 *    - 0x4f465257 ("WRFO") → FFT frame
 *    - 0x41554449 ("AUDI") → Audio frame
 *    - anything else        → log warning, drop
 *
 *  Protocol (client → worker):
 *    { type: 'subscribe', receiverId }
 *    { type: 'unsubscribe', receiverId }
 *    { type: 'control', receiverId, command: 'setFrequency', value: 14_205_000 }
 *
 *  Protocol (worker → client):
 *    { type: 'fft', receiverId, data: ArrayBuffer }
 *    { type: 'audio', receiverId, data: ArrayBuffer }
 *    { type: 'metadata', receiverId, data: ReceiverMetadata }
 *    { type: 'decoder', receiverId, data: DecoderEventEnvelope }
 *    { type: 'open', receiverId }
 *    { type: 'close', receiverId, code, reason }
 *
 *  See ADR-001 § Cross-Window Data Sharing.
 */

import { FFT_HEADER_MAGIC, AUDIO_HEADER_MAGIC } from '@openwebrx-plus/shared-types';

interface ClientMessage {
  type: 'subscribe' | 'unsubscribe' | 'control';
  receiverId: string;
  command?: string;
  value?: unknown;
}

// SharedWorker scope: this script runs in a SharedWorkerGlobalScope, which is
// not in the default lib.dom.d.ts. We use a typed alias to `self` for clarity
// and to satisfy the TypeScript compiler when we set onconnect.
type SharedWorkerScope = DedicatedWorkerGlobalScope & {
  onconnect: ((ev: MessageEvent) => void) | null;
};
const workerScope = self as unknown as SharedWorkerScope;

const subscriptions = new Map<string, Set<MessagePort>>();
const sockets = new Map<string, WebSocket>();

workerScope.onconnect = (e: MessageEvent) => {
  const port: MessagePort = e.ports[0];
  port.onmessage = (ev: MessageEvent) => {
    const msg = ev.data as ClientMessage;
    switch (msg.type) {
      case 'subscribe':
        subscribe(msg.receiverId, port);
        break;
      case 'unsubscribe':
        unsubscribe(msg.receiverId, port);
        break;
      case 'control':
        sendControl(msg.receiverId, msg);
        break;
      default:
        console.warn('[SharedWorker] unknown message', msg);
    }
  };
  port.start();
};

function subscribe(receiverId: string, port: MessagePort): void {
  let subs = subscriptions.get(receiverId);
  if (!subs) {
    subs = new Set();
    subscriptions.set(receiverId, subs);
    openSocket(receiverId);
  }
  subs.add(port);
}

function unsubscribe(receiverId: string, port: MessagePort): void {
  const subs = subscriptions.get(receiverId);
  if (!subs) return;
  subs.delete(port);
  if (subs.size === 0) {
    closeSocket(receiverId);
    subscriptions.delete(receiverId);
  }
}

function openSocket(receiverId: string): void {
  // Derive backend WS URL from current origin (dev proxy from Vite, or direct
  // in production where the same origin serves both the app and the WS).
  const wsUrl = `${location.origin.replace(/^http/, 'ws')}/ws/${receiverId}`;
  console.info(`[SharedWorker] opening WebSocket for receiver ${receiverId}: ${wsUrl}`);
  const ws = new WebSocket(wsUrl);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    fanout(receiverId, { type: 'open', receiverId });
  };
  ws.onclose = (ev) => {
    fanout(receiverId, { type: 'close', receiverId, code: ev.code, reason: ev.reason });
  };
  ws.onerror = (ev) => {
    console.error(`[SharedWorker] WS error for ${receiverId}:`, ev);
  };
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      // JSON text frames: metadata (legacy, after every binary frame) or
      // decoder events (ADR-003) — dispatch on the payload's type field.
      try {
        const json = JSON.parse(ev.data);
        if (json && json.type === 'decoder') {
          fanout(receiverId, { type: 'decoder', receiverId, data: json });
        } else {
          fanout(receiverId, { type: 'metadata', receiverId, data: json });
        }
      } catch (e) {
        console.error('[SharedWorker] bad JSON from server', e);
      }
    } else {
      // Binary frame — peek at the first 4 bytes to determine type.
      const buf = ev.data as ArrayBuffer;
      if (buf.byteLength < 4) {
        console.warn('[SharedWorker] binary frame too small:', buf.byteLength);
        return;
      }
      const magic = new DataView(buf).getUint32(0, true);
      if (magic === FFT_HEADER_MAGIC) {
        fanout(receiverId, { type: 'fft', receiverId, data: buf });
      } else if (magic === AUDIO_HEADER_MAGIC) {
        fanout(receiverId, { type: 'audio', receiverId, data: buf });
      } else {
        console.warn(
          `[SharedWorker] unknown binary magic: 0x${magic.toString(16)} (${buf.byteLength} bytes)`,
        );
      }
    }
  };

  sockets.set(receiverId, ws);
}

function closeSocket(receiverId: string): void {
  const ws = sockets.get(receiverId);
  if (ws) {
    ws.close();
    sockets.delete(receiverId);
  }
}

function sendControl(receiverId: string, msg: ClientMessage): void {
  const ws = sockets.get(receiverId);
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    console.warn(`[SharedWorker] cannot send control to ${receiverId}: not open`);
    return;
  }
  ws.send(JSON.stringify({ type: 'control', command: msg.command, value: msg.value }));
}

function fanout(receiverId: string, message: unknown, transfer?: Transferable[]): void {
  const subs = subscriptions.get(receiverId);
  if (!subs) return;
  for (const port of subs) {
    if (transfer && transfer.length > 0) {
      port.postMessage(message, transfer);
    } else {
      port.postMessage(message);
    }
  }
}
