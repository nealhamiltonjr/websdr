/** Rendering side of the source config forms (see sourceFormModel.ts). */

import { createSignal, For, onMount, Show, type JSX } from 'solid-js';
import { api, formatHz, type IqFixture } from '../lib/api';
import { SOURCE_FORMS } from './sourceFormModel';



export interface SourceConfigFormProps {
  sourceType: string;
  values: Record<string, string | boolean>;
  onChange: (key: string, value: string | boolean) => void;
}

const inputCls =
  'w-full rounded border border-base-700 bg-base-950 px-2 py-1 font-mono text-xs text-base-100 placeholder:text-base-500 focus:border-cyan-450 focus:outline-none';

// Baked-fixture list is fetched at most once per page (cached promise —
// the modal can be opened and closed repeatedly).
let fixturesCache: Promise<IqFixture[]> | null = null;
function fetchFixtures(): Promise<IqFixture[]> {
  fixturesCache ??= api.listFixtures().catch(() => []);
  return fixturesCache;
}

function fixtureLabel(f: IqFixture): string {
  const rate = f.sample_rate ? ` · ${formatHz(f.sample_rate)}` : '';
  const label = f.label ? ` — ${f.label}` : '';
  return `${f.name}${rate}${label}`;
}

function FixtureSelect(props: { onPick: (path: string) => void }): JSX.Element {
  const [fixtures, setFixtures] = createSignal<IqFixture[]>([]);
  onMount(() => {
    void fetchFixtures().then(setFixtures);
  });
  return (
    <select
      class={inputCls}
      onChange={(e) => props.onPick((e.target as HTMLSelectElement).value)}
    >
      <option value="">— pick a baked fixture —</option>
      <For each={fixtures()}>
        {(f) => <option value={f.path}>{fixtureLabel(f)}</option>}
      </For>
    </select>
  );
}

export function SourceConfigForm(props: SourceConfigFormProps): JSX.Element {
  const spec = SOURCE_FORMS[props.sourceType];
  return (
    <Show when={spec} fallback={<FreeFormKwargs onChange={props.onChange} />}>
      <div class="grid grid-cols-[130px_1fr] gap-x-3 gap-y-2">
        <For each={spec.fields}>
          {(f) => (
            <>
              <label class="flex items-center justify-end pr-1 text-right font-mono text-[11px] text-base-300">
                {f.label}
              </label>
              <div>
                <Show
                  when={f.kind !== 'checkbox' && f.kind !== 'fixture'}
                  fallback={
                    <Show
                      when={f.kind === 'fixture'}
                      fallback={
                        <input
                          type="checkbox"
                          checked={Boolean(props.values[f.key])}
                          onChange={(e) =>
                            props.onChange(f.key, (e.target as HTMLInputElement).checked)
                          }
                          class="mt-0.5 h-3.5 w-3.5 accent-cyan-450"
                        />
                      }
                    >
                      <FixtureSelect
                        onPick={(path) => path && props.onChange('file_path', path)}
                      />
                    </Show>
                  }
                >
                  <Show
                    when={f.kind === 'select'}
                    fallback={
                      <input
                        type={f.kind === 'number' ? 'number' : 'text'}
                        step="any"
                        class={inputCls}
                        placeholder={String(f.placeholder ?? '')}
                        value={String(props.values[f.key] ?? '')}
                        onInput={(e) =>
                          props.onChange(f.key, (e.target as HTMLInputElement).value)
                        }
                      />
                    }
                  >
                    <select
                      class={inputCls}
                      value={String(props.values[f.key] ?? '')}
                      onChange={(e) =>
                        props.onChange(f.key, (e.target as HTMLSelectElement).value)
                      }
                    >
                      <For each={f.options ?? []}>
                        {([value, label]) => <option value={value}>{label}</option>}
                      </For>
                    </select>
                  </Show>
                </Show>
                <Show when={f.hint}>
                  <div class="mt-0.5 font-mono text-[10px] text-base-500">{f.hint}</div>
                </Show>
              </div>
            </>
          )}
        </For>
      </div>
    </Show>
  );
}

/** Fallback for unknown/plugin sources: one JSON textarea → kwargs. */
function FreeFormKwargs(props: { onChange: (key: string, value: string | boolean) => void }): JSX.Element {
  const [text, setText] = createSignal('{}');
  const [err, setErr] = createSignal('');
  const parse = (t: string) => {
    try {
      const obj = JSON.parse(t) as Record<string, unknown>;
      setErr('');
      for (const [k, v] of Object.entries(obj)) props.onChange(k, String(v));
    } catch (e) {
      setErr(String(e));
    }
  };
  return (
    <div>
      <textarea
        class={`${inputCls} h-20`}
        value={text()}
        onInput={(e) => {
          setText((e.target as HTMLTextAreaElement).value);
          parse((e.target as HTMLTextAreaElement).value);
        }}
        spellcheck={false}
      />
      <Show when={err()}>
        <div class="mt-1 font-mono text-[10px] text-rose-450">{err()}</div>
      </Show>
      <div class="mt-1 font-mono text-[10px] text-base-500">
        JSON source_kwargs for this plugin source
      </div>
    </div>
  );
}
