/** AircraftListViz — the ADS-B aircraft table (ADR-003 first consumer).
 *
 *  Subscribes to the receiver's decoderStream (fed by the SharedWorker
 *  from WS decoder events) and renders the live aircraft table, plus a
 *  self-contained attach/detach control for the bundled ADS-B decoder.
 *  Any ADSB_DECODERS family member feeds this table: the in-process
 *  "adsb" plugin (default attach) or the subprocess "dump1090" plugin
 *  (attach via REST when a dump1090-class binary is configured — its
 *  rows additionally carry live positions). When no decoder is attached
 *  the panel shows the one-click "attach ADS-B" CTA — the receiver
 *  needs 2 MSPS IQ (e.g. the adsb_1090 fixture or an RTL-SDR at
 *  1090 MHz); the server rejects anything else with an actionable 400.
 */

import { createSignal, onCleanup, onMount, Show, For } from 'solid-js';
import { ADSB_DECODERS } from '@openwebrx-plus/shared-types';
import { receiverRegistry } from '../sessions/ReceiverSession';
import { api, ApiError, formatHz } from '../lib/api';
import { registerViz, type VizProps } from './registry';
import {
  applyDecoderEvent,
  formatAge,
  formatAltitude,
  formatPosition,
  initialAircraftState,
  type AircraftFeedState,
} from './aircraftModel';

/** Attached by the one-click control — the in-process plugin is always
 *  available (no external binary needed). */
const DEFAULT_DECODER = 'adsb';
const adsbDecoderNames: readonly string[] = ADSB_DECODERS;

