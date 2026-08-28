/**
 * Tests for the AudioPlayer state machine (slice-24).
 *
 * Verifies:
 *   - Initial state: muted, no audio context, denoise disabled.
 *   - enable() creates an AudioContext and unmutes.
 *   - enqueue() is a no-op until enable() is called.
 *   - toggleMute() transitions correctly.
 *   - enableClientDenoise() returns false when the WASM module isn't
 *     deployed (the slice-19 loader returns null — see RNNoiseLoader.test.ts).
 *   - disableClientDenoise() is idempotent and a no-op when not enabled.
 *   - disable() tears down the AudioContext and resets all state.
 *
 * The browser AudioContext is mocked with a minimal stub that records
 * method calls so the tests can assert on the state machine without
 * needing real audio hardware or a real AudioWorklet.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createAudioPlayer } from './AudioPlayer';

// Minimal mock of the Web Audio API surface used by AudioPlayer.
// We capture calls so tests can assert on them.
interface MockGainNode {
  gain: { value: number };
  connect: ReturnType<typeof vi.fn>;
  disconnect: ReturnType<typeof vi.fn>;
}
interface MockBufferSource {
  buffer: unknown;
  connect: ReturnType<typeof vi.fn>;
  start: ReturnType<typeof vi.fn>;
  disconnect: ReturnType<typeof vi.fn>;
}
interface MockAudioContext {
  state: 'suspended' | 'running';
  currentTime: number;
  destination: unknown;
  createGain: ReturnType<typeof vi.fn>;
  createBuffer: ReturnType<typeof vi.fn>;
  createBufferSource: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
  resume: ReturnType<typeof vi.fn>;
  audioWorklet?: { addModule: ReturnType<typeof vi.fn> };
}

function createMockAudioContext(): MockAudioContext {
  const mockGain: MockGainNode = {
    gain: { value: 0 },
    connect: vi.fn(),
    disconnect: vi.fn(),
  };
  const mockBufferSource: MockBufferSource = {
    buffer: null,
    connect: vi.fn(),
    start: vi.fn(),
    disconnect: vi.fn(),
  };
  return {
    state: 'running',
    currentTime: 0,
    destination: {},
    createGain: vi.fn(() => mockGain),
    createBuffer: vi.fn(() => ({
      copyToChannel: vi.fn(),
      duration: 0.1,
    })),
    createBufferSource: vi.fn(() => mockBufferSource),
    close: vi.fn().mockResolvedValue(undefined),
    resume: vi.fn().mockResolvedValue(undefined),
    audioWorklet: { addModule: vi.fn().mockResolvedValue(undefined) },
  };
}

// Stubs for `window.AudioContext` and `window.webkitAudioContext`.
// Vitest's node environment doesn't have a `window` global, so we
// stub it explicitly via vi.stubGlobal before each test that needs it.
function installMockAudioContext(mock: MockAudioContext): void {
  const Ctor = vi.fn(() => mock) as unknown as typeof AudioContext;
  vi.stubGlobal('AudioContext', Ctor);
  vi.stubGlobal('webkitAudioContext', Ctor);
}

describe('AudioPlayer initial state', () => {
  let player: ReturnType<typeof createAudioPlayer>;

  beforeEach(() => {
    player = createAudioPlayer();
  });

  it('starts muted with volume 0.5', () => {
    expect(player.muted()).toBe(true);
    expect(player.volume()).toBe(0.5);
  });

  it('starts with client denoise disabled', () => {
    expect(player.clientDenoiseEnabled()).toBe(false);
  });

  it('enqueue() is a no-op when muted (no AudioContext)', () => {
    const samples = new Int16Array(128);
    expect(() => player.enqueue(samples, 48000)).not.toThrow();
  });

  it('setVolume updates the signal but does not unmute', () => {
    player.setVolume(0.8);
    expect(player.volume()).toBe(0.8);
    expect(player.muted()).toBe(true);
  });

  it('toggleMute() when muted calls enable() (unmutes)', async () => {
    const mock = createMockAudioContext();
    installMockAudioContext(mock);

    player.toggleMute(); // muted → calls enable() asynchronously
    // Drain the microtask queue so enable() resolves.
    await vi.waitFor(() => expect(player.muted()).toBe(false));

    expect(player.muted()).toBe(false);
  });
});

describe('AudioPlayer enable/disable', () => {
  let player: ReturnType<typeof createAudioPlayer>;
  let mock: MockAudioContext;

  beforeEach(() => {
    mock = createMockAudioContext();
    installMockAudioContext(mock);
    player = createAudioPlayer();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('enable() creates an AudioContext and unmutes', async () => {
    await player.enable();
    expect(player.muted()).toBe(false);
    expect(mock.createGain).toHaveBeenCalled();
  });

  it('enable() when already enabled just unmutes (no new AudioContext)', async () => {
    await player.enable();
    const callCountAfterFirst = mock.createGain.mock.calls.length;
    await player.enable();
    expect(mock.createGain.mock.calls.length).toBe(callCountAfterFirst);
  });

  it('disable() closes the AudioContext and mutes', async () => {
    await player.enable();
    expect(player.muted()).toBe(false);
    player.disable();
    expect(player.muted()).toBe(true);
    expect(mock.close).toHaveBeenCalled();
  });

  it('enqueue() after enable() schedules a BufferSource', async () => {
    await player.enable();
    const samples = new Int16Array(1024);
    samples.fill(16384); // ~half-scale sine-like sample
    player.enqueue(samples, 48000);
    expect(mock.createBuffer).toHaveBeenCalled();
    expect(mock.createBufferSource).toHaveBeenCalled();
  });
});

describe('AudioPlayer client-side denoise (slice-24)', () => {
  let player: ReturnType<typeof createAudioPlayer>;
  let mock: MockAudioContext;

  beforeEach(() => {
    mock = createMockAudioContext();
    installMockAudioContext(mock);
    player = createAudioPlayer();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('enableClientDenoise() returns false before enable() (no AudioContext)', async () => {
    const ok = await player.enableClientDenoise();
    expect(ok).toBe(false);
    expect(player.clientDenoiseEnabled()).toBe(false);
  });

  it('enableClientDenoise() returns false when WASM module is not deployed', async () => {
    await player.enable();
    // The slice-19 loader probes /pkg/rnnoise_wasm.js — in the test env
    // it 404s, so loadRNNoiseModule() returns null.
    const ok = await player.enableClientDenoise();
    expect(ok).toBe(false);
    expect(player.clientDenoiseEnabled()).toBe(false);
  });

  it('disableClientDenoise() is a no-op when not enabled', () => {
    expect(() => player.disableClientDenoise()).not.toThrow();
    expect(player.clientDenoiseEnabled()).toBe(false);
  });

  it('disable() resets denoise state', async () => {
    await player.enable();
    player.disable();
    expect(player.clientDenoiseEnabled()).toBe(false);
  });
});

describe('AudioPlayer volume control', () => {
  let player: ReturnType<typeof createAudioPlayer>;
  let mock: MockAudioContext;

  beforeEach(() => {
    mock = createMockAudioContext();
    installMockAudioContext(mock);
    player = createAudioPlayer();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('setVolume updates the gain node value after enable()', async () => {
    await player.enable();
    const gainNode = mock.createGain() as MockGainNode;
    player.setVolume(0.75);
    expect(player.volume()).toBe(0.75);
    expect(gainNode.gain.value).toBe(0.75);
  });

  it('toggleMute() second call re-mutes', async () => {
    await player.enable();
    expect(player.muted()).toBe(false);
    player.toggleMute();
    expect(player.muted()).toBe(true);
  });
});
