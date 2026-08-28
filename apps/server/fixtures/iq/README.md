# IQ fixtures — regeneration required

The four `.cf32` recordings (47 MB total) are intentionally NOT committed to
this repository. Run `scripts/generate_iq_fixtures.py` from the repo root to
regenerate them byte-identically into this directory.

```bash
# from the repo root
python3 scripts/generate_iq_fixtures.py        # writes .cf32 files into this dir
```

Files (after regeneration):
- `hf_20m_evening.cf32` (250 kS/s)
- `vhf_fm_broadcast.cf32` (1 MS/s)
- `adsb_1090.cf32` (2 MS/s — 14 CRC-valid Mode S frames, 3 aircraft)
- `smoke.cf32` (250 kS/s)

Each has a SigMF-style `.meta` sidecar (committed to the repo) that
`FileSource` auto-loads. The fixture generator is deterministic — running it
twice produces identical bytes; if you have a checksum file from a prior
handoff bundle, you can `sha256sum *.cf32` and compare to verify.
