# OpenWebRX+ Backend (apps/server)

Python backend orchestration for OpenWebRX+. Preserves upstream OpenWebRX+ source backends, pycsdr DSP chain, WebSocket protocol, and plugin ecosystem.

## Status (slice-3.5: drivers, fixtures, VFOs, network sources)

- [x] Settings load from env + TOML
- [x] FastAPI app boots at `/`; REST: health, version, sources, hardware, directory, receivers
- [x] WebSocket `/ws/{receiver_id}` — binary FFT + audio frames + JSON metadata
- [x] pycsdr DSP engine: FftChain + AudioChain (AM/NFM/WFM/USB/LSB/CW)
- [x] **Real SDR drivers** (all hardware-free unit-tested):
  - `rtl_sdr` — USB (ctypes/librtlsdr) | rtl_tcp (native asyncio) | `rtl_sdr` CLI subprocess, auto-probed. V4 HF via `direct_sampling=2`.
  - `airspy` — ctypes/libairspy; linearity/sensitivity/manual gain modes; bias tee.
  - `sdrplay` — cffi against API v3 (callback streaming; verify cdef on first hardware bring-up).
  - `soapy` — universal SoapySDR transport (any SDR with a Soapy module).
- [x] **Network sources** (ADR-006 — live signals in dev without hardware):
  - `rtl_tcp` — any rtl_tcp / rsp_tcp server on the internet (self-host or public).
  - `kiwi` — any of the 1000+ public KiwiSDR receivers (0–30 MHz HF),
    websocket `mod=IQ` int16 stream → full pycsdr chain locally.
  - Receiver directories: `GET /api/directory/kiwi` (rx.kiwisdr.com) and
    `GET /api/directory/receiverbook` (receiverbook.de), TTL-cached,
    stale-on-failure, field-tolerant parsing.
- [x] Hardware detection sweep → `GET /api/hardware`
- [x] **IQ fixtures** (deterministic, `scripts/generate_iq_fixtures.py`):
  20 m evening scene, FM broadcast band, ADS-B with valid CRC-24 Mode S
  frames (dump1090-decodable), smoke. FileSource replays them real-time
  paced — the dev default, so the frontend shows real signals with zero
  hardware.
- [x] **VFO sub-receivers** (ADR-005): one wideband capture → N narrowband
  child receivers via pycsdr DDC taps. `POST /api/receivers
  {"source_type": "vfo", "source_kwargs": {"parent_receiver_id": ...}}`.
- [x] 137/137 tests · mypy strict clean · ruff clean

## Run

```bash
# from repo root
make dev-server

# or directly
cd apps/server
uv sync
uv run openwebrx-plus
```

Listens on http://localhost:8073 by default. With no SDR hardware the
default `rx-default` session replays the baked 20 m fixture — CW, SSB,
FT8 traces and QRN scroll across the waterfall exactly like a live SDR.

## Test

```bash
cd apps/server
uv run pytest          # 137 tests (fixtures must be baked — see below)
```

Fixtures live in `apps/server/fixtures/iq/` and ship with the repo. To
re-bake (deterministic, byte-identical):

```bash
python3 scripts/generate_iq_fixtures.py          # repo root
```

## Using real hardware

```bash
# What's plugged in?
curl localhost:8073/api/hardware

# Spawn a receiver on the first RTL-SDR (USB → rtl_tcp → CLI, auto-probed)
curl -X POST localhost:8073/api/receivers -H 'content-type: application/json' \
  -d '{"source_type": "rtl_sdr", "center_freq": 7150000, "sample_rate": 2400000, "mode": "USB"}'

# RTL-SDR Blog V4 on HF (built-in direct-sampling path)
curl -X POST localhost:8073/api/receivers -H 'content-type: application/json' \
  -d '{"source_type": "rtl_sdr", "source_kwargs": {"direct_sampling": 2},
       "center_freq": 7100000, "sample_rate": 250000, "mode": "USB"}'

# rtl_tcp on the network
curl -X POST localhost:8073/api/receivers -H 'content-type: application/json' \
  -d '{"source_type": "rtl_sdr", "source_kwargs": {"transport": "tcp", "host": "192.168.1.50"},
       "center_freq": 1090000000, "sample_rate": 2400000, "mode": "AM"}'

# VFO children off one wideband capture (ADR-005)
curl -X POST localhost:8073/api/receivers -H 'content-type: application/json' \
  -d '{"source_type": "vfo", "source_kwargs": {"parent_receiver_id": "<wide rx id>"},
       "center_freq": 14074000, "sample_rate": 12000, "mode": "USB"}'
```

