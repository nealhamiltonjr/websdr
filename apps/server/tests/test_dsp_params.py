"""Tests for the DSPParams dataclass + WS setDSPParams handler (slice-5.2).

Covers:
  - DSPParams defaults, from_dict round-trip, to_dict shape, merge semantics
  - ReceiverSession.set_dsp_params() partial update + chain rebuild
  - WS setDSPParams command handler + metadata echo of dspParams
  - AudioChain construction with dsp_params (manual bandpass / AGC / Squelch /
    Gain / NfmDeemphasis insertion)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openwebrx_plus.api.rest import create_app
from openwebrx_plus.config import Settings
from openwebrx_plus.config.user_settings import (
    reset_user_settings_service,
)
from openwebrx_plus.dsp.types import DSPParams
from openwebrx_plus.sessions import (
    create_session,
    destroy_session,
    init_default_sessions,
)
from openwebrx_plus.sources import SourceRegistry

# ----------------------------------------------------------------------------
# DSPParams dataclass tests
# ----------------------------------------------------------------------------


def test_dsp_params_defaults_all_none() -> None:
    p = DSPParams()
    assert p.low_cut_hz is None
    assert p.high_cut_hz is None
    assert p.agc_enabled is None
    assert p.squelch_db is None
    assert p.dc_block_enabled is None
    assert p.deemphasis_enabled is None
    assert p.manual_gain_db is None
    assert p.notch_enabled is None
    assert p.notch_freq_hz is None
    assert p.notch_q is None
    assert p.noise_blanker_enabled is None
    assert p.noise_blanker_threshold is None


def test_dsp_params_to_dict_round_trip() -> None:
    p = DSPParams(
        low_cut_hz=300.0,
        high_cut_hz=3000.0,
        agc_enabled=True,
        squelch_db=-50.0,
        dc_block_enabled=False,
        deemphasis_enabled=True,
        manual_gain_db=6.0,
    )
    d = p.to_dict()
    assert d["low_cut_hz"] == 300.0
    assert d["high_cut_hz"] == 3000.0
    assert d["agc_enabled"] is True
    assert d["squelch_db"] == -50.0
    assert d["dc_block_enabled"] is False
    assert d["deemphasis_enabled"] is True
    assert d["manual_gain_db"] == 6.0
    # Round-trip
    p2 = DSPParams.from_dict(d)
    assert p2.to_dict() == d


def test_dsp_params_from_dict_ignores_unknown_fields() -> None:
    p = DSPParams.from_dict(
        {
            "low_cut_hz": 200.0,
            "bogus_field": "should_be_ignored",
            "agc_enabled": True,
        }
    )
    assert p.low_cut_hz == 200.0
    assert p.agc_enabled is True
    assert p.high_cut_hz is None


def test_dsp_params_merge_overrides_only_non_none() -> None:
    base = DSPParams(low_cut_hz=300.0, high_cut_hz=3000.0, agc_enabled=True)
    patch = DSPParams(low_cut_hz=500.0, agc_enabled=False, squelch_db=-40.0)
    merged = base.merge(patch)
    assert merged.low_cut_hz == 500.0  # overridden
    assert merged.high_cut_hz == 3000.0  # unchanged
    assert merged.agc_enabled is False  # overridden
    assert merged.squelch_db == -40.0  # new field set
    assert merged.manual_gain_db is None  # still None
    # Original is untouched
    assert base.low_cut_hz == 300.0
    assert base.agc_enabled is True


def test_dsp_params_merge_no_changes_when_patch_all_none() -> None:
    base = DSPParams(low_cut_hz=300.0, agc_enabled=True)
    patch = DSPParams()  # all None
    merged = base.merge(patch)
    assert merged.to_dict() == base.to_dict()


def test_dsp_params_defaults_factory_returns_fresh_instances() -> None:
    a = DSPParams.defaults()
    b = DSPParams.defaults()
    assert a is not b
    assert a.to_dict() == b.to_dict()
    assert a.low_cut_hz is None


# ----------------------------------------------------------------------------
# ReceiverSession.set_dsp_params tests
# ----------------------------------------------------------------------------


def _make_test_session(receiver_id: str = "rx-test-dsp") -> object:
    """Create a ReceiverSession backed by a simulated source for testing
    DSP param updates. Caller is responsible for destroy_session cleanup."""
    settings = Settings(tier="dev")
    init_default_sessions(settings)
    # Find the simulated source manifest
    manifests = SourceRegistry.all_manifests()
    sim_manifest = next((m for m in manifests if m.source_type == "simulated"), None)
    assert sim_manifest is not None, "simulated source must be registered"
    # Spawn a session via the registry's REST helper

    session = create_session(
        receiver_id=receiver_id,
        source_type="simulated",
        source_kwargs={"signal_set": "am_band"},
        center_freq=1_000_000,
        sample_rate=250_000,
        mode="AM",
    )
    return session


def test_set_dsp_params_partial_update_stores_and_merges() -> None:
    """A patch with only some fields set should leave other fields alone."""
    session = _make_test_session("rx-dsp-partial")
    try:
        # Apply patch with just low_cut_hz
        result = asyncio.run(
            session.set_dsp_params(  # type: ignore[attr-defined]
                DSPParams(low_cut_hz=400.0)
            )
        )
        applied, reason = result
        assert applied, f"unexpected rejection: {reason}"
        assert session.dsp_params.low_cut_hz == 400.0  # type: ignore[attr-defined]
        assert session.dsp_params.high_cut_hz is None  # type: ignore[attr-defined]

        # Apply a second patch with high_cut_hz
        asyncio.run(session.set_dsp_params(DSPParams(high_cut_hz=2500.0)))  # type: ignore[attr-defined]
        assert session.dsp_params.low_cut_hz == 400.0  # type: ignore[attr-defined]
        assert session.dsp_params.high_cut_hz == 2500.0  # type: ignore[attr-defined]
    finally:
        destroy_session("rx-dsp-partial")


def test_set_dsp_params_no_op_when_patch_is_all_none() -> None:
    """A patch with no non-None fields is a no-op — applied=True, no rebuild."""
    session = _make_test_session("rx-dsp-noop")
    try:
        result = asyncio.run(session.set_dsp_params(DSPParams()))  # type: ignore[attr-defined]
        applied, reason = result
        assert applied
        assert reason == ""
    finally:
        destroy_session("rx-dsp-noop")


# ----------------------------------------------------------------------------
# WS setDSPParams handler tests (via TestClient)
# ----------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient wrapped in a `with` so the FastAPI lifespan events fire
    (startup hooks wire the asyncio/threading excepthooks)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    reset_user_settings_service()
    settings = Settings(tier="dev")
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    reset_user_settings_service()
    yield
    reset_user_settings_service()


def _recv_json(ws, predicate, tries: int = 80):
    """Scan incoming WS frames until a JSON text frame matches predicate.

    Uses the RAW ``ws.receive()`` (message dicts) instead of
    receive_text/receive_bytes: starlette's TestClient CONSUMES a frame
    and raises KeyError on a type mismatch. The pump alternates
    [binary, text] pairs and error frames interleave — scanning with a
    generous bound is the honest way through (see
    test_gain_dsp_controls.py for the same pattern).
    """
    for _ in range(tries):
        msg = ws.receive()
        if "text" not in msg:
            continue  # binary FFT/audio frame — dropped deliberately
        try:
            payload = json.loads(msg["text"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and predicate(payload):
            return payload
    raise AssertionError("expected JSON frame not received within bound")


def _recv_metadata(ws, receiver_id: str | None = None, tries: int = 80) -> dict:
    """Convenience: scan for the next metadata frame."""
    pred = (
        (lambda p: p.get("type") == "metadata")
        if receiver_id is None
        else (lambda p: p.get("type") == "metadata" and p.get("receiverId") == receiver_id)
    )
    return _recv_json(ws, pred, tries=tries)


def _recv_error(ws, command: str | None = None, tries: int = 80) -> dict:
    """Convenience: scan for the next error frame."""
    pred = (
        (lambda p: p.get("type") == "error")
        if command is None
        else (lambda p: p.get("type") == "error" and p.get("command") == command)
    )
    return _recv_json(ws, pred, tries=tries)


def _spawn_dedicated_receiver(client: TestClient, rx_id: str) -> None:
    """Spawn a dedicated file-source receiver for this test's WS lifecycle.

    The global rx-default session's stream task gets bound to the first
    TestClient's portal loop; if we reuse it across tests, later WS tests
    hang waiting for frames from a closed loop. Spawning a dedicated
    receiver per test (and destroying it at the end) keeps each test's
    lifecycle self-contained. Same pattern as test_gain_dsp_controls.py.
    """
    from openwebrx_plus.sessions.receiver_session import _default_fixture_path

    fixture = _default_fixture_path(Settings())
    spawn = client.post(
        "/api/receivers",
        json={
            "receiver_id": rx_id,
            "source_type": "file",
            "source_kwargs": {"file_path": str(fixture)},
            "center_freq": 14_150_000,
            "sample_rate": 250_000,
        },
    )
    assert spawn.status_code == 201, spawn.text


def _destroy_receiver(client: TestClient, rx_id: str) -> None:
    with contextlib.suppress(Exception):
        client.delete(f"/api/receivers/{rx_id}")


def test_ws_set_dsp_params_command_accepts_partial_patch(client: TestClient) -> None:
    """The WS setDSPParams command should accept a partial patch, store it
    on the session, and echo the merged dsp_params in subsequent metadata."""
    rx_id = "rx-dsp-partial-ws"
    _spawn_dedicated_receiver(client, rx_id)
    try:
        with client.websocket_connect(f"/ws/{rx_id}") as ws:
            # Receive the first metadata frame — dspParams all-None initially
            meta = _recv_metadata(ws, rx_id)
            assert meta["dspParams"]["low_cut_hz"] is None

            # Send a setDSPParams patch
            ws.send_text(
                json.dumps(
                    {
                        "type": "control",
                        "receiverId": rx_id,
                        "command": "setDSPParams",
                        "value": {"low_cut_hz": 300.0, "high_cut_hz": 3000.0},
                    }
                )
            )

            # Scan until the metadata echo reflects the new params. The pump
            # alternates [binary, text] and the patch isn't applied synchronously
            # — a few stale metadata frames might come through before the
            # session's dsp_params is updated.
            meta2 = _recv_json(
                ws,
                lambda p: (
                    p.get("type") == "metadata"
                    and p.get("receiverId") == rx_id
                    and p.get("dspParams", {}).get("low_cut_hz") == 300.0
                ),
                tries=120,
            )
            assert meta2["dspParams"]["high_cut_hz"] == 3000.0
    finally:
        _destroy_receiver(client, rx_id)


def test_ws_set_dsp_params_rejects_non_object_value(client: TestClient) -> None:
    """If the value field is not a dict, the server should reply with an error
    frame rather than crash."""
    rx_id = "rx-dsp-badval-ws"
    _spawn_dedicated_receiver(client, rx_id)
    try:
        with client.websocket_connect(f"/ws/{rx_id}") as ws:
            # Discard the initial metadata
            _recv_metadata(ws, rx_id)
            # Send a malformed setDSPParams
            ws.send_text(
                json.dumps(
                    {
                        "type": "control",
                        "receiverId": rx_id,
                        "command": "setDSPParams",
                        "value": "not-an-object",
                    }
                )
            )
            # The next error frame should mention "object"
            err = _recv_error(ws, "setDSPParams")
            assert "object" in err["message"]
    finally:
        _destroy_receiver(client, rx_id)


def test_ws_set_dsp_params_unknown_fields_ignored(client: TestClient) -> None:
    """Unknown fields in the patch should be silently ignored, not crash."""
    rx_id = "rx-dsp-unknown-ws"
    _spawn_dedicated_receiver(client, rx_id)
    try:
        with client.websocket_connect(f"/ws/{rx_id}") as ws:
            _recv_metadata(ws, rx_id)  # discard initial metadata
            ws.send_text(
                json.dumps(
                    {
                        "type": "control",
                        "receiverId": rx_id,
                        "command": "setDSPParams",
                        "value": {
                            "agc_enabled": True,
                            "bogus_future_field": "ignored",
                        },
                    }
                )
            )
            # Scan until the metadata echo shows agc_enabled=True.
            meta = _recv_json(
                ws,
                lambda p: (
                    p.get("type") == "metadata"
                    and p.get("receiverId") == rx_id
                    and p.get("dspParams", {}).get("agc_enabled") is True
                ),
                tries=120,
            )
            assert meta is not None
    finally:
        _destroy_receiver(client, rx_id)


def test_ws_set_dsp_params_partial_then_full_state(client: TestClient) -> None:
    """Send one patch, then another — the second patch should merge, not
    replace, the first."""
    rx_id = "rx-dsp-merge-ws"
    _spawn_dedicated_receiver(client, rx_id)
    try:
        with client.websocket_connect(f"/ws/{rx_id}") as ws:
            _recv_metadata(ws, rx_id)  # initial metadata
            # First patch: low_cut_hz=400
            ws.send_text(
                json.dumps(
                    {
                        "type": "control",
                        "receiverId": rx_id,
                        "command": "setDSPParams",
                        "value": {"low_cut_hz": 400.0},
                    }
                )
            )
            meta1 = _recv_json(  # noqa: F841 — first-patch sentinel; the assert is in the predicate
                ws,
                lambda p: (
                    p.get("type") == "metadata"
                    and p.get("receiverId") == rx_id
                    and p.get("dspParams", {}).get("low_cut_hz") == 400.0
                ),
                tries=120,
            )
            # Second patch: high_cut_hz=2500
            ws.send_text(
                json.dumps(
                    {
                        "type": "control",
                        "receiverId": rx_id,
                        "command": "setDSPParams",
                        "value": {"high_cut_hz": 2500.0},
                    }
                )
            )
            # Scan until both fields are set (merged).
            meta2 = _recv_json(
                ws,
                lambda p: (
                    p.get("type") == "metadata"
                    and p.get("receiverId") == rx_id
                    and p.get("dspParams", {}).get("low_cut_hz") == 400.0
                    and p.get("dspParams", {}).get("high_cut_hz") == 2500.0
                ),
                tries=120,
            )
            assert meta2 is not None
    finally:
        _destroy_receiver(client, rx_id)


# ----------------------------------------------------------------------------
# AudioChain construction tests (with dsp_params)
# ----------------------------------------------------------------------------


def test_audio_chain_constructs_with_default_dsp_params() -> None:
    """A chain with no dsp_params should build fine — no optional blocks."""
    from openwebrx_plus.dsp.audio import AudioChain

    chain = AudioChain(
        mode="USB",
        input_rate=240_000,
        output_rate=8000,
        channel_offset_hz=0.0,
        conditioning=True,
    )
    try:
        # Optional blocks should not exist on a default chain
        assert chain.dsp_params.to_dict() == DSPParams().to_dict()
        assert getattr(chain, "_squelch", None) is None
        assert getattr(chain, "_agc", None) is None
        assert getattr(chain, "_gain", None) is None
        assert getattr(chain, "_nfm_deemph", None) is None
    finally:
        chain.stop()


def test_audio_chain_constructs_with_agc() -> None:
    """AGC-enabled chain should have an Agc block and skip the soft Limit."""
    from openwebrx_plus.dsp.audio import AudioChain

    chain = AudioChain(
        mode="USB",
        input_rate=240_000,
        output_rate=8000,
        channel_offset_hz=0.0,
        conditioning=True,
        dsp_params=DSPParams(agc_enabled=True),
    )
    try:
        assert chain.dsp_params.agc_enabled is True
        assert getattr(chain, "_agc", None) is not None
        # The soft Limit should be skipped when AGC is on
        assert getattr(chain, "_limit", None) is None
    finally:
        chain.stop()


def test_audio_chain_constructs_with_squelch() -> None:
    """Squelch-enabled chain should have a Squelch block."""
    from openwebrx_plus.dsp.audio import AudioChain

    chain = AudioChain(
        mode="NFM",
        input_rate=240_000,
        output_rate=8000,
        channel_offset_hz=0.0,
        conditioning=True,
        dsp_params=DSPParams(squelch_db=-40.0),
    )
    try:
        assert chain.dsp_params.squelch_db == -40.0
        assert getattr(chain, "_squelch", None) is not None
    finally:
        chain.stop()


def test_audio_chain_constructs_with_manual_bandpass() -> None:
    """Manual low_cut/high_cut override the mode profile's defaults."""
    from openwebrx_plus.dsp.audio import AudioChain

    chain = AudioChain(
        mode="USB",
        input_rate=240_000,
        output_rate=8000,
        channel_offset_hz=0.0,
        conditioning=True,
        dsp_params=DSPParams(low_cut_hz=200.0, high_cut_hz=2200.0),
    )
    try:
        assert chain.dsp_params.low_cut_hz == 200.0
        assert chain.dsp_params.high_cut_hz == 2200.0
    finally:
        chain.stop()


def test_audio_chain_constructs_with_manual_gain() -> None:
    """Manual gain inserts a Gain block."""
    from openwebrx_plus.dsp.audio import AudioChain

    chain = AudioChain(
        mode="AM",
        input_rate=240_000,
        output_rate=8000,
        channel_offset_hz=0.0,
        conditioning=True,
        dsp_params=DSPParams(manual_gain_db=6.0),
    )
    try:
        assert chain.dsp_params.manual_gain_db == 6.0
        assert getattr(chain, "_gain", None) is not None
    finally:
        chain.stop()


def test_audio_chain_constructs_with_nfm_deemphasis() -> None:
    """NFM + deemphasis_enabled=True inserts an NfmDeemphasis block."""
    from openwebrx_plus.dsp.audio import AudioChain

    chain = AudioChain(
        mode="NFM",
        input_rate=240_000,
        output_rate=8000,
        channel_offset_hz=0.0,
        conditioning=True,
        dsp_params=DSPParams(deemphasis_enabled=True),
    )
    try:
        assert getattr(chain, "_nfm_deemph", None) is not None
    finally:
        chain.stop()
