#!/usr/bin/env python3
"""Regression test: multitrack adaptive seam crossfades.

Verifies that (1) voiced seams get the 8 ms minimum and quiet seams get long
crossfades, (2) every track shrinks identically (sample-aligned outputs — the
hard requirement for the post-cut mix), (3) output duration is exactly
kept-total minus overlap-total, (4) no click-level discontinuity at seams.

Run: python cut/scripts/test_xfade_sync.py
"""
import sys
import tempfile
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).parent))
from cut_audio_multitrack import (
    compute_seam_crossfades, concat_wavs_crossfade, extract_keep_segments,
    build_keep_segments, XFADE_MIN_SEC, XFADE_MAX_SEC,
)

SR = 16000
DUR = 30.0
t = np.arange(int(SR * DUR)) / SR

# Track A: speech-like 220 Hz tone with silences at 9.8-10.4s and 19.7-20.5s
a = 0.3 * np.sin(2 * np.pi * 220 * t).astype(np.float32)
a[int(9.8 * SR):int(10.4 * SR)] = 0.0001 * np.random.randn(int(0.6 * SR)).astype(np.float32)
a[int(19.7 * SR):int(20.5 * SR)] = 0.0001 * np.random.randn(int(0.8 * SR)).astype(np.float32)
# Track B: mostly quiet (other speaker listening), voiced only 14-16s
b = (0.0001 * np.random.randn(len(t))).astype(np.float32)
b[int(14 * SR):int(16 * SR)] += 0.3 * np.sin(2 * np.pi * 150 * t[: int(2 * SR)]).astype(np.float32)

# Cuts: one inside A's silence (quiet seam → long xfade), one mid-tone on A
# (voiced seam → 8 ms minimum), one while B is voiced.
deletes = [(10.0, 10.2), (5.0, 5.5), (15.0, 15.3)]
deletes.sort()

tmp = Path(tempfile.mkdtemp())
wav_a, wav_b = tmp / "a.wav", tmp / "b.wav"
sf.write(wav_a, a, SR)
sf.write(wav_b, b, SR)

keeps = {s: build_keep_segments(deletes, DUR) for s in ("A", "B")}
raws = {"A": wav_a, "B": wav_b}
xf = compute_seam_crossfades(raws, keeps)
assert xf is not None and len(xf) == len(keeps["A"]) - 1, f"bad xfade plan: {xf}"
print("xfades(ms):", [round(x * 1000, 1) for x in xf])

# Seam order corresponds to sorted deletes: 5.0(voiced A), 10.0(quiet), 15.0(voiced B)
assert abs(xf[0] - XFADE_MIN_SEC) < 1e-6, f"voiced seam (A) should be min, got {xf[0]}"
assert xf[1] > 0.04, f"quiet seam should be near max, got {xf[1]}"
assert abs(xf[2] - XFADE_MIN_SEC) < 1e-6, f"voiced seam (B) should be min, got {xf[2]}"

outs = {}
for spk in ("A", "B"):
    segs = extract_keep_segments(raws[spk], keeps[spk], tmp, spk)
    out = tmp / f"{spk}_cut.wav"
    concat_wavs_crossfade(segs, out, xf)
    outs[spk] = out

la = len(sf.read(outs["A"])[0])
lb = len(sf.read(outs["B"])[0])
assert la == lb, f"tracks desynced: {la} vs {lb}"
expected = DUR - sum(e - s for s, e in deletes) - sum(xf)
got = la / SR
assert abs(got - expected) < 0.01, f"duration {got:.3f} vs expected {expected:.3f}"

# No clicks: max sample-to-sample jump at the voiced seam should stay bounded.
ya, _ = sf.read(outs["A"])
jump = np.max(np.abs(np.diff(ya)))
assert jump < 0.25, f"suspicious discontinuity {jump:.3f}"

print(f"OK: tracks aligned ({la} samples), duration {got:.3f}s == expected {expected:.3f}s, max jump {jump:.3f}")
