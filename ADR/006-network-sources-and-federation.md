# ADR-006: Network Sources & the Federation Gateway

**Status:** Accepted — implemented & tested (slice-3.5: rtl_tcp + KiwiSDR remote sources, directories; slice-3.6: OpenWebRX federation client; slice-4.8: SpyServer client)
**Date:** 2026-08-27
**Related:** ADR-004 (source plugin architecture), ADR-001 (workspace, federation pillar), ADR-005 (VFO/wideband)

## Context

Two needs converged on the same machinery:

1. **Dev without hardware.** The NUC dev box (and CI) has no attached SDR.
   Fixtures (ADR-005) give realistic *recorded* signals, but not *live* ones —
   no propagation, no QSB, no surprise. The user asked: *can we connect to
   public SDR receivers over the internet to test live signals in dev?*
2. **Pillar 3 — Federation.** The product vision includes connecting to (and
   eventually listing itself among) the world's public receivers. That needs
   remote-source clients and receiver *directories* regardless.

The public SDR landscape, as it actually exists:

| Tier | Protocol | What you get | Public ecosystem |
|------|----------|--------------|------------------|
| A — raw IQ over network | **rtl_tcp** / rsp_tcp | cu8 IQ, full rate | self-host (best), sparse volunteer servers |
| A | **SpyServer** (Airspy) | cs8/cs16 IQ + waterfall | many public instances |
| A | **SoapyRemote** | CF32 via SoapySDR | self-host clusters |
| B — channelized IQ | **KiwiSDR** websocket, `mod=IQ` | int16 IQ at the sound rate, 0–30 MHz | **1000+ public receivers** (rx.kiwisdr.com) |
| B | **OpenWebRX** ws protocol | FFT (ADPCM) + demodulated audio | **receiverbook.de registry** — now a *display-stream* source |
| C — directories only | rx.kiwisdr.com/json, receiverbook.de | metadata: name/URL/geo/users | discovery layer for A+B |

WebSDR is deliberately excluded: no public API, HTML-scraping only.

## Decision

### 1. Remote receivers are ordinary Sources

`RtlTcpSource` and `KiwiSdrSource` implement the exact Source contract every
local driver does (`spawn(center, rate, gain) → AsyncIterator[complex64]`).
The ReceiverSession, IqHub, VFO taps, pycsdr chains, and the frontend need
**zero special cases**: a Kiwi in New Zealand is interchangeable with the
RTL-SDR on the desk. Both register with `hardware_required=False` — the
hardware is on the other end of the wire.

The rtl_tcp wire protocol lives in **one place** (`sources/rtl_tcp.py`);
`RtlSdrSource`'s tcp transport delegates to it, so the standalone remote
source and the local auto-probe path can't drift apart.

**rtl_tcp** (`sources/rtl_tcp.py`): 12-byte `RTL0` handshake, `>BI`
commands (freq/rate/gain/agc/ppm/direct-sampling/bias-tee), cu8 → cf32.
`ppm` passes through to the server's tuner — remote sticks are usually
uncalibrated. rsp_tcp (SDRplay) speaks the same protocol shape.

**KiwiSDR** (`sources/kiwi.py`): websocket to `ws://host:8073/`, `WHO` →
`SET auth` → `SET mod=IQ freq=<kHz> low/high_cut=<Hz>` → `SET AR` rate
negotiation; binary frames are a 4-byte header + interleaved int16 IQ →
cf32. The session adopts `fixed_sample_rate` (12 kHz default) so the DSP
chains match the stream. **Protocol literals are isolated constants** and
flagged for first-live-connection verification (same policy as the SDRplay
cdef in ADR-004): this dev box has no route to a live Kiwi, so the client
is verified against the in-repo fake server (`tests/test_kiwi_driver.py`),
which doubles as the executable spec for bring-up.

**OpenWebRX federation client** (`sources/openwebrx_remote.py`, slice-3.6):
speaks our native protocol *as a client* to any public OpenWebRX(+)
receiver. The URL a user pastes in a browser is the API:
`source_kwargs={"url": "http://boomerthedog.com:8073/#freq=3570000,mod=lsb,sql=-150"}`
— `parse_openwebrx_url()` extracts host/port/tune from the deep link. The
protocol (extracted from the vendored upstream source): `SERVER DE CLIENT
client=<id> type=receiver` handshake → `connectionproperties` rate request
→ `dspcontrol` tuning (`offset_freq`/`mod`/`squelch_level`/`low_cut`/
`high_cut`) → binary frames tagged 0x01 FFT / 0x02 audio / 0x03 secondary
FFT / 0x04 HD audio, ADPCM-compressed per `sources/_adpcm.py` (exact
libcsdr/JS ports, both directions, round-trip pinned in tests).

