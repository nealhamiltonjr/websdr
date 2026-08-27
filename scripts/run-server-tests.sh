#!/bin/sh
# Run the openwebrx-plus server test suite with the right environment.
#
# IMPORTANT: do NOT use `uv run` / `uv sync` in apps/server — the venv holds
# a manually-restored pycsdr build (see scripts/README-dsp-bootstrap.md).
# A `uv sync` will evict pycsdr from site-packages and break the DSP chain.
# Use this script instead.
#
# Usage:
#   scripts/run-server-tests.sh                     # full suite
#   scripts/run-server-tests.sh tests/test_smoke.py # single file
#   scripts/run-server-tests.sh -k adsb             # keyword filter
#
# The script resolves paths relative to the repo root, so it can be run
# from anywhere. It prefers apps/server/.venv/bin/python if present (the
# dev venv with pycsdr installed), else falls back to system python3 with
# a warning (useful for CI where pycsdr has been built separately).

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVER_DIR="$REPO_ROOT/apps/server"

# libcsdr lives in a user prefix; honor LD_LIBRARY_PATH if already set.
export LD_LIBRARY_PATH="$HOME/.local/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# Prefer the dev venv's python if present (the one with pycsdr installed).
if [ -x "$SERVER_DIR/.venv/bin/python" ]; then
    PY="$SERVER_DIR/.venv/bin/python"
else
    PY="$(command -v python3 2>/dev/null || command -v python)"
    if [ -z "$PY" ]; then
        echo "[run-server-tests] error: no python interpreter found." >&2
        exit 127
    fi
    echo "[run-server-tests] warning: no .venv at $SERVER_DIR/.venv/bin/python;" >&2
    echo "[run-server-tests]          using $PY (make sure pycsdr is importable)." >&2
fi

cd "$SERVER_DIR"
exec "$PY" -m pytest "$@"