function AircraftListViz(props: VizProps): import('solid-js').JSX.Element {
  const [state, setState] = createSignal<AircraftFeedState>(initialAircraftState());
  // Name of the family member actually attached ("adsb" / "dump1090").
  const [attachedName, setAttachedName] = createSignal<string | null>(null);
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal<string | null>(null);
  const [now, setNow] = createSignal(Date.now() / 1000);
  const [sessionRate, setSessionRate] = createSignal<number | null>(null);

  const refreshStatus = async () => {
    try {
      const decoders = await api.listReceiverDecoders(props.receiverId);
      setAttachedName(
        decoders.find((d) => adsbDecoderNames.includes(d.name))?.name ?? null,
      );
      setError(null);
    } catch (e) {
      // Receiver may be gone (tab outliving a destroy) — just note it.
      setError(e instanceof ApiError ? e.detail : 'status unavailable');
    }
  };

  const toggle = async () => {
    setBusy(true);
    setError(null);
    try {
      const current = attachedName();
      if (current !== null) {
        await api.detachDecoder(props.receiverId, current);
        setAttachedName(null);
        setState(initialAircraftState());
      } else {
        await api.attachDecoder(props.receiverId, DEFAULT_DECODER);
        setAttachedName(DEFAULT_DECODER);
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  onMount(() => {
    const session = receiverRegistry.getOrCreate(props.receiverId);
    const unsub = session.decoderStream.subscribe((envelope) => {
      setState((prev) => applyDecoderEvent(prev, envelope));
    });
    const unsubMeta = session.metadataStream.subscribe((meta) => {
      setSessionRate(meta.source?.sampleRate ?? null);
    });
    // Local fallback when metadata hasn't arrived yet.
    setSessionRate(session.getCurrentMetadata()?.source?.sampleRate ?? null);

    void refreshStatus();
    const statusTimer = setInterval(() => void refreshStatus(), 10_000);
    const ageTimer = setInterval(() => setNow(Date.now() / 1000), 1000);
    onCleanup(() => {
      unsub();
      unsubMeta();
      clearInterval(statusTimer);
      clearInterval(ageTimer);
    });
  });

  const rateOk = () => sessionRate() === null || sessionRate() === 2_000_000;

  return (
    <div class="flex h-full w-full flex-col bg-base-950 font-mono text-[11px]">
      {/* header: attach control + counters */}
      <div class="flex shrink-0 items-center justify-between border-b border-base-800 px-2 py-1">
        <div class="flex items-center gap-2 text-[10px] text-base-400">
          <span class="text-base-300">ADS-B</span>
          <Show when={attachedName() === 'dump1090'}>
            <span class="rounded bg-cyan-450/15 px-1 text-cyan-450">dump1090</span>
          </Show>
          <Show when={state().rows.length > 0}>
            <span class="rounded bg-cyan-450/15 px-1 text-cyan-450">
              {state().rows.length} aircraft
            </span>
          </Show>
          <Show when={state().frameCount > 0}>
            <span class="rounded bg-base-800 px-1">{state().frameCount} frames</span>
          </Show>
        </div>
        <Show
          when={attachedName() !== null}
          fallback={<span class="text-[10px] text-base-500">…</span>}
        >
          <button
            type="button"
            disabled={busy()}
            class={`rounded px-1.5 py-0.5 text-[10px] ${
              attachedName()
                ? 'bg-rose-450/15 text-rose-450 hover:bg-rose-450/25'
                : 'bg-amber-450/20 text-amber-450 hover:bg-amber-450/30'
            } disabled:opacity-50`}
            onClick={() => void toggle()}
            title={
              attachedName()
                ? `Detach the ${attachedName()} decoder from this receiver`
                : 'Attach the bundled Mode S decoder (needs 2 MSPS IQ at 1090 MHz)'
            }
          >
            {attachedName() ? 'detach' : '+ attach decoder'}
          </button>
        </Show>
      </div>

      {/* error line */}
      <Show when={error()}>
        <div class="shrink-0 bg-rose-450/10 px-2 py-0.5 text-[10px] text-rose-350">
          {error()}
        </div>
      </Show>

      {/* subprocess decoder lifecycle (restarts / terminal failure) */}
      <Show when={state().decoderState}>
        <div
          class={`shrink-0 px-2 py-0.5 text-[10px] ${
            state().decoderState?.state === 'failed'
              ? 'bg-rose-450/10 text-rose-350'
              : 'bg-amber-450/10 text-amber-450'
          }`}
        >
          decoder {state().decoderState?.state}
          <Show when={state().decoderState?.reason}> — {state().decoderState?.reason}</Show>
        </div>
      </Show>

      {/* body */}
      <Show
        when={attachedName()}
        fallback={
          <div class="flex flex-1 flex-col items-center justify-center gap-2 px-4 text-center text-[10px] text-base-400">
            <Show
              when={rateOk()}
              fallback={
                <span class="text-amber-450">
                  receiver runs at {formatHz(sessionRate() ?? 0)} — ADS-B needs 2 MSPS IQ
                  (spawn a file/adsb_1090 receiver or an RTL-SDR at 1090 MHz)
                </span>
              }
            >
              <span>no decoder attached</span>
            </Show>
            <button
              type="button"
              disabled={busy()}
              class="rounded bg-amber-450/20 px-2 py-1 text-[10px] text-amber-450 hover:bg-amber-450/30 disabled:opacity-50"
              onClick={() => void toggle()}
            >
              + attach ADS-B decoder
            </button>
          </div>
        }
      >
        <Show
          when={state().rows.length > 0}
          fallback={
            <div class="flex flex-1 items-center justify-center text-[10px] text-base-500">
              listening for Mode S frames…
            </div>
          }
        >
          <div class="min-h-0 flex-1 overflow-auto">
            <table class="w-full table-fixed border-collapse">
              <thead class="sticky top-0 bg-base-900 text-[10px] text-base-400">
                <tr>
                  <th class="px-2 py-1 text-left font-semibold">ICAO</th>
                  <th class="px-2 py-1 text-left font-semibold">Callsign</th>
                  <th class="px-2 py-1 text-right font-semibold">Altitude</th>
                  <Show when={state().positionCount > 0}>
                    <th class="px-2 py-1 text-right font-semibold">Position</th>
                  </Show>
                  <th class="px-2 py-1 text-right font-semibold">Frames</th>
                  <th class="px-2 py-1 text-right font-semibold">RSSI</th>
                  <th class="px-2 py-1 text-right font-semibold">Age</th>
                </tr>
              </thead>
              <tbody>
                <For each={state().rows}>
                  {(row) => (
                    <tr class="border-t border-base-850 hover:bg-base-900/60">
                      <td class="px-2 py-0.5 text-cyan-450">{row.icao}</td>
                      <td class="px-2 py-0.5 text-base-100">{row.callsign ?? '—'}</td>
                      <td class="px-2 py-0.5 text-right text-base-200">
                        {formatAltitude(row.altitude_ft)}
                      </td>
                      <Show when={state().positionCount > 0}>
                        <td class="px-2 py-0.5 text-right text-base-300">
                          {formatPosition(row)}
                        </td>
                      </Show>
                      <td class="px-2 py-0.5 text-right text-base-300">{row.frames}</td>
                      <td class="px-2 py-0.5 text-right text-base-300">
                        {row.rssi_dbfs.toFixed(1)}
                      </td>
                      <td class="px-2 py-0.5 text-right text-base-400">
                        {formatAge(now(), row.last_seen)}
                      </td>
                    </tr>
                  )}
                </For>
              </tbody>
            </table>
          </div>
        </Show>

        {/* message feed tail */}
        <Show when={state().lastFrame}>
          <div class="shrink-0 border-t border-base-800 px-2 py-0.5 text-[10px] text-base-500">
            last: {state().lastFrame}
          </div>
        </Show>
      </Show>
    </div>
  );
}

registerViz({
  type: 'aircraft-list',
  displayName: 'ADS-B Aircraft',
  icon: 'plane',
  defaultWidth: 360,
  defaultHeight: 200,
  live: true,
  component: AircraftListViz,
});

export default AircraftListViz;
