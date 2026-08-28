"""AIS decoder tests — protocol layer, demod round-trip, plugin, REST + WS.

Strategy: build a known-good AIS payload, HDLC-frame it (with bit
stuffing + CRC), synthesize GMSK modulated IQ at 48 kS/s, then verify
the demodulator recovers the original message. This is the same
round-trip pattern the ADS-B tests use against the baked fixture.

Message inventory (deterministic — hand-built):
  MMSI 366000001: Type 1 position report (lat 37.8084, lon -122.4180)
  MMSI 366000002: Type 5 static & voyage (vessel "TESTVESSEL", callsign WZ1234)
  MMSI 003669999: Type 18 Class B position report
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from openwebrx_plus.plugins.ais import AisDecoderPlugin
from openwebrx_plus.plugins.ais_demod import (
    AIS_BAUD,
    AIS_SAMPLE_RATE,
    AisReceiver,
)
from openwebrx_plus.plugins.ais_protocol import (
    AisMessage,
    BitReader,
    bits_to_bytes,
    bytes_to_bits,
    crc16_ais,
    decode_ais_payload,
    encode_ais_frame,
)

# === Protocol layer: CRC + 6-bit charset + BitReader =======================


def test_crc16_zero_on_valid_frame() -> None:
    """The CRC of a payload + its CRC must be zero (the verify step)."""
    payload = b"\x01\x02\x03\x04"
    crc = crc16_ais(payload)
    full = payload + bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    assert crc16_ais(full) == 0


def test_crc16_known_vector() -> None:
    """CRC-16-CCITT-FALSE check sequence for "123456789" is 0x29B1.

    This is the canonical CRC-16-CCITT-FALSE test vector — it's what
    the Wikipedia page lists as the reference output for the algorithm
    used by AIS (poly 0x1021, init 0xFFFF, no reflection).
    """
    assert crc16_ais(b"123456789") == 0x29B1


def test_bit_reader_reads_msb_first() -> None:
    br = BitReader(b"\x80")
    assert br.read(1) == 1
    assert br.read(1) == 0
    assert br.read(6) == 0


def test_bit_reader_signed() -> None:
    # 0b10000000 = -128 in two's complement 8-bit
    br = BitReader(b"\x80")
    assert br.read_signed(8) == -128
    # 0b01111111 = +127
    br2 = BitReader(b"\x7f")
    assert br2.read_signed(8) == 127


def test_bit_reader_pads_short_payload_with_zeros() -> None:
    """Reading bits past the end of the payload returns 0 for each missing bit
    (per AIS spec — short messages are zero-padded to the field width).

    With b"\\xff" (8 bits, all 1s):
      - read(4) → 0xF (consumes the top 4 bits of the byte)
      - read(8) → 0xF0 (the bottom 4 bits of the byte are real = 0xF,
                       and the 4 past-end bits are zero-padded → 0xF0)
    """
    br = BitReader(b"\xff")  # 8 bits available
    assert br.read(4) == 0xF  # all 1s
    # 4 real bits (the bottom of the byte) + 4 zero-padded past end.
    assert br.read(8) == 0xF0  # 0b1111_0000
    assert br.remaining == 0

    # A second reader starts past the end → every bit is zero-padded.
    br2 = BitReader(b"")
    assert br2.read(8) == 0
    assert br2.remaining == 0


def test_ais_char_low_range_is_digits_and_letters() -> None:
    """6-bit code 0 → '0', code 1 → '1', ..., code 9 → '9', code 17 → 'A'.

    Per ITU-R M.1371-5 Annex A (libais char table):
      - code 0-47 → ASCII = c + 48 (so '0'-'_' = ASCII 48-95)
      - code 48-63 → ASCII = c - 16 (so ' '-'/' = ASCII 32-47)
    So:
      code 16 → '@' (ASCII 64), code 31 → 'O' (ASCII 79),
      code 32 → 'P' (ASCII 80), code 42 → 'Z' (ASCII 90),
      code 47 → '_' (ASCII 95), code 48 → ' ' (ASCII 32),
      code 63 → '/' (ASCII 47).
    """
    from openwebrx_plus.plugins.ais_protocol import ais_char

    assert ais_char(0) == "0"
    assert ais_char(9) == "9"
    assert ais_char(10) == ":"
    assert ais_char(16) == "@"
    assert ais_char(17) == "A"
    assert ais_char(26) == "J"
    assert ais_char(31) == "O"
    assert ais_char(32) == "P"
    assert ais_char(42) == "Z"
    assert ais_char(47) == "_"
    assert ais_char(48) == " "
    assert ais_char(63) == "/"


# === Bit stuffing / HDLC framing =========================================


def test_stuff_bits_inserts_zero_after_five_ones() -> None:
    """11111 in the payload becomes 111110 — no HDLC_FLAG in the data."""
    from openwebrx_plus.plugins.ais_protocol import destuff, stuff_bits

    bits = [1, 1, 1, 1, 1, 0, 0]
    stuffed = stuff_bits(bits)
    assert stuffed == [1, 1, 1, 1, 1, 0, 0, 0]
    assert destuff(stuffed) == bits


def test_destuff_round_trips_arbitrary_payload() -> None:
    """Stuff then destuff is identity for any bit stream."""
    from openwebrx_plus.plugins.ais_protocol import destuff, stuff_bits

    rng = np.random.default_rng(seed=42)
    bits = [int(b) for b in rng.integers(0, 2, size=500)]
    stuffed = stuff_bits(bits)
    assert destuff(stuffed) == bits


def test_encode_ais_frame_starts_and_ends_with_flag() -> None:
    """An encoded frame has the HDLC flag pattern (01111110) at start and end."""
    from openwebrx_plus.plugins.ais_protocol import HDLC_FLAG

    payload = b"\x01\x02\x03\x04"
    bits = encode_ais_frame(payload)
    # First 16 bits are preamble (01010101 01010101), then start flag.
    # Flag pattern = 01111110.
    expected_flag = bytes_to_bits(bytes([HDLC_FLAG]))
    assert bits[16:24] == expected_flag
    assert bits[-8:] == expected_flag


# === Field decoders (Type 1, 5, 18) ======================================


def _build_type1_payload() -> bytes:
    """Build a Type 1 position report payload (no CRC, no HDLC)."""
    br_bits: list[int] = []

    def push(v: int, n: int) -> None:
        for i in range(n - 1, -1, -1):
            br_bits.append((v >> i) & 1)

    push(1, 6)  # message type
    push(0, 2)  # repeat
    push(366000001, 30)  # MMSI
    push(0, 4)  # nav status: under way using engine
    push(0, 8)  # rot (signed; 0 = no turn)
    push(180, 10)  # sog = 18.0 kn (units: 1/10 kn)
    push(0, 1)  # accuracy
    push(int(-122.4180 * 60000), 28)  # lon (signed)
    push(int(37.8084 * 60000), 27)  # lat (signed)
    push(900, 12)  # cog = 90.0 deg
    push(90, 9)  # heading = 90
    push(30, 6)  # timestamp = 30 s
    push(0, 2)  # maneuver
    push(0, 3)  # spare
    push(0, 1)  # raim
    push(0, 20)  # radio status
    # Pad to byte boundary
    while len(br_bits) % 8 != 0:
        br_bits.append(0)
    return bits_to_bytes(br_bits)


def test_decode_type1_position_report() -> None:
    """Type 1 decode: MMSI + lat/lon + speed/course/heading/timestamp."""
    payload = _build_type1_payload()
    msg = decode_ais_payload(payload, rssi_dbfs=-50.0)
    assert msg is not None
    assert msg.type == 1
    assert msg.mmsi == "366000001"
    assert msg.speed_kn == pytest.approx(18.0, abs=0.01)
    assert msg.longitude == pytest.approx(-122.4180, abs=0.001)
    assert msg.latitude == pytest.approx(37.8084, abs=0.001)
    assert msg.course_deg == pytest.approx(90.0, abs=0.1)
    assert msg.heading_deg == 90
    assert msg.timestamp_sec == 30


def _build_type5_payload() -> bytes:
    """Build a Type 5 static & voyage data payload."""
    br_bits: list[int] = []

    def push(v: int, n: int) -> None:
        for i in range(n - 1, -1, -1):
            br_bits.append((v >> i) & 1)

    def push_text(text: str, length: int) -> None:
        """Encode each char to its 6-bit AIS code per the libais table.

        AIS charset:
          - code 0-47 → ASCII 48-95 (digits, ':;<=>?@', A-O, P-Z, '[\\]^_')
          - code 48-63 → ASCII 32-47 (' !\"#$%&'()*+,-./')
          - code 16 → '@' (end-of-string sentinel)
        """
        for ch in (text + "@" * length)[:length]:
            if ch == "@":
                code = 16  # end-of-string sentinel
            elif 48 <= ord(ch) <= 95:
                code = ord(ch) - 48
            elif 32 <= ord(ch) <= 47:
                code = ord(ch) + 16
            else:
                code = 16  # unsupported char → end sentinel
            push(code, 6)

    push(5, 6)  # type
    push(0, 2)  # repeat
    push(366000002, 30)  # mmsi
    push(0, 2)  # ais version
    push(1234567, 30)  # imo
    push_text("WZ1234", 7)  # callsign
    push_text("TESTVESSEL", 20)  # vessel name
    push(70, 8)  # ship type: cargo
    push(20, 9)  # to_bow
    push(10, 9)  # to_stern
    push(5, 6)  # to_port
    push(5, 6)  # to_starboard
    push(1, 4)  # epfd
    push(3, 4)  # month
    push(15, 5)  # day
    push(8, 5)  # hour
    push(30, 6)  # minute
    push(45, 8)  # draught = 4.5 m
    push_text("OAKLAND", 20)  # destination
    push(0, 1)  # dte
    push(0, 1)  # spare
    while len(br_bits) % 8 != 0:
        br_bits.append(0)
    return bits_to_bytes(br_bits)


def test_decode_type5_static_and_voyage() -> None:
    """Type 5 decode: vessel_name + callsign + imo + draught + destination."""
    payload = _build_type5_payload()
    msg = decode_ais_payload(payload)
    assert msg is not None
    assert msg.type == 5
    assert msg.mmsi == "366000002"
    assert msg.imo == 1234567
    assert msg.callsign == "WZ1234"
    assert msg.vessel_name == "TESTVESSEL"
    assert msg.ship_type == 70
    assert msg.draught_m == pytest.approx(4.5, abs=0.05)
    assert msg.destination == "OAKLAND"


def test_unknown_type_returns_minimal_message() -> None:
    """Type 6 (binary address-specific message) isn't decoded in v1 — caller
    still gets a message with raw hex."""
    # Type 6, repeat 0, mmsi 0, rest zeros
    br_bits: list[int] = []

    def push(v: int, n: int) -> None:
        for i in range(n - 1, -1, -1):
            br_bits.append((v >> i) & 1)

    push(6, 6)  # type (uncommon — not decoded in v1)
    push(0, 2)
    push(0, 30)
    # Pad to byte boundary
    while len(br_bits) % 8 != 0:
        br_bits.append(0)
    payload = bits_to_bytes(br_bits)
    msg = decode_ais_payload(payload)
    assert msg is not None
    assert msg.type == 6
    assert msg.mmsi == "000000000"
    assert msg.vessel_name is None
    assert msg.longitude is None


# === GMSK demodulator round-trip ========================================


def _gmsk_modulate(bits: list[int], sample_rate: int = AIS_SAMPLE_RATE) -> np.ndarray:
    """Synthesize GMSK-modulated IQ at the given sample rate.

    A simplified GMSK: each bit sets the carrier phase to ±π/2
    (FM deviation = ±baud/4 Hz). Gaussian filtering is replaced by a
    triangular smoothing window — enough for the demodulator to
    recover the bits cleanly in the test fixture's high-SNR regime.
    """
    sps = sample_rate // AIS_BAUD
    # Phase per sample: π/2 per bit, divided over SPS samples.
    # Sign determined by bit value (1 = +π/2, 0 = -π/2).
    # Use a triangular smoothing window (Gaussian approximation) over
    # 1 symbol (sps samples) for BT ≈ ∞ — works fine for the high-SNR
    # test fixture.
    total_samples = len(bits) * sps
    # Per-bit phase increments: +π/2 or -π/2 over the symbol period.
    # Smooth across bit boundaries (Gaussian-like) so the instantaneous
    # phase is continuous.
    increments = np.zeros(total_samples, dtype=np.float32)
    for i, bit in enumerate(bits):
        dev = (1 if bit else -1) * (math.pi / 2 / sps)
        increments[i * sps : (i + 1) * sps] = dev
    # Smooth (moving average over sps samples) — this is the "Gaussian" part.
    kernel = np.ones(sps, dtype=np.float32) / sps
    increments_smooth = np.convolve(increments, kernel, mode="same") * sps
    # Integrate phase → instantaneous phase per sample.
    phase = np.cumsum(increments_smooth)
    iq = np.exp(1j * phase).astype(np.complex64)
    return iq


def test_ais_demod_recovers_type1_round_trip() -> None:
    """Encode a Type 1 message → HDLC frame → GMSK modulate → demod.

    The demodulator must recover the original message with the right
    MMSI, lat/lon, speed, and course.
    """
    payload = _build_type1_payload()
    frame_bits = encode_ais_frame(payload)
    # Modulate at 48 kS/s. Pre-pad with some silence so the demod has
    # room to warm up its DC estimate.
    silence = np.zeros(2000, dtype=np.complex64)
    signal = np.concatenate([silence, _gmsk_modulate(frame_bits)])
    rx = AisReceiver(sample_rate=AIS_SAMPLE_RATE)
    msgs = list(rx.feed(signal))
    # Some feeds split the stream into chunks; a single feed should
    # produce at least one frame.
    assert len(msgs) >= 1, f"no frames decoded (stats: {rx.stats})"
    msg = msgs[0]
    assert msg.type == 1
    assert msg.mmsi == "366000001"
    assert msg.longitude == pytest.approx(-122.4180, abs=0.001)
    assert msg.latitude == pytest.approx(37.8084, abs=0.001)
    assert msg.speed_kn == pytest.approx(18.0, abs=0.1)


def test_ais_demod_recovers_type5_round_trip() -> None:
    """Encode a Type 5 message → HDLC frame → GMSK → demod → recover vessel_name."""
    payload = _build_type5_payload()
    frame_bits = encode_ais_frame(payload)
    silence = np.zeros(2000, dtype=np.complex64)
    signal = np.concatenate([silence, _gmsk_modulate(frame_bits)])
    rx = AisReceiver(sample_rate=AIS_SAMPLE_RATE)
    msgs = list(rx.feed(signal))
    assert len(msgs) >= 1, f"no frames decoded (stats: {rx.stats})"
    msg = msgs[0]
    assert msg.type == 5
    assert msg.mmsi == "366000002"
    assert msg.vessel_name == "TESTVESSEL"
    assert msg.callsign == "WZ1234"
    assert msg.imo == 1234567


def test_ais_demod_streams_across_chunks() -> None:
    """Feeding the signal in chunks must still decode frames spanning boundaries."""
    payload = _build_type1_payload()
    frame_bits = encode_ais_frame(payload)
    silence = np.zeros(2000, dtype=np.complex64)
    signal = np.concatenate([silence, _gmsk_modulate(frame_bits)])
    rx = AisReceiver(sample_rate=AIS_SAMPLE_RATE)
    # Feed in 1000-sample chunks (smaller than a frame).
    msgs: list[AisMessage] = []
    for i in range(0, len(signal), 1000):
        msgs.extend(rx.feed(signal[i : i + 1000]))
    assert len(msgs) >= 1
    assert msgs[0].mmsi == "366000001"


def test_ais_demod_sample_rate_guard() -> None:
    """Non-multiple-of-baud sample rates must fail fast."""
    with pytest.raises(ValueError, match="multiple of"):
        AisReceiver(sample_rate=44_100)


# === Plugin: vessel table, status, REST e2e =============================


def test_plugin_manifest_and_registry() -> None:
    """The AIS plugin is registered in the decoder registry by name."""
    from openwebrx_plus.plugins import DecoderRegistry

    cls = DecoderRegistry.get("ais")
    assert cls is not None
    assert cls.manifest.name == "ais"
    assert cls.manifest.required_sample_rate == AIS_SAMPLE_RATE
    assert "frame" in cls.manifest.events
    assert "vessel" in cls.manifest.events


def test_plugin_emits_frame_and_vessel_events() -> None:
    """Feed a Type 1 → expect a frame event + a vessel snapshot (new MMSI)."""
    plugin = AisDecoderPlugin()
    payload = _build_type1_payload()
    frame_bits = encode_ais_frame(payload)
    silence = np.zeros(2000, dtype=np.complex64)
    signal = np.concatenate([silence, _gmsk_modulate(frame_bits)])
    events = plugin.feed_iq(signal)
    kinds = [e["kind"] for e in events]
    assert "frame" in kinds
    assert "vessel" in kinds
    # The vessel snapshot must include our MMSI.
    snapshot = next(e for e in events if e["kind"] == "vessel")
    mmsis = [v["mmsi"] for v in snapshot["vessels"]]
    assert "366000001" in mmsis


def test_plugin_status_reflects_frames_and_crc_failures() -> None:
    """The plugin status surfaces frame counts + CRC failures for diagnostics."""
    plugin = AisDecoderPlugin()
    payload = _build_type1_payload()
    frame_bits = encode_ais_frame(payload)
    silence = np.zeros(2000, dtype=np.complex64)
    signal = np.concatenate([silence, _gmsk_modulate(frame_bits)])
    plugin.feed_iq(signal)
    s = plugin.status()
    assert s["frames"] >= 1
    assert "crc_failures" in s
    assert "vessels" in s
    assert s["vessels"] == 1


def test_plugin_stop_is_noop() -> None:
    """AIS plugin stop() must not raise (pure in-process, no buffers)."""
    plugin = AisDecoderPlugin()
    plugin.stop()


# === REST surface: decoders endpoint lists AIS ============================


def test_rest_decoders_endpoint_lists_ais() -> None:
    """GET /api/decoders must list the AIS plugin in the available set.

    Skipped locally when pycsdr isn't installed (the dev sandbox uses
    a manually-restored venv; CI builds pycsdr from source — see
    scripts/README-dsp-bootstrap.md)."""
    pycsdr = pytest.importorskip("pycsdr")
    assert pycsdr  # silence linter
    from fastapi.testclient import TestClient

    from openwebrx_plus.api.rest import create_app
    from openwebrx_plus.config import Settings

    app = create_app(Settings())
    client = TestClient(app)
    res = client.get("/api/decoders")
    assert res.status_code == 200
    body = res.json()
    names = [d.get("name") for d in body]
    assert "ais" in names
    assert "adsb" in names