Because the remote receiver computes the FFT and demodulates the channel
itself, this is a **display-stream source**, not an IQ source:
`RemoteDisplaySource` yields `RemoteFftFrame` (dB bins + center/rate/
levels) and `RemoteAudioFrame` (int16 PCM at the remote output rate), and
the ReceiverSession bypasses its pycsdr chains — frames are repacked into
the standard WRFO/AUDI wire formats, so the frontend renders them with
zero changes. `setFrequency`/`setMode` from the UI forward to the remote
`dspcontrol`. The ADPCM FFT decode is a pure-Python nibble loop (~5 ms per
4096-bin frame) — fine for one or two remote receivers; vectorizing it is
a known optimization if we ever fan-in many.

Verified against the in-repo fake OpenWebRX+ server
(`tests/test_openwebrx_remote_driver.py`) — handshake, config burst,
ADPCM interop, tuning (the synthetic peak follows `offset_freq`),
backoff refusal, and the full session WRFO/AUDI path. First live
connection: run `scripts/probe_openwebrx_remote.py <url>` from a machine
with internet; it prints server version, config, frame rates, and the
strongest bins, then closes politely.

SoapyRemote needs no new code: `source_type="soapy"` with
`soapy_args={"driver": "remote", "remote": "tcp://host:8080"}`.

### 2. Directories are a separate, TTL-cached service

`sources/directory.py` + `GET /api/directory/kiwi` +
`GET /api/directory/receiverbook`:

- **Kiwi entries are spawnable today** (`source_type="kiwi"` + host/port
  parsed from the entry URL). Receiverbook entries map to the future
  OpenWebRX federation client and feed the map/directory UI meanwhile.
- 5-minute TTL cache with single-flight locking (one process = one fetch
  per window, no matter how many clients ask).
- **Stale-on-failure**: if a refresh fails but a stale list exists, serve
  it (logged). 503 only when there has never been a good fetch. Restricted
  egress must degrade, not crash.
- Field-tolerant parsers: one weird entry is skipped, never fatal.
- The HTTP fetcher is injectable; tests never touch the live directories.

### 3. Dev workflow this enables

```bash
# on the machine with the RTL-SDR V4:
rtl_tcp -a 0.0.0.0

# anywhere (dev box, laptop, CI with network):
POST /api/receivers
{"source_type": "rtl_tcp", "source_kwargs": {"host": "192.168.1.50"},
 "center_freq": 14207000, "sample_rate": 2400000, "mode": "USB"}

# or pick a public Kiwi from GET /api/directory/kiwi and:
POST /api/receivers
{"source_type": "kiwi", "source_kwargs": {"host": "rx.example.com"},
 "center_freq": 14207000, "sample_rate": 12000, "mode": "USB"}
```

The full stack — pycsdr waterfall/demod, WS binary frames, WebGL2
rendering — runs against real ionospheric HF with zero local hardware.

### 4. Etiquette (non-negotiable for a good citizen)

- **Identify honestly**: the Kiwi client sends `WHO am_I=openwebrx_plus`.
  Never spoof a browser.
- **One connection per receiver**; disconnect promptly. Kiwis are
  volunteer-run on home internet connections; 8 user slots each.
- **No reconnect storms**: a dead endpoint fails with a clear error; it
  does not retry-loop (reconnect logic, when it comes, will be
  exponential-backoff and user-initiated).
- **Bias tee stays off** in the remote manifest — powering a stranger's
  antenna hardware over the internet is a foot-gun.
- Directory fetches are cached and rate-limited by design (§2).

## Consequences

- Receiverbook entries are now spawnable end-to-end (`source_type="
  openwebrx_remote"` + the entry URL): the map/directory UI can offer
  one-click "listen on this receiver".
- Live-signal dev without hardware: solved four ways — processed display
  streams from public OpenWebRX receivers (broadest ecosystem), raw IQ from
  public/self-hosted Kiwis (HF, full local DSP), SpyServer (the Airspy
  ecosystem's servers — HF+ / Discovery / R2 / RTL-SDR server-side), and
  rtl_tcp/rsp_tcp/SoapyRemote for self-hosted raw IQ at any frequency.
- The source count grows by four *remote* manifests (11 built-ins now); the
  UI source picker can show a "remote" section driven by
  `hardware_required=False` + `/api/directory/*`.
- Kiwi handshake literals carry bring-up risk: mitigated by the fake-server
  spec, isolated constants, and loud logging on rate mismatch. The same
  policy covers the OpenWebRX client's literals + the ADPCM ports and the
  SpyServer client's literals (20-byte `<IIQI` message frames, SERVER_INFO
  layout, gain-type semantics — all isolated in `sources/spyserver.py`).
- Federation status: remote source + directory + OpenWebRX receiver client
  + SpyServer client are DONE (receive side). Remaining roadmap: SDRangel
  (REST/WS), HD-audio + secondary-demod (digimode text) forwarding for the
  OpenWebRX client, and eventually listing *ourselves* in receiverbook.
- Security posture: outbound `ws://` and `http://` to user-specified hosts
  is accepted (amateur radio norm), no credentials are stored by the
  directory layer, and the bias tee is disabled by default for remotes.
