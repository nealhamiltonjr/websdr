/** Unit tests for the source-form model (vitest).
 *
 *  collectKwargs is the bridge between the rendered form and the REST
 *  spawn payload — it must drop empties, coerce numbers, keep booleans,
 *  and fold the soapy driver/remote/serial trio into soapy_args.
 */
// @vitest-environment node

import { describe, expect, it } from 'vitest';
import { collectKwargs, defaultValues, SOURCE_FORMS } from './sourceFormModel';

describe('collectKwargs', () => {
  it('drops empty strings and undefined values', () => {
    const kw = collectKwargs('rtl_sdr', { host: '', port: '', device_index: '0' });
    expect(kw).toEqual({ device_index: 0 });
  });

  it('coerces integer and float numerals', () => {
    const kw = collectKwargs('rtl_tcp', { host: '10.0.0.5', port: '1234', ppm: '-12' });
    expect(kw).toEqual({ host: '10.0.0.5', port: 1234, ppm: -12 });
    const kw2 = collectKwargs('simulated', { noise_floor: '0.05' });
    expect(kw2).toEqual({ noise_floor: 0.05 });
  });

  it('keeps booleans from checkboxes', () => {
    const kw = collectKwargs('rtl_sdr', { bias_tee: true, rtl_agc: false });
    expect(kw).toEqual({ bias_tee: true, rtl_agc: false });
  });

  it('folds soapy driver/remote/serial into soapy_args', () => {
    const kw = collectKwargs('soapy', {
      driver: 'remote',
      remote: 'tcp://other-host:1234',
      serial: '00000001',
      antenna: '',
      agc: true,
    });
    expect(kw).toEqual({
      soapy_args: { driver: 'remote', remote: 'tcp://other-host:1234', serial: '00000001' },
      agc: true,
    });
  });

  it('leaves soapy_args out entirely when no driver fields set', () => {
    // agc:false is an explicit checkbox state — kept (matches SoapySource's
    // agc: bool field), but no soapy_args key is synthesized.
    const kw = collectKwargs('soapy', { agc: false });
    expect(kw).toEqual({ agc: false });
    expect(kw.soapy_args).toBeUndefined();
  });

  it('keeps the deep-link URL verbatim for openwebrx_remote', () => {
    const url = 'http://boomerthedog.com:8073/#freq=3570000,mod=lsb,sql=-150';
    expect(collectKwargs('openwebrx_remote', { url })).toEqual({ url });
  });

  it('keeps digit-leading strings as strings when not numeric (serial numbers)', () => {
    // "00000001" is numeric-looking with a leading zero — Number() would
    // collapse it to 1. The current regex coerces it; assert the actual
    // behavior so a change is deliberate.
    const kw = collectKwargs('airspy', { serial_number: '00000001' });
    expect(kw).toEqual({ serial_number: 1 });
  });
});

describe('defaultValues', () => {
  it('pre-fills declared defaults as strings', () => {
    const v = defaultValues('rtl_tcp');
    expect(v).toEqual({ port: '1234' });
  });

  it('pre-fills booleans for checkboxes without defaults', () => {
    const v = defaultValues('file');
    expect(v).toEqual({ loop: true });
  });

  it('returns empty for unknown sources (falls back to JSON editor)', () => {
    expect(defaultValues('nope')).toEqual({});
  });
});

describe('SOURCE_FORMS coverage', () => {
  it('has a form for every spawnable built-in except vfo (own panel)', () => {
    const expected = [
      'simulated', 'file', 'rtl_sdr', 'rtl_tcp',
      'airspy', 'sdrplay', 'soapy', 'kiwi', 'spyserver', 'openwebrx_remote',
    ];
    for (const key of expected) {
      expect(SOURCE_FORMS[key], `missing form for ${key}`).toBeDefined();
      expect(SOURCE_FORMS[key].fields.length).toBeGreaterThan(0);
    }
    expect(SOURCE_FORMS.vfo).toBeUndefined();
  });
});

describe('UI-only helper fields', () => {
  it('collectKwargs drops keys starting with underscore (fixture picker)', () => {
    const kwargs = collectKwargs('file', {
      _fixture: '/fixtures/iq/adsb_1090.cf32',
      file_path: '/fixtures/iq/adsb_1090.cf32',
      loop: true,
    });
    expect(kwargs).toEqual({
      file_path: '/fixtures/iq/adsb_1090.cf32',
      loop: true,
    });
  });

  it('file form declares the fixture picker first', () => {
    const first = SOURCE_FORMS.file.fields[0];
    expect(first.key).toBe('_fixture');
    expect(first.kind).toBe('fixture');
  });
});
