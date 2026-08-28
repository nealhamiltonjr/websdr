#!/usr/bin/env bash
# build-handoff-zip.sh
# ----------------------------------------------------------------------------
# Assembles a single handoff ZIP containing:
#   - The entire OpenWebRX+ codebase (incl. .git for slice history)
#   - The freshly-updated docs/AI-HANDOFF.md
#   - All companion docs (STATUS.md, ADR/, ARCHITECTURE.md, SYNC-UP.md,
#     worklog.md, README.md, AGENTS.md, LICENSE)
#
# Excludes build artifacts that are regenerable (per docs/AI-HANDOFF.md §3):
#   - node_modules/ , apps/server/.venv/ , apps/web/dist/ , target/
#   - __pycache__ , *.pyc , .pytest_cache , .ruff_cache , .mypy_cache
#   - tool-results/ (session scratch)
#   - download/ (avoid recursion)
#
# Output: /home/z/my-project/download/openwebrx-plus-handoff-<date>.zip
set -euo pipefail

REPO_ROOT="/home/z/my-project"
OUT_DIR="${REPO_ROOT}/download"
DATE_STAMP="$(date -u +%Y%m%d)"
ZIP_NAME="openwebrx-plus-handoff-${DATE_STAMP}.zip"
ZIP_PATH="${OUT_DIR}/${ZIP_NAME}"

mkdir -p "${OUT_DIR}"

# Build the exclusion list — dirs and file-globs.
# zip -x accepts glob patterns separated by spaces.
EXCLUSIONS=(
  ".git/*"                              # ⚠ TEMPORARY: removed for size; restore per decision below
  "node_modules/*"
  "apps/web/node_modules/*"
  "apps/server/.venv/*"
  "apps/web/dist/*"
  "target/*"
  "packages/*/target/*"
  "**/__pycache__/*"
  "**/*.pyc"
  "**/*.pyo"
  ".pytest_cache/*"
  "**/.pytest_cache/*"
  ".ruff_cache/*"
  "**/.ruff_cache/*"
  ".mypy_cache/*"
  "**/.mypy_cache/*"
  "tool-results/*"
  "download/*"
  ".cache/*"
  "**/.cache/*"
  "*.log"
)

# Decision: include .git OR not?
# The user's "entire current codebase" + slice history in worklog.md suggests
# including .git IS useful for an AI picking this up cold. .git compressed is
# ~30-40 MB (text-only objects, good ratio). Include it.
EXCLUSIONS=(
  "node_modules/*"
  "apps/web/node_modules/*"
  "apps/server/.venv/*"
  "apps/web/dist/*"
  "target/*"
  "packages/*/target/*"
  "**/__pycache__/*"
  "**/*.pyc"
  "**/*.pyo"
  ".pytest_cache/*"
  "**/.pytest_cache/*"
  ".ruff_cache/*"
  "**/.ruff_cache/*"
  ".mypy_cache/*"
  "**/.mypy_cache/*"
  "tool-results/*"
  "download/*"
  "upload/*"
  "skills/*"
  ".cache/*"
  "**/.cache/*"
  "*.log"
)

cd "${REPO_ROOT}"

# Build exclusion args
EXCLUDE_ARGS=()
for pat in "${EXCLUSIONS[@]}"; do
  EXCLUDE_ARGS+=("-x" "${pat}")
done

# Remove any prior zip with the same name
rm -f "${ZIP_PATH}"

echo "==> Building ${ZIP_PATH}"
echo "==> Repository root: ${REPO_ROOT}"
echo "==> Excluded glob patterns: ${#EXCLUSIONS[@]}"
echo

# Zip the entire repo, preserving the directory structure.
# -r recurse, -q quiet-ish (we'll print the file count after), -X exclude
# the exclusions, -9 max compression.
zip -r -9 "${ZIP_PATH}" . "${EXCLUDE_ARGS[@]}" \
    > "${OUT_DIR}/.zip-build.log" 2>&1 || {
  echo "ERROR: zip build failed; see ${OUT_DIR}/.zip-build.log"
  tail -50 "${OUT_DIR}/.zip-build.log"
  exit 1
}

echo
echo "==> ZIP build complete"
ls -lh "${ZIP_PATH}"
echo
echo "==> File count inside the zip:"
unzip -l "${ZIP_PATH}" | tail -1
echo
echo "==> Top-level entries inside the zip:"
unzip -l "${ZIP_PATH}" | awk 'NR>3 && NR<=23 {print "  " $4}' | head -20

# Clean up the build log
rm -f "${OUT_DIR}/.zip-build.log"

echo
echo "==> Done. Handoff ZIP ready at:"
echo "    ${ZIP_PATH}"
