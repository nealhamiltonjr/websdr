# OpenWebRX+ — Dockerfile (slice-64)
# Multi-stage build: build libcsdr + pycsdr + frontend, then copy to a slim runtime image.

# --- Stage 1: Build C/C++/Rust dependencies ---
FROM debian:bookworm-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git pkg-config libfftw3-dev libsamplerate0-dev \
    python3.12 python3.12-venv python3-pip curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Rust (for packages/ai-rust).
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal
ENV PATH="/root/.cargo/bin:${PATH}"

WORKDIR /build

# Clone + build libcsdr.
RUN git clone --depth 1 https://github.com/jketterl/csdr.git && \
    cd csdr && \
    sed -i '/target_link_libraries(csdr/i\
    target_include_directories(csdr PUBLIC ${SAMPLERATE_INCLUDE_DIRS} ${FFTW3_INCLUDE_DIRS})\
    target_link_directories(csdr PUBLIC ${SAMPLERATE_LIBRARY_DIRS} ${FFTW3_LIBRARY_DIRS})' \
    src/lib/CMakeLists.txt && \
    mkdir build && cd build && \
    cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local .. && \
    make -j$(nproc) && make install && ldconfig

# Clone + build pycsdr.
RUN git clone --depth 1 https://github.com/jketterl/pycsdr.git && \
    cd pycsdr && \
    python3.12 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-build-isolation --no-deps . && \
    PKG_CONFIG_PATH=/usr/local/lib/pkgconfig \
    CFLAGS="-I/usr/local/include" \
    LDFLAGS="-L/usr/local/lib -Wl,-rpath,/usr/local/lib" \
    /opt/venv/bin/pip install --no-build-isolation --no-deps .

# --- Stage 2: Build frontend ---
FROM node:22-bookworm-slim AS frontend-builder

WORKDIR /build
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
RUN npm install -g pnpm@9 && pnpm install --frozen-lockfile
COPY apps/web/ apps/web/
COPY packages/shared-types/ packages/shared-types/
RUN pnpm --filter openwebrx-plus-web run build

# --- Stage 3: Runtime image ---
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libfftw3-3 libsamplerate0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy the built libcsdr.
COPY --from=builder /usr/local/lib/libcsdr* /usr/local/lib/
COPY --from=builder /usr/local/include/csdr/ /usr/local/include/csdr/
RUN ldconfig

# Copy the venv with pycsdr.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
ENV LD_LIBRARY_PATH="/usr/local/lib"

# Copy the built frontend.
COPY --from=frontend-builder /build/apps/web/dist/ /app/static/

# Copy the server source.
COPY apps/server/ /app/server/
WORKDIR /app/server

# Install Python deps (without pycsdr — already in the venv).
RUN /opt/venv/bin/pip install --no-deps -e .

# Copy the Rust cdylib if built.
COPY --from=builder /build/packages/ai-rust/target/release/libowrx_ai.so /app/packages/ai-rust/target/release/ 2>/dev/null || true

EXPOSE 8073

ENV OPENWEBRX_HOST=0.0.0.0
ENV OPENWEBRX_PORT=8073
ENV OPENWEBRX_TIER=prod

CMD ["python", "-m", "openwebrx_plus"]
