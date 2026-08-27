"""IMA ADPCM codecs — exact ports of the OpenWebRX(+) wire formats (ADR-006).

Ported line-by-line from the two reference implementations that every public
receiver and browser actually interoperate with:

  * server side (libcsdr): ``src/lib/adpcm.cpp`` + ``include/adpcm.hpp``
    → :class:`FftAdpcmEncoder` and :class:`AudioAdpcmSyncEncoder`
  * client side (browser): upstream ``htdocs/lib/AudioEngine.js``
    ``ImaAdpcmCodec`` → :class:`ImaAdpcmCodec` (decode paths), including its
    quirks — the decoder caches ``step`` at the *end* of each nibble and
    starts from ``step = 0`` after a reset, while the C++ encoder reads
    ``stepSizeTable[0] = 7``. The mismatch is exactly what the FFT pad
    region exists to absorb (see below), so being bug-compatible with the
    JS decoder is what makes us interoperate with real servers.

Both directions live in this module on purpose:

  * the **decoders** power the federation client
    (:mod:`openwebrx_plus.sources.openwebrx_remote`);
  * the **encoders** power the in-repo fake OpenWebRX server
    (``tests/test_openwebrx_remote_driver.py``) — round-trip tests pin both
    ports against each other, so a mistake in either shows up as a huge
    decode error;
  * when this server wants to *serve* compressed waterfall/audio to plain
    OpenWebRX clients one day (federation transmit side), the encoders are
    ready and already verified.

Wire formats (binary ws messages carry a 1-byte type tag; these describe the
payload *after* the tag has been stripped):

FFT frame (tag ``0x01``, ``fft_compression = "adpcm"``)::

    [5 pad bytes][fft_size/2 data bytes]          # (fft_size + 10)/2 total

  * each byte packs two samples: low nibble = even sample, high nibble = odd
  * the codec state RESETS at the start of every frame
  * the 5 pad bytes each encode ``bins[0]`` twice, giving the decoder 10
    samples to converge from its reset state — clients discard them
  * sample values are dB × 100 (short), so decoded/100 = dB

Audio stream (tag ``0x02``, ``audio_compression = "adpcm"``)::

    [ "SYNC" ][int16le index][int16le predictor][~1001 data bytes] repeating

  * state PERSISTS across frames and across ws messages (it is one stream)
  * each data byte = two int16 samples (low nibble first)
  * "none" compression for either stream: raw little-endian samples with no
    framing (float32 for FFT, int16 for audio)
"""

from __future__ import annotations

import struct

import numpy as np

# From libcsdr adpcm.cpp / AudioEngine.js — the IMA step-size & index tables.
_INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8)

_STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
    19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
    5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
)

# libcsdr adpcm.hpp: "We will pad the FFT at the beginning, with the first
# value of the input data, COMPRESS_FFT_PAD_N times." (must be even)
COMPRESS_FFT_PAD_N = 10

_SYNC_WORD = b"SYNC"
_SYNC_STATE_BYTES = 4  # int16le index + int16le predictor
_SYNC_INTERVAL_BYTES = 1000  # data bytes between sync frames (see encoder)
_DB_SCALE = 100  # FFT dB ↔ short scaling


def _clamp(value: int, lo: int, hi: int) -> int:
    return lo if value < lo else hi if value > hi else value


