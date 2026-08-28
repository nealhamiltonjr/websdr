/** Receiver identity — what a ReceiverSession looks like from the client's perspective.
 *
 *  These mirror the server-side `openwebrx_plus.sessions.ReceiverSession` fields.
 */

export type SourceType =
  | 'rtl_sdr'
  | 'rtl_tcp'
  | 'sdrplay'
  | 'hackrf'
  | 'airspy'
  | 'airspyhf'
  | 'kiwi'
  | 'spyserver'
  | 'sdrangel'
  | 'soxy'
  | 'websdr';

export type ReceiverMode =
  | 'USB' | 'LSB' | 'AM' | 'SAM' | 'FM' | 'NBFM' | 'WBFM'
  | 'CW' | 'FreeDV'
  | 'RTTY' | 'PSK31' | 'PSK63' | 'Olivia'
  | 'FT8' | 'JT65' | 'JT9' | 'WSPR' | 'Q65'
  | 'SSTV' | 'FAX' | 'Packet'
  | 'DAB' | 'ADS-B' | 'UAT' | 'AIS' | 'ATC' | 'ACARS';

export interface SourceInfo {
  type: SourceType;
  label: string;
  /** Sample rate the source captures at, Hz (e.g. 2400000 for RTL-SDR). */
  sampleRate: number;
  /** For federated sources, the URL/hostname. */
  endpoint?: string;
  /** The source's advertised manual-gain range in dB (slice-4.7 metadata
   *  echo; null = no advertised range). */
  gainRange?: [number, number] | null;
  /** Whether "auto" gain is a real AGC on this source (vs. unit gain). */
  supportsAgc?: boolean;
}

export interface ReceiverMetadata {
  receiverId: string;
  /** Tuned frequency, Hz. */
  frequency: number;
  mode: ReceiverMode;
  source: SourceInfo;
  /** Set when this receiver is a VFO of a wideband source. */
  parentVfoId?: string;
  /** Manual gain in dB, or null = auto/AGC (slice-4.7 control echo). */
  gain?: number | null;
  /** Server-side DSP mode of the audio chain (ADR-002, slice-4.7). */
  dspMode?: DSPMode;
}

/** Four DSP+AI cascade modes. See ADR-002. */
export type DSPMode = 'raw' | 'classic' | 'ai' | 'cascade';
