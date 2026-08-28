#!/usr/bin/env python3
"""Append a new section to worklog.md using proper append mode."""
from pathlib import Path

NEW_ENTRY = """
---
Task ID: 30
Agent: super-z (main agent)
Task: Push slices 27/28/29 (FT8 LDPC v2/v2.1 work) to origin/main using the user-provided GitHub PAT. Verify CI green. Update stale AI-HANDOFF.md \u00a74/\u00a75.1 to reflect actual state.

Work Log:
- User provided fresh GitHub PAT (fine-grained, repos:write+contents:write+actions:read) via IM at session start.
- Read prior session handoff summary; per the standing "Continue" directive, began by attempting the cheapest roadmap item from AI-HANDOFF.md \u00a75.1: add `# type: ignore[untyped-decorator]` at lines 193/207 of apps/server/openwebrx_plus/sources/sdrplay.py to silence the 2 documented mypy --strict errors.
- Applied the edits via MultiEdit, ran `mypy --strict openwebrx_plus/sources/sdrplay.py` \u2192 "Success: no issues found" (because the type:ignore was in place). Then ran `mypy --strict openwebrx_plus` \u2192 clean across 60 files.
- Ran `scripts/run-server-tests.sh` \u2192 504 passed, 1 skipped (up from documented 456+1; slice-21/22 + 26/28/29 tests added).
- Before committing, discovered via `git log origin/main..HEAD` that slices 27/28/29 (FT8 LDPC v2 + v2.1 work) were already committed locally but NOT pushed (origin/main was at slice-26 = 3e88175, local HEAD was fd7c98c = slice-29).
- Inspected slice-23 (commit 6800d05) and found the prior session had already addressed the EXACT issue I was about to fix:
  * slice-23 added `# type: ignore[untyped-decorator]` on the same two lines, then REMOVED them because they cause CI to FAIL.
  * Reason: CI runs `uv run mypy openwebrx_plus` which reads pyproject.toml where `disallow_untyped_decorators = false`. Under that config, the type: ignore is "unused" which is itself an error in strict mode. The `mypy --strict` CLI flag overrides pyproject to re-enable the check \u2014 that's the only invocation that surfaces the 2 errors, and that's NOT how CI invokes mypy.
  * Conclusion: the AI-HANDOFF.md \u00a75.1 "sdrplay mypy --strict fix" roadmap item was stale; the \u00a74 "2 known mypy --strict errors" verified-status note was misleading (CI is actually green).
- Reverted my changes via `git checkout apps/server/openwebrx_plus/sources/sdrplay.py` to match origin/main state. Verified working tree clean.
- Confirmed the actual mypy state on the reverted file:
  * `mypy openwebrx_plus` (CI invocation): Success, 60 source files clean.
  * `mypy --strict openwebrx_plus` (CLI override, NOT how CI invokes): 2 errors on sdrplay.py lines 193/207 \u2014 but these are NOT blocking CI.
- Verified the full local gate on HEAD (fd7c98c = slice-29) before pushing:
  * mypy openwebrx_plus: Success, 60 source files clean.
  * ruff check .: All checks passed!
  * server pytest: 504 passed, 1 skipped (84% coverage).
  * web tsc --noEmit: clean (no output).
  * web vitest run: 178 passed across 14 test files.
- Pushed slices 27/28/29 to origin/main via PAT-strip protocol. Used the safer "inline URL argument" variant: `git push "https://x-access-token:${PAT}@github.com/...git" main:main`. This avoids `git remote set-url` entirely, so the configured remote URL is never modified and the PAT never enters git config or reflog. The PAT existed only in shell memory for the duration of the single push command; `unset PAT` immediately afterward.
- Verified post-push:
  * `git remote get-url origin` returns `https://github.com/nealhamiltonjr/websdr.git` (clean, no PAT)
  * `git fetch origin` \u2192 `3e88175..fd7c98c  main -> origin/main`
  * `git rev-parse HEAD` == `git rev-parse origin/main` == fd7c98cb42d506c4ec4b710fd7329e2ce97969b4
- Queried GitHub Actions API for CI status on the new HEAD. Run 33142141112 (head_sha fd7c98cb42) was `in_progress` at first query; polled per-job status: 4/5 jobs green within ~90s, Backend (the longest \u2014 pytest 504 tests + mypy + ruff) was the last to finish. After ~2 minutes total, run 33142141112 completed with conclusion=success, all 5 jobs green.
- Updated stale AI-HANDOFF.md sections to reflect actual state (file had been written at "post slice-21" and never updated for slices 22-29):
  * \u00a74 verified-status table: server tests 456+1 \u2192 504+1; mypy row rewritten to clarify CI invocation = clean / --strict CLI invocation = 2 errors NOT a CI failure; web vitest 163 \u2192 178; added CI run row (33142141112 all-5-jobs success).
  * \u00a74 ft8 bullet: rewritten from "contract stub (slice-21); FSK demod remains" to "v2.1 fully wired (slices 21\u219226\u219228\u219229)" with detail on hard+v1, real LDPC v2, soft FSK v2.1 primary path.
  * \u00a74 AI denoise cascade bullet: rewritten from "Stage 2b NOT yet wired" to "slice-19 + slice-24 closes the AudioWorklet gap" with detail on rnnoise-processor.ts.
  * \u00a74 slice history table: prepended 8 new rows for slices 22-29 (FT8 v1+v2+v2.1, SDRangel v1, RNNoise worklet, sdrplay type:ignore add+revert, secondary FFT wire format, uv.lock sync).
  * \u00a75.1 sdrplay mypy fix: rewritten from "immediate hygiene" to "\u2705 RESOLVED (slice-23 + slice-23b)" with explanation of why the naive type:ignore re-breaks CI.
  * \u00a75.2 FT8 demodulator: rewritten from "closes slice-21 stub" to "\u2705 SHIPPED through v2.1" with detail on v1 (slice-26), v2 (slice-28), v2.1 (slice-29), and remaining v3 items.
  * \u00a75.3 RNNoise AudioWorklet: rewritten from "closes slice-19 loader" to "\u2705 SHIPPED (slice-24, commit cc68991)" with implementation detail.
  * \u00a75.4 SDRangel REST+WS: rewritten from "closes slice-20 scaffold" to "\u2705 v1 SHIPPED (slice-25)" with audio-over-WS still deferred.
- Updated stale STATUS.md:
  * Header addendum line: 163 web tests \u2192 178 across 14 files; added CI run 33142141112 reference; corrected sdrplay slice-23 wording to "reverted in slice-23b".
  * TL;DR: FIVE \u2192 SIX live decoders; AI denoiser \u2192 AI denoise cascade with three stages; added 'WRSF' secondary FFT wire format mention; updated remaining frontier.
  * Verified health table: 412+/412+ \u2192 504+1 server; 142/142 \u2192 178/178 web; mypy 54 \u2192 60 files; CI row updated to run 33142141112 for fd7c98c.
  * Codebase size: ~25.5k Python \u2192 ~32.5k; ~12.5k TS/TSX \u2192 ~14.5k.
  * Registered sources 11 \u2192 12 (sdrangel added in slice-20/25).
  * Decoder plugins 5 \u2192 6 (ft8 added in slice-21/26).
- Caught and recovered from a tooling error mid-task: used the Write tool (which OVERWRITES) on worklog.md instead of appending. Lost 1018 lines of prior history. Recovered via `git checkout HEAD -- worklog.md` before staging; this script (scripts/append_worklog.py) is the safe-append pattern going forward.

Stage Summary:
- Three real FT8 LDPC commits pushed: slice-27 (uv.lock sync), slice-28 (real LDPC 174,91 codec with syndrome check + sum-product decoder), slice-29 (v2.1 wires soft FSK demod \u2192 sum-product LDPC as primary decode path with hard-decision fallback).
- Origin/main now matches local HEAD: `fd7c98cb42d506c4ec4b710fd7329e2ce97969b4`.
- The "sdrplay mypy --strict fix" roadmap item from the stale AI-HANDOFF.md \u00a75.1 is moot \u2014 slice-23b already concluded CI is green under the project's actual mypy invocation; the 2 --strict-CLI errors are a non-CI artifact.
- PAT-strip protocol hardened: switched from `git remote set-url` (which leaves the PAT in git config until manually stripped) to inline-URL push (PAT never enters git config/reflog at all).
- All local gates verified green on HEAD before push: mypy 60 files clean / ruff clean / server 504+1 / web tsc clean / web vitest 178.
- CI run 33142141112 for fd7c98c completed/success \u2014 all 5 jobs green (Frontend / Backend / DSP / AI / Shared-Types).
- AI-HANDOFF.md + STATUS.md now honestly reflect actual state for the next session.
"""

worklog = Path("/home/z/my-project/worklog.md")
with worklog.open("a", encoding="utf-8") as f:
    f.write(NEW_ENTRY)

print(f"Appended {len(NEW_ENTRY)} chars to {worklog}")
print(f"New line count: {sum(1 for _ in worklog.open())}")
