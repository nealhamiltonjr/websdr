# IQ fixtures — regeneration required

The four .cf32 recordings (47 MB total) are intentionally NOT in this
archive: `scripts/generate_iq_fixtures.py` (openwebrx-plus/scripts/) is
seeded and regenerates them byte-identically.

    cd openwebrx-plus
    python3 scripts/generate_iq_fixtures.py        # writes into this dir

Verify against CHECKSUMS-fixtures.txt at the archive root.

Files: hf_20m_evening.cf32 (250 kS/s), vhf_fm_broadcast.cf32 (1 MS/s),
adsb_1090.cf32 (2 MS/s — 14 CRC-valid Mode S frames, 3 aircraft),
smoke.cf32 (250 kS/s). Each has a SigMF-style .meta sidecar that
FileSource auto-loads.
