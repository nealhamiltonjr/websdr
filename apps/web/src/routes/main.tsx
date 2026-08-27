/** Main route — the Base Station UI. Hosts the WorkspaceManager + TuningBar +
 *  AudioPlayer + spawn-receiver button.
 *
 *  Slice-4.5: per-receiver tuning. The TuningBar edits the ACTIVE receiver
 *  (click a receiver tab to switch); every receiver keeps its own
 *  {frequency, mode} learned from metadata frames. Audio follows the active
 *  receiver too — no more simultaneous demodulated streams.
 *
 *  Slice-4.6: click-to-tune from any FFT canvas (waterfall/spectrum) — the
 *  clicked receiver becomes active and the TuningBar follows (tuneBus).
 *
 *  Slice-4.7: per-receiver gain + DSP mode — same pattern as frequency/mode:
 *  optimistic control send, confirming metadata echo, TuningBar knob bound
 *  to the active receiver.
 */

import { createSignal, onMount, onCleanup, Show, For } from 'solid-js';
import { WorkspaceManager } from '../components/WorkspaceManager';
import { TuningBar } from '../components/TuningBar';
import { AddReceiverModal, type Section } from '../components/AddReceiverModal';
import { SettingsPanel } from '../components/SettingsPanel';
import { DebugPanel } from '../components/DebugPanel';
import { DSPControls } from '../components/DSPControls';
import { receiverRegistry, parseFFTFrame, parseAudioFrame } from '../sessions/ReceiverSession';
import { registerTuneHandler } from '../sessions/tuneBus';
import {
  initialTuningModel,
  applyMetadata,
  applyControl,
  setActive,
  dropReceiver,
  activeTuning,
  tabLabel,
  CONTROL_DEFAULTS,
} from '../sessions/receiverTuning';
import { createAudioPlayer } from '../lib/audio/AudioPlayer';
import type { DSPMode, ReceiverMetadata, ReceiverMode } from '@openwebrx-plus/shared-types';

const SLICE_1_RECEIVER_ID = 'rx-default';
const TUNING_DEFAULTS = { ...CONTROL_DEFAULTS, frequency: 14_205_000, mode: 'USB' as const };

