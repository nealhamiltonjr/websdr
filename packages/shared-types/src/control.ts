/** Client → server control messages (text frames over the same WS as FFT).
 *
 *  The SharedWorker serializes these as JSON; the backend's ws.py parses them.
 */

import type { ReceiverMode, DSPMode } from './receiver.js';

export interface SubscribeMessage {
  type: 'subscribe';
  receiverId: string;
}

export interface UnsubscribeMessage {
  type: 'unsubscribe';
  receiverId: string;
}

export interface SetFrequencyMessage {
  type: 'control';
  receiverId: string;
  command: 'setFrequency';
  value: number;  // Hz
}

export interface SetModeMessage {
  type: 'control';
  receiverId: string;
  command: 'setMode';
  value: ReceiverMode;
}

export interface SetGainMessage {
  type: 'control';
  receiverId: string;
  command: 'setGain';
  value: number | 'auto';  // dB, or 'auto' for AGC
}

export interface SetDSPModeMessage {
  type: 'control';
  receiverId: string;
  command: 'setDSPMode';
  value: DSPMode;
}

export type ClientToServerMessage =
  | SubscribeMessage
  | UnsubscribeMessage
  | SetFrequencyMessage
  | SetModeMessage
  | SetGainMessage
  | SetDSPModeMessage;
