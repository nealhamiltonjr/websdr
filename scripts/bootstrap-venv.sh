#!/usr/bin/env bash
# bootstrap-venv.sh
# ----------------------------------------------------------------------------
# Full from-source bootstrap of the OpenWebRX+ dev environment.
# Idempotent — re-running skips steps that are already done.
#
# Produces:
#   - ~/.local/usr/lib/libcsdr.so.0.19 (built from jketterl/csdr.git)
#   - apps/server/.venv with pycsdr installed (built from jketterl/pycsdr.git)
#   - apps/server/fixtures/iq/*.cf32 (deterministic IQ recordings)
#
# After bootstrap, the test wrapper scripts/run-server-tests.sh sets
# LD_LIBRARY_PATH automatically.
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/home/z/my-project}"
USER_PREFIX="${USER_PREFIX:-$HOME/.local}"
USER_USR="${USER_PREFIX}/usr"
PY_VENV="${REPO_ROOT}/apps/server/.venv"

echo "==> 1/6: Prerequisites (libsamplerate0-dev headers, libfftw3-dev)"
# dpkg-deb extraction preserves the package's ./usr/... layout, so we
# extract to USER_PREFIX (not USER_USR) → files land at
# ${USER_USR}/lib/... as expected by cmake's PKG_CONFIG_PATH.
if [ ! -f "${USER_USR}/lib/x86_64-linux-gnu/pkgconfig/samplerate.pc" ]; then
    mkdir -p "${USER_PREFIX}/pkgs"
    cd "${USER_PREFIX}/pkgs"
    # libsamplerate0 (runtime) is likely already on the system, but the
    # .pc + headers are not. Download + extract both into the user prefix.
    apt-get download libsamplerate0 libsamplerate0-dev 2>&1 | tail -5
    for deb in libsamplerate0_*.deb libsamplerate0-dev_*.deb; do
        [ -f "$deb" ] || continue
        dpkg-deb -x "$deb" "${USER_PREFIX}"
    done
    # Patch the .pc prefix so pkg-config reports the user-prefix paths
    PC="${USER_USR}/lib/x86_64-linux-gnu/pkgconfig/samplerate.pc"
    if [ -f "$PC" ]; then
        sed -i "s|^prefix=/usr$|prefix=${USER_USR}|" "$PC"
    fi
    echo "    libsamplerate0-dev installed to ${USER_USR}"
else
    echo "    already installed"
fi

# libfftw3-dev is typically installed system-wide on the dev runner;
# verify the .so is findable.
if ! ldconfig -p | grep -q libfftw3f; then
    echo "    WARN: libfftw3 not found — install 'libfftw3-dev' if build fails"
fi

echo
echo "==> 2/6: Build libcsdr from source (jketterl/csdr)"
CSDR_BUILD="${USER_PREFIX}/csdr-build"
if [ ! -f "${USER_USR}/lib/libcsdr.so" ] && [ ! -f "${USER_USR}/lib/x86_64-linux-gnu/libcsdr.so" ]; then
    rm -rf "${CSDR_BUILD}"
    git clone --depth 1 https://github.com/jketterl/csdr.git "${CSDR_BUILD}"
    cd "${CSDR_BUILD}"
    # PATCH: src/lib/CMakeLists.txt doesn't propagate SAMPLERATE / FFTW3
    # include/link dirs to targets. Add before target_link_libraries(csdr ...).
    sed -i '/target_link_libraries(csdr/i\
    target_include_directories(csdr PUBLIC ${SAMPLERATE_INCLUDE_DIRS} ${FFTW3_INCLUDE_DIRS})\
    target_link_directories(csdr PUBLIC ${SAMPLERATE_LIBRARY_DIRS} ${FFTW3_LIBRARY_DIRS})' \
        src/lib/CMakeLists.txt
    export PKG_CONFIG_PATH="${USER_USR}/lib/x86_64-linux-gnu/pkgconfig"
    export CMAKE_PREFIX_PATH="${USER_USR}:${USER_PREFIX}"
    mkdir -p build && cd build
    cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="${USER_USR}" ..
    make -j"$(nproc)"
    make install
    echo "    libcsdr built + installed to ${USER_USR}"