// SharedWorker bootstrap: spin up the worker, subscribe to the default
// receiver, and route worker messages into the ReceiverSession.
// Returns the worker port so callers can send control messages.
function useSharedWorker() {
  const [tuning, setTuning] = createSignal(initialTuningModel(SLICE_1_RECEIVER_ID, TUNING_DEFAULTS));
  let port: MessagePort | null = null;

  // Audio player — must be created in the SolidJS scope so it can use onCleanup.
  const audioPlayer = createAudioPlayer();

  // Active receivers — starts with just rx-default, grows when the user
  // spawns one through the AddReceiverModal.
  const [receiverIds, setReceiverIds] = createSignal<string[]>([SLICE_1_RECEIVER_ID]);
  // AddReceiverModal visibility + the section it opens on ('+vfo' tab
  // shortcuts jump straight to the VFO panel).
  const [modal, setModal] = createSignal<{ open: boolean; section: Section }>({
    open: false,
    section: 'quick',
  });
  const openModal = (section: Section = 'quick') => setModal({ open: true, section });
  const closeModal = () => setModal({ open: false, section: 'quick' });

  // Slice-5.1: Settings + Debugger panel visibility.
  const [settingsOpen, setSettingsOpen] = createSignal(false);
  const [debugOpen, setDebugOpen] = createSignal(false);
  // Slice-5.2: per-receiver DSP controls panel visibility.
  const [dspControlsOpen, setDspControlsOpen] = createSignal(false);
  // The active receiver's dsp_params, learned from metadata frames (slice-5.2).
  const [activeDspParams, setActiveDspParams] = createSignal<
    import('../lib/api').DSPParams | undefined
  >(undefined);

  onMount(() => {
    const worker = new SharedWorker(
      new URL('../workers/sdr.shared-worker.ts', import.meta.url),
      { type: 'module', name: 'openwebrx-sdr' },
    );

    port = worker.port;
    port.start();

    // Re-adopt receivers that still exist on the backend (spawned in a
    // previous page session). Without this, a reload would forget them and
    // the persisted Dockview layout would strip their panels.
    // NOTE: GET /api/receivers returns a BARE array of ReceiverInfo (see
    // api.ts listReceivers) — slice-4 shipped this reading `body.receivers`,
    // which was always undefined (re-adoption never ran; found in slice-4.5).
    void (async () => {
      try {
        const res = await fetch('/api/receivers');
        if (!res.ok) return;
        const list = (await res.json()) as Array<{ receiver_id: string }>;
        for (const r of Array.isArray(list) ? list : []) {
          adoptSpawned(r.receiver_id);
        }
      } catch (e) {
        console.warn('[main] failed to re-adopt receivers from the backend', e);
      }
    })();

    // Default receiver session is created lazily when the first FFT frame
    // arrives (via `receiverRegistry.getOrCreate(msg.receiverId)`).

    port.onmessage = (ev: MessageEvent) => {
      const msg = ev.data;
      if (!msg || typeof msg !== 'object') return;
      switch (msg.type) {
        case 'fft': {
          try {
            const buf = msg.data as ArrayBuffer;
            const frame = parseFFTFrame(buf, msg.receiverId);
            // Get or create the session for this receiver (might be a
            // dynamically-spawned one, not just the default).
            const sess = receiverRegistry.getOrCreate(msg.receiverId);
            sess.ingestFFT(frame);
          } catch (e) {
            console.error('[main] failed to parse FFT frame', e);
          }
          break;
        }
        case 'audio': {
          try {
            const buf = msg.data as ArrayBuffer;
            const frame = parseAudioFrame(buf);
            const sess = receiverRegistry.getOrCreate(msg.receiverId);
            sess.ingestAudio(frame);
            // Slice-4.5: audio follows the ACTIVE receiver — the demodulated
            // stream of every other receiver is ingested into its session
            // (popouts / future mixers can still use it) but not played.
            if (msg.receiverId === tuning().activeId) {
              audioPlayer.enqueue(frame.samples, frame.sampleRate);
            }
          } catch (e) {
            console.error('[main] failed to parse audio frame', e);
          }
          break;
        }
        case 'metadata': {
          try {
            const env = msg.data as {
              type: string;
              receiverId: string;
              frequency: number;
              mode: string;
              gain?: number | null;
              dspMode?: string;
              source: {
                type: string;
                label: string;
                sampleRate: number;
                endpoint?: string;
                gainRange?: [number, number] | null;
                supportsAgc?: boolean;
              };
            };
            const meta: ReceiverMetadata = {
              receiverId: env.receiverId,
              frequency: env.frequency,
              mode: env.mode as ReceiverMetadata['mode'],
              gain: env.gain ?? null,
              dspMode: env.dspMode as DSPMode | undefined,
              source: {
                type: env.source.type as ReceiverMetadata['source']['type'],
                label: env.source.label,
                sampleRate: env.source.sampleRate,
                endpoint: env.source.endpoint,
                gainRange: env.source.gainRange ?? null,
                supportsAgc: !!env.source.supportsAgc,
              },
            };
            const sess = receiverRegistry.getOrCreate(env.receiverId);
            sess.ingestMetadata(meta);
            // Slice-4.5: every receiver's tuning is tracked (not just the
            // default's) — the TuningBar shows whichever receiver is active.
            // Slice-4.7: gain + dspMode ride the same metadata frames.
            // Slice-5.2: dspParams (fine-grained DSP controls) ride too.
            const dspParamsRaw = (env as { dspParams?: import('../lib/api').DSPParams }).dspParams;
            setTuning((m) =>
              applyMetadata(m, env.receiverId, {
                frequency: meta.frequency,
                mode: meta.mode,
                gain: meta.gain,
                dspMode: env.dspMode,
                gainRange: env.source.gainRange ?? null,
                supportsAgc: !!env.source.supportsAgc,
              }),
            );
            // Update the active receiver's dsp_params when its metadata lands.
            if (dspParamsRaw && env.receiverId === tuning().activeId) {
              setActiveDspParams(dspParamsRaw);
            }
          } catch (e) {
            console.error('[main] failed to parse metadata', e);
          }
          break;
        }
        case 'decoder': {
          // ADR-003 decoder event (ADS-B aircraft table, message feeds…).
          // The SharedWorker already parsed the JSON envelope.
          try {
            const envelope = msg.data as import('@openwebrx-plus/shared-types').DecoderEventEnvelope;
            const sess = receiverRegistry.getOrCreate(msg.receiverId);
            sess.ingestDecoder(envelope);
          } catch (e) {
            console.error('[main] failed to ingest decoder event', e);
          }
          break;
        }
        case 'open': {
          console.info(`[rx ${msg.receiverId}] WS open`);
          break;
        }
        case 'close': {
          console.info(`[rx ${msg.receiverId}] WS closed`, msg.code, msg.reason);
          break;
        }
      }
    };

    // Subscribe to the default receiver (will trigger SharedWorker to open WS).
    port.postMessage({ type: 'subscribe', receiverId: SLICE_1_RECEIVER_ID });

    // Slice-4.6: click-to-tune — waterfall/spectrum canvas clicks arrive via
    // the tune bus (viz components have no port access). Tuning the clicked
    // receiver also makes it ACTIVE so the TuningBar + audio follow.
    const unregisterTune = registerTuneHandler((rxId, hz) => {
      port?.postMessage({
        type: 'control',
        receiverId: rxId,
        command: 'setFrequency',
        value: hz,
      });
      setTuning((m) => setActive(applyControl(m, rxId, { frequency: hz }), rxId));
    });
    onCleanup(unregisterTune);
  });

  // Control message senders — called by the TuningBar. Slice-4.5: they
  // target the ACTIVE receiver (click a tab to switch), not just rx-default.
  const sendSetFrequency = (hz: number) => {
    const rxId = tuning().activeId;
    port?.postMessage({
      type: 'control',
      receiverId: rxId,
      command: 'setFrequency',
      value: hz,
    });
    // Optimistic update — the confirming metadata frame lands within ~1/fft_fps.
    setTuning((m) => applyControl(m, rxId, { frequency: hz }));
  };

  const sendSetMode = (newMode: ReceiverMode) => {
    const rxId = tuning().activeId;
    port?.postMessage({
      type: 'control',
      receiverId: rxId,
      command: 'setMode',
      value: newMode,
    });
    setTuning((m) => applyControl(m, rxId, { mode: newMode }));
  };

  // Slice-4.7: gain — null (auto) serializes as the "auto" wire value.
  const sendSetGain = (db: number | null) => {
    const rxId = tuning().activeId;
    port?.postMessage({
      type: 'control',
      receiverId: rxId,
      command: 'setGain',
      value: db === null ? 'auto' : db,
    });
    setTuning((m) => applyControl(m, rxId, { gain: db }));
  };

  const sendSetDSPMode = (newDspMode: DSPMode) => {
    const rxId = tuning().activeId;
    port?.postMessage({
      type: 'control',
      receiverId: rxId,
      command: 'setDSPMode',
      value: newDspMode,
    });
    setTuning((m) => applyControl(m, rxId, { dspMode: newDspMode }));
  };

  // Slice-5.2: send a setDSPParams patch via the SharedWorker. Only the
  // active receiver is edited.
  const sendSetDSPParams = (patch: Partial<import('../lib/api').DSPParams>) => {
    const rxId = tuning().activeId;
    port?.postMessage({
      type: 'control',
      receiverId: rxId,
      command: 'setDSPParams',
      value: patch,
    });
    // Optimistic local update so the panel reflects the change immediately;
    // the metadata echo will confirm a frame or two later.
    setActiveDspParams((prev) => prev ? { ...prev, ...patch } as import('../lib/api').DSPParams : prev);
  };

  // Slice-4.5: select the receiver the TuningBar edits (tab click).
  const selectReceiver = (id: string) => {
    setTuning((m) => setActive(m, id));
  };

  // Adopt a freshly spawned receiver: add its tab, subscribe via the
  // SharedWorker so its FFT/audio frames get routed into a ReceiverSession,
  // and make it the ACTIVE receiver — the user spawned it to use it.
  // (The REST POST itself happened inside AddReceiverModal.)
  const adoptSpawned = (newId: string) => {
    setReceiverIds((prev) => (prev.includes(newId) ? prev : [...prev, newId]));
    port?.postMessage({ type: 'subscribe', receiverId: newId });
    setTuning((m) => setActive(m, newId));
  };

  const removeReceiver = async (id: string) => {
    // Don't allow removing the default — it's the entry point.
    if (id === SLICE_1_RECEIVER_ID) return;
    // Tell the backend to destroy the session.
    try {
      await fetch(`/api/receivers/${id}`, { method: 'DELETE' });
    } catch (e) {
      console.warn('[removeReceiver] REST delete failed', e);
    }
    // Unsubscribe from the SharedWorker (will close the WS if no other
    // subscribers).
    port?.postMessage({ type: 'unsubscribe', receiverId: id });
    // Drop from the list; if it was active, tuning falls back to rx-default.
    setReceiverIds((prev) => prev.filter((rid) => rid !== id));
    setTuning((m) => dropReceiver(m, id, SLICE_1_RECEIVER_ID));
    receiverRegistry.destroy(id);
  };

  return {
    tuning,
    sendSetFrequency,
    sendSetMode,
    sendSetGain,
    sendSetDSPMode,
    selectReceiver,
    audioPlayer,
    receiverIds,
    modal,
    openModal,
    closeModal,
    adoptSpawned,
    removeReceiver,
    settingsOpen,
    setSettingsOpen,
    debugOpen,
    setDebugOpen,
    dspControlsOpen,
    setDspControlsOpen,
    activeDspParams,
    sendSetDSPParams,
  };
}

