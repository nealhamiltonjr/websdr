/** RemoteBrowser — browse + connect to public remote receivers (ADR-006).
 *
 *  Two directories:
 *    - receiverbook.de → public OpenWebRX(+) receivers → spawns
 *      source_type="openwebrx_remote" with the entry's URL (deep links work)
 *    - rx.kiwisdr.com  → public KiwiSDRs (0–30 MHz) → spawns
 *      source_type="kiwi" with host/port parsed from the entry URL
 *
 *  503 (offline + no stale cache) degrades to an inline notice — this box
 *  has restricted egress, and the failure path is a real user path too.
 */

import { createResource, createSignal, For, Show, type JSX } from 'solid-js';
import { api, type RemoteReceiver } from '../lib/api';

export interface RemoteBrowserProps {
  /** Spawn a receiver for the chosen remote entry. */
  onConnect: (sourceType: string, sourceKwargs: Record<string, unknown>) => void;
  /** True while the spawn POST is in flight (disables buttons). */
  connecting?: boolean;
}

type Provider = 'receiverbook' | 'kiwi';

const PROVIDER_META: Record<Provider, { title: string; hint: string }> = {
  receiverbook: {
    title: 'Receiverbook',
    hint: 'public OpenWebRX / OpenWebRX+ receivers',
  },
  kiwi: { title: 'KiwiSDR', hint: 'rx.kiwisdr.com — HF 0–30 MHz' },
};

/** Extract host[:port] from an entry URL for sources that want them split. */
function hostPortOf(url: string): { host: string; port: number } {
  try {
    const u = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(url) ? url : `http://${url}`);
    return { host: u.hostname, port: u.port ? Number(u.port) : 8073 };
  } catch {
    return { host: url, port: 8073 };
  }
}

export function RemoteBrowser(props: RemoteBrowserProps): JSX.Element {
  const [provider, setProvider] = createSignal<Provider>('receiverbook');
  const [query, setQuery] = createSignal('');
  const [spinning, setSpinning] = createSignal<string | null>(null);

  const [directory, { refetch }] = createResource(
    () => provider(),
    (p) => api.fetchDirectory(p),
  );

  const filtered = (): RemoteReceiver[] => {
    const rows = directory.latest?.receivers ?? directory()?.receivers ?? [];
    const q = query().trim().toLowerCase();
    if (!q) return rows;
    return rows.filter(
      (r) =>
        r.name.toLowerCase().includes(q) ||
        r.url.toLowerCase().includes(q) ||
        String(r.extra?.bands ?? '').toLowerCase().includes(q) ||
        String(r.extra?.country ?? '').toLowerCase().includes(q) ||
        String(r.extra?.mode ?? '').toLowerCase().includes(q),
    );
  };

  const connect = (entry: RemoteReceiver) => {
    setSpinning(entry.id);
    if (entry.source_type === 'kiwi') {
      const { host, port } = hostPortOf(entry.url);
      props.onConnect('kiwi', { host, port });
    } else {
      // openwebrx_remote takes the URL verbatim (deep links included).
      props.onConnect('openwebrx_remote', { url: entry.url });
    }
    // Optimistically clear the spinner when the parent takes over — the
    // modal closes on success, so a short timeout is enough.
    setTimeout(() => setSpinning(null), 4000);
  };

  return (
    <div class="flex h-full min-h-0 flex-col gap-2">
      {/* Provider tabs + search */}
      <div class="flex items-center gap-2">
        <div class="flex overflow-hidden rounded border border-base-700">
          <For each={Object.keys(PROVIDER_META) as Provider[]}>
            {(p) => (
              <button
                type="button"
                class={`px-2.5 py-1 font-mono text-[11px] ${
                  provider() === p
                    ? 'bg-cyan-450/20 text-cyan-450'
                    : 'bg-base-850 text-base-300 hover:bg-base-800'
                }`}
                onClick={() => setProvider(p)}
              >
                {PROVIDER_META[p].title}
              </button>
            )}
          </For>
        </div>
        <input
          type="text"
          class="min-w-0 flex-1 rounded border border-base-700 bg-base-950 px-2 py-1 font-mono text-xs text-base-100 placeholder:text-base-500 focus:border-cyan-450 focus:outline-none"
          placeholder="search callsign, city, band…"
          value={query()}
          onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
        />
        <button
          type="button"
          class="rounded bg-base-800 px-2 py-1 font-mono text-[11px] text-base-300 hover:bg-base-700"
          onClick={() => void refetch()}
          title="Bypass the 5-minute cache"
        >
          ↻ refresh
        </button>
      </div>
      <div class="font-mono text-[10px] text-base-500">
        {PROVIDER_META[provider()].hint}
      </div>

      {/* List */}
      <div class="min-h-0 flex-1 overflow-auto rounded border border-base-800 bg-base-950">
        <Show
          when={!directory.loading}
          fallback={
            <div class="flex h-full items-center justify-center font-mono text-xs text-base-400">
              loading directory…
            </div>
          }
        >
          <Show
            when={!directory.error}
            fallback={
              <div class="flex h-full flex-col items-center justify-center gap-2 p-4 text-center">
                <span class="font-mono text-xs text-rose-450">directory unreachable</span>
                <span class="max-w-md font-mono text-[10px] text-base-500">
                  {(directory.error as Error | undefined)?.message ??
                    'No cached copy — check network egress and retry.'}
                </span>
                <button
                  type="button"
                  class="rounded bg-base-800 px-2 py-1 font-mono text-[11px] text-base-200 hover:bg-base-700"
                  onClick={() => void refetch()}
                >
                  retry
                </button>
              </div>
            }
          >
            <div class="divide-y divide-base-800">
              <For each={filtered()}>
                {(entry) => (
                  <button
                    type="button"
                    class="flex w-full items-center gap-3 px-3 py-1.5 text-left hover:bg-base-900 disabled:opacity-50"
                    onClick={() => connect(entry)}
                    disabled={props.connecting || spinning() === entry.id}
                    title={`Connect to ${entry.name}`}
                  >
                    <span
                      class={`h-1.5 w-1.5 shrink-0 rounded-full ${
                        entry.online ? 'bg-emerald-400' : 'bg-base-600'
                      }`}
                    />
                    <span class="min-w-0 flex-1">
                      <span class="block truncate font-mono text-xs text-base-100">
                        {entry.name || entry.id}
                      </span>
                      <span class="block truncate font-mono text-[10px] text-base-500">
                        {entry.url}
                        <Show when={entry.extra?.bands}>
                          {' · '}
                          {String(entry.extra?.bands)}
                        </Show>
                        <Show when={entry.extra?.country}>
                          {' · '}
                          {String(entry.extra?.country)}
                        </Show>
                      </span>
                    </span>
                    <Show when={entry.users}>
                      <span class="shrink-0 font-mono text-[10px] text-base-400">
                        {entry.users} users
                      </span>
                    </Show>
                    <span class="shrink-0 rounded bg-cyan-450/10 px-1.5 py-0.5 font-mono text-[10px] text-cyan-450">
                      {spinning() === entry.id ? 'connecting…' : 'connect'}
                    </span>
                  </button>
                )}
              </For>
              <Show when={filtered().length === 0}>
                <div class="px-3 py-6 text-center font-mono text-xs text-base-500">
                  no receivers match “{query()}”
                </div>
              </Show>
            </div>
          </Show>
        </Show>
      </div>
      <div class="font-mono text-[10px] text-base-500">
        Etiquette: one connection per receiver, honest client identification,
        no reconnect storms (ADR-006).
      </div>
    </div>
  );
}