class ImaAdpcmCodec:
    """Stateful IMA ADPCM decoder — the AudioEngine.js port.

    Used for the *audio* stream, where state persists across websocket
    messages. The FFT path uses the (stateless, faster) module function
    :func:`decode_fft_adpcm` instead.
    """

    def __init__(self) -> None:
        self.step_index = 0
        self.predictor = 0
        self.step = 0  # JS quirk: starts at 0, not stepSizeTable[0]
        # sync state machine
        self._phase = 0  # 0 = scan for SYNC, 1 = read state, 2 = decode
        self._synchronized = 0
        self._sync_counter = 0
        self._sync_buffer = bytearray(_SYNC_STATE_BYTES)
        self._sync_buffer_index = 0

    def reset(self) -> None:
        """Reset codec + sync state (a fresh audio stream)."""
        self.step_index = 0
        self.predictor = 0
        self.step = 0
        self._phase = 0
        self._synchronized = 0
        self._sync_counter = 0
        self._sync_buffer_index = 0

    # -- raw decode (no sync framing) ---------------------------------------

    def decode(self, data: bytes) -> np.ndarray:
        """Decode a plain ADPCM byte block (low nibble first) → int16."""
        out = np.empty(len(data) * 2, dtype=np.int16)
        for i, b in enumerate(data):
            out[i * 2] = self.decode_nibble(b & 0x0F)
            out[i * 2 + 1] = self.decode_nibble(b >> 4)
        return out

    def decode_nibble(self, nibble: int) -> int:
        """One sample. Bug-compatible with AudioEngine.js: the index is
        updated *before* the diff is built, but the diff uses the ``step``
        cached at the end of the previous call (0 right after reset)."""
        self.step_index += _INDEX_TABLE[nibble]
        self.step_index = _clamp(self.step_index, 0, 88)

        step = self.step
        diff = step >> 3
        if nibble & 1:
            diff += step >> 2
        if nibble & 2:
            diff += step >> 1
        if nibble & 4:
            diff += step
        if nibble & 8:
            diff = -diff

        self.predictor = _clamp(self.predictor + diff, -32768, 32767)
        self.step = _STEP_TABLE[self.step_index]
        return self.predictor

    # -- sync-framed decode (the audio wire format) --------------------------

    def decode_with_sync(self, data: bytes) -> np.ndarray:
        """Decode a chunk of the sync-framed audio stream.

        The stream is ``"SYNC" + int16le(index) + int16le(predictor)`` followed
        by ~1001 data bytes, repeating. Chunks may split frames anywhere —
        the phase machine scans, resynchronizes, and decodes only complete
        data bytes (exactly like the JS client).
        """
        out = np.empty(len(data) * 2, dtype=np.int16)
        oi = 0
        for b in data:
            if self._phase == 0:
                # scan for the sync word, byte by byte (exact JS semantics:
                # post-increment compare, reset to 0 on mismatch — a byte
                # that mismatches is consumed, not retried as a new start)
                if b != _SYNC_WORD[self._synchronized]:
                    self._synchronized = 0
                else:
                    self._synchronized += 1
                if self._synchronized == 4:
                    self._sync_buffer_index = 0
                    self._phase = 1
            elif self._phase == 1:
                self._sync_buffer[self._sync_buffer_index] = b
                self._sync_buffer_index += 1
                if self._sync_buffer_index == _SYNC_STATE_BYTES:
                    step_index, predictor = struct.unpack(
                        "<hh", bytes(self._sync_buffer)
                    )
                    self.step_index = _clamp(step_index, 0, 88)
                    self.predictor = _clamp(predictor, -32768, 32767)
                    self.step = _STEP_TABLE[self.step_index]
                    self._sync_counter = _SYNC_INTERVAL_BYTES
                    self._phase = 2
            else:
                out[oi] = self.decode_nibble(b & 0x0F)
                oi += 1
                out[oi] = self.decode_nibble(b >> 4)
                oi += 1
                # JS: `if (this.syncCounter-- === 0)` — compare first, then
                # decrement. 1001 data bytes are decoded per sync period.
                if self._sync_counter == 0:
                    self._synchronized = 0
                    self._phase = 0
                    self._sync_counter = -1  # unused in phase 0 (JS leaves it)
                else:
                    self._sync_counter -= 1
        return out[:oi]


def decode_fft_adpcm(data: bytes, fft_size: int | None = None) -> np.ndarray:
    """Decode one ADPCM-compressed FFT frame → float32 dB bins.

    State resets per frame (libcsdr ``FftAdpcmEncoder`` resets its codec for
    every frame); the first ``COMPRESS_FFT_PAD_N`` decoded samples are pad and
    are discarded, and the remaining samples are divided by 100 to get dB.

    If ``fft_size`` is given and disagrees with the frame length, the frame
    wins (logged by the caller if it cares) — servers can resize their FFT.
    """
    n = len(data)
    if fft_size is None:
        fft_size = n * 2 - COMPRESS_FFT_PAD_N
    step_table = _STEP_TABLE
    index_table = _INDEX_TABLE
    step_index = 0
    predictor = 0
    step = 0  # JS decoder quirk — absorbed by the pad region
    total_samples = n * 2
    samples = np.empty(total_samples, dtype=np.int16)
    si = 0
    for b in data:
        for nib in (b & 0x0F, b >> 4):
            step_index += index_table[nib]
            if step_index < 0:
                step_index = 0
            elif step_index > 88:
                step_index = 88
            diff = step >> 3
            if nib & 1:
                diff += step >> 2
            if nib & 2:
                diff += step >> 1
            if nib & 4:
                diff += step
            if nib & 8:
                diff = -diff
            predictor += diff
            if predictor > 32767:
                predictor = 32767
            elif predictor < -32768:
                predictor = -32768
            step = step_table[step_index]
            samples[si] = predictor
            si += 1
    bins = samples[COMPRESS_FFT_PAD_N:].astype(np.float32)
    bins /= np.float32(_DB_SCALE)
    if len(bins) != fft_size:
        # Tolerant: honor the frame over the (possibly stale) config value.
        pass
    return bins


