# DSP Bootstrap — building libcsdr + pycsdr from source

One-time setup to get the pycsdr DSP chain running on a dev machine
(Debian/Ubuntu). pycsdr is NOT on PyPI and requires the libcsdr C++ library
(headers + shared object) to be present before it can build.

Runtime result: `pycsdr` importable from Python, backed by SIMD C++ DSP.

## Prerequisites

- Python 3.12+ (matches `apps/server/pyproject.toml`)
- A C++ compiler (`g++` / `clang++`)
- `libfftw3-dev` (FFT library used by libcsdr)
- `libsamplerate` (runtime + headers — see note below)
- `cmake` (any 3.x; `pip install cmake` works without root)

### Note on libsamplerate

libcsdr's CMake hard-requires the `samplerate` pkg-config module. On a stock
Debian box, `libsamplerate0` (runtime) is usually installed but
`libsamplerate0-dev` (headers) is not. Without root, extract both .debs into
a user prefix and point pkg-config at it:

```bash
mkdir -p ~/.local-pkgs ~/.local
cd ~/.local-pkgs
apt-get download libsamplerate0 libsamplerate0-dev
dpkg-deb -x libsamplerate0_*.deb ~/.local
dpkg-deb -x libsamplerate0-dev_*.deb ~/.local

# Fix the .pc prefix so pkg-config reports the user-prefix paths:
sed -i 's|^prefix=/usr$|prefix=/home/z/.local/usr|' \
    ~/.local/usr/lib/x86_64-linux-gnu/pkgconfig/samplerate.pc
```

## 1. Build libcsdr

```bash
git clone --depth 1 https://github.com/jketterl/csdr.git csdr-build
cd csdr-build
# PATCH (required until fixed upstream): src/lib/CMakeLists.txt doesn't
# propagate SAMPLERATE include/link dirs to targets. Add before
# target_link_libraries(csdr ...):
#   target_include_directories(csdr PUBLIC ${SAMPLERATE_INCLUDE_DIRS} ${FFTW3_INCLUDE_DIRS})
#   target_link_directories(csdr PUBLIC ${SAMPLERATE_LIBRARY_DIRS} ${FFTW3_LIBRARY_DIRS})
export PKG_CONFIG_PATH="$HOME/.local/usr/lib/x86_64-linux-gnu/pkgconfig"
export CMAKE_PREFIX_PATH="$HOME/.local/usr:$HOME/.local"
mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$HOME/.local/usr" ..
make -j"$(nproc)"
make install
```

## 2. Build pycsdr against it

```bash
git clone --depth 1 https://github.com/jketterl/pycsdr.git pycsdr-build
cd pycsdr-build
export PKG_CONFIG_PATH="$HOME/.local/usr/lib/x86_64-linux-gnu/pkgconfig"
export LD_LIBRARY_PATH="$HOME/.local/usr/lib/x86_64-linux-gnu:$HOME/.local/usr/lib"
export CFLAGS="-I$HOME/.local/usr/include"
export CPPFLAGS="-I$HOME/.local/usr/include"
export LDFLAGS="-L$HOME/.local/usr/lib -Wl,-rpath,$HOME/.local/usr/lib"
pip install --no-build-isolation .
```

Note: the upstream default branch is `develop` (there is no `main`).

## 3. Verify

```bash
export LD_LIBRARY_PATH="$HOME/.local/usr/lib/x86_64-linux-gnu:$HOME/.local/usr/lib"
python3 -c "import pycsdr.modules; print('pycsdr OK:', pycsdr.modules.csdr_version)"
```

Then run the DSP smoke tests from the repo root:

```bash
python3 scripts/test_fft_chain.py    # FFT chain peak-bin correctness
python3 scripts/test_audio_chain.py  # AM demod + 1 kHz tone detection
python3 scripts/test_audio_modes.py  # USB / LSB / NFM demods
```

And the server test suite:

```bash
cd apps/server && python3 -m pytest tests/ -q
```

## Environment for daily runs

Any process importing pycsdr needs libcsdr.so on its library path:

```bash
export LD_LIBRARY_PATH="$HOME/.local/usr/lib/x86_64-linux-gnu:$HOME/.local/usr/lib"
```

(Or add the path to `/etc/ld.so.conf.d/` on machines where you have root.)

## Troubleshooting

- **`csdr/module.hpp: No such file or directory`** when building pycsdr:
  libcsdr wasn't installed (step 1) or `CFLAGS` doesn't point at
  `$HOME/.local/usr/include`.
- **`cannot find -lsamplerate`** when linking libcsdr: the `samplerate.pc`
  prefix fix above wasn't applied, or `PKG_CONFIG_PATH` isn't exported.
- **`import pycsdr` raises `OSError: libcsdr.so.0.19`**: `LD_LIBRARY_PATH`
  isn't set for the Python process.
- **No FFT frames from small chunks**: pycsdr's `Fft` module drops
  sub-window data via its `every_n_samples` skip logic. `FftChain.feed()`
  stages chunks < 2×fft_size automatically; if you write your own feed
  loop, batch to at least fft_size samples per write.
