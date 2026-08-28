#!/usr/bin/env python3
"""Append a new section to worklog.md using proper append mode."""
from pathlib import Path

NEW_ENTRY = """
---
Task ID: 31
Agent: super-z (main agent)
Task: Slice-31 — dump1090 fixture improvements (AI-HANDOFF.md \u00a75.6). Implement (1) auto-detect dump1090-fa/mutability/readsb fork identity in the SBS1 bridge; (2) auto-discovery of a running dump1090 SBS1 server in the dump1090 plugin; (3) two new failure modes in fake_dump1090.py for the runner's vanish/partial-JSON recovery paths.

Work Log:
- Read AI-HANDOFF.md \u00a75.6: three deliverables \u2014 (a) auto-detect dump1090-fa vs mutability; (b) --net-ro-port auto-discovery; (c) extend fake_dump1090 failure modes (TCP-EOF mid-handshake, malformed CSV, partial JSON row).
- Inspected existing scripts/sbs1_to_ndjson.py + apps/server/openwebrx_plus/plugins/dump1090.py + apps/server/tests/fakes/fake_dump1090.py + apps/server/tests/test_sbs1_bridge.py + apps/server/tests/test_subprocess_plugins.py to understand the existing failure mode surface (--crash-after, --garbage-lines, --stall-secs, --echo-stats) and the existing test patterns (_FakeSbs1Server, _spec/_plugin helpers, restart_backoff=(0.05,) for fast tests).
- Designed slice-31 scope:
  * Fork auto-detect: probe <bin> --version (or -V for readsb) once at startup; grep for known fork signatures ("dump1090-fa" / "dump1090-mutability" / "readsb"); report in the ready event. Override via OPENWEBRX_PLUS_DUMP1090_FORK env var.
  * Auto-discovery: if OPENWEBRX_PLUS_DUMP1090_BIN is unset, probe 127.0.0.1:30003 (the standard SBS1 port for dump1090-fa/mutability/readsb). If reachable, default to the SBS1 bridge script in --no-spawn mode against that endpoint \u2014 operators with a running dump1090 service need no extra config.
  * Failure modes: --vanish-after-ready-secs N (emit ready, sleep, close stdout, exit cleanly \u2014 tests the runner's "decoder vanished after ready" path) + --emit-partial-json-die (emit ready, write truncated JSON, exit 0 \u2014 tests the runner's JSON parser recovery).
- Implemented _probe_fork(binary) + _resolve_fork() in scripts/sbs1_to_ndjson.py:
  * Subprocess.run with 1.0s timeout, never raises.
  * Order matters in the signature matching: "mutability" checked before "fa" (mutability's version string also contains "dump1090" but not "fa"); "readsb" has no "dump1090" in its output at all.
  * Bare "dump1090 1.x" version strings (no -fa suffix) treated as fa-shaped (most modern forks are fa-derived).
  * Override path: invalid override value falls back to "unknown" and warns on stderr.
- Added "fork" field to the ready event emitted by sbs1_to_ndjson.py.
- Implemented _probe_local_sbs1(host, port, timeout_s) + _bridge_script_path() + auto-discovery branch in apps/server/openwebrx_plus/plugins/dump1090.py:
  * Best-effort TCP probe: socket.create_connection with timeout; OSError \u2192 False; never raises.
  * _bridge_script_path resolves scripts/sbs1_to_ndjson.py via Path(__file__).parents[4] (plugins/ \u2192 openwebrx_plus/ \u2192 apps/server/ \u2192 apps/ \u2192 repo root). Returns bare filename as fallback if the script doesn't exist (operator's PATH must include it).
  * _default_spec: when OPENWEBRX_PLUS_DUMP1090_BIN is unset AND _probe_local_sbs1 returns True, returns a SubprocessSpec pointing at "python3 <bridge_path> --no-spawn --connect-host 127.0.0.1 --connect-port 30003" with ready_timeout=5.0; otherwise falls through to the legacy "dump1090" default.
  * Restructured nested if (SIM102) into flat bin_unset + sbs1_reachable intermediate variables \u2014 same logic, ruff-clean.
- Bumped dump1090 plugin manifest version 0.1.0 \u2192 0.2.0; extended description to mention auto-discovery + fork detection.
- Added two new failure modes to apps/server/tests/fakes/fake_dump1090.py:
  * --vanish-after-ready-secs N: emit ready, sleep N secs, close stdout, sleep 0.1s for stdout reader to wake, return (Python runtime exits cleanly).
  * --emit-partial-json-die: emit ready, write a truncated JSON line ('{"kind": "frame", "icao": "ABC123", "raw": "this line is deliberately truncated,,,'), close stdout, exit 0.
- Wrote tests:
  * tests/test_sbs1_bridge.py extended with TestForkAutoDetect (8 tests): probe returns "fa" for dump1090-fa + bare dump1090 1.x; "mutability"; "readsb"; None for unrecognized output; None for missing binary; _resolve_fork honors OPENWEBRX_PLUS_DUMP1090_FORK override; returns "unknown" when probe fails; warns on invalid override value. Added assertion to existing test_bridge_translates_sbs1_to_ndjson that the ready event includes a "fork" field.
  * tests/test_dump1090_plugin.py (NEW, 9 tests): _probe_local_sbs1 returns True when a server is listening (uses _TinyTcpServer fixture); False on connection refused; False on timeout (uses 192.0.2.1 RFC 5737 documentation address); False on DNS failure (invalid.invalid.invalid hostname). _bridge_script_path returns absolute path when script exists; bare filename when missing. _default_spec uses bridge mode when env unset + probe True; legacy mode when env set (probe NOT called \u2014 side_effect=AssertionError verifies); legacy mode when env unset + probe False; bridge mode when env empty string.
  * tests/test_subprocess_plugins.py extended with 2 failure-mode tests: test_vanish_after_ready_triggers_restart_then_failure + test_emit_partial_json_die_counts_parse_error_then_fails. Both use restart_backoff=(0.05,) for 1 restart; assert ready event seen, restarts >= 1, final state = "failed", failed decoder_state event surfaced. The partial-JSON test additionally asserts parse_errors >= 1.
- Caught and fixed mid-implementation bugs:
  * Initial _bridge_script_path used parents[3] (off-by-one \u2014 should be parents[4] for the repo root). Test failure exposed it: returned bare "sbs1_to_ndjson.py" instead of the absolute path. Fixed.
  * Initial ruff SIM102 violation in _default_spec (nested if not os.environ.get + if _probe_local_sbs1). Restructured as flat intermediate variables (bin_unset + sbs1_reachable). Clean.
  * Initial ruff I001 (unsorted imports) in tests/test_dump1090_plugin.py. Auto-fixed via ruff --fix.
- All quality gates verified GREEN on HEAD before push:
  * mypy openwebrx_plus (CI invocation): Success, no issues found in 60 source files.
  * ruff check .: All checks passed!
  * server pytest: 525 passed, 1 skipped (was 504+1; +21 new tests: 8 fork detection + 9 dump1090_plugin + 2 ready event field assertion + 2 failure modes).
  * web vitest: 178/178 pass across 14 files (unchanged; no web changes).

Stage Summary:
- Slice-31 closes AI-HANDOFF.md \u00a75.6: dump1090 fixture improvements ship. (1) Fork auto-detect via --version probe + OPENWEBRX_PLUS_DUMP1090_FORK override; (2) auto-discovery of running SBS1 server via TCP probe of 127.0.0.1:30003; (3) two new fake_dump1090 failure modes for the runner's vanish/partial-JSON recovery paths.
- Operator UX improvement: a stock dump1090-fa/mutability/readsb service running on 127.0.0.1:30003 now "just works" with OpenWebRX+ \u2014 no OPENWEBRX_PLUS_DUMP1090_BIN env var required. The plugin probes 30003 at startup and if reachable, default to the SBS1 bridge in --no-spawn mode against it.
- The fork field in the ready event gives operators diagnostic visibility: they can see at attach time whether the bridge identified their binary as dump1090-fa / mutability / readsb / unknown.
- The two new failure modes give the test suite coverage of the runner's "decoder vanished after ready" and "decoder emitted broken JSON" recovery paths \u2014 previously only the crash-restart path was covered.
- All local gates verified green on HEAD before push: mypy 60 files clean / ruff clean / server 525+1 / web 178.
- AI-HANDOFF.md \u00a75.6 will be marked RESOLVED in the next doc-refresh commit.
"""

worklog = Path("/home/z/my-project/worklog.md")
with worklog.open("a", encoding="utf-8") as f:
    f.write(NEW_ENTRY)

print(f"Appended {len(NEW_ENTRY)} chars to {worklog}")
print(f"New line count: {sum(1 for _ in worklog.open())}")