class _ImaAdpcmEncoderCodec:
    """Encoder-side codec state (libcsdr ``AdpcmCodec`` port).

    Kept separate from :class:`ImaAdpcmCodec` because the encoder's state
    update (``decodeSample``) uses ``stepSizeTable[index]`` *before* the
    index update — the C++ order, not the JS caching order. The encoders
    must match the C++ side bit-for-bit; the decoders must match the JS
    side. The pad region absorbs the (intended) skew between them.
    """

    __slots__ = ("step_index", "predictor")

    def __init__(self) -> None:
        self.step_index = 0
        self.predictor = 0

    def reset(self) -> None:
        self.step_index = 0
        self.predictor = 0

    def encode_sample(self, sample: int) -> int:
        """Encode one sample → 4-bit code (state updated via decode)."""
        diff = sample - self.predictor
        step = _STEP_TABLE[self.step_index]
        code = 0
        if diff < 0:
            code = 8
            diff = -diff
        if diff >= step:
            code |= 4
            diff -= step
        step >>= 1
        if diff >= step:
            code |= 2
            diff -= step
        step >>= 1
        if diff >= step:
            code |= 1
        self.decode_sample(code)
        return code

    def encode_float_sample(self, value: float) -> int:
        """libcsdr: ``encodeSample((short)(input * 100))`` — truncation
        toward zero included."""
        return self.encode_sample(int(value * _DB_SCALE))

    def decode_sample(self, code: int) -> int:
        step = _STEP_TABLE[self.step_index]
        diff = step >> 3
        if code & 1:
            diff += step >> 2
        if code & 2:
            diff += step >> 1
        if code & 4:
            diff += step
        if code & 8:
            diff = -diff
        self.predictor = _clamp(self.predictor + diff, -32768, 32767)
        self.step_index = _clamp(self.step_index + _INDEX_TABLE[code], 0, 88)
        return self.predictor


class FftAdpcmEncoder:
    """libcsdr ``FftAdpcmEncoder`` port: float32 dB bins → ADPCM bytes.

    One call encodes exactly one frame (state resets per call, matching the
    server's per-frame reset that clients rely on).
    """

    def __init__(self, fft_size: int) -> None:
        if fft_size % 2:
            raise ValueError(f"fft_size must be even, got {fft_size}")
        self.fft_size = fft_size

    def encode(self, bins: np.ndarray) -> bytes:
        if len(bins) != self.fft_size:
            raise ValueError(f"expected {self.fft_size} bins, got {len(bins)}")
        codec = _ImaAdpcmEncoderCodec()
        out = bytearray()
        # 5 pad bytes: each encodes bins[0] twice (COMPRESS_FFT_PAD_N samples)
        for _ in range(COMPRESS_FFT_PAD_N // 2):
            lo = codec.encode_float_sample(float(bins[0]))
            hi = codec.encode_float_sample(float(bins[0]))
            out.append(lo | (hi << 4))
        for i in range(0, self.fft_size, 2):
            lo = codec.encode_float_sample(float(bins[i]))
            hi = codec.encode_float_sample(float(bins[i + 1]))
            out.append(lo | (hi << 4))
        return bytes(out)


class AudioAdpcmSyncEncoder:
    """libcsdr ``AdpcmEncoder(sync=True)`` port: int16 PCM → sync-framed bytes.

    The sync counter persists across calls (as it does across the C++
    process() chunks), so the byte stream is identical no matter how the
    PCM is chunked. Emits ``SYNC`` + state every ~1001 data bytes.

    An odd trailing sample is carried over to the next call (libcsdr's ring
    buffer does the same implicitly — ``available() / 2`` rounds down), so
    chunking NEVER drops or duplicates samples.
    """

    def __init__(self) -> None:
        self._codec = _ImaAdpcmEncoderCodec()
        self._sync_counter = 0  # first pair triggers a sync, like libcsdr
        self._pending: int | None = None  # odd tail sample from the last call

    def reset(self) -> None:
        self._codec.reset()
        self._sync_counter = 0
        self._pending = None

    def encode(self, pcm: np.ndarray) -> bytes:
        samples = np.asarray(pcm)
        if self._pending is not None:
            samples = np.concatenate(([self._pending], samples))
        if len(samples) % 2:
            self._pending = int(samples[-1])
            samples = samples[:-1]
        else:
            self._pending = None
        out = bytearray()
        for i in range(0, len(samples), 2):
            counter = self._sync_counter
            self._sync_counter = counter - 1
            if counter <= 0:
                out += _SYNC_WORD
                out += struct.pack(
                    "<hh", self._codec.step_index, self._codec.predictor
                )
                self._sync_counter = _SYNC_INTERVAL_BYTES
            lo = self._codec.encode_sample(int(samples[i]))
            hi = self._codec.encode_sample(int(samples[i + 1]))
            out.append(lo | (hi << 4))
        return bytes(out)
