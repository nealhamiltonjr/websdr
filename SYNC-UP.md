# Sync-Up Workflow

How and when the local working copy gets synced to the upstream GitHub
repository at <https://github.com/nealhamiltonjr/websdr>.

## Cadence rule

**After every major revision and at the boundary of every major section,
sync up with the repo.** Do not batch multiple major changes into a single
push — each major revision deserves its own commit (or its own small,
focused commit series) so the history tells the story of the build.

### What counts as a "major revision"

Use the slice model from `docs/AI-HANDOFF.md` as the calibration:

- A **slice boundary** (e.g. slice-4.7 → slice-4.8, or slice-4.9 → slice-5.0).
- A **new feature landing end-to-end** with its tests green (a new source
  backend, a new decoder plugin, a new visualization, a new DSP mode).
- A **cross-cutting change** that touches multiple packages (e.g. a wire
  format change in `packages/shared-types` that ripples into both the server
  and the web client).
- A **CI/build/bootstrap change** (anything inside `.github/workflows/`,
  the top-level `Makefile`, or `scripts/README-dsp-bootstrap.md`).
- A **documentation pass** that meaningfully changes the orientation
  (`README.md`, `ARCHITECTURE.md`, an ADR, `docs/STATUS.md`).

### What does NOT count as a major revision

- Single-file test or doc tweaks.
- Local scratch work that doesn't compile or pass gates yet.
- Cosmetic formatting.
- Trivial typo fixes.

Bundle these into the next major sync-up commit; don't push them on their
own.

## Pre-push checklist (the slice protocol, abridged)

Before pushing, all of these must be true:

1. **Server tests green** — `scripts/run-server-tests.sh` exits 0.
2. **Web tests green** — `cd apps/web && pnpm exec vitest run && pnpm exec tsc --noEmit` exits 0.
3. **Lint clean** — `ruff check apps/server` and `pnpm -r run lint` both clean.
4. **mypy strict** — `mypy openwebrx_plus` in `apps/server` clean.
5. **Vite build** — `pnpm -r run build` succeeds.
6. **No secrets staged** — verify with `git diff --cached --name-only | grep -iE '(env|secret|key|pem|p12)'` (empty output expected).
7. **Commit message describes the slice** — use the conventional format
   `slice-4.X: <one-line subject>` then a body that lists what was built,
  what was fixed, and any open follow-ups.

## Push protocol

The remote is configured as `origin` pointing at
`https://github.com/nealhamiltonjr/websdr.git`. Push with explicit tracking:

```bash
# from the repo root
git push -u origin main
```

If the push is rejected because GitHub requires `workflow` scope (the
commit modifies `.github/workflows/*.yml`), regenerate the PAT with the
`Workflows: Read and write` permission and retry.

## Token hygiene

- Use fine-grained PATs scoped to **only this repository**.
- Required permissions: `Contents: Read and write`, `Workflows: Read and
  write` (the latter only for commits that touch CI files).
- Set the PAT into the remote URL **temporarily**, push, then strip it
  back to the bare HTTPS URL so the token is not persisted in `.git/config`.
- Rotate the PAT after every major sync-up if you want to be cautious.

## Commit message template

```
slice-X.Y: <one-line subject>

What was built:
- <feature/fix 1>
- <feature/fix 2>

What was fixed:
- <bug 1>

Open follow-ups:
- <deferred item, if any>

Quality gates:
- server: 229/229 tests, mypy strict, ruff clean
- web: 95/95 tests, tsc clean, vite build clean
```
