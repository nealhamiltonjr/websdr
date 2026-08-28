"""End-to-end WebSocket FFT frame smoke test.

Boots the FastAPI app via TestClient, connects to /ws/rx-default, reads
the first FFT frame, and asserts the binary wire format is correct.
"""

from __future__ import annotations

import contextlib
import struct

import pytest
from fastapi.testclient import TestClient

from openwebrx_plus.api.rest import create_app
from openwebrx_plus.config import Settings
from openwebrx_plus.sessions.receiver_session import (
    FFT_HEADER_MAGIC,
    FFT_HEADER_SIZE_BYTES,
    FFT_HEADER_VERSION,
)


@pytest.fixture
def client() -> TestClient:
    settings = Settings(tier="dev")
    app = create_app(settings)
    return TestClient(app)


def test_ws_fft_frame_wire_format(client: TestClient) -> None:
    """Connect to /ws/rx-default, receive first binary frame, verify the
    32-byte header + body structure matches the shared-types spec.

    The server alternates: send_bytes (FFT frame) → send_text (metadata) →
    send_bytes → ... So receive_bytes() will eventually return the first
    binary frame (within ~100ms given fft_fps=10).

    Slice-3: rx-default replays the baked 20 m fixture — 14.150 MHz at
    250 kSPS (see create_default_session + ADR-005).
    """
    with client.websocket_connect("/ws/rx-default") as ws:
        binary_frame: bytes | None = None
        # Try receive_bytes a few times; if we get a text message first,
        # we'll see an exception that we catch and continue.
        for _ in range(5):
            try:
                binary_frame = ws.receive_bytes()
                break
            except Exception:
                # Starlette's TestClient raises WebSocketDisconnect on text
                # messages when you call receive_bytes. Drain and retry.
                with contextlib.suppress(Exception):
                    ws.receive_text()

        assert binary_frame is not None, "no binary FFT frame received in 5 attempts"
        assert len(binary_frame) > FFT_HEADER_SIZE_BYTES, "frame too small"

        # Parse the 32-byte header.
        (magic, version, rx_hash, center_freq, sample_rate, min_db, max_db, bin_count) = (
            struct.unpack("<IIIffffI", binary_frame[:FFT_HEADER_SIZE_BYTES])
        )
        assert magic == FFT_HEADER_MAGIC
        assert version == FFT_HEADER_VERSION
        assert rx_hash != 0
        assert center_freq == 14_150_000  # baked hf_20m_evening fixture
        assert sample_rate == 250_000
        assert min_db == -100.0
        assert max_db == -20.0
        assert bin_count > 0

        # Body: binCount * 4 bytes of float32.
        body_len = len(binary_frame) - FFT_HEADER_SIZE_BYTES
        assert body_len == bin_count * 4, (
            f"body len {body_len} != binCount {bin_count} * 4 = {bin_count * 4}"
        )
