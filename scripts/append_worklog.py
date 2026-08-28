#!/usr/bin/env python3
"""Append a new section to worklog.md using proper append mode."""
from pathlib import Path

NEW_ENTRY = """
---
Task ID: 32
Agent: super-z (main agent)
Task: Slice-32 — federation polish follow-up: secondary-demod forwarding for openwebrx_remote (closes the AI-HANDOFF.md \u00a75.7 sub-bullet). Decoder events from upstream OpenWebRX+ peers should reach the client's viz panels.

Work Log:
- Read AI-HANDOFF.md \u00a75.7 for the "Federation polish follow-up" item: "decoder events from upstream receivers should reach the client's viz. The HD audio half shipped in slice-14."
- Inspected existing federation protocol surface in sources/openwebrx_remote.py: wire types 0x01 FFT, 0x02 audio, 0x03 secondary FFT (slice-22), 0x04 HD audio (slice-14). The DisplayStreamSource protocol in sources/base.py declares the yieldable frame types; ReceiverSession._run_display in sessions/receiver_session.py dispatches them by isinstance() check and repacks into the wire formats broadcast to WS subscribers.
- Inspected slice-22's pattern as the template for slice-32:
  * sources/base.py: RemoteSecondaryFftFrame dataclass with the channel-scope bins + center_freq + sample_rate.
  * sources/openwebrx_remote.py: _TYPE_SECONDARY_FFT = 0x03 + decode branch in _handle_binary that yields a RemoteSecondaryFftFrame.
  * sessions/receiver_session.py: _run_display dispatch branch that calls _pack_secondary_fft_frame and broadcasts.
  * tests/test_openwebrx_remote_driver.py: FakeOpenWebRxServer.send_secondary_fft option + 0x03 frame emission in the pump loop + _collect helper extended to optionally collect RemoteSecondaryFftFrame + 3 tests (decode, absent-when-not-configured, session-forwarding).
- Designed slice-32 mirror:
  * New wire type 0x05 \u2014 _TYPE_DECODER_EVENT.
  * Wire format: [1-byte type=0x05][2-byte decoder_name_len LE][N-byte decoder_name UTF-8][4-byte event_json_len LE][M-byte event_json UTF-8]. The 2-byte / 4-byte length prefixes cap name at 65535 bytes + JSON payload at 4 GiB (more than enough for any decoder event).
  * New dataclass RemoteDecoderEvent in sources/base.py: decoder_name: str + event: dict[str, Any].
  * Decode branch in _handle_binary that parses the wire format defensively (too-short, truncated, JSON-parse-failed, not-a-dict all return None with a debug log).
  * ReceiverSession._run_display dispatch branch that broadcasts a JSON envelope matching the local decoder event shape (type/decoder/receiverId/event) PLUS a new "remote": true field so the frontend can optionally render a "remote" badge.
- Updated DisplayStreamSource protocol type annotation in sources/base.py to include RemoteDecoderEvent in the AsyncGenerator yield type.
- Updated display_stream + _pump return type annotations in sources/openwebrx_remote.py to match.
- Extended FakeOpenWebRxServer with 3 new fields: send_decoder_event, decoder_name, decoder_event_kind. When send_decoder_event=True, the pump loop emits 0x05 frames with a canned FT8-style payload (kind="digi_message", callsign="OH8ABC", grid="JO30", raw="CQ OH8ABC JO30", db=-10, frequency=3570000+frame_idx).
- Extended _collect helper with want_decoder=0 parameter + a 4th return list (decoder_frames). Updated 4 existing _collect callers to unpack 4 elements (3 used _ for the new 4th; 2 used bare `await _collect(...)` which is unchanged).
- Wrote 3 new tests in tests/test_openwebrx_remote_driver.py:
  * test_decoder_event_frames_decode_with_remote_payload: FakeOpenWebRxServer(send_decoder_event=True) + _collect(want_decoder=1) \u2192 RemoteDecoderEvent with decoder_name="ft8" + event.kind="digi_message" + event.callsign="OH8ABC" + event.grid="JO30" + event.raw="CQ OH8ABC JO30".
  * test_decoder_event_frames_absent_when_not_configured: send_decoder_event=False \u2192 decoder_frames == [].
  * test_decoder_event_session_forwards_as_json_envelope: end-to-end through ReceiverSession \u2192 broadcast JSON envelope with type="decoder", decoder="ft8", receiverId="rx-decoder-test", remote=true, event.{kind,callsign,grid} matching the canned payload.
- All quality gates verified GREEN on HEAD before push:
  * mypy openwebrx_plus (CI invocation): Success, no issues found in 60 source files.
  * ruff check .: All checks passed!
  * server pytest: 528 passed, 1 skipped (was 525+1; +3 new slice-32 tests).
  * web tsc --noEmit: clean (no output).
  * web vitest: 178/178 pass across 14 test files (unchanged; no web changes).
- AI-HANDOFF.md updates:
  * \u00a74 verified-status table: server tests 525 \u2192 528; CI run row marked as slice-31 \u00d7 pending slice-32 CI run.
  * \u00a74 slice history table: prepended row for slice-32.
  * \u00a75.7 federation polish follow-up sub-bullet: marked \u2705 SHIPPED (slice-32) + added the slice-32 detail block at the end of \u00a75.7.
  * \u00a74 footer "31 entries" \u2192 "32 entries".

Stage Summary:
- Slice-32 closes the AI-HANDOFF.md \u00a75.7 "Federation polish follow-up" sub-bullet: secondary-demod forwarding for openwebrx_remote. Decoder events (FT8 messages, ADS-B frames, AIS sentences, CW characters) emitted by upstream OpenWebRX+ peers now reach downstream clients' viz panels without the client needing to re-run the demod locally.
- Operator UX improvement: an operator chaining OpenWebRX+ peers (e.g. a remote SDR at a friend's QTH feeding their local OpenWebRX+ instance) now sees decoded FT8 messages / aircraft tracks in their own UI, sourced from the upstream receiver. Legacy OpenWebRX / KiwiSDR / SpyServer peers never send 0x05 \u2014 the decode branch is a graceful no-op for them.
- The "remote: true" field in the JSON envelope tags these events for the frontend; a follow-up frontend slice can optionally render a "remote" badge on the digi-message row. (No frontend changes in this slice.)
- All local gates verified green on HEAD before push: mypy 60 files clean / ruff clean / server 528+1 / web tsc clean / web vitest 178.
"""

worklog = Path("/home/z/my-project/worklog.md")
with worklog.open("a", encoding="utf-8") as f:
    f.write(NEW_ENTRY)

print(f"Appended {len(NEW_ENTRY)} chars to {worklog}")
print(f"New line count: {sum(1 for _ in worklog.open())}")