## Using remote receivers over the internet (ADR-006)

Live signals in dev with zero local hardware. Two flavors:

```bash
# 1. rtl_tcp — self-host next to your own RTL-SDR V4 and connect from anywhere.
#    On the machine with the stick:      rtl_tcp -a 0.0.0.0
curl -X POST localhost:8073/api/receivers -H 'content-type: application/json' \
  -d '{"source_type": "rtl_tcp", "source_kwargs": {"host": "192.168.1.50", "ppm": 21},
       "center_freq": 7150000, "sample_rate": 2400000, "mode": "USB"}'

# 2. KiwiSDR — the 1000+ public HF receivers (0–30 MHz). Browse them:
curl localhost:8073/api/directory/kiwi

#    then listen to one (host/port from any entry's URL):
curl -X POST localhost:8073/api/receivers -H 'content-type: application/json' \
  -d '{"source_type": "kiwi", "source_kwargs": {"host": "rx.example.com", "port": 8073},
       "center_freq": 14207000, "sample_rate": 12000, "mode": "USB"}'

# Remote SoapySDR servers work too, via the universal transport:
curl -X POST localhost:8073/api/receivers -H 'content-type: application/json' \
  -d '{"source_type": "soapy",
       "source_kwargs": {"soapy_args": {"driver": "remote", "remote": "tcp://192.168.1.60:55132"}},
       "center_freq": 100000000, "sample_rate": 1000000, "mode": "FM"}'
```

Etiquette (ADR-006): public receivers are volunteer-run — we identify as
`openwebrx_plus`, take one connection per receiver, disconnect promptly, and
never reconnect-storm. The Kiwi handshake literals are verified against the
in-repo fake server; check them on your first live connection (see
`sources/kiwi.py` bring-up notes).

Capturing your own IQ from an RTL-SDR (FileSource reads raw cu8 directly):

```bash
rtl_sdr -f 14150000 -s 250000 -g 400 -n 2000000 mycapture.cu8
curl -X POST localhost:8073/api/receivers -H 'content-type: application/json' \
  -d '{"source_type": "file", "source_kwargs": {"file_path": "/abs/path/mycapture.cu8"}}'
```

(`cf32` / `.cfile` / `cs16` / `cu8` / SigMF-data supported; a sibling
`.meta` JSON supplies center/rate — see FileSource.)

## Settings

All settings are configurable via env vars (prefix `OPENWEBRX_`):

```bash
OPENWEBRX_HOST=0.0.0.0 \
OPENWEBRX_PORT=8073 \
OPENWEBRX_LOG_LEVEL=DEBUG \
OPENWEBRX_DEFAULT_SOURCE_TYPE=rtl_sdr      # default session source (default: file)
OPENWEBRX_DSP__DEFAULT_MODE=cascade \
OPENWEBRX_FEDERATION__ENABLED=true \
uv run openwebrx-plus
```

## Layout

```
openwebrx_plus/
├── __main__.py             # Entrypoint (uv run openwebrx-plus)
├── config/                 # Pydantic-settings
├── dsp/                    # pycsdr chains: FftChain, AudioChain
├── sources/                # SDR source backends + registry + probe
│   ├── base.py            # Source protocol, manifests, SourceRegistry
│   ├── rtl_sdr.py         # USB / rtl_tcp / subprocess transports
│   ├── rtl_tcp.py         # rtl_tcp wire protocol + remote source (ADR-006)
│   ├── airspy.py          # ctypes/libairspy binding
│   ├── sdrplay.py         # cffi / SDRplay API v3 binding
│   ├── soapy.py           # universal SoapySDR transport (incl. remote)
│   ├── kiwi.py            # KiwiSDR websocket IQ client (ADR-006)
│   ├── directory.py       # receiver directories: kiwi + receiverbook (ADR-006)
│   ├── wideband.py        # IqHub + VfoTapSource (ADR-005)
│   ├── file_source.py     # IQ replay (real-time paced)
│   ├── simulated.py       # synthetic signal scenes
│   └── probe.py           # hardware detection sweep
├── sessions/               # ReceiverSession + registry (hub-integrated)
├── api/                    # FastAPI REST + WebSocket
├── plugins/                # Decoder plugin SDK (ADR-003)
└── observability/          # structlog, Prometheus, OTel (planned)
fixtures/iq/                # Baked IQ fixtures + SigMF sidecars
tests/                      # 137 tests — drivers, network sources, fixtures, VFOs, wire formats
```
