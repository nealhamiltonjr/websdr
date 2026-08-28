# OpenWebRX+ — top-level Makefile
# Orchestrates pnpm (web + shared-types), uv (server), cargo (AI), and zig (DSP).

.PHONY: install dev dev-web dev-server build lint typecheck test clean check-prereqs

SHELL := /bin/bash

# --- Prereqs ----------------------------------------------------------------

check-prereqs:
	@echo "Checking toolchain..."
	@command -v node   >/dev/null 2>&1 || { echo "node is required (>=22)"; exit 1; }
	@command -v pnpm   >/dev/null 2>&1 || { echo "pnpm is required (>=9)"; exit 1; }
	@command -v uv     >/dev/null 2>&1 || { echo "uv is required (https://docs.astral.sh/uv)"; exit 1; }
	@command -v cargo  >/dev/null 2>&1 || { echo "cargo is required (https://rustup.rs)"; exit 1; }
	@command -v zig    >/dev/null 2>&1 || { echo "zig is required (>=0.13)"; exit 1; }
	@echo "All tools present."

# --- Install ----------------------------------------------------------------

install: install-web install-server install-dsp install-ai install-shared-types
	@echo "All workspaces installed."

install-web:
	pnpm install --filter openwebrx-plus-web...

install-shared-types:
	pnpm install --filter openwebrx-plus-shared-types...

install-server:
	cd apps/server && uv sync

install-dsp:
	cd packages/dsp-zig && zig build 2>/dev/null || echo "(dsp-zig: build skipped, dependencies not yet wired)"

install-ai:
	cd packages/ai-rust && cargo check 2>/dev/null || echo "(ai-rust: check skipped, dependencies not yet wired)"

# --- Dev --------------------------------------------------------------------

dev: dev-web dev-server  # NOTE: run in two terminals if you want both logs live

dev-web:
	pnpm --filter openwebrx-plus-web run dev

dev-server:
	cd apps/server && uv run openwebrx-plus

# --- Build / verify ----------------------------------------------------------

build:
	pnpm -r run build
	cd packages/dsp-zig && zig build
	cd packages/ai-rust && cargo build --release

lint:
	pnpm -r run lint
	cd apps/server && uv run ruff check .

typecheck:
	pnpm -r run typecheck
	cd apps/server && uv run mypy openwebrx_plus

test:
	pnpm -r run test
	cd apps/server && uv run pytest
	cd packages/dsp-zig && zig build test
	cd packages/ai-rust && cargo test

# --- Clean ------------------------------------------------------------------

clean:
	rm -rf apps/web/dist apps/web/node_modules
	rm -rf packages/shared-types/dist packages/shared-types/node_modules
	rm -rf apps/server/.venv apps/server/**/__pycache__
	rm -rf packages/dsp-zig/zig-out packages/dsp-zig/.zig-cache
	rm -rf packages/ai-rust/target
	find . -name '*.tsbuildinfo' -delete
