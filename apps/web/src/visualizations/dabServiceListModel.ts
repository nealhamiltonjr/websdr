/** DabServiceListModel — pure-logic model for the DabServiceListViz (slice-56).
 *
 *  Subscribes to DAB decoder events and maintains a list of discovered
 *  services. Pure function from (state, event) → state.
 */

import type { DecoderEventEnvelope } from '@openwebrx-plus/shared-types';

export interface DabServiceRow {
  service_id: number;
  label: string;
  program_type: number;
  subchannel_id: number | null;
}

export interface DabServiceListState {
  services: DabServiceRow[];
  ensemble_index: number;
  last_update: number;
}

export function initialDabServiceListState(): DabServiceListState {
  return {
    services: [],
    ensemble_index: 0,
    last_update: 0,
  };
}

export function applyDecoderEvent(
  state: DabServiceListState,
  envelope: DecoderEventEnvelope,
): DabServiceListState {
  if (envelope.decoder !== 'dab') return state;
  const event = envelope.event as any;
  if (event.kind === 'service') {
    // Add or update the service by ID.
    const existing = state.services.findIndex(s => s.service_id === event.service_id);
    const row: DabServiceRow = {
      service_id: event.service_id,
      label: event.label,
      program_type: event.program_type,
      subchannel_id: event.subchannel_id,
    };
    if (existing >= 0) {
      const newServices = [...state.services];
      newServices[existing] = row;
      return { ...state, services: newServices, last_update: event.ts };
    }
    return { ...state, services: [...state.services, row], last_update: event.ts };
  }
  if (event.kind === 'ensemble') {
    return { ...state, ensemble_index: event.ensemble_index ?? state.ensemble_index, last_update: event.ts };
  }
  return state;
}

export function formatTime(ts: number): string {
  if (!ts) return '--:--:--';
  const d = new Date(ts * 1000);
  return d.toISOString().slice(11, 19) + 'Z';
}

export function formatAge(ts: number, now: number): string {
  if (!ts) return '--';
  const age = Math.max(0, Math.floor(now - ts));
  if (age < 60) return `${age}s`;
  if (age < 3600) return `${Math.floor(age / 60)}m`;
  return `${Math.floor(age / 3600)}h`;
}
