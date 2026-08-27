"""Wire-format dataclasses returned by the pycsdr DSP chains.

These mirror the binary wire format defined in
``packages/shared-types/src/fft.ts`` and ``packages/shared-types/src/audio.ts``.
The DSP chains return the raw payload (float32 bins for FFT, int16 PCM for
audio) plus enough metadata to pack the wire header — packing itself is the
caller's responsibility so that this module stays free of struct / asyncio
imports.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class FftFrame:
    """One frame of FFT output — float32 dB power per bin, DC-centered.

    ``bins`` is a ``memoryview`` over a contiguous ``fft_size * 4`` byte
    float32 buffer. The DSP chain retains ownership of the underlying
    buffer; callers that need to hold the data past the next ``drain()``
    call should copy ``bytes(bins)``.
    """

    bins: memoryview
    fft_size: int
    center_freq: int
    sample_rate: int
    min_db: float
    max_db: float


@dataclass(slots=True, frozen=True)
class AudioFrame:
    """One frame of audio output — int16 mono PCM at ``sample_rate`` Hz."""

    pcm: memoryview
    sample_rate: int
    frame_count: int


# ----------------------------------------------------------------------------
# DSP control parameters (slice-5.2 — fine-grained controls)
# ----------------------------------------------------------------------------


@dataclass(slots=True)
class DSPParams:
    """Per-receiver DSP control parameters (slice-5.2).

    All fields are optional — ``None`` means "use the mode default". When
    any field changes, the ReceiverSession rebuilds the AudioChain so the
    new parameters take effect at the next IQ chunk.

    The struct is intentionally flat (no nesting) so the WS protocol can
    serialize it as a single JSON object without nested dicts.

    Fields:
        low_cut_hz / high_cut_hz
            Manual bandpass width override. When non-None, the chain's
            Bandpass block is built with these cuts instead of the mode
            profile defaults. Useful for narrowing SSB bandwidth on a
            noisy channel or widening AM for clearer audio.

        agc_enabled
            When True, an Agc block is inserted before the resampler.
            Slope/attack/decay are pycsdr defaults for now; per-mode AGC
            profiles land in slice-5.3.

        squelch_db
            When non-None, a Squelch block is inserted after the demod
            with the given threshold (in dBFS, 0 = max, -100 = silence).
            None = no squelch (always open).

        dc_block_enabled
            Toggles the DcBlock stage on AM/NFM demod output. Default True
            (matches the "classic" mode profile). Set to False for modes
            where DC is desirable (rare) or to compare carrier-leak
            behavior.

        deemphasis_enabled
            Toggles NfmDeemphasis / WfmDeemphasis on the demodulated
            audio. Default True for WFM; False for NFM (NfmDeemphasis
            is rarely needed for voice channels but useful for data
            modes).

        manual_gain_db
            When non-None, a Gain block with the given linear gain is
            inserted before the resampler. Use this when AGC is off and
            you want a fixed makeup gain.

        notch_enabled / notch_freq_hz / notch_q
            NOT IMPLEMENTED in slice-5.2 — pycsdr has no native Notch
            block. Queued for slice-5.3 (either a custom Python notch or
            an upstream pycsdr contribution). The fields exist so the UI
            can show the controls with an "experimental" badge; the
            AudioChain honors them by inserting a no-op Bandpass for now.

        noise_blanker_enabled / noise_blanker_threshold
            NOT IMPLEMENTED in slice-5.2 — pycsdr has no native Nb
            block. Same situation as notch: fields exist for UI
            completeness, AudioChain honors them with a no-op until
            slice-5.3 lands the real implementation.
    """

    low_cut_hz: float | None = None
    high_cut_hz: float | None = None
    agc_enabled: bool | None = None
    squelch_db: float | None = None
    dc_block_enabled: bool | None = None
    deemphasis_enabled: bool | None = None
    manual_gain_db: float | None = None
    # Slice-5.3 — not yet implemented in the chain.
    notch_enabled: bool | None = None
    notch_freq_hz: float | None = None
    notch_q: float | None = None
    noise_blanker_enabled: bool | None = None
    noise_blanker_threshold: float | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize for WS metadata echoes + REST responses."""
        return {
            "low_cut_hz": self.low_cut_hz,
            "high_cut_hz": self.high_cut_hz,
            "agc_enabled": self.agc_enabled,
            "squelch_db": self.squelch_db,
            "dc_block_enabled": self.dc_block_enabled,
            "deemphasis_enabled": self.deemphasis_enabled,
            "manual_gain_db": self.manual_gain_db,
            "notch_enabled": self.notch_enabled,
            "notch_freq_hz": self.notch_freq_hz,
            "notch_q": self.notch_q,
            "noise_blanker_enabled": self.noise_blanker_enabled,
            "noise_blanker_threshold": self.noise_blanker_threshold,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> DSPParams:
        """Build a DSPParams from a dict (the WS /api params payload).

        Unknown fields are silently ignored so a client sending extra
        keys (e.g. a future ``agc_attack_ms`` field) doesn't crash older
        servers.
        """
        known = {
            k: v
            for k, v in data.items()
            if k
            in {
                "low_cut_hz",
                "high_cut_hz",
                "agc_enabled",
                "squelch_db",
                "dc_block_enabled",
                "deemphasis_enabled",
                "manual_gain_db",
                "notch_enabled",
                "notch_freq_hz",
                "notch_q",
                "noise_blanker_enabled",
                "noise_blanker_threshold",
            }
        }
        return cls(**known)  # type: ignore[arg-type]

    def merge(self, patch: DSPParams) -> DSPParams:
        """Return a new DSPParams with non-None fields from ``patch``
        overriding the corresponding fields on ``self``. Used by the
        WS ``setDSPParams`` handler so partial updates don't blow away
        previously-set values."""
        return DSPParams(
            low_cut_hz=patch.low_cut_hz if patch.low_cut_hz is not None else self.low_cut_hz,
            high_cut_hz=patch.high_cut_hz if patch.high_cut_hz is not None else self.high_cut_hz,
            agc_enabled=patch.agc_enabled if patch.agc_enabled is not None else self.agc_enabled,
            squelch_db=patch.squelch_db if patch.squelch_db is not None else self.squelch_db,
            dc_block_enabled=patch.dc_block_enabled if patch.dc_block_enabled is not None else self.dc_block_enabled,
            deemphasis_enabled=patch.deemphasis_enabled if patch.deemphasis_enabled is not None else self.deemphasis_enabled,
            manual_gain_db=patch.manual_gain_db if patch.manual_gain_db is not None else self.manual_gain_db,
            notch_enabled=patch.notch_enabled if patch.notch_enabled is not None else self.notch_enabled,
            notch_freq_hz=patch.notch_freq_hz if patch.notch_freq_hz is not None else self.notch_freq_hz,
            notch_q=patch.notch_q if patch.notch_q is not None else self.notch_q,
            noise_blanker_enabled=patch.noise_blanker_enabled if patch.noise_blanker_enabled is not None else self.noise_blanker_enabled,
            noise_blanker_threshold=patch.noise_blanker_threshold if patch.noise_blanker_threshold is not None else self.noise_blanker_threshold,
        )

    @classmethod
    def defaults(cls) -> DSPParams:
        """A fresh DSPParams with all fields None — the "use mode defaults"
        state. Equivalent to ``DSPParams()`` but explicit for clarity."""
        return cls()


__all__ = ["AudioFrame", "DSPParams", "FftFrame"]
