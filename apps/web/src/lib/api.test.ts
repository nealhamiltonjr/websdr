/** Unit tests for the REST client's pure helpers (vitest).
 *
 *  Covers the deep-link parser (the client side of the ADR-006 federation
 *  URL convention) and the Hz formatter.
 */
// @vitest-environment node

import { describe, expect, it } from 'vitest';
import { formatHz, parseRemoteUrl } from './api';

describe('parseRemoteUrl — OpenWebRX deep links', () => {
  it('parses the canonical boomerthedog example end to end', () => {
    const p = parseRemoteUrl('http://boomerthedog.com:8073/#freq=3570000,mod=lsb,sql=-150');
    expect(p).not.toBeNull();
    expect(p!.sourceType).toBe('openwebrx_remote');
    expect(p!.sourceKwargs).toEqual({ url: 'http://boomerthedog.com:8073/#freq=3570000,mod=lsb,sql=-150' });
    expect(p!.freqHz).toBe(3_570_000);
    expect(p!.mod).toBe('lsb');
  });

  it('accepts bare host:port without a scheme', () => {
    const p = parseRemoteUrl('boomerthedog.com:8073/#freq=3570000,mod=lsb');
    expect(p).not.toBeNull();
    expect(p!.freqHz).toBe(3_570_000);
    expect(p!.mod).toBe('lsb');
  });

  it('accepts ws:// and wss:// URLs', () => {
    expect(parseRemoteUrl('ws://rx.example.com:8073/'))?.not.toBeNull();
    expect(parseRemoteUrl('wss://rx.example.com/'))?.not.toBeNull();
  });

  it('returns null for empty and garbage input', () => {
    expect(parseRemoteUrl('')).toBeNull();
    expect(parseRemoteUrl('   ')).toBeNull();
    expect(parseRemoteUrl('http://')).toBeNull();
    expect(parseRemoteUrl('not a url at all //')).toBeNull();
  });

  it('tolerates a hash without freq/mod and non-numeric freq', () => {
    const p = parseRemoteUrl('http://rx.example.com:8073/#unk=1');
    expect(p!.freqHz).toBeNull();
    expect(p!.mod).toBeNull();
    const q = parseRemoteUrl('http://rx.example.com/#freq=abc,mod=usb');
    expect(q!.freqHz).toBeNull();
    expect(q!.mod).toBe('usb');
  });

  it('keeps the full deep-link URL verbatim in kwargs', () => {
    const url = 'https://rx.example.org:8443/#freq=14074000,mod=usb,sql=-80';
    expect(parseRemoteUrl(url)!.sourceKwargs).toEqual({ url });
  });
});

describe('parseRemoteUrl — KiwiSDR', () => {
  it('splits host/port for kiwi sources (no deep-link convention)', () => {
    const p = parseRemoteUrl('http://rx.example.kiwisdr.com:8073/', 'kiwi');
    expect(p!.sourceType).toBe('kiwi');
    expect(p!.sourceKwargs).toEqual({ host: 'rx.example.kiwisdr.com', port: 8073 });
    expect(p!.freqHz).toBeNull();
    expect(p!.mod).toBeNull();
  });

  it('defaults kiwi port to 8073 when the URL has none', () => {
    const p = parseRemoteUrl('http://rx.example.kiwisdr.com/', 'kiwi');
    expect(p!.sourceKwargs).toEqual({ host: 'rx.example.kiwisdr.com', port: 8073 });
  });

  it('maps https to 443', () => {
    const p = parseRemoteUrl('https://rx.example.kiwisdr.com/', 'kiwi');
    expect(p!.sourceKwargs).toEqual({ host: 'rx.example.kiwisdr.com', port: 443 });
  });

  it('accepts a bare kiwi hostname', () => {
    const p = parseRemoteUrl('rx.example.kiwisdr.com', 'kiwi');
    expect(p!.sourceKwargs).toEqual({ host: 'rx.example.kiwisdr.com', port: 8073 });
  });
});

describe('formatHz', () => {
  it('formats across the decades', () => {
    expect(formatHz(3_570_000)).toBe('3.5700 MHz');
    expect(formatHz(14_205_000)).toBe('14.2050 MHz');
    expect(formatHz(1_090_000_000)).toBe('1.090 GHz');
    expect(formatHz(12_500)).toBe('12.5 kHz');
    expect(formatHz(500)).toBe('500 Hz');
  });
});
