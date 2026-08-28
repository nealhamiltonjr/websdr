"""OpenWebRX+ backend orchestration.

This package:
  - Loads configuration (TOML + env, via pydantic-settings)
  - Spawns SDR source backends (RTL-SDR, KiwiSDR, Spyserver, ...)
  - Runs ReceiverSessions (one per client receiver)
  - Hosts the REST API and WebSocket server
  - Manages plugin subprocesses (RF-band + audio-band decoders)

Slice-1 status: only the bare minimum to confirm the contract.
  - Settings load from env / TOML
  - FastAPI app boots at / and /api/health
  - WebSocket at /ws/{receiver_id} echoes a "hello" then idles
  - RTL-SDR source stub raises NotImplementedError on actual IQ
"""

__version__ = "0.1.0"
