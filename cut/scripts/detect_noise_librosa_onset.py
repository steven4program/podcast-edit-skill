#!/usr/bin/env python3
"""
librosa-only short-burst detector — complements YAMNet for events that get
masked by neighbouring speech in YAMNet's 0.96s window.

Heuristic: find short (50-500ms) energy bursts where spectral flatness is
elevated (noise-like rather than harmonic). Throat-clearing, cough, sniff and
mouth clicks fit this signature; sustained speech doesn't.

Outputs the same JSON schema as detect_noise_events.py so the librosa refiner
and the panel can chew on it the same way.

Usage:
    python3 detect_noise_librosa_onset.py --input audio.mp3 \
        --output noise_candidates_librosa.json --speaker Ted \
        [--min-flatness 0.10] [--peak-mult 2.0]
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
sys.stderr.reconfigure(line_buffering=True, encoding="utf-8")


def load_audio_16k_mono(path: str):
    import numpy as np
    cmd = ["ffmpeg", "-v", "error", "-i", path,
           "-f", "s16le", "-acodec", "pcm_s16le",
           "-ar", "16000", "-ac", "1", "-"]
    p = subprocess.run(cmd, capture_output=True, check=True)
    raw = np.frombuffer(p.stdout, dtype=np.int16)
    return raw.astype(np.float32) / 32768.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", "-i", required=True)
    ap.add_argument("--output", "-o", required=True)
    ap.add_argument("--speaker", default=None)
    ap.add_argument("--min-flatness", type=float, default=0.08,
                    help="minimum spectral flatness for a frame to count as noise-like (default 0.08)")
    ap.add_argument("--peak-mult", type=float, default=1.5,
                    help="frame RMS must be > median * this to count (default 1.5)")
    ap.add_argument("--min-dur-ms", type=int, default=40)
    ap.add_argument("--max-dur-ms", type=int, default=600)
    ap.add_argument("--merge-gap-ms", type=int, default=120,
                    help="merge bursts within this gap (default 120ms)")
    args = ap.parse_args()

    import numpy as np
    import librosa

    print(f"🎧 input: {args.input}")
    y = load_audio_16k_mono(args.input)
    sr = 16000
    print(f"   samples: {len(y)} ({len(y)/sr:.1f}s)")

    # 10ms hop, 25ms window — fine resolution
    hop = int(sr * 0.010)
    win = int(sr * 0.025)
    n_fft = 512

    rms = librosa.feature.rms(y=y, frame_length=win, hop_length=hop)[0]
    flat = librosa.feature.spectral_flatness(y=y, n_fft=n_fft, hop_length=hop)[0]
    # Trim flatness array to rms length
    n = min(len(rms), len(flat))
    rms = rms[:n]
    flat = flat[:n]

    rms_median = float(np.median(rms[rms > 1e-4])) if (rms > 1e-4).any() else 1e-3
    rms_thresh = rms_median * args.peak_mult

    # Frames that are loud-enough AND noise-like
    mask = (rms > rms_thresh) & (flat > args.min_flatness)
    print(f"   rms_median={rms_median:.4f} thresh={rms_thresh:.4f} flat_thresh={args.min_flatness}")
    print(f"   frames above mask: {int(mask.sum())} / {n}")

    # Run-length encode the mask → bursts
    bursts = []
    i = 0
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j < n and mask[j]:
            j += 1
        start_s = i * hop / sr
        end_s = j * hop / sr
        peak_rms = float(rms[i:j].max())
        mean_flat = float(flat[i:j].mean())
        bursts.append((start_s, end_s, peak_rms, mean_flat))
        i = j

    # Merge bursts that are close together
    merged = []
    gap = args.merge_gap_ms / 1000
    for b in bursts:
        if merged and b[0] - merged[-1][1] <= gap:
            prev = merged[-1]
            merged[-1] = (
                prev[0], b[1],
                max(prev[2], b[2]),
                (prev[3] + b[3]) / 2,
            )
        else:
            merged.append(b)

    # Filter by duration
    min_dur = args.min_dur_ms / 1000
    max_dur = args.max_dur_ms / 1000
    kept = [b for b in merged if min_dur <= (b[1] - b[0]) <= max_dur]
    print(f"   bursts: {len(bursts)} → merged: {len(merged)} → kept (dur in [{args.min_dur_ms},{args.max_dur_ms}]ms): {len(kept)}")

    candidates = []
    for s, e, peak, mean_flat in kept:
        # Use a composite score so the panel can sort/filter later
        score = round(min(1.0, peak * mean_flat * 5), 3)
        c = {
            "start": round(s, 3),
            "end": round(e, 3),
            "class": "Librosa burst",
            "score": score,
        }
        if args.speaker:
            c["speaker"] = args.speaker
        candidates.append(c)

    out = {
        "audio_file": os.path.basename(args.input),
        "duration": round(len(y) / sr, 2),
        "model": "librosa-onset",
        "params": {
            "min_flatness": args.min_flatness,
            "peak_mult": args.peak_mult,
            "min_dur_ms": args.min_dur_ms,
            "max_dur_ms": args.max_dur_ms,
            "merge_gap_ms": args.merge_gap_ms,
        },
        "candidates": candidates,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"💾 wrote {args.output} ({len(candidates)} candidates)")


if __name__ == "__main__":
    main()