else
    echo "    already built"
fi

echo
echo "==> 3/6: Bootstrap Python venv (uv)"
if [ ! -f "${PY_VENV}/bin/python" ]; then
    cd "${REPO_ROOT}/apps/server"
    uv venv
    echo "    venv at ${PY_VENV}"
else
    echo "    already bootstrapped"
fi

echo
echo "==> 4/6: Install Python deps + openwebrx_plus editable + dev tooling"
PY="${PY_VENV}/bin/python"
if ! "${PY}" -c "import fastapi, numpy, structlog" 2>/dev/null; then
    cd "${REPO_ROOT}/apps/server"
    uv pip install --python "${PY}" \
        fastapi uvicorn websockets httpx pydantic pydantic-settings \
        numpy structlog prometheus-client tomli_w
    # Editable install of the openwebrx_plus package itself, without deps
    # (the pyproject.toml declares pycsdr as a git direct reference that
    # would try to build from source on every sync — we manage pycsdr
    # separately below to keep control of the build env).
    uv pip install --python "${PY}" -e . --no-deps
    uv pip install --python "${PY}" \
        pytest pytest-asyncio pytest-cov anyio ruff mypy
    echo "    deps installed"
else
    echo "    already installed"
fi

echo
echo "==> 5/6: Build + install pycsdr against the user-prefix libcsdr"
if ! "${PY}" -c "import pycsdr.modules" 2>/dev/null; then
    PYCSDR_BUILD="${USER_PREFIX}/pycsdr-build"
    rm -rf "${PYCSDR_BUILD}"
    git clone --depth 1 https://github.com/jketterl/pycsdr.git "${PYCSDR_BUILD}"
    cd "${PYCSDR_BUILD}"
    # Note: upstream default branch is `develop`, no `main`.
    export PKG_CONFIG_PATH="${USER_USR}/lib/x86_64-linux-gnu/pkgconfig"
    export LD_LIBRARY_PATH="${USER_USR}/lib/x86_64-linux-gnu:${USER_USR}/lib"
    export CFLAGS="-I${USER_USR}/include"
    export CPPFLAGS="-I${USER_USR}/include"
    export LDFLAGS="-L${USER_USR}/lib -Wl,-rpath,${USER_USR}/lib"
    # --no-build-isolation so the build uses the env vars above (the
    # isolated build env wouldn't see our CFLAGS/LDFLAGS).
    "${PY}" -m pip install --no-build-isolation --no-deps .
    echo "    pycsdr built + installed into venv"
else
    echo "    already installed"
fi

echo
echo "==> 6/6: Regenerate IQ fixtures (deterministic)"
if [ ! -f "${REPO_ROOT}/apps/server/fixtures/iq/hf_20m_evening.cf32" ]; then
    cd "${REPO_ROOT}"
    export LD_LIBRARY_PATH="${USER_USR}/lib/x86_64-linux-gnu:${USER_USR}/lib"
    "${PY}" scripts/generate_iq_fixtures.py
    echo "    fixtures baked"
else
    echo "    already baked"
fi

echo
echo "==> Bootstrap complete. Sanity checks:"
export LD_LIBRARY_PATH="${USER_USR}/lib/x86_64-linux-gnu:${USER_USR}/lib"
"${PY}" -c "import pycsdr.modules; print('  pycsdr.modules.csdr_version:', pycsdr.modules.csdr_version)"
"${PY}" -c "import openwebrx_plus; print('  openwebrx_plus: ok')"
echo
echo "Run server tests with:"
echo "  ${REPO_ROOT}/scripts/run-server-tests.sh"
