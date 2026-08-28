/** DebugPanel — the in-app Debugger modal (slice-5.1).
 *
 *  Two views:
 *    Logs    — every captured log event, filterable by level / logger /
 *              message substrings, paginated, with stats + export.
 *    Errors  — just warnings and above (a focused red-triangle list).
 *
 *  Polls GET /api/debug/logs (or /api/debug/errors) at a slow rate while
 *  open. The user can manually refresh or set auto-refresh (5s default).
 *  POST /api/debug/clear wipes the ring buffer; GET /api/debug/export
 *  downloads an NDJSON dump.
 *
 *  The level / logger / message filters compose — only events matching
 *  all three substrings are returned. Pagination is via offset+limit
 *  query params on the same endpoint.
 */

import {
  createEffect,
  createSignal,
  For,
  Show,
  type JSX,
  onCleanup,
} from 'solid-js';
import {
  api,
  type LogEntry,
  type DebugLogStats,
} from '../lib/api';

export interface DebugPanelProps {
  onClose: () => void;
}

type View = 'logs' | 'errors';

const LEVEL_COLORS: Record<string, string> = {
  debug: 'text-slate-400',
  info: 'text-sky-400',
  warning: 'text-amber-400',
  error: 'text-red-400',
  critical: 'text-red-500 font-bold',
};

const LEVELS = ['debug', 'info', 'warning', 'error', 'critical'] as const;
const PAGE_SIZE = 50;