export function MainRoute() {
  const {
    tuning,
    sendSetFrequency,
    sendSetMode,
    sendSetGain,
    sendSetDSPMode,
    selectReceiver,
    audioPlayer,
    receiverIds,
    modal,
    openModal,
    closeModal,
    adoptSpawned,
    removeReceiver,
    settingsOpen,
    setSettingsOpen,
    debugOpen,
    setDebugOpen,
    dspControlsOpen,
    setDspControlsOpen,
    activeDspParams,
    sendSetDSPParams,
  } = useSharedWorker();

  // Active receiver's tuning — what the TuningBar shows/edits.
  const active = () => activeTuning(tuning());

  // Escape closes whichever modal is open (settings > debug > DSP > source picker).
  const onKeydown = (e: KeyboardEvent) => {
    if (e.key === 'Escape') {
      if (settingsOpen()) setSettingsOpen(false);
      else if (debugOpen()) setDebugOpen(false);
      else if (dspControlsOpen()) setDspControlsOpen(false);
      else closeModal();
    }
  };
  onMount(() => window.addEventListener('keydown', onKeydown));
  onCleanup(() => window.removeEventListener('keydown', onKeydown));

  return (
    <div class="flex h-screen w-screen flex-col bg-base-900">
      {/* Top bar — title, spawn-receiver button, audio toggle, command palette */}
      <header class="flex h-10 items-center justify-between border-b border-base-800 px-4">
        <div class="flex items-center gap-2">
          <span class="font-mono text-sm font-bold text-cyan-450">OpenWebRX+</span>
          <span class="font-mono text-xs text-base-400">v0.1.0-slice5.1</span>
        </div>
        <div class="flex items-center gap-3">
          {/* Spawn-receiver button — opens the source picker */}
          <button
            type="button"
            class="rounded bg-amber-450/20 px-2 py-0.5 font-mono text-xs text-amber-450 hover:bg-amber-450/30"
            onClick={() => openModal('quick')}
            title="Add a receiver — local hardware, IQ file, remote receiver, or VFO"
          >
            + receiver
          </button>
          {/* Slice-5.1: Settings button */}
          <button
            type="button"
            class="rounded bg-base-800 px-2 py-0.5 font-mono text-xs text-base-200 hover:bg-base-700"
            onClick={() => setSettingsOpen(true)}
            title="Open the Settings panel — display, audio, DSP, sources, decoders, debug"
          >
            ⚙ settings
          </button>
          {/* Slice-5.1: Debugger button */}
          <button
            type="button"
            class="rounded bg-base-800 px-2 py-0.5 font-mono text-xs text-base-200 hover:bg-base-700"
            onClick={() => setDebugOpen(true)}
            title="Open the Debugger — captured logs + errors, with filters + export"
          >
            🐛 debug
          </button>
          {/* Slice-5.2: per-receiver DSP controls button */}
          <button
            type="button"
            class="rounded bg-emerald-450/20 px-2 py-0.5 font-mono text-xs text-emerald-450 hover:bg-emerald-450/30"
            onClick={() => setDspControlsOpen(true)}
            title="Open the per-receiver DSP controls — bandpass width, AGC, squelch, notch (experimental), noise blanker (experimental)"
          >
            🌡 DSP
          </button>
          {/* Audio toggle */}
          <button
            type="button"
            class="rounded bg-base-800 px-2 py-0.5 font-mono text-xs text-base-200 hover:bg-base-700"
            onClick={() => audioPlayer.toggleMute()}
            title={audioPlayer.muted() ? 'Click to enable audio' : 'Audio is live — click to mute'}
          >
            {audioPlayer.muted() ? '🔇 muted' : '🔊 live'}
          </button>
          {/* Volume slider */}
          <input
            type="range"
            min={0}
            max={1}
            step={0.01}
            value={audioPlayer.volume()}
            onInput={(e) => audioPlayer.setVolume(Number((e.target as HTMLInputElement).value))}
            class="h-1 w-24 cursor-pointer appearance-none rounded-full bg-base-700 accent-cyan-450"
            title="Master volume"
          />
          <span class="font-mono text-xs text-base-400">
            Press <kbd class="rounded bg-base-800 px-1">⌘K</kbd> for command palette
          </span>
        </div>
      </header>

      {/* Tuning bar — edits the ACTIVE receiver (click a receiver tab to
          switch). The chip shows which receiver is on the knob. Row 2 carries
          the gain knob + DSP mode (slice-4.7). */}
      <TuningBar
        receiverLabel={tuning().activeId}
        frequency={active().frequency}
        mode={active().mode}
        gain={active().gain}
        gainRange={active().gainRange}
        supportsAgc={active().supportsAgc}
        dspMode={active().dspMode}
        onSetFrequency={sendSetFrequency}
        onSetMode={sendSetMode}
        onSetGain={sendSetGain}
        onSetDSPMode={sendSetDSPMode}
      />

      {/* Receiver tabs — click to select the active receiver (tuning +
          audio); each chip shows the receiver's tuned frequency. */}
      <Show when={receiverIds().length > 1}>
        <div class="flex h-7 items-center gap-1 border-b border-base-800 bg-base-850 px-2 font-mono text-[11px]">
          <For each={receiverIds()}>
            {(rid) => (
              <span
                class={`flex items-center rounded px-2 py-0.5 ${
                  rid === tuning().activeId
                    ? 'bg-amber-450/20 text-amber-450 ring-1 ring-amber-450/50'
                    : 'bg-base-800 text-base-300 hover:bg-base-700'
                }`}
                title={
                  rid === tuning().activeId
                    ? 'Active receiver — the tuning bar + audio follow it'
                    : 'Click to make this the active receiver (tuning + audio)'
                }
              >
                <button
                  type="button"
                  class="cursor-pointer"
                  onClick={() => selectReceiver(rid)}
                >
                  {tabLabel(rid, tuning().tunings[rid], TUNING_DEFAULTS)}
                </button>
                <button
                  type="button"
                  class="ml-1 text-cyan-450 hover:text-cyan-350"
                  onClick={() => openModal('vfo')}
                  title={`Spawn a VFO sub-receiver on top of ${rid}`}
                >
                  +vfo
                </button>
                <Show when={rid !== SLICE_1_RECEIVER_ID}>
                  <button
                    type="button"
                    class="ml-1 text-rose-450 hover:text-rose-350"
                    onClick={() => void removeReceiver(rid)}
                    title="Destroy this receiver"
                  >
                    ×
                  </button>
                </Show>
              </span>
            )}
          </For>
        </div>
      </Show>

      {/* Main workspace */}
      <main class="flex-1 overflow-hidden">
        <WorkspaceManager receiverIds={receiverIds} onRemoveReceiver={removeReceiver} />
      </main>

      {/* Source picker — quick connect / catalog / remote directory / VFO */}
      <Show when={modal().open}>
        <AddReceiverModal
          onClose={closeModal}
          onSpawned={adoptSpawned}
          initialSection={modal().section}
        />
      </Show>

      {/* Slice-5.1: Settings panel */}
      <Show when={settingsOpen()}>
        <SettingsPanel onClose={() => setSettingsOpen(false)} />
      </Show>

      {/* Slice-5.1: Debugger panel */}
      <Show when={debugOpen()}>
        <DebugPanel onClose={() => setDebugOpen(false)} />
      </Show>

      {/* Slice-5.2: per-receiver DSP controls panel */}
      <Show when={dspControlsOpen()}>
        <DSPControls
          receiverId={tuning().activeId}
          dspParams={activeDspParams()}
          onPatch={sendSetDSPParams}
          onClose={() => setDspControlsOpen(false)}
        />
      </Show>
    </div>
  );
}
