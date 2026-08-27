"""ADS-B / Mode S decoder tests — demod core, plugin, REST + WS e2e.

The baked fixture (fixtures/iq/adsb_1090.cf32, 2 MSPS, 1 s) is the oracle:
14 CRC-valid frames from 3 aircraft plus 2 distant fragments (ICAO
AABBCC), verified at bake time by an INDEPENDENT PPM decoder
(test_fixtures.py). These tests drive the production demodulator against
the same bytes.

Message inventory (deterministic — same seed as the generator):
  4D22AA "OWRX001": 2× DF11 all-call, DF17 callsign, DF17 altitude 12500 ft
  3C70EE "N42OWRX": same pattern
  06A1B2 "OPENWEB1": same pattern
  AABBCC (distant, weak): 2× DF11 all-call
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from openwebrx_plus.api.rest import create_app
from openwebrx_plus.config import Settings
from openwebrx_plus.plugins.adsb import AdsbDecoderPlugin
from openwebrx_plus.plugins.modes import (
    MODE_S_SAMPLE_RATE,
    ModeSReceiver,
    crc24_mode_s,
)
from openwebrx_plus.sessions import destroy_session, get_session

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "iq" / "adsb_1090.cf32"
)

AIRCRAFT = {"4D22AA": "OWRX001", "3C70EE": "N42OWRX", "06A1B2": "OPENWEB1"}


def _load_fixture() -> np.ndarray:
    return np.fromfile(FIXTURE, dtype=np.complex64)


# ---------------------------------------------------------------------------
# Mode S core: CRC + field decode
# ---------------------------------------------------------------------------


def test_crc24_dump1090_vector() -> None:
    """The canonical dump1090 test vector must verify."""
    assert crc24_mode_s(bytes.fromhex("8D4840D6202CC371C32CE0")) == 0x576098


def test_crc24_zero_on_valid_frame() -> None:
    frame = bytes.fromhex("8D4840D6202CC371C32CE0576098")
    assert crc24_mode_s(frame) == 0


def test_sample_rate_guard() -> None:
    with pytest.raises(ValueError, match="2_?000_?000|2 MSPS"):
        ModeSReceiver(sample_rate=2_400_000)


def test_callsign_charset() -> None:
    from openwebrx_plus.plugins.modes import _decode_callsign

    # "OWRX001 " in 6-bit ICAO charset, MSB-first
    def pack(text: str) -> bytes:
        acc = 0
        for ch in (text + "        ")[:8]:
            if ch == " ":
                v = 32
            elif ch.isdigit():
                v = 48 + int(ch)
            else:
                v = ord(ch) - 64
            acc = (acc << 6) | v
        return acc.to_bytes(6, "big")

    assert _decode_callsign(pack("OWRX001")) == "OWRX001"
    assert _decode_callsign(pack("OPENWEB1")) == "OPENWEB1"
    assert _decode_callsign(b"\x00" * 6) is None  # all no-information


def test_df11_address_parity_variant() -> None:
    """DF11 with PI = CRC(data) ⊕ ICAO must verify via the address check."""
    icao = 0x4D22AA
    msg = bytes([(11 << 3) | 5, (icao >> 16) & 0xFF, (icao >> 8) & 0xFF, icao & 0xFF])
    pi = crc24_mode_s(msg) ^ icao  # address/parity
    frame = msg + pi.to_bytes(3, "big")

    from openwebrx_plus.plugins.modes import decode_frame_fields

    icao_hex, _, _ = decode_frame_fields(frame)
    assert icao_hex == "4D22AA"
    # And the receiver accepts it through the same path
    wave = _ppm_modulate(frame)
    rx = ModeSReceiver()
    frames = rx.feed(wave)
    assert len(frames) == 1
    assert frames[0].parity == "address"
    assert frames[0].icao == "4D22AA"


# ---------------------------------------------------------------------------
# Demodulator: synthetic round-trip + the baked fixture
# ---------------------------------------------------------------------------


def _ppm_modulate(message: bytes, fs: int = 2_000_000, amplitude: float = 0.8) -> np.ndarray:
    """Independent PPM encoder (mirrors the fixture generator's shape)."""
    spm = fs // 1_000_000
    total_bits = len(message) * 8
    n = int((8.0 + total_bits) * spm) + 2
    env = np.zeros(n, dtype=np.float32)

    def pulse(t_us: float) -> None:
        env[int(round(t_us * spm)): int(round((t_us + 0.5) * spm))] = 1.0

    for t in (0.0, 1.0, 2.5, 3.5, 4.5):
        pulse(t)
    for k, bit in enumerate(f"{int.from_bytes(message, 'big'):0{total_bits}b}"):
        t_bit = 8.0 + k
        pulse(t_bit if bit == "1" else t_bit + 0.5)

    rng = np.random.default_rng(7)
    noise = rng.normal(0, 0.02, n) + 1j * rng.normal(0, 0.02, n)
    return (amplitude * env + noise).astype(np.complex64)


def test_synthetic_roundtrip() -> None:
    """Encode → decode a DF17 callsign message built in-test."""
    msg = bytes.fromhex("8D4840D6202CC371C32CE0576098")  # dump1090 vector
    rx = ModeSReceiver()
    frames = rx.feed(_ppm_modulate(msg))
    assert len(frames) == 1
    f = frames[0]
    assert f.df == 17
    assert f.icao == "4840D6"
    assert f.raw == "8D4840D6202CC371C32CE0576098"
    assert f.parity == "data"


def test_noise_only_yields_nothing() -> None:
    rng = np.random.default_rng(42)
    noise = (rng.normal(0, 0.02, 2_000_000) + 1j * rng.normal(0, 0.02, 2_000_000)).astype(
        np.complex64
    )
    rx = ModeSReceiver()
    assert rx.feed(noise) == []
    assert rx.crc_failures == 0


def test_frame_straddling_chunk_boundary() -> None:
    """A frame split across two feeds must decode exactly once."""
    msg = bytes.fromhex("8D4840D6202CC371C32CE0576098")
    wave = _ppm_modulate(msg)
    split = 60  # mid-preamble — deep inside the frame
    rx = ModeSReceiver()
    out = rx.feed(wave[:split])
    assert out == []  # nothing complete yet
    out = rx.feed(wave[split:])
    assert [f.raw for f in out] == ["8D4840D6202CC371C32CE0576098"]


def test_fixture_decodes_all_frames() -> None:
    iq = _load_fixture()
    rx = ModeSReceiver()
    frames = rx.feed(iq)
    assert len(frames) == 14
    assert rx.crc_failures == 0

    icaos = {f.icao for f in frames}
    assert icaos == set(AIRCRAFT) | {"AABBCC"}

    callsigns = {f.icao: f.callsign for f in frames if f.callsign}
    assert callsigns == AIRCRAFT

    # Every aircraft broadcast altitude 12_500 ft exactly once
    for icao in AIRCRAFT:
        alt = [f.altitude_ft for f in frames if f.icao == icao and f.altitude_ft is not None]
        assert alt == [12_500]

    # Distant fragments: DF11 only, weak RSSI
    distant = [f for f in frames if f.icao == "AABBCC"]
    assert len(distant) == 2
    assert all(f.df == 11 for f in distant)
    assert all(f.rssi_dbfs < -15 for f in distant)

    # Monotonic sample offsets (scan order)
    offsets = [f.sample_offset for f in frames]
    assert offsets == sorted(offsets)


def test_fixture_chunked_feed_identical() -> None:
    """Any chunk size yields the same frames in the same order."""
    iq = _load_fixture()
    reference = [f.raw for f in ModeSReceiver().feed(iq)]
    for chunk in (997, 4_096, 65_536, 700_000):
        rx = ModeSReceiver()
        got: list[str] = []
        for k in range(0, iq.size, chunk):
            got.extend(f.raw for f in rx.feed(iq[k : k + chunk]))
        assert got == reference, f"chunk size {chunk} diverged"


# ---------------------------------------------------------------------------
# Plugin: aircraft table + event stream
# ---------------------------------------------------------------------------


def test_plugin_events_and_aircraft_table() -> None:
    plugin = AdsbDecoderPlugin()
    events = plugin.feed_iq(_load_fixture())

    frames = [e for e in events if e["kind"] == "frame"]
    snapshots = [e for e in events if e["kind"] == "aircraft"]
    assert len(frames) == 14
    assert snapshots, "at least one aircraft snapshot must follow the frames"

    table = snapshots[-1]["aircraft"]
    by_icao = {a["icao"]: a for a in table}
    assert set(by_icao) == set(AIRCRAFT) | {"AABBCC"}
    for icao, callsign in AIRCRAFT.items():
        row = by_icao[icao]
        assert row["callsign"] == callsign
        assert row["altitude_ft"] == 12_500
        assert row["frames"] == 4
    assert by_icao["AABBCC"]["frames"] == 2
    assert by_icao["AABBCC"]["callsign"] is None

    # Snapshot ordering: most recently heard first
    assert table == sorted(table, key=lambda a: -a["last_seen"])

    # Status reflects the counters
    status = plugin.status()
    assert status["frames"] == 14
    assert status["crc_failures"] == 0
    assert status["aircraft"] == 4


def test_plugin_snapshot_rate_limit() -> None:
    """Known-aircraft updates coalesce at 0.5 s; new aircraft flush now."""
    plugin = AdsbDecoderPlugin()
    msg = bytes.fromhex("8D4840D6202CC371C32CE0576098")
    wave = _ppm_modulate(msg)

    first = plugin.feed_iq(wave)
    assert len([e for e in first if e["kind"] == "aircraft"]) == 1

    # Same aircraft again immediately: frame event yes, snapshot no
    second = plugin.feed_iq(_ppm_modulate(msg))
    assert len([e for e in second if e["kind"] == "frame"]) == 1
    assert second[-1]["kind"] == "frame"


# ---------------------------------------------------------------------------
# REST + WS end-to-end
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    """Context-managed TestClient — one persistent portal event loop.

    Without the `with` block every request gets a TEMPORARY loop; tasks
    spawned during a request (the session's hub pump + stream task)
    would die with it and nothing would ever reach a WebSocket.
    """
    settings = Settings(tier="dev")
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_decoder_manifests_endpoint(client: TestClient) -> None:
    r = client.get("/api/decoders")
    assert r.status_code == 200
    decoders = r.json()
    adsb = next(d for d in decoders if d["name"] == "adsb")
    assert adsb["tap_point"] == "rf_band"
    assert adsb["required_sample_rate"] == MODE_S_SAMPLE_RATE
    assert "frame" in adsb["events"] and "aircraft" in adsb["events"]


def test_fixtures_endpoint_lists_adsb(client: TestClient) -> None:
    r = client.get("/api/fixtures")
    assert r.status_code == 200
    adsb = next(f for f in r.json() if f["name"] == "adsb_1090")
    assert adsb["sample_rate"] == 2_000_000
    assert adsb["center_freq"] == 1_090_000_000
    assert Path(adsb["path"]).exists()


def _spawn_adsb_receiver(client: TestClient, **overrides: object) -> str:
    r = client.post(
        "/api/receivers",
        json={
            "source_type": "file",
            "source_kwargs": {
                "file_path": str(FIXTURE),
                "loop": True,
                "realtime": True,
            },
            **overrides,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["receiver_id"]


def test_attach_errors(client: TestClient) -> None:
    rid = _spawn_adsb_receiver(client)
    try:
        # Unknown decoder name → 400
        r = client.post(f"/api/receivers/{rid}/decoders", json={"name": "nope"})
        assert r.status_code == 400
        assert "unknown decoder" in r.json()["detail"]

        # Attach works
        r = client.post(f"/api/receivers/{rid}/decoders", json={"name": "adsb"})
        assert r.status_code == 201
        assert r.json() == {"name": "adsb", "attached": True}

        # Double attach → 409
        r = client.post(f"/api/receivers/{rid}/decoders", json={"name": "adsb"})
        assert r.status_code == 409

        # Status shows it
        r = client.get(f"/api/receivers/{rid}/decoders")
        assert r.status_code == 200
        assert [d["name"] for d in r.json()] == ["adsb"]

        # Detach → 204, then 404 on repeat
        assert client.delete(f"/api/receivers/{rid}/decoders/adsb").status_code == 204
        assert client.delete(f"/api/receivers/{rid}/decoders/adsb").status_code == 404
        assert client.get(f"/api/receivers/{rid}/decoders").json() == []
    finally:
        client.delete(f"/api/receivers/{rid}")


def test_attach_rejects_wrong_sample_rate(client: TestClient) -> None:
    """rx-default runs the 250 kSPS HF fixture — ADS-B needs 2 MSPS."""
    r = client.post("/api/receivers/rx-default/decoders", json={"name": "adsb"})
    assert r.status_code == 400
    assert "requires 2000000" in r.json()["detail"]


def test_attach_rejects_display_stream_source() -> None:
    """Remote display sessions have no IQ — a decoder can't tap them."""
    import asyncio

    from openwebrx_plus.plugins.base import DecoderAttachError
    from openwebrx_plus.sessions.receiver_session import ReceiverSession
    from openwebrx_plus.sources.simulated import SimulatedSource

    class FakeDisplaySource(SimulatedSource):
        """Minimal display-stream stand-in (duck-typed attribute)."""

        display_stream = True  # type: ignore[assignment]

    session = ReceiverSession(
        receiver_id="rx-display-test",
        source=FakeDisplaySource(),  # type: ignore[arg-type]
    )
    with pytest.raises(DecoderAttachError, match="no raw IQ"):
        asyncio.run(session.attach_decoder("adsb"))


def test_decoder_events_over_websocket(client: TestClient) -> None:
    """Fixture replay → decoder attach → WS text frames carry the events."""
    rid = _spawn_adsb_receiver(client)
    try:
        r = client.post(f"/api/receivers/{rid}/decoders", json={"name": "adsb"})
        assert r.status_code == 201

        frame_events: list[dict] = []
        aircraft_events: list[dict] = []

        def all_callsigns(snapshot: dict) -> bool:
            """True once every fixture aircraft has reported its callsign
            (4D22AA's callsign frame sits at 0.73 s of each 1 s loop)."""
            table = {a["icao"]: a for a in snapshot["aircraft"]}
            return all(
                icao in table and table[icao]["callsign"] == callsign
                for icao, callsign in AIRCRAFT.items()
            )

        with client.websocket_connect(f"/ws/{rid}") as ws:
            # The fixture loops every wall-clock second; the callsigns are
            # complete by ~1.8 s (pass 2). 4 s is a comfortable deadline.
            import time

            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                if aircraft_events and all_callsigns(aircraft_events[-1]):
                    break
                try:
                    text = ws.receive_text()
                except KeyError:
                    continue  # binary FFT/audio frame — not ours
                if not text:
                    continue
                data = json.loads(text)
                if data.get("type") != "decoder":
                    continue  # metadata
                assert data["decoder"] == "adsb"
                assert data["receiverId"] == rid
                event = data["event"]
                if event["kind"] == "frame":
                    frame_events.append(event)
                elif event["kind"] == "aircraft":
                    aircraft_events.append(event)

        assert frame_events, "no decoder frame events arrived"
        assert aircraft_events, "no aircraft snapshots arrived"

        # All three fixture aircraft present in the final snapshot
        final = aircraft_events[-1]["aircraft"]
        icaos = {a["icao"] for a in final}
        assert set(AIRCRAFT) <= icaos
        by_icao = {a["icao"]: a for a in final}
        assert by_icao["4D22AA"]["callsign"] == "OWRX001"
        assert by_icao["4D22AA"]["altitude_ft"] == 12_500

        # Frame events carry the full decoded payload
        sample = frame_events[0]
        assert sample["df"] in (11, 17)
        assert sample["icao"] is not None
        assert "raw" in sample and len(sample["raw"]) in (14, 28)

        # REST status agrees with the stream
        r = client.get(f"/api/receivers/{rid}/decoders")
        status = r.json()[0]
        assert status["frames"] >= 14
        assert status["aircraft"] >= 4
    finally:
        client.delete(f"/api/receivers/{rid}")


def test_destroy_receiver_stops_decoder_cleanly(client: TestClient) -> None:
    rid = _spawn_adsb_receiver(client)
    assert client.post(f"/api/receivers/{rid}/decoders", json={"name": "adsb"}).status_code == 201
    session = get_session(rid)
    assert session is not None
    attachment = session._decoders["adsb"]

    assert client.delete(f"/api/receivers/{rid}").status_code == 204
    assert get_session(rid) is None
    assert attachment.task.done() or attachment.task.cancelled()
    assert await_destroy_clean(rid)


def await_destroy_clean(rid: str) -> bool:
    """Post-destroy sanity: the registry no longer tracks the session."""
    return get_session(rid) is None


@pytest.mark.asyncio
async def test_direct_attach_detach_lifecycle() -> None:
    """Session-level API without REST: attach, feed, detach, re-attach."""
    from openwebrx_plus.plugins.base import DecoderAlreadyAttached
    from openwebrx_plus.sessions.receiver_session import ReceiverSession
    from openwebrx_plus.sources.file_source import FileSource

    source = FileSource(file_path=FIXTURE, loop=True, realtime=False)
    session = ReceiverSession(
        receiver_id="rx-direct-test",
        source=source,  # type: ignore[arg-type]
        center_freq=1_090_000_000,
        sample_rate=2_000_000,
    )
    try:
        result = await session.attach_decoder("adsb")
        assert result["name"] == "adsb"
        with pytest.raises(DecoderAlreadyAttached):
            await session.attach_decoder("adsb")

        # Let the (unpaced) replay run through; the decoder counts frames.
        import asyncio

        await asyncio.sleep(0.5)
        status = session.decoder_status()[0]
        assert status["frames"] >= 1

        assert await session.detach_decoder("adsb") is True
        assert await session.detach_decoder("adsb") is False
        # Re-attach must work after a detach (fresh plugin instance).
        await session.attach_decoder("adsb")
    finally:
        await session.stop()
        await destroy_session("rx-direct-test")
