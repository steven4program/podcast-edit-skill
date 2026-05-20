#!/usr/bin/env python3
"""
Low-frequency voiced-burst detector for cough / throat-clearing / "嗯哼" /
similar non-speech vocal events.

Designed from ground-truth analysis of two known cough events:
    Ted 13.40-13.55  (轻咳嗽)
    Ted 24.00-24.10  (轻咳嗽)

Both share three features that normal Mandarin speech — including tone-3,
nasals, low-pitched syllables — does NOT share simultaneously:

    1. ACTIVE     RMS > rms_median * 3      (loud enough, not silence)
    2. BASSY      rolloff_85 < 1500 Hz       (85% of energy below 1.5kHz —
                                              normal speech keeps F2 at 2-4kHz)
    3. VOICED     ZCR < 0.08                 (low zero-crossing rate, not a
                                              fricative or breath)

Optional supporting features tracked for the panel:
    - centroid (typically 400-800 Hz for these events)
    - duration (40-500 ms range)

Usage:
    python3 detect_noise_lowfreq.py --input audio.mp3 \
        --output noise_lowfreq.json --speaker Ted

Tuneable thresholds via CLI; defaults are calibrated to the two known events.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
sys.stderr.reconfigure(line_buffering=True, encoding="utf-8")


def load_audio_16k_mono(path):
    import numpy as np
    cmd = ["ffmpeg", "-v", "error", "-i", path,
           "-f", "s16le", "-acodec", "pcm_s16le",
           "-ar", "16000", "-ac", "1", "-"]
    p = subprocess.run(cmd, capture_output=True, check=True)
    return np.frombuffer(p.stdout, dtype=np.int16).astype("float32") / 32768.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", "-i", required=True)
    ap.add_argument("--output", "-o", required=True)
    ap.add_argument("--speaker", default=None)
    ap.add_argument("--rolloff-max-hz", type=float, default=1500.0,
                    help="rolloff_85 must be below this (default 1500 Hz)")
    ap.add_argument("--zcr-max", type=float, default=0.08,
                    help="zero-crossing rate must be below this (default 0.08)")
    ap.add_argument("--rms-min-mult", type=float, default=3.0,
                    help="RMS must exceed median*this (default 3.0)")
    ap.add_argument("--centroid-max-hz", type=float, default=900.0,
                    help="spectral centroid must be below this (default 900 Hz)")
    ap.add_argument("--min-dur-ms", type=int, default=60)
    ap.add_argument("--max-dur-ms", type=int, default=500)
    ap.add_argument("--merge-gap-ms", type=int, default=80,
                    help="bridge bursts within this gap (default 80ms)")
    ap.add_argument("--min-frames", type=int, default=3,
                    help="event needs at least this many consecutive flagged frames before merging (default 3)")
    args = ap.parse_args()

    import numpy as np
    import librosa

    print(f"🎧 {args.input}")
    y = load_audio_16k_mono(args.input)
    sr = 16000
    print(f"   {len(y)} samples ({len(y)/sr:.1f}s)")

    hop = 160      # 10ms
    win = 400      # 25ms
    n_fft = 512

    rms = librosa.feature.rms(y=y, frame_length=win, hop_length=hop)[0]
    cent = librosa.feature.spectral_centroid(y=y, sr=sr, n_fft=n_fft, hop_length=hop)[0]
    rolloff = librosa.feature.spectral_rolloff(y=y, sr=sr, n_fft=n_fft, hop_length=hop, roll_percent=0.85)[0]
    zcr = librosa.feature.zero_crossing_rate(y=y, frame_length=win, hop_length=hop)[0]
    n = min(len(rms), len(cent), len(rolloff), len(zcr))
    rms = rms[:n]; cent = cent[:n]; rolloff = rolloff[:n]; zcr = zcr[:n]

    rms_med = float(np.median(rms[rms > 1e-4])) if (rms > 1e-4).any() else 1e-3
    rms_min = rms_med * args.rms_min_mult
    print(f"   rms_med={rms_med:.5f}, rms_min={rms_min:.5f}")
    print(f"   thresholds: rolloff<{args.rolloff_max_hz:.0f}Hz, "
          f"zcr<{args.zcr_max}, centroid<{args.centroid_max_hz:.0f}Hz")

    # All three (or four) conditions AND together
    flag = (
        (rms > rms_min) &
        (rolloff < args.rolloff_max_hz) &
        (zcr < args.zcr_max) &
        (cent < args.centroid_max_hz)
    )
    print(f"   frames meeting ALL conditions: {int(flag.sum())} / {n}")

    # Run-length encode
    bursts = []
    i = 0
    while i < n:
        if not flag[i]:
            i += 1
            continue
        j = i
        while j < n and flag[j]:
            j += 1
        if j - i >= args.min_frames:
            bursts.append((i, j))
        i = j

    # Merge bursts that sit close together (same cough often has 2 syllabic peaks)
    gap_frames = int(args.merge_gap_ms / 1000 * sr / hop)
    merged = []
    for a, b in bursts:
        if merged and a - merged[-1][1] <= gap_frames:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))

    min_f = int(args.min_dur_ms / 1000 * sr / hop)
    max_f = int(args.max_dur_ms / 1000 * sr / hop)

    candidates = []
    for a, b in merged:
        dur_f = b - a
        if dur_f < min_f or dur_f > max_f:
            continue
        start_s = a * hop / sr
        end_s = b * hop / sr
        mean_cent = float(cent[a:b].mean())
        mean_rolloff = float(rolloff[a:b].mean())
        mean_zcr = float(zcr[a:b].mean())
        peak_rms = float(rms[a:b].max())
        # Composite confidence — all three features at strongest = high confidence
        c_score = max(0, 1 - mean_cent / args.centroid_max_hz)
        r_score = max(0, 1 - mean_rolloff / args.rolloff_max_hz)
        z_score = max(0, 1 - mean_zcr / args.zcr_max)
        score = round(min(1.0, 0.4 * r_score + 0.3 * c_score + 0.3 * z_score), 3)
        c = {
            "start": round(start_s, 3),
            "end": round(end_s, 3),
            "class": "Low-freq voiced burst",
            "score": score,
            "centroid_hz": round(mean_cent, 0),
            "rolloff_hz": round(mean_rolloff, 0),
            "zcr": round(mean_zcr, 3),
            "rms_peak": round(peak_rms, 4),
        }
        if args.speaker:
            c["speaker"] = args.speaker
        candidates.append(c)

    print(f"   raw bursts: {len(bursts)} → merged: {len(merged)} → "
          f"dur-filtered: {len(candidates)}")
    for c in candidates:
        print(f"   {c['start']:>7.2f}-{c['end']:>7.2f}s  "
              f"cent={c['centroid_hz']:>4.0f}Hz  rolloff={c['rolloff_hz']:>4.0f}Hz  "
              f"zcr={c['zcr']:.3f}  rms_peak={c['rms_peak']:.3f}  score={c['score']:.2f}")

    out = {
        "audio_file": os.path.basename(args.input),
        "duration": round(len(y) / sr, 2),
        "model": "lowfreq-voiced-burst",
        "params": {
            "rolloff_max_hz": args.rolloff_max_hz,
            "zcr_max": args.zcr_max,
            "rms_min_mult": args.rms_min_mult,
            "centroid_max_hz": args.centroid_max_hz,
            "min_dur_ms": args.min_dur_ms,
            "max_dur_ms": args.max_dur_ms,
            "merge_gap_ms": args.merge_gap_ms,
        },
        "candidates": candidates,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"💾 wrote {args.output}")


if __name__ == "__main__":
    main()