export function DebugPanel(props: DebugPanelProps): JSX.Element {
  const [view, setView] = createSignal<View>('logs');
  const [level, setLevel] = createSignal<string>('');
  const [logger, setLogger] = createSignal<string>('');
  const [message, setMessage] = createSignal<string>('');
  const [offset, setOffset] = createSignal(0);
  const [entries, setEntries] = createSignal<LogEntry[]>([]);
  const [stats, setStats] = createSignal<DebugLogStats | null>(null);
  const [error, setError] = createSignal<string | null>(null);
  const [autoRefresh, setAutoRefresh] = createSignal(true);
  const [loading, setLoading] = createSignal(false);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const levelParam = view() === 'logs' && level() ? level() : undefined;
      const loggerParam = view() === 'logs' && logger() ? logger() : undefined;
      const messageParam =
        view() === 'logs' && message() ? message() : undefined;
      if (view() === 'logs') {
        const resp = await api.getDebugLogs({
          level: levelParam,
          logger: loggerParam,
          message: messageParam,
          limit: PAGE_SIZE,
          offset: offset(),
        });
        setEntries(resp.entries);
        setStats(resp.stats);
      } else {
        const resp = await api.getDebugErrors(PAGE_SIZE, offset());
        setEntries(resp.entries);
        setStats(resp.stats);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Refresh failed');
    } finally {
      setLoading(false);
    }
  }

  // Initial fetch + auto-refresh poll.
  createEffect(() => {
    // Re-fetch when view / filters / offset change.
    refresh();
  });

  let timer: ReturnType<typeof setInterval> | null = null;
  createEffect(() => {
    if (timer) clearInterval(timer);
    if (autoRefresh()) {
      timer = setInterval(refresh, 5000);
    }
  });

  onCleanup(() => {
    if (timer) clearInterval(timer);
  });

  async function handleClear() {
    if (!confirm('Clear all captured log entries?')) return;
    try {
      setError(null);
      await api.clearDebugLogs();
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Clear failed');
    }
  }

  async function handleExport() {
    try {
      setError(null);
      const blob = await api.exportDebugLogs();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `openwebrx-plus-logs-${new Date()
        .toISOString()
        .replace(/[:.]/g, '-')}.ndjson`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Export failed');
    }
  }

  function handleFilterReset() {
    setLevel('');
    setLogger('');
    setMessage('');
    setOffset(0);
  }

  const s = () => stats();

  return (
    <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div class="flex h-[80vh] w-full max-w-4xl flex-col rounded-lg border border-slate-700 bg-slate-900 shadow-2xl">
        {/* Header */}
        <div class="flex items-center justify-between border-b border-slate-700 px-6 py-3">
          <h2 class="text-lg font-semibold text-slate-100">Debugger</h2>
          <div class="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setAutoRefresh(!autoRefresh())}
              class="rounded border border-slate-600 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700"
            >
              Auto-refresh: {autoRefresh() ? 'ON' : 'OFF'}
            </button>
            <button
              type="button"
              onClick={refresh}
              disabled={loading()}
              class="rounded border border-slate-600 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700 disabled:opacity-50"
            >
              {loading() ? 'Refreshing…' : 'Refresh'}
            </button>
            <button
              type="button"
              onClick={handleExport}
              class="rounded border border-slate-600 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700"
            >
              Export NDJSON
            </button>
            <button
              type="button"
              onClick={handleClear}
              class="rounded border border-red-700 px-2 py-1 text-xs text-red-300 hover:bg-red-900"
            >
              Clear
            </button>
            <button
              type="button"
              onClick={props.onClose}
              class="rounded px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-100"
              aria-label="Close debugger"
            >
              ✕
            </button>
          </div>
        </div>

        {/* Stats bar */}
        <div class="flex flex-wrap gap-3 border-b border-slate-800 bg-slate-950/40 px-6 py-2 text-xs text-slate-400">
          <Show when={s()}>
            {(stats) => (
              <>
                <span>
                  captured: <span class="text-slate-200">{stats().total_captured}</span>
                </span>
                <span>
                  dropped: <span class="text-slate-200">{stats().total_dropped}</span>
                </span>
                <span>
                  buffer: <span class="text-slate-200">{stats().all_current}</span> /{' '}
                  {stats().all_capacity}
                </span>
                <span>
                  errors: <span class="text-slate-200">{stats().errors_current}</span> /{' '}
                  {stats().errors_capacity}
                </span>
                <span>·</span>
                <For each={LEVELS}>
                  {(lv) => (
                    <span>
                      {lv}:{' '}
                      <span class="text-slate-200">
                        {stats().counts_by_level[lv] ?? 0}
                      </span>
                    </span>
                  )}
                </For>
              </>
            )}
          </Show>
        </div>

        {/* View switch + filters */}
        <div class="flex flex-wrap items-center gap-2 border-b border-slate-800 px-6 py-2">
          <div class="flex rounded border border-slate-700">
            <button
              type="button"
              onClick={() => {
                setView('logs');
                setOffset(0);
              }}
              class={`px-3 py-1 text-xs ${
                view() === 'logs'
                  ? 'bg-slate-700 text-slate-100'
                  : 'text-slate-400 hover:bg-slate-800'
              }`}
            >
              Logs
            </button>
            <button
              type="button"
              onClick={() => {
                setView('errors');
                setOffset(0);
              }}
              class={`px-3 py-1 text-xs ${
                view() === 'errors'
                  ? 'bg-slate-700 text-slate-100'
                  : 'text-slate-400 hover:bg-slate-800'
              }`}
            >
              Errors only
            </button>
          </div>
          <Show when={view() === 'logs'}>
            <select
              class="rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
              value={level()}
              onChange={(e) => {
                setLevel(e.currentTarget.value);
                setOffset(0);
              }}
            >
              <option value="">All levels</option>
              <For each={LEVELS}>{(lv) => <option value={lv}>{lv}</option>}</For>
            </select>
            <input
              type="text"
              class="rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
              placeholder="logger substring"
              value={logger()}
              onInput={(e) => {
                setLogger(e.currentTarget.value);
                setOffset(0);
              }}
            />
            <input
              type="text"
              class="rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
              placeholder="message substring"
              value={message()}
              onInput={(e) => {
                setMessage(e.currentTarget.value);
                setOffset(0);
              }}
            />
            <button
              type="button"
              onClick={handleFilterReset}
              class="rounded border border-slate-600 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700"
            >
              Clear filters
            </button>
          </Show>
          <div class="ml-auto flex gap-2">
            <button
              type="button"
              onClick={() => setOffset(Math.max(0, offset() - PAGE_SIZE))}
              disabled={offset() === 0}
              class="rounded border border-slate-600 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700 disabled:opacity-30"
            >
              ← Prev
            </button>
            <span class="text-xs text-slate-400">
              offset {offset()}–{offset() + entries().length}
            </span>
            <button
              type="button"
              onClick={() => setOffset(offset() + PAGE_SIZE)}
              disabled={entries().length < PAGE_SIZE}
              class="rounded border border-slate-600 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700 disabled:opacity-30"
            >
              Next →
            </button>
          </div>
        </div>

        {/* Error banner */}
        <Show when={error()}>
          <div class="border-b border-red-900 bg-red-950/50 px-6 py-2 text-xs text-red-300">
            Error: {error()}
          </div>
        </Show>

        {/* Entry list */}
        <div class="flex-1 overflow-y-auto bg-slate-950 font-mono text-xs">
          <Show
            when={entries().length > 0}
            fallback={
              <div class="flex h-full items-center justify-center text-slate-500">
                No entries match the current filters.
              </div>
            }
          >
            <table class="w-full border-collapse">
              <thead class="sticky top-0 bg-slate-900 text-slate-400">
                <tr>
                  <th class="border-b border-slate-800 px-2 py-1 text-left">
                    Time
                  </th>
                  <th class="border-b border-slate-800 px-2 py-1 text-left">
                    Level
                  </th>
                  <th class="border-b border-slate-800 px-2 py-1 text-left">
                    Logger
                  </th>
                  <th class="border-b border-slate-800 px-2 py-1 text-left">
                    Message
                  </th>
                </tr>
              </thead>
              <tbody>
                <For each={entries()}>
                  {(entry) => (
                    <tr class="hover:bg-slate-900">
                      <td class="border-b border-slate-900 px-2 py-1 text-slate-500">
                        {entry.timestamp}
                      </td>
                      <td
                        class={`border-b border-slate-900 px-2 py-1 ${
                          LEVEL_COLORS[entry.level] ?? 'text-slate-300'
                        }`}
                      >
                        {entry.level}
                      </td>
                      <td class="border-b border-slate-900 px-2 py-1 text-slate-400">
                        {entry.logger}
                      </td>
                      <td class="border-b border-slate-900 px-2 py-1 text-slate-200">
                        {entry.message}
                        <Show when={Object.keys(entry.fields).length > 0}>
                          <span class="ml-2 text-slate-500">
                            {JSON.stringify(entry.fields)}
                          </span>
                        </Show>
                      </td>
                    </tr>
                  )}
                </For>
              </tbody>
            </table>
          </Show>
        </div>
      </div>
    </div>
  );
}
